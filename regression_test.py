"""
门店盘点差异复盘工具 — 完整回归测试
覆盖:
  1. 导入校验（销售/库存坏行拦截，带文件和行号）
  2. 重复导入去重
  3. 差异归因 + 证据链
  4. 复核备注 + 状态流转 + 流转日志
  5. 规则变更 → 历史差异全量重算（旧归因/旧规则版本被刷新）
  6. 规则写坏 → 旧数据保留
  7. 门店筛选
  8. CSV/JSON 导出包含证据/流转/备注
  9. 重启后数据一致
"""
import os
import sys
import json
import sqlite3
import io
import csv
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff.db")

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
    print("🧹 已清空旧数据库")

from db import (
    init_db, get_conn, get_discrepancies, get_evidence_for_discrepancy,
    get_status_log, get_stores, get_import_records,
    transition_status, update_review_note, get_active_rule_version,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_CLOSED, STATUS_LABELS,
)
from import_service import import_csv, validate_row, compute_file_hash, REQUIRED_FIELDS, NUMERIC_FIELDS
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config, get_version_history
from sample_data import generate_sample_data, SAMPLE_DIR

init_db()
generate_sample_data()


def read_sample(fname):
    with open(os.path.join(SAMPLE_DIR, fname), "rb") as f:
        return f.read()


errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}  {detail}")
        errors_total.append((name, detail))


print("\n" + "=" * 70)
print("测试 1: 行级校验单元 — 空数量/空条码被拦截")
print("=" * 70)
check("缺barcode的销售行被拦截",
      any("缺少必填字段 'barcode'" in e for e in validate_row("sales",
          {"store_id": "S001", "barcode": "", "sale_qty": "5", "sale_date": "2025-06-20"}, 2)))
check("空字符串sale_qty被拦截(根因修复)",
      any("缺少必填字段 'sale_qty'" in e for e in validate_row("sales",
          {"store_id": "S001", "barcode": "6900000000001", "sale_qty": "", "sale_date": "2025-06-20"}, 3)))
check("None值sale_qty被拦截",
      any("缺少必填字段 'sale_qty'" in e for e in validate_row("sales",
          {"store_id": "S001", "barcode": "6900000000001", "sale_qty": None, "sale_date": "2025-06-20"}, 4)))
check("非数值sale_qty被拦截",
      any("不是有效数值" in e for e in validate_row("sales",
          {"store_id": "S001", "barcode": "6900000000001", "sale_qty": "abc", "sale_date": "2025-06-20"}, 5)))
check("缺sale_date被拦截",
      any("缺少必填字段 'sale_date'" in e for e in validate_row("sales",
          {"store_id": "S001", "barcode": "6900000000001", "sale_qty": "5", "sale_date": ""}, 6)))
check("正常销售行无错误",
      len(validate_row("sales",
          {"store_id": "S001", "barcode": "6900000000001", "sale_qty": "5", "sale_date": "2025-06-20"}, 7)) == 0)

print("\n" + "=" * 70)
print("测试 2: 导入四类样例CSV")
print("=" * 70)
r_inv = import_csv("inventory", "inventory.csv", read_sample("inventory.csv"))
check("库存导入成功", r_inv["success"], str(r_inv))
check("库存导入10行有效", r_inv.get("valid_rows") == 10, str(r_inv))

r_sal = import_csv("sales", "sales.csv", read_sample("sales.csv"))
check("销售导入成功", r_sal["success"])
check("销售导入3行有效", r_sal.get("valid_rows") == 3, str(r_sal))

r_tra = import_csv("transfer", "transfer.csv", read_sample("transfer.csv"))
check("调拨导入成功", r_tra["success"])
check("调拨导入2行有效", r_tra.get("valid_rows") == 2, str(r_tra))

r_stk = import_csv("stocktake", "stocktake.csv", read_sample("stocktake.csv"))
check("盘点导入成功", r_stk["success"])
check("盘点导入10行有效", r_stk.get("valid_rows") == 10, str(r_stk))

print("\n" + "=" * 70)
print("测试 3: 重复导入拦截（哈希去重）")
print("=" * 70)
r_dup = import_csv("inventory", "inventory_copy.csv", read_sample("inventory.csv"))
check("重复导入被拦截", r_dup.get("duplicate") is True, str(r_dup))
check("拦截信息包含'已导入过'", "已导入过" in r_dup.get("error", ""), r_dup.get("error"))

