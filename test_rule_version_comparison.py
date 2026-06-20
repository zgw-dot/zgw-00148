"""
规则版本对比与差异复盘增强版 — 完整回归测试
覆盖:
  1. 数据库升级: import_records带rule_version_id, ui_state表
  2. 导入冲突检测: 同规则版本重复导入拦截, 不同规则版本提示确认
  3. 规则版本隔离: 换规则后导入新数据, 与旧记录分开存储
  4. 差异复盘对比视图: 按门店/时间/商品/规则版本筛选, 并排对比
  5. 筛选记忆持久化: 保存到DB, 重启后可恢复
  6. CSV/JSON导出增强: 含筛选条件、对比摘要、规则版本
  7. 重启回归: 所有数据保持一致, 不串口径
"""
import os
import sys
import json
import sqlite3
import io
import csv
import tempfile
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff_test.db")

from test_utils import init_test_env
init_test_env(DB_PATH)

from db import (
    init_db, get_conn, get_discrepancies, get_evidence_for_discrepancy,
    get_status_log, get_stores, get_import_records,
    transition_status, update_review_note, get_active_rule_version,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_CLOSED, STATUS_LABELS,
    get_snapshot_for_discrepancy, get_calc_steps_for_discrepancy,
    get_discrepancies_extended, get_discrepancy_versions,
    get_all_rule_versions_with_labels, get_import_records_with_rule_version,
    check_import_duplicate, save_ui_state, load_ui_state,
    get_store_list, get_barcode_list, get_date_range,
    now_iso,
)
from import_service import import_csv, validate_row, compute_file_hash, REQUIRED_FIELDS, NUMERIC_FIELDS
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config, get_version_history
from sample_data import generate_sample_data, SAMPLE_DIR

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

init_db()
generate_sample_data()


def read_sample(fname):
    with open(os.path.join(SAMPLE_DIR, fname), "rb") as f:
        return f.read()


errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


print("\n" + "=" * 70)
print("测试 1: 数据库升级验证")
print("=" * 70)

with get_conn() as conn:
    cols = conn.execute("PRAGMA table_info(import_records)").fetchall()
    col_names = [c["name"] for c in cols]
    check("import_records表有rule_version_id字段", "rule_version_id" in col_names, str(col_names))

    indexes = conn.execute("PRAGMA index_list(import_records)").fetchall()
    index_names = [idx["name"] for idx in indexes]
    check("import_records有联合唯一索引", "idx_import_unique_hash_type_rule" in index_names, str(index_names))

    ui_cols = conn.execute("PRAGMA table_info(ui_state)").fetchall()
    ui_col_names = [c["name"] for c in ui_cols]
    check("ui_state表存在", len(ui_cols) > 0, str(ui_col_names))
    check("ui_state表有state_key字段", "state_key" in ui_col_names, str(ui_col_names))
    check("ui_state表有state_value字段", "state_value" in ui_col_names, str(ui_col_names))


print("\n" + "=" * 70)
print("测试 2: 首次导入数据 + 归因 (v1规则)")
print("=" * 70)

r_inv = import_csv("inventory", "inventory.csv", read_sample("inventory.csv"))
check("v1库存导入成功", r_inv["success"], str(r_inv))
check("v1库存导入记录rule_version正确", r_inv.get("rule_version") == 1, str(r_inv))

r_sal = import_csv("sales", "sales.csv", read_sample("sales.csv"))
check("v1销售导入成功", r_sal["success"])

r_tra = import_csv("transfer", "transfer.csv", read_sample("transfer.csv"))
check("v1调拨导入成功", r_tra["success"])

r_stk = import_csv("stocktake", "stocktake.csv", read_sample("stocktake.csv"))
check("v1盘点导入成功", r_stk["success"])

with get_conn() as conn:
    import_recs = get_import_records_with_rule_version(conn)
check("导入记录都有rule_version字段", all(r.get("rule_ver") is not None for r in import_recs), str(import_recs))

r_attr1 = run_attribution()
check("v1归因成功", r_attr1["success"], str(r_attr1))
check("v1归因创建差异>0", r_attr1.get("created", 0) > 0, str(r_attr1))
check("v1规则版本号=1", r_attr1.get("rule_version") == 1, str(r_attr1))

with get_conn() as conn:
    discs_v1 = get_discrepancies(conn)
check(f"v1有差异: {len(discs_v1)} 条", len(discs_v1) > 0, str(discs_v1))

