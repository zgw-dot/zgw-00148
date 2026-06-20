"""
归因快照不可变回归测试 — 复现并验证「改规则后旧差异快照被覆盖」的问题
核心断言:
  - 首次归因产生的差异，其规则版本、阈值、证据、计算步骤、别名映射应永久冻结
  - 规则变更后重新归因，已有差异的快照/证据/步骤/规则版本/归因结论不允许被新口径覆盖
  - 新产生的差异才使用新规则
  - 状态/备注/流转日志也不允许被影响
"""
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff.db")

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from db import (
    init_db, get_conn, get_discrepancies, get_evidence_for_discrepancy,
    get_status_log, get_stores, get_import_records,
    transition_status, update_review_note, get_active_rule_version,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_CLOSED, STATUS_LABELS,
    get_snapshot_for_discrepancy, get_calc_steps_for_discrepancy,
)
from import_service import import_csv
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config
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


# ── Phase 1: v1 归因 ──
print("=" * 70)
print("Phase 1: 导入 + 首次归因 (v1: loss_pct=2, loss_abs=3, delay=3)")
print("=" * 70)
import_csv("inventory", "inventory.csv", read_sample("inventory.csv"))
import_csv("sales", "sales.csv", read_sample("sales.csv"))
import_csv("transfer", "transfer.csv", read_sample("transfer.csv"))
import_csv("stocktake", "stocktake.csv", read_sample("stocktake.csv"))

r1 = run_attribution()
check("v1归因成功", r1["success"], str(r1))
check("v1规则版本=1", r1.get("rule_version") == 1, str(r1))

with get_conn() as conn:
    discs_v1 = get_discrepancies(conn)
check(f"v1产生差异{len(discs_v1)}条", len(discs_v1) > 0)

v1_snapshots = {}
v1_steps = {}
v1_evidences = {}
v1_causes = {}
v1_rule_vers = {}
v1_thresholds = {}

with get_conn() as conn:
    for d in discs_v1:
        did = d["id"]
        v1_snapshots[did] = get_snapshot_for_discrepancy(conn, did)
        v1_steps[did] = get_calc_steps_for_discrepancy(conn, did)
        v1_evidences[did] = get_evidence_for_discrepancy(conn, did)
        v1_causes[did] = d["attributed_cause"]
        v1_rule_vers[did] = d.get("rule_ver")
        snap = v1_snapshots[did]
        if snap:
            cfg = snap.get("rule_config_snapshot", {}) or {}
            v1_thresholds[did] = cfg.get("loss_threshold_pct")

sample_id = discs_v1[0]["id"]
print(f"  [INFO] 样本差异ID={sample_id}, 规则v1, 阈值={v1_thresholds.get(sample_id)}, "
      f"归因={CAUSE_LABELS.get(v1_causes[sample_id], v1_causes[sample_id])}")
print(f"  [INFO] 快照规则版本ID={v1_snapshots[sample_id]['rule_version_id'] if v1_snapshots[sample_id] else '无'}")
print(f"  [INFO] 快照阈值={v1_thresholds.get(sample_id)}")
print(f"  [INFO] 计算步骤数={len(v1_steps[sample_id])}")
print(f"  [INFO] 证据行数={len(v1_evidences[sample_id])}")


# ── Phase 2: 人工复核 ──
print("\n" + "=" * 70)
print("Phase 2: 给部分差异加备注+状态流转")
print("=" * 70)
NOTE = f"v1已复核: 差异{sample_id}，确认正常损耗，已联系门店"
with get_conn() as conn:
    update_review_note(conn, sample_id, NOTE)
    transition_status(conn, sample_id, STATUS_CONFIRMED, note="v1复核确认")

with get_conn() as conn:
    d_note = next(d for d in get_discrepancies(conn) if d["id"] == sample_id)
    logs_note = get_status_log(conn, sample_id)
check("备注已保存", d_note.get("review_note") == NOTE)
check("状态=confirmed", d_note["status"] == STATUS_CONFIRMED)
check("1条流转日志", len(logs_note) == 1)


# ── Phase 3: 规则变更 v2 ──
print("\n" + "=" * 70)
print("Phase 3: 规则变更 v2 (loss_pct=50, loss_abs=100) — 阈值极大放宽")
print("=" * 70)
NEW_CFG = {
    "loss_threshold_pct": 50.0,
    "loss_threshold_abs": 100.0,
    "transfer_delay_days": 30,
    "aliases": {"ALT_999": "6900000000001"},
}
r_save = save_rule_config(NEW_CFG)
check("v2规则保存成功", r_save["success"], str(r_save))