print("\n" + "=" * 70)
print("测试 4: 坏行销售CSV — 缺条码/数量/日期/非数值均被拦截，含行号")
print("=" * 70)
r_bad_sales = import_csv("sales", "sales_with_bad_rows.csv", read_sample("sales_with_bad_rows.csv"))
check("坏行销售导入返回成功（部分行有效）", r_bad_sales["success"], str(r_bad_sales))
check("仅1行有效（5行-4坏行）", r_bad_sales.get("valid_rows") == 1, str(r_bad_sales))
check("至少4条错误（4种坏行）", len(r_bad_sales.get("detail_errors", [])) >= 4, str(r_bad_sales.get("detail_errors")))
errs = r_bad_sales.get("detail_errors", [])
check("行号出现'第 2 行'（缺条码）", any("第 2 行" in e for e in errs), str(errs))
check("行号出现'第 3 行'（缺数量）", any("第 3 行" in e for e in errs), str(errs))
check("行号出现'第 4 行'（非数值）", any("第 4 行" in e for e in errs), str(errs))
check("行号出现'第 5 行'（缺日期）", any("第 5 行" in e for e in errs), str(errs))
check("错误包含'sale_qty'关键字", any("sale_qty" in e for e in errs), str(errs))
check("错误包含'barcode'关键字", any("barcode" in e for e in errs), str(errs))

print("\n" + "=" * 70)
print("测试 5: 运行归因，生成差异+证据链+规则版本")
print("=" * 70)
r_attr = run_attribution()
check("归因成功", r_attr["success"], str(r_attr))
check("生成差异>0", r_attr.get("created", 0) > 0, str(r_attr))
check("规则版本号v1", r_attr.get("rule_version") == 1, str(r_attr))

with get_conn() as conn:
    discs = get_discrepancies(conn)
check(f"DB中有差异: {len(discs)} 条", len(discs) > 0, str(discs))

sample_disc = discs[0]
with get_conn() as conn:
    evs = get_evidence_for_discrepancy(conn, sample_disc["id"])
check(f"差异 {sample_disc['id']} 有证据行: {len(evs)} 条", len(evs) >= 2, str(evs))
check("证据行关联到原始raw_data", all(e.get("raw_data_id") for e in evs), str(evs))
check("证据含source_line（可追溯到CSV行号）", all(e.get("source_line") is not None for e in evs), str(evs))

print("\n" + "=" * 70)
print("测试 6: 复核备注 + 状态流转 + 流转日志")
print("=" * 70)
d1 = discs[0]
NOTE_TEXT = f"回归测试: 已复核差异{d1['id']}，确认属于正常损耗范围"
with get_conn() as conn:
    update_review_note(conn, d1["id"], NOTE_TEXT)
    transition_status(conn, d1["id"], STATUS_CONFIRMED, note="自动流转到已确认")
    transition_status(conn, d1["id"], STATUS_CLOSED, note="确认无误关闭")

with get_conn() as conn:
    d1_updated = get_discrepancies(conn)[0]
    logs = get_status_log(conn, d1["id"])
check("复核备注已保存", d1_updated.get("review_note") == NOTE_TEXT, d1_updated.get("review_note"))
check("状态已流转到closed", d1_updated["status"] == STATUS_CLOSED, d1_updated["status"])
check("有2条流转日志", len(logs) == 2, str(logs))
check("日志1: pending→confirmed", logs[0]["from_status"] == STATUS_PENDING_REVIEW and logs[0]["to_status"] == STATUS_CONFIRMED, str(logs[0]))
check("日志2: confirmed→closed", logs[1]["from_status"] == STATUS_CONFIRMED and logs[1]["to_status"] == STATUS_CLOSED, str(logs[1]))

d2 = None
with get_conn() as conn:
    for dd in get_discrepancies(conn):
        if dd["id"] != d1["id"]:
            d2 = dd
            break
if d2:
    NOTE_D2 = f"差异{d2['id']}复核: 待进一步追责"
    with get_conn() as conn:
        update_review_note(conn, d2["id"], NOTE_D2)
        transition_status(conn, d2["id"], STATUS_CONFIRMED)
    with get_conn() as conn:
        d2_upd = next(dd for dd in get_discrepancies(conn) if dd["id"] == d2["id"])
    check(f"差异{d2['id']} 备注已保存", d2_upd.get("review_note") == NOTE_D2)
    check(f"差异{d2['id']} 状态confirmed", d2_upd["status"] == STATUS_CONFIRMED)

print("\n" + "=" * 70)
print("测试 7: 门店筛选")
print("=" * 70)
with get_conn() as conn:
    stores = get_stores(conn)