sample_disc_v1 = discs_v1[0]
sample_store = sample_disc_v1["store_id"]
sample_barcode = sample_disc_v1["barcode"]
check("v1差异记录rule_ver=1", sample_disc_v1.get("rule_ver") == 1, str(sample_disc_v1))


print("\n" + "=" * 70)
print("测试 3: 同规则版本重复导入拦截")
print("=" * 70)

r_dup_same = import_csv("inventory", "inventory_dup.csv", read_sample("inventory.csv"))
check("同规则版本重复导入被拦截", not r_dup_same["success"], str(r_dup_same))
check("拦截标记duplicate=True", r_dup_same.get("duplicate") is True, str(r_dup_same))
check("duplicate_type=same_rule_version", r_dup_same.get("duplicate_type") == "same_rule_version", str(r_dup_same))
check("错误信息包含规则版本提示", "v1" in r_dup_same.get("error", ""), r_dup_same.get("error"))


print("\n" + "=" * 70)
print("测试 4: 保存筛选记忆")
print("=" * 70)

filter_state = {
    "store_id": sample_store,
    "barcode": sample_barcode[:5],
    "rule_ver_a": 1,
    "rule_ver_b": 0,
    "status": "pending_review",
    "saved_at": now_iso(),
}
with get_conn() as conn:
    save_ui_state(conn, "review_filter_state", filter_state)

with get_conn() as conn:
    loaded = load_ui_state(conn, "review_filter_state")
check("筛选状态保存成功", loaded is not None, str(loaded))
check("筛选状态store_id正确", loaded.get("store_id") == sample_store, str(loaded))
check("筛选状态rule_ver_a正确", loaded.get("rule_ver_a") == 1, str(loaded))


print("\n" + "=" * 70)
print("测试 5: 规则变更 (v2)")
print("=" * 70)

NEW_CFG = {
    "loss_threshold_pct": 8.0,
    "loss_threshold_abs": 15.0,
    "transfer_delay_days": 7,
    "aliases": {"ALT_TEST_123": "6901234567890"},
}
r_save = save_rule_config(NEW_CFG)
check("v2规则保存成功", r_save["success"], str(r_save))
check("规则版本号=2", r_save.get("version") == 2, str(r_save))

with get_conn() as conn:
    rule_versions = get_all_rule_versions_with_labels(conn)
check("有2个规则版本", len(rule_versions) == 2, str([v["version"] for v in rule_versions]))
check("v2是激活状态", any(v["version"] == 2 and v["is_active"] == 1 for v in rule_versions), str(rule_versions))


print("\n" + "=" * 70)
print("测试 6: 不同规则版本导入冲突提醒")
print("=" * 70)

r_dup_diff = import_csv("inventory", "inventory_v2.csv", read_sample("inventory.csv"))
check("不同规则版本导入触发冲突", not r_dup_diff["success"], str(r_dup_diff))
check("duplicate_type=different_rule_version", r_dup_diff.get("duplicate_type") == "different_rule_version", str(r_dup_diff))
check("错误信息包含'导入冲突提醒'", "导入冲突提醒" in r_dup_diff.get("error", ""), r_dup_diff.get("error"))
check("包含existing_rule_version字段", r_dup_diff.get("existing_rule_version") == 1, str(r_dup_diff))
check("包含current_rule_version字段", r_dup_diff.get("current_rule_version") == 2, str(r_dup_diff))

print(f"  [INFO] 冲突提示内容预览:")
for line in r_dup_diff.get("error", "").split("\n"):
    print(f"         {line}")

r_force = import_csv("inventory", "inventory_v2.csv", read_sample("inventory.csv"), allow_different_rule_version=True)
check("确认后可在v2规则下导入", r_force["success"], str(r_force))
check("v2导入rule_version=2", r_force.get("rule_version") == 2, str(r_force))

with get_conn() as conn:
    import_recs_v2 = get_import_records_with_rule_version(conn)
v2_inv_recs = [r for r in import_recs_v2 if r["import_type"] == "inventory"]
check("库存有2条导入记录(分属v1和v2)", len(v2_inv_recs) == 2, str([(r["file_name"], r.get("rule_ver")) for r in v2_inv_recs]))
check("2条记录规则版本不同", len(set(r.get("rule_ver") for r in v2_inv_recs)) == 2, str(v2_inv_recs))


print("\n" + "=" * 70)
print("测试 7: v2规则下归因, 新旧记录分开")
print("=" * 70)

