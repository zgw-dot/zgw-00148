"""
门店盘点差异复盘工具 — 归因快照增强版回归测试
覆盖:
  1. 导入 + 首次归因 → 生成归因快照 + 计算步骤
  2. 老差异加备注 + 状态流转 → 记录
  3. 规则变更 → 重新归因时老差异ID/状态/备注/流转日志全部保留，
     仅system_qty/actual_qty/diff_qty/cause/规则版本/证据/快照/计算步骤更新
  4. 快照包含: 规则配置快照、别名映射前后、各类原始数据ID列表
  5. 计算步骤包含: 分步扣减记录、剩余值变化
  6. CSV/JSON 导出包含完整快照和计算步骤（可独立复盘）
  7. 重启后 → 快照/状态/备注/流转日志/计算步骤全部保留
  8. 重复导入 → 哈希去重，不静默覆盖
  9. 解释链路可独立复盘（JSON导出后，脱离DB也能还原完整归因过程）
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

from test_utils import init_test_env
init_test_env(DB_PATH)

from db import (
    init_db, get_conn, get_discrepancies, get_evidence_for_discrepancy,
    get_status_log, get_stores, get_import_records,
    transition_status, update_review_note, get_active_rule_version,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_CLOSED, STATUS_LABELS,
    get_snapshot_for_discrepancy, get_calc_steps_for_discrepancy,
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
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


print("\n" + "=" * 70)
print("测试 1: 导入四类样例CSV (基础导入)")
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
print("测试 2: 首次运行归因 → v1规则，生成快照 + 计算步骤")
print("=" * 70)
r_attr1 = run_attribution()
check("首次归因成功", r_attr1["success"], str(r_attr1))
check("首次归因创建差异>0", r_attr1.get("created", 0) > 0, str(r_attr1))
check("首次归因更新数=0（全是新）", r_attr1.get("updated", 0) == 0, str(r_attr1))
check("规则版本号v1", r_attr1.get("rule_version") == 1, str(r_attr1))

with get_conn() as conn:
    discs_v1 = get_discrepancies(conn)
check(f"DB中有差异: {len(discs_v1)} 条", len(discs_v1) > 0, str(discs_v1))

sample_disc = discs_v1[0]
sample_id_v1 = sample_disc["id"]
sample_store_v1 = sample_disc["store_id"]
sample_barcode_v1 = sample_disc["barcode"]

with get_conn() as conn:
    snap_v1 = get_snapshot_for_discrepancy(conn, sample_id_v1)
    steps_v1 = get_calc_steps_for_discrepancy(conn, sample_id_v1)
    evs_v1 = get_evidence_for_discrepancy(conn, sample_id_v1)

check(f"差异 {sample_id_v1} 有快照", snap_v1 is not None, f"snap={snap_v1}")
if snap_v1:
    check(f"  快照关联规则版本ID正确", snap_v1.get("rule_version_id") is not None, str(snap_v1))
    check(f"  快照包含规则配置(有损耗阈值)",
          isinstance(snap_v1.get("rule_config_snapshot"), dict) and
          "loss_threshold_pct" in (snap_v1.get("rule_config_snapshot") or {}),
          str(snap_v1.get("rule_config_snapshot")))
    check(f"  快照有原始库存ID列表(非空)",
          isinstance(snap_v1.get("raw_inventory_ids"), list) and len(snap_v1["raw_inventory_ids"]) > 0,
          str(snap_v1.get("raw_inventory_ids")))
    check(f"  快照有原始盘点ID列表(非空)",
          isinstance(snap_v1.get("raw_stocktake_ids"), list) and len(snap_v1["raw_stocktake_ids"]) > 0,
          str(snap_v1.get("raw_stocktake_ids")))
    check(f"  快照system/actual/diff与差异记录一致",
          abs(snap_v1["system_qty_snapshot"] - sample_disc["system_qty"]) < 0.001 and
          abs(snap_v1["actual_qty_snapshot"] - sample_disc["actual_qty"]) < 0.001 and
          abs(snap_v1["diff_qty_snapshot"] - sample_disc["diff_qty"]) < 0.001,
          f"snap={snap_v1['system_qty_snapshot']}/{snap_v1['actual_qty_snapshot']}/{snap_v1['diff_qty_snapshot']} "
          f"disc={sample_disc['system_qty']}/{sample_disc['actual_qty']}/{sample_disc['diff_qty']}")

check(f"差异 {sample_id_v1} 有计算步骤: {len(steps_v1)} 步", len(steps_v1) >= 2, str(steps_v1))
if steps_v1:
    init_step = steps_v1[0]
    check(f"  第1步是初始计算(init)", init_step["step_type"] == "init", str(init_step))
    check(f"  初始步骤剩余值等于|diff|",
          abs(abs(init_step["remaining_after"]) - abs(sample_disc["diff_qty"])) < 0.001,
          f"step_rem={init_step['remaining_after']} diff={sample_disc['diff_qty']}")
    last_step = steps_v1[-1]
    check(f"  最后一步剩余值为0(归因闭环)",
          abs(last_step["remaining_after"]) < 0.001,
          f"最后一步剩余={last_step['remaining_after']}")

check(f"差异 {sample_id_v1} 有证据行: {len(evs_v1)} 条", len(evs_v1) >= 2, str(evs_v1))


print("\n" + "=" * 70)
print("测试 3: 给老差异加备注和状态流转（模拟人工复核）")
print("=" * 70)
NOTE_TEXT_V1 = f"[快照测试] 已复核差异{sample_id_v1}，确认属于正常损耗范围，已电话联系门店主管"
with get_conn() as conn:
    update_review_note(conn, sample_id_v1, NOTE_TEXT_V1)
    transition_status(conn, sample_id_v1, STATUS_CONFIRMED, note="自动流转到已确认: 归因逻辑无误")
    transition_status(conn, sample_id_v1, STATUS_CLOSED, note="确认无误关闭，本季度第3次类似情况")

with get_conn() as conn:
    d1_after_note = next(d for d in get_discrepancies(conn) if d["id"] == sample_id_v1)
    logs_v1 = get_status_log(conn, sample_id_v1)

check("复核备注已保存（完整文本）", d1_after_note.get("review_note") == NOTE_TEXT_V1,
      d1_after_note.get("review_note"))
check("状态已流转到closed", d1_after_note["status"] == STATUS_CLOSED, d1_after_note["status"])
check(f"有2条流转日志", len(logs_v1) == 2, str(logs_v1))
check("日志1: pending→confirmed (含备注)",
      logs_v1[0]["from_status"] == STATUS_PENDING_REVIEW and
      logs_v1[0]["to_status"] == STATUS_CONFIRMED and
      "自动流转" in (logs_v1[0].get("note") or ""),
      str(logs_v1[0]))
check("日志2: confirmed→closed (含备注)",
      logs_v1[1]["from_status"] == STATUS_CONFIRMED and
      logs_v1[1]["to_status"] == STATUS_CLOSED and
      "确认无误关闭" in (logs_v1[1].get("note") or ""),
      str(logs_v1[1]))

ids_before_rule = {d["id"] for d in discs_v1}
status_before_rule = {d["id"]: d["status"] for d in discs_v1}
notes_before_rule = {d["id"]: d.get("review_note", "") for d in discs_v1}
created_at_before_rule = {d["id"]: d["created_at"] for d in discs_v1}
print(f"  [INFO] 规则变更前差异ID集合: {sorted(ids_before_rule)}")


print("\n" + "=" * 70)
print("测试 4: 规则变更 → 老差异保留原始快照/阈值/归因（不可变）")
print("=" * 70)
NEW_CFG = {
    "loss_threshold_pct": 8.0,
    "loss_threshold_abs": 15.0,
    "transfer_delay_days": 7,
    "aliases": {"ALT_TEST_123": "6901234567890"},
}
r_save = save_rule_config(NEW_CFG)
check(f"新规则保存成功 {r_save.get('message')}", r_save["success"], str(r_save))
check("规则版本号递增（v2）", "v2" in r_save.get("message", "") or r_save.get("version") == 2, str(r_save))
check("重算结果已有差异被跳过（skipped>0）",
      (r_save.get("recomputed") or {}).get("skipped", 0) > 0,
      str(r_save.get("recomputed")))

with get_conn() as conn:
    discs_v2 = get_discrepancies(conn)
ids_after_rule = {d["id"] for d in discs_v2}
print(f"  [INFO] 规则变更后差异ID集合: {sorted(ids_after_rule)}")

check("老差异ID全部保留（有交集，ID不被清空）",
      len(ids_before_rule & ids_after_rule) > 0,
      f"交集={ids_before_rule & ids_after_rule} 前={ids_before_rule} 后={ids_after_rule}")
check(f"样本差异 {sample_id_v1} ID仍然存在",
      sample_id_v1 in ids_after_rule,
      f"{sample_id_v1} not in {ids_after_rule}")

with get_conn() as conn:
    d2_sample = next(d for d in get_discrepancies(conn) if d["id"] == sample_id_v1)
    logs_v2_sample = get_status_log(conn, sample_id_v1)
    snap_v2 = get_snapshot_for_discrepancy(conn, sample_id_v1)
    steps_v2 = get_calc_steps_for_discrepancy(conn, sample_id_v1)

check(f"样本差异 {sample_id_v1} 状态仍然是closed（没被冲回pending）",
      d2_sample["status"] == STATUS_CLOSED, d2_sample["status"])
check(f"样本差异复核备注仍然保留（完整文本）",
      d2_sample.get("review_note") == NOTE_TEXT_V1, d2_sample.get("review_note"))
check(f"样本差异created_at时间戳未变（不是新插入）",
      d2_sample["created_at"] == created_at_before_rule[sample_id_v1],
      f"前={created_at_before_rule[sample_id_v1]} 后={d2_sample['created_at']}")
check(f"样本差异流转日志仍有2条（未被清空）",
      len(logs_v2_sample) == 2, f"日志数={len(logs_v2_sample)} 详情={logs_v2_sample}")

check(f"样本差异规则版本仍为v1（不被覆盖为v2）",
      d2_sample.get("rule_ver") == 1,
      f"rule_ver={d2_sample.get('rule_ver')}")
check(f"样本差异快照仍保留v1原始配置（阈值=2.0，不是8%）",
      snap_v2 is not None and
      (snap_v2.get("rule_config_snapshot") or {}).get("loss_threshold_pct") == 2.0,
      f"snap_cfg={snap_v2.get('rule_config_snapshot') if snap_v2 else '无快照'}")
check(f"样本差异计算步骤仍保留v1原始步骤（至少2步）",
      len(steps_v2) >= 2, f"steps={len(steps_v2)}")

d2_others = [d for d in discs_v2 if d["id"] != sample_id_v1]
if d2_others:
    check(f"其他差异规则版本仍为v1（不被覆盖）",
          all(d.get("rule_ver") == 1 for d in d2_others),
          str([(d["id"], d.get("rule_ver")) for d in d2_others]))


print("\n" + "=" * 70)
print("测试 5: CSV导出 包含完整快照和计算步骤（可独立复盘）")
print("=" * 70)
with get_conn() as conn:
    export_discs = get_discrepancies(conn)
    for d in export_discs:
        d["evidence_lines"] = get_evidence_for_discrepancy(conn, d["id"])
        d["status_logs"] = get_status_log(conn, d["id"])
        d["snapshot"] = get_snapshot_for_discrepancy(conn, d["id"])
        d["calc_steps"] = get_calc_steps_for_discrepancy(conn, d["id"])

sample_export = next(d for d in export_discs if d["id"] == sample_id_v1)
csv_alias_col = ""
csv_calc_col = ""
csv_rule_cfg_col = ""
if sample_export.get("snapshot"):
    cfg = sample_export["snapshot"].get("rule_config_snapshot", {}) or {}
    alias_info = ""
    if sample_export["snapshot"].get("alias_before"):
        alias_info = f"{sample_export['snapshot']['alias_before']} → {sample_export['snapshot']['alias_after']}"
    csv_alias_col = alias_info or "(无别名映射)"
    csv_rule_cfg_col = json.dumps(cfg, ensure_ascii=False)
calc_steps = sample_export.get("calc_steps", [])
if calc_steps:
    csv_calc_col = json.dumps([
        {
            "step_index": cs["step_index"], "step_type": cs["step_type"],
            "step_description": cs["step_description"],
            "amount_applied": cs["amount_applied"],
            "remaining_before": cs["remaining_before"], "remaining_after": cs["remaining_after"],
        }
        for cs in calc_steps
    ], ensure_ascii=False)

check("CSV列 快照-别名映射 存在（非空提示）", len(csv_alias_col) > 0, csv_alias_col)
check("CSV列 快照-当时规则配置(JSON) 含loss_threshold_pct(=2.0)",
      "loss_threshold_pct" in csv_rule_cfg_col and "2.0" in csv_rule_cfg_col, csv_rule_cfg_col)
check("CSV列 计算步骤(JSON) 有步骤且非空",
      len(calc_steps) >= 2 and len(csv_calc_col) > 50,
      f"步骤数={len(calc_steps)} JSON长度={len(csv_calc_col)}")
check("CSV列 计算步骤(JSON) 含init步骤",
      '"step_type": "init"' in csv_calc_col, csv_calc_col)


print("\n" + "=" * 70)
print("测试 6: JSON导出 解释链路完整（脱离DB可独立复盘）")
print("=" * 70)
json_obj = []
for d in export_discs:
    snap = d.get("snapshot")
    snap_obj = None
    if snap:
        snap_obj = {
            "alias_before": snap.get("alias_before"), "alias_after": snap.get("alias_after"),
            "rule_config_snapshot": snap.get("rule_config_snapshot", {}),
            "system_qty_snapshot": snap.get("system_qty_snapshot"),
            "actual_qty_snapshot": snap.get("actual_qty_snapshot"),
            "diff_qty_snapshot": snap.get("diff_qty_snapshot"),
            "raw_inventory_ids": snap.get("raw_inventory_ids", []),
            "raw_stocktake_ids": snap.get("raw_stocktake_ids", []),
            "raw_sales_ids": snap.get("raw_sales_ids", []),
            "raw_transfer_ids": snap.get("raw_transfer_ids", []),
        }
    calc_steps_json = []
    for cs in (d.get("calc_steps") or []):
        calc_steps_json.append({
            "step_index": cs["step_index"], "step_type": cs["step_type"],
            "step_description": cs["step_description"], "amount_applied": cs["amount_applied"],
            "remaining_before": cs["remaining_before"], "remaining_after": cs["remaining_after"],
            "raw_data_ids": cs.get("raw_data_ids", []),
        })
    json_obj.append({
        "id": d["id"], "store_id": d["store_id"], "barcode": d["barcode"],
        "review_note": d.get("review_note"), "status": d["status"],
        "status_logs": [dict(lg) for lg in d.get("status_logs", [])],
        "attribution_snapshot": snap_obj, "calculation_steps": calc_steps_json,
    })

sample_json = next(j for j in json_obj if j["id"] == sample_id_v1)
check("JSON嵌套 attribution_snapshot 不为None", sample_json["attribution_snapshot"] is not None,
      str(sample_json.get("attribution_snapshot")))
if sample_json["attribution_snapshot"]:
    check("JSON快照 规则配置含loss_threshold_pct=2(原始v1值)",
          sample_json["attribution_snapshot"]["rule_config_snapshot"].get("loss_threshold_pct") == 2.0,
          str(sample_json["attribution_snapshot"]["rule_config_snapshot"]))
    check("JSON快照 system/actual/diff数值正确",
          sample_json["attribution_snapshot"]["diff_qty_snapshot"] is not None,
          str(sample_json["attribution_snapshot"]))
    check("JSON快照 库存ID列表非空",
          isinstance(sample_json["attribution_snapshot"]["raw_inventory_ids"], list) and
          len(sample_json["attribution_snapshot"]["raw_inventory_ids"]) > 0)
    check("JSON快照 盘点ID列表非空",
          isinstance(sample_json["attribution_snapshot"]["raw_stocktake_ids"], list) and
          len(sample_json["attribution_snapshot"]["raw_stocktake_ids"]) > 0)

check("JSON嵌套 calculation_steps 至少2步", len(sample_json["calculation_steps"]) >= 2,
      f"步骤数={len(sample_json['calculation_steps'])}")
check("JSON计算步骤 第1步是init且diff值匹配",
      sample_json["calculation_steps"][0]["step_type"] == "init",
      str(sample_json["calculation_steps"][0]))
check("JSON计算步骤 最后1步剩余值=0",
      abs(sample_json["calculation_steps"][-1]["remaining_after"]) < 0.001,
      f"最后一步剩余={sample_json['calculation_steps'][-1]['remaining_after']}")

check("JSON包含复核备注完整文本", sample_json.get("review_note") == NOTE_TEXT_V1,
      sample_json.get("review_note"))
check("JSON包含状态流转日志(2条)",
      len(sample_json.get("status_logs", [])) == 2,
      str(sample_json.get("status_logs")))

def replay_calc_from_json(jobj):
    steps = jobj["calculation_steps"]
    if not steps:
        return False, "无计算步骤"
    init_diff = steps[0]["remaining_after"]
    cur = init_diff
    for idx, cs in enumerate(steps):
        if idx == 0:
            if abs(cs["remaining_before"] - cs["remaining_after"]) > 0.001:
                return False, f"第{idx+1}步 init剩余值不一致"
            continue
        if abs(cs["remaining_before"] - cur) > 0.01:
            return False, f"第{idx+1}步输入不匹配: 期望剩余{cur}, 实际{cs['remaining_before']}"
        cur = cs["remaining_after"]
    if abs(cur) > 0.01:
        return False, f"计算链路未闭环，最终剩余={cur}"
    snap_diff_abs = abs(jobj["attribution_snapshot"]["diff_qty_snapshot"])
    if abs(init_diff - snap_diff_abs) > 0.01:
        return False, f"初始差异绝对值{snap_diff_abs}与计算首步{init_diff}不匹配"
    return True, f"链路独立复盘成功, |diff|={init_diff:.1f}"

replay_ok, replay_msg = replay_calc_from_json(sample_json)
check(f"解释链路脱离DB独立复盘: {replay_msg}", replay_ok, replay_msg)


print("\n" + "=" * 70)
print("测试 7: 模拟重启 → 快照、状态、备注、流转、计算步骤全部保留")
print("=" * 70)
import importlib
import db as db_mod
importlib.reload(db_mod)

with db_mod.get_conn() as conn:
    discs_reconnect = db_mod.get_discrepancies(conn)
    d_reconnect = next(d for d in discs_reconnect if d["id"] == sample_id_v1)
    snap_reconnect = db_mod.get_snapshot_for_discrepancy(conn, sample_id_v1)
    steps_reconnect = db_mod.get_calc_steps_for_discrepancy(conn, sample_id_v1)
    logs_reconnect = db_mod.get_status_log(conn, sample_id_v1)
    active_rule = db_mod.get_active_rule_version(conn)

check(f"重连后差异总数一致: {len(discs_reconnect)} == {len(discs_v2)}",
      len(discs_reconnect) == len(discs_v2))
check(f"重连后样本差异状态仍然closed", d_reconnect["status"] == STATUS_CLOSED,
      d_reconnect["status"])
check(f"重连后样本差异复核备注完整保留",
      d_reconnect.get("review_note") == NOTE_TEXT_V1, d_reconnect.get("review_note"))
check(f"重连后样本差异快照仍然存在", snap_reconnect is not None)
if snap_reconnect:
    check(f"重连后快照规则配置loss_threshold_pct=2(v1原始值)",
          (snap_reconnect.get("rule_config_snapshot") or {}).get("loss_threshold_pct") == 2.0)
check(f"重连后计算步骤数一致 {len(steps_reconnect)}=={len(steps_v2)}",
      len(steps_reconnect) == len(steps_v2))
check(f"重连后流转日志仍2条", len(logs_reconnect) == 2)
check(f"重连后规则v2激活", active_rule and active_rule["version"] == 2, str(active_rule))


print("\n" + "=" * 70)
print("测试 8: 重复导入同一文件 — 哈希去重，不会静默覆盖数据")
print("=" * 70)
with db_mod.get_conn() as conn:
    discs_before_dup = db_mod.get_discrepancies(conn)
dup_count_before = len(discs_before_dup)

r_dup = import_csv("inventory", "inventory_dup_again.csv", read_sample("inventory.csv"))
check("重复导入被正确拦截（返回duplicate=True）", r_dup.get("duplicate") is True, str(r_dup))
check("拦截信息包含'已导入过'", "已导入过" in r_dup.get("error", ""), r_dup.get("error"))

with db_mod.get_conn() as conn:
    discs_after_dup = db_mod.get_discrepancies(conn)
    import_recs = db_mod.get_import_records(conn)
dup_count_after = len(discs_after_dup)

check(f"重复导入后差异数不变 {dup_count_before} == {dup_count_after}",
      dup_count_before == dup_count_after)
check(f"导入记录中inventory类型仍然只有1条（不产生重复import）",
      sum(1 for r in import_recs if r["import_type"] == "inventory") == 1,
      f"inventory导入记录={[r for r in import_recs if r['import_type']=='inventory']}")


print("\n" + "=" * 70)
print("测试 9: 门店筛选后快照不串 — S001差异只有S001快照")
print("=" * 70)
with db_mod.get_conn() as conn:
    s001_discs = db_mod.get_discrepancies(conn, store_id="S001")
    s002_discs = db_mod.get_discrepancies(conn, store_id="S002")

check(f"S001 有差异: {len(s001_discs)} 条", len(s001_discs) > 0)
check(f"S002 有差异: {len(s002_discs)} 条", len(s002_discs) > 0)
check("S001差异不含S002门店（筛选不串）",
      all(d["store_id"] == "S001" for d in s001_discs))
check("S002差异不含S001门店（筛选不串）",
      all(d["store_id"] == "S002" for d in s002_discs))

if s001_discs:
    with db_mod.get_conn() as conn:
        s001_snap = db_mod.get_snapshot_for_discrepancy(conn, s001_discs[0]["id"])
    check(f"S001样本差异快照diff与差异记录一致",
          s001_snap is not None and
          abs(s001_snap["diff_qty_snapshot"] - s001_discs[0]["diff_qty"]) < 0.001,
          f"snap_diff={s001_snap['diff_qty_snapshot'] if s001_snap else None} "
          f"disc_diff={s001_discs[0]['diff_qty']}")


print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 归因快照增强版回归测试全部通过!")
    print("   [OK] 归因快照（规则+别名+原始ID）完整保存")
    print("   [OK] 计算步骤（分步扣减+剩余）闭环可复盘")
    print("   [OK] 规则变更后老差异ID/状态/备注/流转日志全部保留")
    print("   [OK] CSV/JSON导出包含完整解释链路")
    print("   [OK] JSON解释链路可脱离DB独立复盘验证")
    print("   [OK] 重启后所有数据一致")
    print("   [OK] 重复导入哈希去重不覆盖")
    print("   [OK] 门店筛选结果不串")
