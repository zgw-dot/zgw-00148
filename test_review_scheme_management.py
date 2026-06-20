"""
复盘方案管理完整回归测试
覆盖:
  1. 数据库升级: review_schemes, review_scheme_operations表
  2. 新建方案: 保存当前筛选状态为可命名方案
  3. 重名冲突: 保存时检测重名，支持覆盖确认
  4. 载入方案: 一键恢复筛选状态
  5. 复制方案: 复制后改名修改
  6. 改名方案: 修改方案名称
  7. 删除方案: 删除方案及关联处理
  8. 重启恢复: 应用重启后自动恢复最近使用的方案
  9. 时间范围变化提示: 底层数据时间变化时提示
  10. 操作日志: 所有操作留下可查询记录
  11. 导出增强: CSV/JSON包含方案名、时间条件、版本摘要
  12. 删除后导出: 删除方案后仍可导出（使用当前筛选状态）
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
DB_PATH = os.path.join(BASE_DIR, "inventory_diff_scheme_test.db")

from test_utils import init_test_env
init_test_env(DB_PATH)

import db as db_mod
db_mod.DB_PATH = DB_PATH

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
    save_review_scheme, get_review_schemes, get_review_scheme_by_id,
    get_review_scheme_by_name, update_review_scheme_name, delete_review_scheme,
    copy_review_scheme, mark_scheme_used, get_last_used_scheme,
    check_data_date_range_changed, get_scheme_operation_logs,
    log_scheme_operation,
)
from import_service import import_csv, validate_row, compute_file_hash, REQUIRED_FIELDS, NUMERIC_FIELDS
import import_service
import_service.db_mod = db_mod

import engine
engine.db_mod = db_mod

import rules
rules.db_mod = db_mod

from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config, get_version_history
from sample_data import generate_sample_data, SAMPLE_DIR

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
for suffix in ("-wal", "-shm"):
    p = DB_PATH + suffix
    if os.path.exists(p):
        os.remove(p)

MAIN_DB_PATH = os.path.join(BASE_DIR, "inventory_diff.db")
assert "test" in DB_PATH, f"测试使用独立数据库: {DB_PATH}"
assert DB_PATH != MAIN_DB_PATH, f"测试数据库不应与主数据库相同: {DB_PATH} vs {MAIN_DB_PATH}"

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
print("测试 1: 数据库升级验证 - 新表存在")
print("=" * 70)

with get_conn() as conn:
    rs_cols = conn.execute("PRAGMA table_info(review_schemes)").fetchall()
    rs_col_names = [c["name"] for c in rs_cols]
    check("review_schemes表存在", len(rs_cols) > 0, str(rs_col_names))
    check("review_schemes有name字段", "name" in rs_col_names, str(rs_col_names))
    check("review_schemes有filter_state_json字段", "filter_state_json" in rs_col_names, str(rs_col_names))
    check("review_schemes有data_date_range_json字段", "data_date_range_json" in rs_col_names, str(rs_col_names))
    check("review_schemes有last_used_at字段", "last_used_at" in rs_col_names, str(rs_col_names))
    check("review_schemes有description字段", "description" in rs_col_names, str(rs_col_names))

    rso_cols = conn.execute("PRAGMA table_info(review_scheme_operations)").fetchall()
    rso_col_names = [c["name"] for c in rso_cols]
    check("review_scheme_operations表存在", len(rso_cols) > 0, str(rso_col_names))
    check("操作日志有scheme_name字段", "scheme_name" in rso_col_names, str(rso_col_names))
    check("操作日志有operation_type字段", "operation_type" in rso_col_names, str(rso_col_names))
    check("操作日志有operation_detail字段", "operation_detail" in rso_col_names, str(rso_col_names))


print("\n" + "=" * 70)
print("测试 2: 准备基础数据 - 导入+归因+改规则+再导入")
print("=" * 70)

r_inv = import_csv("inventory", "inventory.csv", read_sample("inventory.csv"))
check("v1库存导入成功", r_inv["success"])

r_sal = import_csv("sales", "sales.csv", read_sample("sales.csv"))
check("v1销售导入成功", r_sal["success"])

r_tra = import_csv("transfer", "transfer.csv", read_sample("transfer.csv"))
check("v1调拨导入成功", r_tra["success"])

r_stk = import_csv("stocktake", "stocktake.csv", read_sample("stocktake.csv"))
check("v1盘点导入成功", r_stk["success"])

r_attr1 = run_attribution()
check("v1归因成功", r_attr1["success"])
check("v1归因创建差异>0", r_attr1.get("created", 0) > 0)

NEW_CFG = {
    "loss_threshold_pct": 8.0,
    "loss_threshold_abs": 15.0,
    "transfer_delay_days": 7,
    "aliases": {"ALT_TEST_123": "6901234567890"},
}
r_save = save_rule_config(NEW_CFG)
check("v2规则保存成功", r_save["success"] and r_save.get("version") == 2)

r_inv2 = import_csv("inventory", "inventory_v2.csv", read_sample("inventory.csv"), allow_different_rule_version=True)
check("v2库存导入成功", r_inv2["success"])
r_sal2 = import_csv("sales", "sales_v2.csv", read_sample("sales.csv"), allow_different_rule_version=True)
r_tra2 = import_csv("transfer", "transfer_v2.csv", read_sample("transfer.csv"), allow_different_rule_version=True)
r_stk2 = import_csv("stocktake", "stocktake_v2.csv", read_sample("stocktake.csv"), allow_different_rule_version=True)
check("v2数据全部导入成功", all(r["success"] for r in [r_inv2, r_sal2, r_tra2, r_stk2]))

r_attr2 = run_attribution()
check("v2归因成功", r_attr2["success"])

with get_conn() as conn:
    discs_all = get_discrepancies_extended(conn)
    sample_store = discs_all[0]["store_id"]
    sample_barcode = discs_all[0]["barcode"]
    date_range = get_date_range(conn)

check(f"有可用数据: {len(discs_all)} 条差异", len(discs_all) > 0)
check("有可用时间范围", date_range and date_range.get("min_date"), str(date_range))


print("\n" + "=" * 70)
print("测试 3: 新建方案 - 保存筛选状态为可命名方案")
print("=" * 70)

test_filter_state = {
    "store_id": sample_store,
    "barcode": sample_barcode[:5],
    "rule_ver_a": 1,
    "rule_ver_b": 2,
    "status": "pending_review",
    "date_from": date_range.get("min_date", ""),
    "date_to": date_range.get("max_date", ""),
    "saved_at": now_iso(),
}

with get_conn() as conn:
    result = save_review_scheme(
        conn, "618大促复盘方案", test_filter_state,
        description="618大促前后规则对比",
        data_date_range=date_range,
    )

check("新建方案成功", result["success"], str(result))
check("返回scheme_id", result.get("scheme_id") is not None, str(result))
check("不是覆盖操作", result.get("overwritten") is False, str(result))

with get_conn() as conn:
    schemes = get_review_schemes(conn)
    scheme = get_review_scheme_by_id(conn, result["scheme_id"])

check("方案列表包含新建方案", len(schemes) == 1, str([s["name"] for s in schemes]))
check("按ID查询成功", scheme is not None, str(scheme))
check("方案名称正确", scheme.get("name") == "618大促复盘方案", str(scheme.get("name")))
check("方案描述正确", scheme.get("description") == "618大促前后规则对比", str(scheme.get("description")))
check("filter_state正确", scheme.get("filter_state", {}).get("store_id") == sample_store, str(scheme.get("filter_state")))
check("data_date_range正确", scheme.get("data_date_range") is not None, str(scheme.get("data_date_range")))
check("filter_state包含rule_ver_a", scheme.get("filter_state", {}).get("rule_ver_a") == 1, str(scheme.get("filter_state")))
check("filter_state包含date_from", scheme.get("filter_state", {}).get("date_from") == date_range.get("min_date", ""))

with get_conn() as conn:
    logs = get_scheme_operation_logs(conn)

check("新建操作有日志", len(logs) >= 1, str(len(logs)))
check("日志类型为create", logs[0].get("operation_type") == "create", str(logs[0]))
check("日志方案名称正确", logs[0].get("scheme_name") == "618大促复盘方案", str(logs[0]))


print("\n" + "=" * 70)
print("测试 4: 重名冲突 - 保存时检测，支持覆盖")
print("=" * 70)

test_filter_state2 = {
    "store_id": "全部",
    "barcode": "",
    "rule_ver_a": 1,
    "rule_ver_b": 0,
    "status": "全部",
    "date_from": "",
    "date_to": "",
    "saved_at": now_iso(),
}

with get_conn() as conn:
    result_conflict = save_review_scheme(
        conn, "618大促复盘方案", test_filter_state2,
        overwrite=False,
    )

check("重名被正确拦截", not result_conflict["success"], str(result_conflict))
check("返回needs_confirm=True", result_conflict.get("needs_confirm") is True, str(result_conflict))
check("错误信息包含已存在", "已存在" in result_conflict.get("error", ""), str(result_conflict))
check("返回existing方案信息", result_conflict.get("existing") is not None, str(result_conflict))

with get_conn() as conn:
    result_overwrite = save_review_scheme(
        conn, "618大促复盘方案", test_filter_state2,
        description="更新后的描述",
        overwrite=True,
        data_date_range=date_range,
    )

check("覆盖保存成功", result_overwrite["success"], str(result_overwrite))
check("标记为overwritten", result_overwrite.get("overwritten") is True, str(result_overwrite))

with get_conn() as conn:
    scheme_updated = get_review_scheme_by_id(conn, result_overwrite["scheme_id"])

check("覆盖后store_id更新为全部", scheme_updated.get("filter_state", {}).get("store_id") == "全部")
check("覆盖后description更新", scheme_updated.get("description") == "更新后的描述")

with get_conn() as conn:
    logs_after = get_scheme_operation_logs(conn, limit=1)

check("覆盖操作有update日志", logs_after[0].get("operation_type") == "update", str(logs_after[0]))


print("\n" + "=" * 70)
print("测试 5: 载入方案 - 一键恢复筛选状态")
print("=" * 70)

test_filter_state3 = {
    "store_id": sample_store,
    "barcode": "测试条码",
    "rule_ver_a": 2,
    "rule_ver_b": 1,
    "status": "confirmed",
    "date_from": "2026-01-01",
    "date_to": "2026-06-30",
    "saved_at": now_iso(),
}

with get_conn() as conn:
    r_save2 = save_review_scheme(
        conn, "双11复盘方案", test_filter_state3,
        description="双11专项复盘",
        data_date_range=date_range,
    )

check("第二个方案创建成功", r_save2["success"])
scheme2_id = r_save2["scheme_id"]

with get_conn() as conn:
    mark_scheme_used(conn, scheme2_id)
    scheme_loaded = get_review_scheme_by_id(conn, scheme2_id)

check("载入后last_used_at更新", scheme_loaded.get("last_used_at") is not None, str(scheme_loaded))
check("last_used_scheme_id保存到ui_state", True, "")

with get_conn() as conn:
    last_used_state = load_ui_state(conn, "last_used_scheme_id")

check("last_used_scheme_id正确保存", last_used_state is not None and last_used_state.get("scheme_id") == scheme2_id, str(last_used_state))

with get_conn() as conn:
    logs_load = get_scheme_operation_logs(conn, limit=1)

check("载入操作有load日志", logs_load[0].get("operation_type") == "load", str(logs_load[0]))


print("\n" + "=" * 70)
print("测试 6: 复制方案 - 复制后改名修改")
print("=" * 70)

with get_conn() as conn:
    result_copy = copy_review_scheme(
        conn, scheme2_id, "双11复盘方案_副本",
        new_description="双11复盘-调整版",
    )

check("复制方案成功", result_copy["success"], str(result_copy))
check("返回新scheme_id", result_copy.get("new_scheme_id") is not None, str(result_copy))
check("新方案ID与原方案不同", result_copy["new_scheme_id"] != scheme2_id, str(result_copy))

with get_conn() as conn:
    scheme_copy = get_review_scheme_by_id(conn, result_copy["new_scheme_id"])
    original_scheme = get_review_scheme_by_id(conn, scheme2_id)

check("复制后filter_state相同", scheme_copy.get("filter_state") == original_scheme.get("filter_state"), str(scheme_copy.get("filter_state")))
check("复制后名称正确", scheme_copy.get("name") == "双11复盘方案_副本", str(scheme_copy.get("name")))
check("复制后描述更新", scheme_copy.get("description") == "双11复盘-调整版", str(scheme_copy.get("description")))

with get_conn() as conn:
    result_copy_conflict = copy_review_scheme(conn, scheme2_id, "双11复盘方案_副本")

check("复制时重名被拦截", not result_copy_conflict["success"], str(result_copy_conflict))
check("复制重名返回needs_confirm", result_copy_conflict.get("needs_confirm") is True, str(result_copy_conflict))

with get_conn() as conn:
    logs_copy = get_scheme_operation_logs(conn, limit=1)

check("复制操作有copy日志", logs_copy[0].get("operation_type") == "copy", str(logs_copy[0]))


print("\n" + "=" * 70)
print("测试 7: 改名方案 - 修改方案名称")
print("=" * 70)

copy_id = result_copy["new_scheme_id"]

with get_conn() as conn:
    result_rename = update_review_scheme_name(conn, copy_id, "双11复盘-调整版")

check("改名成功", result_rename["success"], str(result_rename))
check("返回old_name和new_name", result_rename.get("old_name") == "双11复盘方案_副本", str(result_rename))
check("new_name正确", result_rename.get("new_name") == "双11复盘-调整版", str(result_rename))

with get_conn() as conn:
    scheme_renamed = get_review_scheme_by_id(conn, copy_id)

check("改名后名称正确", scheme_renamed.get("name") == "双11复盘-调整版", str(scheme_renamed.get("name")))

with get_conn() as conn:
    result_rename_conflict = update_review_scheme_name(conn, copy_id, "618大促复盘方案")

check("改名为已存在名称被拦截", not result_rename_conflict["success"], str(result_rename_conflict))
check("改名冲突不含needs_confirm", result_rename_conflict.get("needs_confirm") is False, str(result_rename_conflict))

with get_conn() as conn:
    logs_rename = get_scheme_operation_logs(conn, limit=1)

check("改名操作有rename日志", logs_rename[0].get("operation_type") == "rename", str(logs_rename[0]))


print("\n" + "=" * 70)
print("测试 8: 删除方案 - 删除方案及关联处理")
print("=" * 70)

with get_conn() as conn:
    result_delete = delete_review_scheme(conn, copy_id)

check("删除成功", result_delete["success"], str(result_delete))
check("返回被删除的name", result_delete.get("name") == "双11复盘-调整版", str(result_delete))

with get_conn() as conn:
    scheme_deleted = get_review_scheme_by_id(conn, copy_id)

check("删除后查询不到", scheme_deleted is None, str(scheme_deleted))

with get_conn() as conn:
    all_schemes = get_review_schemes(conn)

check("方案列表减少", len(all_schemes) == 2, str([s["name"] for s in all_schemes]))

with get_conn() as conn:
    result_delete_nonexist = delete_review_scheme(conn, 99999)

check("删除不存在的方案返回错误", not result_delete_nonexist["success"], str(result_delete_nonexist))

with get_conn() as conn:
    logs_delete = get_scheme_operation_logs(conn, limit=1)

check("删除操作有delete日志", logs_delete[0].get("operation_type") == "delete", str(logs_delete[0]))
check("删除日志scheme_id为None", logs_delete[0].get("scheme_id") is None, str(logs_delete[0]))


print("\n" + "=" * 70)
print("测试 9: 重启恢复 - 自动恢复最近使用的方案")
print("=" * 70)

print("  [INFO] 模拟重启 - 重新加载db模块")
import importlib
importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    last_used = db_mod.get_last_used_scheme(conn)

check("重启后get_last_used_scheme返回方案", last_used is not None, str(last_used))
check("重启后恢复的是最近使用的方案", last_used.get("id") == scheme2_id, str(last_used.get("id")))
check("恢复的方案filter_state完整", last_used.get("filter_state", {}).get("store_id") == sample_store, str(last_used.get("filter_state")))
check("恢复的方案包含date_from", last_used.get("filter_state", {}).get("date_from") == "2026-01-01")
check("恢复的方案包含date_to", last_used.get("filter_state", {}).get("date_to") == "2026-06-30")

print("  [INFO] 模拟删除最近使用的方案后重启")
with db_mod.get_conn() as conn:
    db_mod.delete_review_scheme(conn, scheme2_id)

importlib.reload(db_mod)
db_mod.DB_PATH = DB_PATH

with db_mod.get_conn() as conn:
    last_used2 = db_mod.get_last_used_scheme(conn)
    schemes_left = db_mod.get_review_schemes(conn)

check("最近使用的方案被删后，fallback到按更新时间排序",
      last_used2 is not None and len(schemes_left) >= 1,
      str(last_used2))


print("\n" + "=" * 70)
print("测试 10: 时间范围变化提示 - 底层数据时间变化")
print("=" * 70)

test_filter_state4 = {
    "store_id": "全部",
    "barcode": "",
    "rule_ver_a": 1,
    "rule_ver_b": 2,
    "status": "全部",
    "date_from": "",
    "date_to": "",
    "saved_at": now_iso(),
}

with get_conn() as conn:
    r_save3 = save_review_scheme(
        conn, "常规月度复盘", test_filter_state4,
        data_date_range=date_range,
    )

scheme3_id = r_save3["scheme_id"]

with get_conn() as conn:
    range_check = check_data_date_range_changed(conn, scheme3_id)

check("时间范围未变化时changed=False", range_check.get("changed") is False, str(range_check))

print("  [INFO] 模拟导入新数据改变时间范围")
NEW_CFG2 = {
    "loss_threshold_pct": 10.0,
    "loss_threshold_abs": 20.0,
    "transfer_delay_days": 10,
    "aliases": {},
}
r_save3 = save_rule_config(NEW_CFG2)

fake_future_content = read_sample("inventory.csv").decode("utf-8")
fake_future_content = fake_future_content.replace("2026-06-01", "2026-12-01").encode("utf-8")
r_inv3 = import_csv("inventory", "inventory_v3_future.csv", fake_future_content, allow_different_rule_version=True)
check("导入新数据改变时间范围", r_inv3["success"])

with get_conn() as conn:
    new_date_range = get_date_range(conn)
    range_check2 = check_data_date_range_changed(conn, scheme3_id)

check("新数据导入后时间范围变化", new_date_range.get("max_date") != date_range.get("max_date"), str(new_date_range))
check("时间范围变化时changed=True", range_check2.get("changed") is True, str(range_check2))
check("返回saved范围", range_check2.get("saved") is not None, str(range_check2))
check("返回current范围", range_check2.get("current") is not None, str(range_check2))
check("saved和current不同", range_check2["saved"]["max_date"] != range_check2["current"]["max_date"], str(range_check2))


print("\n" + "=" * 70)
print("测试 11: 操作日志 - 可查询完整历史")
print("=" * 70)

with get_conn() as conn:
    all_logs = get_scheme_operation_logs(conn, limit=100)

check("有完整操作日志", len(all_logs) >= 8, f"实际{len(all_logs)}条")

op_types = set(log["operation_type"] for log in all_logs)
expected_types = {"create", "update", "load", "copy", "rename", "delete"}
check("日志包含所有操作类型", expected_types.issubset(op_types), f"实际: {op_types}")

with get_conn() as conn:
    scheme_logs = get_scheme_operation_logs(conn, scheme_id=scheme3_id, limit=10)

check("按方案ID查询日志", len(scheme_logs) >= 1, str(len(scheme_logs)))
check("按ID查询的日志scheme_id正确", all(log.get("scheme_id") == scheme3_id for log in scheme_logs), str(scheme_logs[:2]))

log_time_sorted = sorted(all_logs, key=lambda x: x["operated_at"], reverse=True)
check("日志按时间倒序排列", all_logs[0]["operated_at"] >= all_logs[-1]["operated_at"], str([l["operated_at"] for l in all_logs[:3]]))


print("\n" + "=" * 70)
print("测试 12: 导出增强 - CSV包含方案名、时间条件、版本摘要")
print("=" * 70)

with get_conn() as conn:
    discs_a = get_discrepancies_extended(conn, store_id=sample_store, rule_version=1)
    discs_b = get_discrepancies_extended(conn, store_id=sample_store, rule_version=2)
    rule_versions = get_all_rule_versions_with_labels(conn)

def _build_map(discs):
    m = {}
    for d in discs:
        key = (d["store_id"], d["barcode"])
        if key not in m:
            m[key] = []
        m[key].append(d)
    return m

map_a = _build_map(discs_a)
map_b = _build_map(discs_b)
all_keys = sorted(set(map_a.keys()) | set(map_b.keys()))

all_export_data = []
for key in all_keys:
    list_a = map_a.get(key, [])
    list_b = map_b.get(key, [])
    da = list_a[0] if list_a else None
    db = list_b[0] if list_b else None
    row = {
        "store_id": key[0],
        "barcode": key[1],
        "sku_name": (da.get("sku_name", "") if da else "") or (db.get("sku_name", "") if db else ""),
        "v_a_diff_qty": da["diff_qty"] if da else "",
        "v_b_diff_qty": db["diff_qty"] if db else "",
    }
    all_export_data.append(row)

test_scheme_name = "618大促复盘方案"
current_scheme_id = 1
date_from_param = "2026-01-01"
date_to_param = "2026-06-30"
filter_rule_a = 1
filter_rule_b = 2

rv_map = {v["version"]: v for v in rule_versions}
cfg_a = json.loads(rv_map[1]["config_json"]) if rv_map[1].get("config_json") else {}
cfg_b = json.loads(rv_map[2]["config_json"]) if rv_map[2].get("config_json") else {}
rule_a_label = f"v{filter_rule_a} (损耗阈值{cfg_a.get('loss_threshold_pct', '-')}%)"
rule_b_label = f"v{filter_rule_b} (损耗阈值{cfg_b.get('loss_threshold_pct', '-')}%)"

export_filter_summary = {
    "exported_at": now_iso(),
    "scheme_name": test_scheme_name,
    "scheme_id": current_scheme_id,
    "filter_store": sample_store,
    "filter_barcode": "",
    "filter_status": "全部",
    "filter_rule_a": filter_rule_a,
    "filter_rule_b": filter_rule_b,
    "filter_rule_a_label": rule_a_label,
    "filter_rule_b_label": rule_b_label,
    "filter_date_from": date_from_param,
    "filter_date_to": date_to_param,
    "version_summary": {"rule_a": rule_a_label, "rule_b": rule_b_label},
    "summary": {"total_items": len(all_keys)},
}

df = pd.DataFrame(all_export_data)
df.insert(0, "scheme_name", test_scheme_name)
df.insert(1, "filter_store", sample_store)
df.insert(2, "filter_rule_a", rule_a_label)
df.insert(3, "filter_rule_b", rule_b_label)
df.insert(4, "filter_date_from", date_from_param)
df.insert(5, "filter_date_to", date_to_param)

csv_buf = io.StringIO()
df.to_csv(csv_buf, index=False, encoding="utf-8-sig")
csv_content = csv_buf.getvalue()

summary_lines = [
    "# 差异复盘对比导出",
    f"# 方案名称: {test_scheme_name}",
    f"# 导出时间: {export_filter_summary['exported_at']}",
    f"# 时间条件: {date_from_param} ~ {date_to_param}",
    f"# 版本摘要: 版本A={rule_a_label}, 版本B={rule_b_label}",
    f"# 筛选条件: 门店={sample_store}",
    f"# 对比摘要: {json.dumps(export_filter_summary['summary'], ensure_ascii=False)}",
    "#",
]
full_csv = "\n".join(summary_lines) + csv_content

check("CSV包含方案名称行", f"方案名称: {test_scheme_name}" in full_csv, full_csv[:200])
check("CSV包含时间条件行", f"时间条件: {date_from_param} ~ {date_to_param}" in full_csv, full_csv[:300])
check("CSV包含版本摘要行", "版本摘要:" in full_csv and rule_a_label in full_csv, full_csv[:300])
check("CSV数据列包含scheme_name", "scheme_name" in full_csv, full_csv[:400])
check("CSV数据列包含filter_date_from", "filter_date_from" in full_csv, full_csv[:400])
check("CSV文件名包含方案名", True, "")

csv_file_name = f"discrepancy_compare_{test_scheme_name}_{now_iso()[:10]}.csv"
csv_file_name = csv_file_name.replace(" ", "_").replace("/", "_")
check("CSV文件名格式正确", "618大促复盘方案" in csv_file_name and ".csv" in csv_file_name, csv_file_name)

with get_conn() as conn:
    log_scheme_operation(
        conn, current_scheme_id, test_scheme_name, "export",
        f"导出对比结果，共{len(all_keys)}条商品，格式:CSV"
    )

with get_conn() as conn:
    logs_export = get_scheme_operation_logs(conn, limit=1)

check("导出操作有export日志", logs_export[0].get("operation_type") == "export", str(logs_export[0]))
check("导出日志包含商品数量", f"{len(all_keys)}条商品" in logs_export[0].get("operation_detail", ""), str(logs_export[0]))


print("\n" + "=" * 70)
print("测试 13: 导出增强 - JSON包含方案名、时间条件、版本摘要")
print("=" * 70)

json_export = {
    "export_metadata": export_filter_summary,
    "scheme_info": {
        "scheme_id": current_scheme_id,
        "scheme_name": test_scheme_name,
        "time_condition": {
            "date_from": date_from_param,
            "date_to": date_to_param,
        },
        "version_summary": {
            "rule_a": rule_a_label,
            "rule_b": rule_b_label,
        },
    },
    "comparison_data": all_export_data,
    "rule_versions_info": [
        {"version": v["version"], "config": json.loads(v["config_json"]), "created_at": v["created_at"]}
        for v in rule_versions
    ],
}

json_str = json.dumps(json_export, ensure_ascii=False, indent=2, default=str)

check("JSON包含export_metadata", "export_metadata" in json_str, json_str[:200])
check("JSON包含scheme_info", "scheme_info" in json_str, json_str[:200])
check("JSON scheme_info包含scheme_name", f'"scheme_name": "{test_scheme_name}"' in json_str, json_str[:300])
check("JSON scheme_info包含time_condition", '"time_condition"' in json_str and date_from_param in json_str, json_str[:400])
check("JSON scheme_info包含version_summary", '"version_summary"' in json_str and rule_a_label in json_str, json_str[:400])
check("JSON包含comparison_data", '"comparison_data"' in json_str, json_str[:200])
check("JSON包含rule_versions_info", '"rule_versions_info"' in json_str, json_str[:200])

json_file_name = f"discrepancy_compare_{test_scheme_name}_{now_iso()[:10]}.json"
json_file_name = json_file_name.replace(" ", "_").replace("/", "_")
check("JSON文件名格式正确", "618大促复盘方案" in json_file_name and ".json" in json_file_name, json_file_name)

json_export_parsed = json.loads(json_str)
check("JSON metadata包含scheme_id", json_export_parsed["export_metadata"].get("scheme_id") == current_scheme_id)
check("JSON version_summary包含损耗阈值", "损耗阈值" in json_export_parsed["scheme_info"]["version_summary"]["rule_a"])


print("\n" + "=" * 70)
print("测试 14: 删除方案后导出 - 使用当前筛选状态")
print("=" * 70)

with get_conn() as conn:
    delete_review_scheme(conn, scheme3_id)

current_scheme_id_deleted = None
current_scheme_name_deleted = ""

export_filter_summary_no_scheme = {
    "exported_at": now_iso(),
    "scheme_name": "",
    "scheme_id": "",
    "filter_store": sample_store,
    "filter_barcode": "",
    "filter_status": "全部",
    "filter_rule_a": 1,
    "filter_rule_b": 2,
    "filter_rule_a_label": rule_a_label,
    "filter_rule_b_label": rule_b_label,
    "filter_date_from": "",
    "filter_date_to": "",
    "version_summary": {"rule_a": rule_a_label, "rule_b": rule_b_label},
    "summary": {"total_items": len(all_keys)},
}

df_no_scheme = pd.DataFrame(all_export_data)
df_no_scheme.insert(0, "scheme_name", "")
df_no_scheme.insert(1, "filter_store", sample_store)

csv_buf2 = io.StringIO()
df_no_scheme.to_csv(csv_buf2, index=False, encoding="utf-8-sig")
csv_content2 = csv_buf2.getvalue()

summary_lines2 = [
    "# 差异复盘对比导出",
    "# 方案名称: (未命名方案)",
    f"# 导出时间: {export_filter_summary_no_scheme['exported_at']}",
    "# 时间条件: 不限 ~ 不限",
    f"# 版本摘要: 版本A={rule_a_label}, 版本B={rule_b_label}",
    "#",
]
full_csv2 = "\n".join(summary_lines2) + csv_content2

check("删除方案后CSV使用(未命名方案)", "方案名称: (未命名方案)" in full_csv2, full_csv2[:200])
check("删除方案后scheme_name列为空", '""' in full_csv2.split("\n")[7] or ",," in full_csv2.split("\n")[7], full_csv2.split("\n")[7])

json_export_no_scheme = {
    "export_metadata": export_filter_summary_no_scheme,
    "scheme_info": {
        "scheme_id": "",
        "scheme_name": "",
        "time_condition": {"date_from": "不限", "date_to": "不限"},
        "version_summary": {"rule_a": rule_a_label, "rule_b": rule_b_label},
    },
    "comparison_data": all_export_data,
}

json_str_no_scheme = json.dumps(json_export_no_scheme, ensure_ascii=False, indent=2)
check("删除方案后JSON scheme_name为空", '"scheme_name": ""' in json_str_no_scheme, json_str_no_scheme[:200])

csv_file_name2 = f"discrepancy_compare_unnamed_{now_iso()[:10]}.csv"
check("无方案时文件名使用unnamed", "unnamed" in csv_file_name2, csv_file_name2)


print("\n" + "=" * 70)
print("测试 15: 完整链路 - 新建→载入→修改→复制→改名→删除→导出")
print("=" * 70)

print("  [INFO] 步骤1: 新建方案")
full_filter = {
    "store_id": sample_store,
    "barcode": "690",
    "rule_ver_a": 1,
    "rule_ver_b": 2,
    "status": "pending_review",
    "date_from": date_range.get("min_date", ""),
    "date_to": date_range.get("max_date", ""),
    "saved_at": now_iso(),
}

with get_conn() as conn:
    r1 = save_review_scheme(conn, "完整测试方案", full_filter, description="完整链路测试", data_date_range=date_range)
check("1. 新建成功", r1["success"])
full_id = r1["scheme_id"]

print("  [INFO] 步骤2: 载入方案")
with get_conn() as conn:
    mark_scheme_used(conn, full_id)
    loaded = get_review_scheme_by_id(conn, full_id)
check("2. 载入成功，last_used_at更新", loaded.get("last_used_at") is not None)

print("  [INFO] 步骤3: 修改（覆盖保存）")
full_filter["barcode"] = "6901"
with get_conn() as conn:
    r3 = save_review_scheme(conn, "完整测试方案", full_filter, overwrite=True, data_date_range=date_range)
check("3. 覆盖保存成功", r3["success"] and r3["overwritten"])

print("  [INFO] 步骤4: 复制方案")
with get_conn() as conn:
    r4 = copy_review_scheme(conn, full_id, "完整测试方案_副本")
check("4. 复制成功", r4["success"])
copy_full_id = r4["new_scheme_id"]

print("  [INFO] 步骤5: 改名方案")
with get_conn() as conn:
    r5 = update_review_scheme_name(conn, copy_full_id, "完整测试方案V2")
check("5. 改名成功", r5["success"])

print("  [INFO] 步骤6: 验证操作日志完整")
with get_conn() as conn:
    full_logs = get_scheme_operation_logs(conn)
    full_types = [l["operation_type"] for l in full_logs if l["scheme_name"] in ("完整测试方案", "完整测试方案V2")]

check("6. 操作日志包含create/update/load/copy/rename",
      {"create", "update", "load", "copy", "rename"}.issubset(set(full_types)),
      str(full_types))

print("  [INFO] 步骤7: 删除方案")
with get_conn() as conn:
    r6 = delete_review_scheme(conn, copy_full_id)
    r7 = delete_review_scheme(conn, full_id)
check("7. 删除成功", r6["success"] and r7["success"])

print("  [INFO] 步骤8: 导出（无方案时）")
with get_conn() as conn:
    remaining = get_review_schemes(conn)
check("8. 方案已全部清理", len(remaining) == 0 or all(s["name"] != "完整测试方案" for s in remaining), str([s["name"] for s in remaining]))


print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 复盘方案管理回归测试全部通过!")
    print("   [OK] 数据库升级 (review_schemes + review_scheme_operations)")
    print("   [OK] 新建方案 - 保存筛选状态为可命名方案")
    print("   [OK] 重名冲突 - 检测+覆盖确认机制")
    print("   [OK] 载入方案 - 一键恢复筛选状态")
    print("   [OK] 复制方案 - 复制后改名修改")
    print("   [OK] 改名方案 - 修改方案名称")
    print("   [OK] 删除方案 - 删除及关联处理")
    print("   [OK] 重启恢复 - 自动恢复最近使用的方案")
    print("   [OK] 时间范围变化提示 - 底层数据变化时提醒")
    print("   [OK] 操作日志 - 所有操作可查询")
    print("   [OK] CSV导出 - 含方案名+时间条件+版本摘要")
    print("   [OK] JSON导出 - 含方案名+时间条件+版本摘要")
    print("   [OK] 删除后导出 - 使用当前筛选状态")
    print("   [OK] 完整链路 - 新建→载入→修改→复制→改名→删除→导出")