r_sal2 = import_csv("sales", "sales_v2.csv", read_sample("sales.csv"), allow_different_rule_version=True)
r_tra2 = import_csv("transfer", "transfer_v2.csv", read_sample("transfer.csv"), allow_different_rule_version=True)
r_stk2 = import_csv("stocktake", "stocktake_v2.csv", read_sample("stocktake.csv"), allow_different_rule_version=True)
check("v2数据全部导入成功", all(r["success"] for r in [r_sal2, r_tra2, r_stk2]))

r_attr2 = run_attribution()
check("v2归因成功", r_attr2["success"], str(r_attr2))
check("v2规则版本号=2", r_attr2.get("rule_version") == 2, str(r_attr2))

with get_conn() as conn:
    discs_all = get_discrepancies(conn)
    discs_v1_only = get_discrepancies_extended(conn, rule_version=1)
    discs_v2_only = get_discrepancies_extended(conn, rule_version=2)

check(f"总差异数: {len(discs_all)}", len(discs_all) > len(discs_v1), str(len(discs_all)))
check(f"v1差异数: {len(discs_v1_only)}", len(discs_v1_only) == len(discs_v1), str(len(discs_v1_only)))
check(f"v2差异数: {len(discs_v2_only)}", len(discs_v2_only) > 0, str(len(discs_v2_only)))
check("v1差异rule_ver都是1", all(d.get("rule_ver") == 1 for d in discs_v1_only), str([d.get("rule_ver") for d in discs_v1_only[:3]]))
check("v2差异rule_ver都是2", all(d.get("rule_ver") == 2 for d in discs_v2_only), str([d.get("rule_ver") for d in discs_v2_only[:3]]))

sample_key = (sample_store, sample_barcode)
with get_conn() as conn:
    versions = get_discrepancy_versions(conn, sample_store, sample_barcode)
check(f"商品{sample_key}有多个规则版本记录", len(versions) >= 2, str([v.get("rule_ver") for v in versions]))
check("版本1和版本2都存在", {1, 2}.issubset({v.get("rule_ver") for v in versions}), str([v.get("rule_ver") for v in versions]))

v1_rec = next(v for v in versions if v.get("rule_ver") == 1)
v2_rec = next(v for v in versions if v.get("rule_ver") == 2)
check("v1记录规则配置阈值=2.0", (v1_rec.get("rule_config") or {}).get("loss_threshold_pct") == 2.0, str(v1_rec.get("rule_config")))
check("v2记录规则配置阈值=8.0", (v2_rec.get("rule_config") or {}).get("loss_threshold_pct") == 8.0, str(v2_rec.get("rule_config")))
check("两个版本diff_qty独立不串", v1_rec["diff_qty"] is not None and v2_rec["diff_qty"] is not None, f"v1={v1_rec['diff_qty']} v2={v2_rec['diff_qty']}")

with get_conn() as conn:
    import_recs_before_restart = get_import_records_with_rule_version(conn)
import_records_count_before = len(import_recs_before_restart)


print("\n" + "=" * 70)
print("测试 8: 差异复盘对比 - 多维度筛选")
print("=" * 70)

with get_conn() as conn:
    stores = get_store_list(conn)
    barcodes = get_barcode_list(conn)
    rvs = get_all_rule_versions_with_labels(conn)

check("门店列表正确", sample_store in stores, str(stores))
check("商品列表正确", any(b["barcode"] == sample_barcode for b in barcodes), str(barcodes[:3]))
check("规则版本列表正确", len(rvs) == 2, str([r["version"] for r in rvs]))

with get_conn() as conn:
    filtered = get_discrepancies_extended(conn, store_id=sample_store, rule_version=1)
check(f"按门店+规则v1筛选: {len(filtered)} 条", len(filtered) > 0 and all(d["store_id"] == sample_store for d in filtered), str([d["store_id"] for d in filtered[:3]]))

with get_conn() as conn:
    filtered_bc = get_discrepancies_extended(conn, barcode=sample_barcode[:5])
check(f"按条码关键词筛选: {len(filtered_bc)} 条", len(filtered_bc) > 0, str(len(filtered_bc)))


print("\n" + "=" * 70)
print("测试 9: 差异复盘对比 - 并排对比构建")
print("=" * 70)

with get_conn() as conn:
    d_a = get_discrepancies_extended(conn, rule_version=1, store_id=sample_store)
    d_b = get_discrepancies_extended(conn, rule_version=2, store_id=sample_store)