check(f"识别出门店: {stores}", len(stores) >= 2, str(stores))
with get_conn() as conn:
    s001 = get_discrepancies(conn, store_id="S001")
    s002 = get_discrepancies(conn, store_id="S002")
check(f"S001 有 {len(s001)} 条差异", len(s001) > 0)
check(f"S002 有 {len(s002)} 条差异", len(s002) > 0)
check("S001差异不含S002", all(d["store_id"] == "S001" for d in s001), str([d["store_id"] for d in s001]))
check("S002差异不含S001", all(d["store_id"] == "S002" for d in s002), str([d["store_id"] for d in s002]))

print("\n" + "=" * 70)
print("测试 8: 规则写坏不冲旧数据")
print("=" * 70)
cfg_before, ver_before = get_current_config()
r_bad_cfg = save_rule_config({"loss_threshold_pct": "not_a_number", "aliases": "bad"})
check("坏规则被拒绝（失败）", r_bad_cfg["success"] is False, str(r_bad_cfg))
cfg_after, ver_after = get_current_config()
check("规则版本未变", ver_before == ver_after, f"{ver_before} vs {ver_after}")
check("当前激活规则参数未变", cfg_before == cfg_after, f"{cfg_before} vs {cfg_after}")

print("\n" + "=" * 70)
print("测试 9: 规则变更 → 历史差异全量重算（核心验证）")
print("=" * 70)
# 记录重算前快照
with get_conn() as conn:
    discs_before = get_discrepancies(conn)
    all_ev_before = {}
    all_log_before = {}
    for d in discs_before:
        all_ev_before[d["id"]] = get_evidence_for_discrepancy(conn, d["id"])
        all_log_before[d["id"]] = get_status_log(conn, d["id"])
    old_rule_ver = discs_before[0].get("rule_ver")
    old_ids = {d["id"] for d in discs_before}

NEW_CFG = {
    "loss_threshold_pct": 5.0,
    "loss_threshold_abs": 10.0,
    "transfer_delay_days": 7,
    "aliases": {"ALT_TEST_123": "6901234567890"},
}
r_save = save_rule_config(NEW_CFG)
check(f"新规则保存成功 {r_save.get('message')}", r_save["success"], str(r_save))
check("规则版本号递增（v2）", "v2" in r_save.get("message", "") or r_save.get("version") == 2, str(r_save))

with get_conn() as conn:
    discs_after = get_discrepancies(conn)
    new_ids = {d["id"] for d in discs_after}
    new_rule_ver = discs_after[0].get("rule_ver") if discs_after else None
    ev_cnt_after = sum(len(get_evidence_for_discrepancy(conn, d["id"])) for d in discs_after)
    log_cnt_after = sum(len(get_status_log(conn, d["id"])) for d in discs_after)

check("旧差异ID全部被清空（旧ID与新ID无交集）",
      len(old_ids & new_ids) == 0, f"old={old_ids} new={new_ids}")
check("所有新差异都绑定新规则版本v2",
      new_rule_ver == 2 and all(d.get("rule_ver") == 2 for d in discs_after),
      f"新规则版本={new_rule_ver}, 各条={[d.get('rule_ver') for d in discs_after]}")
check("新差异都有证据行（证据链重建）", ev_cnt_after > 0 and ev_cnt_after >= len(discs_after) * 2, f"证据行总数={ev_cnt_after}")
check("旧状态流转日志全部清空（因差异全量重算）",
      log_cnt_after == 0, f"日志数={log_cnt_after}")

print("\n" + "=" * 70)
print("测试 10: CSV/JSON 导出内容完整性 — 证据+流转+备注")
print("=" * 70)
# 先给新差异加备注和流转，便于校验导出
with get_conn() as conn:
    new_discs = get_discrepancies(conn)
EXPORT_NOTE = "导出校验: 这条差异的备注、证据、流转必须全部出现在CSV和JSON里"
with get_conn() as conn:
    update_review_note(conn, new_discs[0]["id"], EXPORT_NOTE)
    transition_status(conn, new_discs[0]["id"], STATUS_CONFIRMED, note="导出测试流转")
with get_conn() as conn:
    export_discs = get_discrepancies(conn)
    for d in export_discs:
        d["evidence_lines"] = get_evidence_for_discrepancy(conn, d["id"])
        d["status_logs"] = get_status_log(conn, d["id"])