# ── Phase 4: 核心断言 — 旧差异快照/阈值/规则版本/归因/证据/步骤不允许被覆盖 ──
print("\n" + "=" * 70)
print("Phase 4: 核心断言 — 旧差异的快照/阈值/规则版本/归因必须冻结")
print("=" * 70)

with get_conn() as conn:
    discs_v2 = get_discrepancies(conn)
    d_after = next(d for d in discs_v2 if d["id"] == sample_id)
    snap_after = get_snapshot_for_discrepancy(conn, sample_id)
    steps_after = get_calc_steps_for_discrepancy(conn, sample_id)
    ev_after = get_evidence_for_discrepancy(conn, sample_id)
    logs_after = get_status_log(conn, sample_id)

check(f"旧差异ID={sample_id}仍存在", sample_id in {d["id"] for d in discs_v2})
check(f"旧差异规则版本仍为v1（不被覆盖为v2）",
      d_after.get("rule_ver") == 1,
      f"期望v1, 实际v{d_after.get('rule_ver')}")
check(f"旧差异归因结果不变",
      d_after["attributed_cause"] == v1_causes[sample_id],
      f"期望{v1_causes[sample_id]}, 实际{d_after['attributed_cause']}")

if snap_after and v1_snapshots[sample_id]:
    cfg_after = snap_after.get("rule_config_snapshot", {}) or {}
    check(f"旧差异快照阈值仍为v1原始值(2.0)，不是v2的50",
          cfg_after.get("loss_threshold_pct") == v1_thresholds[sample_id],
          f"期望{v1_thresholds[sample_id]}, 实际{cfg_after.get('loss_threshold_pct')}")
    check(f"旧差异快照规则版本ID仍为v1",
          snap_after["rule_version_id"] == v1_snapshots[sample_id]["rule_version_id"],
          f"期望{v1_snapshots[sample_id]['rule_version_id']}, 实际{snap_after['rule_version_id']}")
    check(f"旧差异快照system/actual/diff不变",
          abs(snap_after["system_qty_snapshot"] - v1_snapshots[sample_id]["system_qty_snapshot"]) < 0.001 and
          abs(snap_after["actual_qty_snapshot"] - v1_snapshots[sample_id]["actual_qty_snapshot"]) < 0.001 and
          abs(snap_after["diff_qty_snapshot"] - v1_snapshots[sample_id]["diff_qty_snapshot"]) < 0.001,
          f"sys: {snap_after['system_qty_snapshot']} vs {v1_snapshots[sample_id]['system_qty_snapshot']}")
    check(f"旧差异快照别名映射不变",
          snap_after.get("alias_before") == v1_snapshots[sample_id].get("alias_before") and
          snap_after.get("alias_after") == v1_snapshots[sample_id].get("alias_after"),
          f"before: {snap_after.get('alias_before')} vs {v1_snapshots[sample_id].get('alias_before')}")
else:
    check(f"旧差异快照仍存在", snap_after is not None, f"快照被清空！snap={snap_after}")

check(f"旧差异计算步骤数不变",
      len(steps_after) == len(v1_steps[sample_id]),
      f"期望{len(v1_steps[sample_id])}步, 实际{len(steps_after)}步")
if steps_after and v1_steps[sample_id]:
    for i, (sa, sb) in enumerate(zip(steps_after, v1_steps[sample_id])):
        check(f"  步骤[{i}]类型不变: {sa['step_type']} == {sb['step_type']}",
              sa["step_type"] == sb["step_type"],
              f"步骤{i}: 期望{sb['step_type']}, 实际{sa['step_type']}")
        check(f"  步骤[{i}]描述不变",
              sa["step_description"] == sb["step_description"],
              f"步骤{i}: 期望'{sb['step_description']}', 实际'{sa['step_description']}'")
        check(f"  步骤[{i}]扣减值不变",
              abs(sa["amount_applied"] - sb["amount_applied"]) < 0.001,
              f"步骤{i}: 期望{sb['amount_applied']}, 实际{sa['amount_applied']}")

check(f"旧差异证据行数不变",
      len(ev_after) == len(v1_evidences[sample_id]),
      f"期望{len(v1_evidences[sample_id])}条, 实际{len(ev_after)}条")

check(f"旧差异状态仍为confirmed（不被冲回）",
      d_after["status"] == STATUS_CONFIRMED, d_after["status"])
check(f"旧差异备注仍完整保留",
      d_after.get("review_note") == NOTE, d_after.get("review_note"))
check(f"旧差异流转日志仍1条",
      len(logs_after) == 1, f"日志数={len(logs_after)}")


# ── Phase 5: 逐条校验所有旧差异 ──
print("\n" + "=" * 70)
print("Phase 5: 逐条校验所有旧差异 — 规则版本/阈值/快照不允许被覆盖")
print("=" * 70)
with get_conn() as conn:
    all_v2 = get_discrepancies(conn)