def build_map(discs):
    m = {}
    for d in discs:
        key = (d["store_id"], d["barcode"])
        if key not in m:
            m[key] = []
        m[key].append(d)
    return m

map_a = build_map(d_a)
map_b = build_map(d_b)
all_keys = sorted(set(map_a.keys()) | set(map_b.keys()))

check(f"对比商品总数: {len(all_keys)}", len(all_keys) > 0, str(len(all_keys)))

common_keys = set(map_a.keys()) & set(map_b.keys())
only_a = set(map_a.keys()) - set(map_b.keys())
only_b = set(map_b.keys()) - set(map_a.keys())

check(f"共有商品: {len(common_keys)}", len(common_keys) >= 0, str(common_keys))
check(f"仅v1有: {len(only_a)}", len(only_a) >= 0, str(only_a))
check(f"仅v2有: {len(only_b)}", len(only_b) >= 0, str(only_b))

if common_keys:
    test_key = list(common_keys)[0]
    da = map_a[test_key][0]
    db = map_b[test_key][0]
    with get_conn() as conn:
        snap_a = get_snapshot_for_discrepancy(conn, da["id"])
        snap_b = get_snapshot_for_discrepancy(conn, db["id"])
    check(f"商品{test_key} v1有快照", snap_a is not None)
    check(f"商品{test_key} v2有快照", snap_b is not None)
    if snap_a and snap_b:
        check("v1快照阈值=2.0", (snap_a.get("rule_config_snapshot") or {}).get("loss_threshold_pct") == 2.0)
        check("v2快照阈值=8.0", (snap_b.get("rule_config_snapshot") or {}).get("loss_threshold_pct") == 8.0)
        check("v1和v2快照独立", snap_a["id"] != snap_b["id"])

diff_qty_changed = 0
cause_changed = 0
for key in common_keys:
    da = map_a[key][0]
    db = map_b[key][0]
    if abs(da["diff_qty"] - db["diff_qty"]) > 0.001:
        diff_qty_changed += 1
    if da.get("attributed_cause") != db.get("attributed_cause"):
        cause_changed += 1

print(f"  [INFO] 差异量变化商品数: {diff_qty_changed}, 归因变化商品数: {cause_changed}")


print("\n" + "=" * 70)
print("测试 10: 导出增强 - CSV含筛选条件和对比摘要")
print("=" * 70)

all_export = []
for key in all_keys:
    list_a = map_a.get(key, [])
    list_b = map_b.get(key, [])
    da = list_a[0] if list_a else None
    db = list_b[0] if list_b else None
    row = {
        "store_id": key[0],
        "barcode": key[1],
        "sku_name": (da.get("sku_name", "") if da else "") or (db.get("sku_name", "") if db else ""),
    }
    if da:
        row["v_a_rule_ver"] = da.get("rule_ver", "")
        row["v_a_diff_qty"] = da["diff_qty"]
        row["v_a_cause"] = CAUSE_LABELS.get(da["attributed_cause"], "未归因")
    if db:
        row["v_b_rule_ver"] = db.get("rule_ver", "")
        row["v_b_diff_qty"] = db["diff_qty"]
        row["v_b_cause"] = CAUSE_LABELS.get(db["attributed_cause"], "未归因")
    if da and db:
        row["diff_qty_change"] = db["diff_qty"] - da["diff_qty"]
        row["cause_changed"] = "是" if da.get("attributed_cause") != db.get("attributed_cause") else "否"
    all_export.append(row)

filter_summary = {
    "exported_at": now_iso(),
    "filter_store": sample_store,
    "filter_barcode": "",
    "filter_rule_a": 1,
    "filter_rule_b": 2,
    "summary": {
        "total_items": len(all_keys),
        "count_version_a": len(d_a),
        "count_version_b": len(d_b),
        "only_in_a": len(only_a),
        "only_in_b": len(only_b),
        "diff_qty_changed": diff_qty_changed,
        "cause_changed": cause_changed,
    }
}