# 组装CSV输出
csv_rows = []
for d in export_discs:
    ev_parts = []
    for ev in d["evidence_lines"]:
        tl = {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}.get(
            ev.get("source_type", ""), ev.get("source_type", ""))
        ev_parts.append(f"[{tl}] 行{ev.get('source_line')}: {ev.get('description','')}")
    log_parts = []
    for lg in d["status_logs"]:
        from_l = STATUS_LABELS.get(lg["from_status"], lg["from_status"] or "新建")
        to_l = STATUS_LABELS.get(lg["to_status"], lg["to_status"])
        log_parts.append(f"{lg['changed_at'][:19]}: {from_l}→{to_l}")
    csv_rows.append({
        "复核备注": d.get("review_note", ""),
        "来源证据": " | ".join(ev_parts),
        "状态流转": " | ".join(log_parts),
        "规则版本": d.get("rule_ver"),
    })

target_row = next(r for r in csv_rows if r["复核备注"] == EXPORT_NOTE)
check("CSV列包含复核备注全文", EXPORT_NOTE in target_row["复核备注"], target_row["复核备注"])
check("CSV列包含来源证据（含行号）", "行" in target_row["来源证据"] and len(target_row["来源证据"]) > 20, target_row["来源证据"])
check("CSV列包含状态流转（含→）", "→" in target_row["状态流转"] and "待复核" in target_row["状态流转"], target_row["状态流转"])
check("CSV列包含规则版本v2", target_row["规则版本"] == 2, str(target_row["规则版本"]))

# 组装JSON输出
json_obj = []
for d in export_discs:
    json_obj.append({
        "review_note": d.get("review_note"),
        "rule_version": d.get("rule_ver"),
        "evidence_lines": [
            {"source_type": e.get("source_type"), "source_line": e.get("source_line"), "description": e.get("description")}
            for e in d["evidence_lines"]
        ],
        "status_logs": [
            {"from_status": lg.get("from_status"), "to_status": lg.get("to_status"), "note": lg.get("note")}
            for lg in d["status_logs"]
        ],
    })
target_json = next(j for j in json_obj if j["review_note"] == EXPORT_NOTE)
check("JSON包含复核备注", target_json["review_note"] == EXPORT_NOTE)
check("JSON嵌套evidence_lines数组且有内容", len(target_json["evidence_lines"]) >= 2, str(target_json["evidence_lines"]))
check("JSON证据含source_line", all(e["source_line"] is not None for e in target_json["evidence_lines"]), str(target_json["evidence_lines"]))
check("JSON嵌套status_logs数组且有内容", len(target_json["status_logs"]) >= 1, str(target_json["status_logs"]))
check("JSON流转日志含'导出测试流转'备注", any("导出测试流转" in str(lg.get("note", "")) for lg in target_json["status_logs"]), str(target_json["status_logs"]))
check("JSON规则版本为v2", target_json["rule_version"] == 2, str(target_json["rule_version"]))

print("\n" + "=" * 70)
print("测试 11: 重启模拟（断开重连数据库后数据一致性）")
print("=" * 70)
# 关闭所有连接再重新打开
import importlib
import db as db_mod
importlib.reload(db_mod)

with db_mod.get_conn() as conn:
    discs_reconnect = db_mod.get_discrepancies(conn)
    stores_reconnect = db_mod.get_stores(conn)
    d_first = discs_reconnect[0]
    ev_first = db_mod.get_evidence_for_discrepancy(conn, d_first["id"])
    log_first = db_mod.get_status_log(conn, d_first["id"])
    active_rule = db_mod.get_active_rule_version(conn)

check(f"重连后差异数一致: {len(discs_reconnect)}", len(discs_reconnect) == len(export_discs))
check(f"重连后门店数一致: {stores_reconnect}", set(stores_reconnect) == set(stores))
check(f"重连后差异 {d_first['id']} 有证据", len(ev_first) >= 2)
check(f"重连后规则v2激活", active_rule and active_rule["version"] == 2, str(active_rule))

# 验证带备注的那条
noted = next((d for d in discs_reconnect if d.get("review_note") == EXPORT_NOTE), None)
check("重连后复核备注仍存在", noted is not None, f"找备注={EXPORT_NOTE[:20]}...")
if noted:
    with db_mod.get_conn() as conn:
        noted_logs = db_mod.get_status_log(conn, noted["id"])
    check("重连后该条的流转日志存在", len(noted_logs) >= 1, str(noted_logs))

print("\n" + "=" * 70)
if errors_total:
    print(f"❌ 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("🎉 全部回归测试通过!")