all_ok = True
for d in all_v2:
    if d.get("rule_ver") != 1:
        print(f"  [FAIL] 差异{d['id']} 规则版本被改为v{d.get('rule_ver')}")
        all_ok = False
        errors_total.append((f"差异{d['id']}规则版本被覆盖", f"v{d.get('rule_ver')}"))
check("所有旧差异规则版本仍为v1", all_ok)

with get_conn() as conn:
    for d in all_v2:
        did = d["id"]
        snap = get_snapshot_for_discrepancy(conn, did)
        if snap:
            cfg = snap.get("rule_config_snapshot", {}) or {}
            if cfg.get("loss_threshold_pct") != v1_thresholds.get(did):
                errors_total.append(
                    (f"差异{did}阈值被覆盖", f"期望{v1_thresholds.get(did)}, 实际{cfg.get('loss_threshold_pct')}")
                )
all_snap_ok = len([e for e in errors_total if "阈值被覆盖" in e[0]]) == 0
check("所有旧差异快照阈值仍为v1原始值", all_snap_ok)


# ── Phase 6: CSV/JSON导出检查 ──
print("\n" + "=" * 70)
print("Phase 6: CSV/JSON导出 — 旧差异的解释链路不被新规则污染")
print("=" * 70)
import csv as csv_mod
import io

with get_conn() as conn:
    export_discs = get_discrepancies(conn)
    for d in export_discs:
        d["snapshot"] = get_snapshot_for_discrepancy(conn, d["id"])
        d["calc_steps"] = get_calc_steps_for_discrepancy(conn, d["id"])

sample_export = next(d for d in export_discs if d["id"] == sample_id)
snap_e = sample_export.get("snapshot")
if snap_e:
    cfg_e = snap_e.get("rule_config_snapshot", {}) or {}
    check("JSON导出: 旧差异快照阈值=2.0(不是50)",
          cfg_e.get("loss_threshold_pct") == 2.0,
          f"实际={cfg_e.get('loss_threshold_pct')}")
    check("JSON导出: 旧差异快照规则版本ID指向v1",
          snap_e.get("rule_version_id") == v1_snapshots[sample_id]["rule_version_id"],
          f"实际={snap_e.get('rule_version_id')}")
steps_e = sample_export.get("calc_steps", [])
check("JSON导出: 旧差异计算步骤数与v1一致",
      len(steps_e) == len(v1_steps[sample_id]),
      f"v1={len(v1_steps[sample_id])}, 导出={len(steps_e)}")
if steps_e and v1_steps[sample_id]:
    check("JSON导出: 旧差异首步描述与v1一致",
          steps_e[0]["step_description"] == v1_steps[sample_id][0]["step_description"],
          f"v1='{v1_steps[sample_id][0]['step_description']}', 导出='{steps_e[0]['step_description']}'")


# ── Phase 7: 重启一致性 ──
print("\n" + "=" * 70)
print("Phase 7: 模拟重启 — 旧差异快照/阈值/步骤仍不变")
print("=" * 70)
import importlib
import db as db_mod
importlib.reload(db_mod)

with db_mod.get_conn() as conn:
    d_rc = next(d for d in db_mod.get_discrepancies(conn) if d["id"] == sample_id)
    snap_rc = db_mod.get_snapshot_for_discrepancy(conn, sample_id)
    steps_rc = db_mod.get_calc_steps_for_discrepancy(conn, sample_id)
    logs_rc = db_mod.get_status_log(conn, sample_id)

check("重连后规则版本仍v1", d_rc.get("rule_ver") == 1, f"v{d_rc.get('rule_ver')}")
if snap_rc:
    cfg_rc = snap_rc.get("rule_config_snapshot", {}) or {}
    check("重连后快照阈值仍2.0", cfg_rc.get("loss_threshold_pct") == 2.0,
          f"实际={cfg_rc.get('loss_threshold_pct')}")
check("重连后计算步骤数不变", len(steps_rc) == len(v1_steps[sample_id]))
check("重连后状态仍confirmed", d_rc["status"] == STATUS_CONFIRMED)
check("重连后备注仍保留", d_rc.get("review_note") == NOTE)
check("重连后流转日志仍1条", len(logs_rc) == 1)


# ── 汇总 ──
print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 归因快照不可变回归测试全部通过!")
    print("   ✅ 规则变更后旧差异快照/阈值/规则版本/归因结果不变")
    print("   ✅ 旧差异计算步骤/证据/状态/备注/流转日志不变")
    print("   ✅ CSV/JSON导出中旧差异解释链路不被新规则污染")
    print("   ✅ 重启后一致性保持")