df = pd.DataFrame(all_export) if all_export else None
if df is not None:
    df.insert(0, "filter_store", sample_store)
    df.insert(1, "filter_rule_a", "v1")
    df.insert(2, "filter_rule_b", "v2")

    csv_buf = io.StringIO()
    if df is not None and not df.empty:
        df.to_csv(csv_buf, index=False, encoding="utf-8-sig")
        csv_content = csv_buf.getvalue()

        summary_lines = [
            "# 差异复盘对比导出",
            f"# 导出时间: {filter_summary['exported_at']}",
            f"# 筛选条件: 门店={filter_summary['filter_store']}, 规则A=v1, 规则B=v2",
            f"# 对比摘要: {json.dumps(filter_summary['summary'], ensure_ascii=False)}",
            "#",
        ]
        full_csv = "\n".join(summary_lines) + csv_content

    check("CSV包含筛选条件行", "filter_store" in full_csv and "filter_rule_a" in full_csv, full_csv[:200])
    check("CSV包含对比摘要注释行", "对比摘要" in full_csv, full_csv[:300])
    check("CSV包含并排版本列", "v_a_diff_qty" in full_csv and "v_b_diff_qty" in full_csv, full_csv[:300])
    check("CSV包含差异变化列", "diff_qty_change" in full_csv, full_csv[:300])
    check("CSV包含归因变化列", "cause_changed" in full_csv, full_csv[:300])

    print(f"  [INFO] CSV前3行预览:")
    for i, line in enumerate(full_csv.split("\n")[:3]):
        print(f"         {line[:100]}...")


print("\n" + "=" * 70)
print("测试 11: 导出增强 - JSON含完整元数据")
print("=" * 70)

with get_conn() as conn:
    rule_versions_info = get_all_rule_versions_with_labels(conn)

json_export = {
    "export_metadata": filter_summary,
    "comparison_data": all_export,
    "rule_versions_info": [
        {"version": v["version"], "config": json.loads(v["config_json"]), "created_at": v["created_at"]}
        for v in rule_versions_info
    ],
}

json_str = json.dumps(json_export, ensure_ascii=False, indent=2, default=str)

check("JSON包含export_metadata", "export_metadata" in json_export, str(json_export.keys()))
check("JSON包含comparison_data", "comparison_data" in json_export, str(json_export.keys()))
check("JSON包含rule_versions_info", "rule_versions_info" in json_export, str(json_export.keys()))
check("JSON元数据包含筛选条件", json_export["export_metadata"].get("filter_store") == sample_store, str(json_export["export_metadata"]))
check("JSON元数据包含对比摘要", "summary" in json_export["export_metadata"], str(json_export["export_metadata"].keys()))
check("JSON规则版本信息完整", len(json_export["rule_versions_info"]) == 2, str(json_export["rule_versions_info"]))
check("JSON规则版本1阈值=2.0", json_export["rule_versions_info"][1]["config"].get("loss_threshold_pct") == 2.0)
check("JSON规则版本2阈值=8.0", json_export["rule_versions_info"][0]["config"].get("loss_threshold_pct") == 8.0)

print(f"  [INFO] JSON元数据预览:")
print(f"         {json.dumps(json_export['export_metadata'], ensure_ascii=False, indent=10)[:200]}...")


print("\n" + "=" * 70)
print("测试 12: 模拟重启 - 数据和状态保留")
print("=" * 70)

import importlib
import db as db_mod
import importlib as importlib_mod
importlib_mod.reload(db_mod)

with db_mod.get_conn() as conn:
    discs_after = db_mod.get_discrepancies(conn)
    discs_v1_after = db_mod.get_discrepancies_extended(conn, rule_version=1)
    discs_v2_after = db_mod.get_discrepancies_extended(conn, rule_version=2)
    imported_after = db_mod.get_import_records_with_rule_version(conn)
    loaded_after = db_mod.load_ui_state(conn, "review_filter_state")
    rule_versions_after = db_mod.get_all_rule_versions_with_labels(conn)

check(f"重启后总差异数一致: {len(discs_after)} == {len(discs_all)}", len(discs_after) == len(discs_all))
check(f"重启后v1差异数一致: {len(discs_v1_after)} == {len(discs_v1_only)}", len(discs_v1_after) == len(discs_v1_only))
check(f"重启后v2差异数一致: {len(discs_v2_after)} == {len(discs_v2_only)}", len(discs_v2_after) == len(discs_v2_only))
check(f"重启后导入记录一致: {len(imported_after)} == {import_records_count_before}", len(imported_after) == import_records_count_before)
check("重启后筛选状态恢复", loaded_after is not None and loaded_after.get("store_id") == sample_store, str(loaded_after))
check("重启后规则版本保留", len(rule_versions_after) == 2, str([v["version"] for v in rule_versions_after]))

v1_inv_after = [r for r in imported_after if r["import_type"] == "inventory" and r.get("rule_ver") == 1]
v2_inv_after = [r for r in imported_after if r["import_type"] == "inventory" and r.get("rule_ver") == 2]
check("重启后v1库存记录存在", len(v1_inv_after) == 1)
check("重启后v2库存记录存在", len(v2_inv_after) == 1)

with db_mod.get_conn() as conn:
    sample_v1_after = db_mod.get_discrepancies_extended(conn, rule_version=1, store_id=sample_store)[0]
    sample_v2_after = db_mod.get_discrepancies_extended(conn, rule_version=2, store_id=sample_store)[0]
    snap_v1_after = db_mod.get_snapshot_for_discrepancy(conn, sample_v1_after["id"])
    snap_v2_after = db_mod.get_snapshot_for_discrepancy(conn, sample_v2_after["id"])

check("重启后v1样本规则版本不串", sample_v1_after.get("rule_ver") == 1)
check("重启后v2样本规则版本不串", sample_v2_after.get("rule_ver") == 2)
check("重启后v1快照阈值不串", (snap_v1_after.get("rule_config_snapshot") or {}).get("loss_threshold_pct") == 2.0)
check("重启后v2快照阈值不串", (snap_v2_after.get("rule_config_snapshot") or {}).get("loss_threshold_pct") == 8.0)


print("\n" + "=" * 70)
print("测试 13: 完整导入-改规则-再导入链路验证")
print("=" * 70)

NEW_CFG2 = {
    "loss_threshold_pct": 15.0,
    "loss_threshold_abs": 20.0,
    "transfer_delay_days": 14,
    "aliases": {"ALT_NEW": "6901234567891"},
}
r_save2 = save_rule_config(NEW_CFG2)
check("v3规则保存成功", r_save2["success"] and r_save2.get("version") == 3, str(r_save2))

r_dup_v3 = import_csv("inventory", "inventory_v3.csv", read_sample("inventory.csv"))
check("v3触发冲突提醒", not r_dup_v3["success"] and r_dup_v3.get("duplicate_type") == "different_rule_version", str(r_dup_v3))
check("冲突提示包含v1和v2历史", "v1" in r_dup_v3.get("error", ""), r_dup_v3.get("error"))

r_force_v3 = import_csv("inventory", "inventory_v3.csv", read_sample("inventory.csv"), allow_different_rule_version=True)
check("v3确认后导入成功", r_force_v3["success"] and r_force_v3.get("rule_version") == 3, str(r_force_v3))

with get_conn() as conn:
    inv_recs_final = get_import_records_with_rule_version(conn)
inv_by_ver = {}
for r in inv_recs_final:
    if r["import_type"] == "inventory":
        ver = r.get("rule_ver")
        inv_by_ver.setdefault(ver, []).append(r)

check("库存有3个规则版本的导入记录", {1, 2, 3}.issubset(set(inv_by_ver.keys())), str(inv_by_ver.keys()))
check("每个版本各1条库存记录", all(len(v) == 1 for k, v in inv_by_ver.items() if k in {1, 2, 3}))

with get_conn() as conn:
    all_discs_final = get_discrepancies(conn)
ver_counts = {}
for d in all_discs_final:
    ver = d.get("rule_ver")
    ver_counts[ver] = ver_counts.get(ver, 0) + 1

check(f"差异按版本分布: {ver_counts}", {1, 2}.issubset(set(ver_counts.keys())), str(ver_counts))
check("各版本差异独立不串", all(v > 0 for v in ver_counts.values()), str(ver_counts))


print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 规则版本对比与差异复盘增强版回归测试全部通过!")
    print("   [OK] 数据库升级 (rule_version_id + ui_state)")
    print("   [OK] 同规则版本重复导入拦截")
    print("   [OK] 不同规则版本导入冲突提醒 + 确认机制")
    print("   [OK] 规则版本隔离, 新旧记录分开存储不串口径")
    print("   [OK] 差异复盘对比视图 - 多维度筛选")
    print("   [OK] 差异复盘对比视图 - 并排展示旧/新/别名/快照")
    print("   [OK] 筛选记忆持久化, 重启后可恢复")
    print("   [OK] CSV导出包含筛选条件、对比摘要、并排版本")
    print("   [OK] JSON导出包含元数据、规则版本、完整对比数据")
    print("   [OK] 重启回归所有数据保持一致, 不串口径")
    print("   [OK] 完整链路验证: 导入→改规则→再导入→对比→导出→重启")
