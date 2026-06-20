"""
别名映射不可变回归测试 — 复现并修复“改别名后同一批旧数据重生差异”的问题
核心断言:
  1. 导入时，原始条码对应的规范条码(canonical_barcode)应该永久固化
  2. 规则变更（别名映射修改）后，重新归因，已有原始数据不应该产生新差异
  3. 旧差异的规范条码、证据、状态、备注、流转日志全部保留不变
  4. 只有**新导入**的数据才会使用新的别名映射解析
"""
import os
import sys
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "inventory_diff.db")

from test_utils import init_test_env
init_test_env(DB_PATH)

from db import (
    init_db, get_conn, get_discrepancies, get_evidence_for_discrepancy,
    get_status_log, transition_status, update_review_note,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_CLOSED,
    get_snapshot_for_discrepancy, get_calc_steps_for_discrepancy,
)
from import_service import import_csv
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config

init_db()

errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


def make_csv(content_str):
    return content_str.encode("utf-8-sig")


print("=" * 70)
print("Phase 1: 初始化规则 v1 — 别名映射 ALT_001 → CANON_001")
print("=" * 70)
cfg_v1 = {
    "loss_threshold_pct": 2.0,
    "loss_threshold_abs": 3.0,
    "transfer_delay_days": 3,
    "aliases": {"ALT_001": "CANON_001"},
}
r_save = save_rule_config(cfg_v1)
check("v1规则保存成功", r_save["success"], str(r_save))
check("v1规则版本=1", r_save.get("version") == 1, str(r_save))


print("\n" + "=" * 70)
print("Phase 2: 导入库存和盘点（含原始条码 ALT_001）")
print("=" * 70)
inv_csv = make_csv("""store_id,barcode,sku_name,system_qty
S001,ALT_001,测试商品A,100
S001,CANON_002,测试商品B,50
""")
stk_csv = make_csv("""store_id,barcode,sku_name,actual_qty
S001,ALT_001,测试商品A,90
S001,CANON_002,测试商品B,45
""")

r_inv = import_csv("inventory", "inv.csv", inv_csv)
check("库存导入成功", r_inv["success"], str(r_inv))
r_stk = import_csv("stocktake", "stk.csv", stk_csv)
check("盘点导入成功", r_stk["success"], str(r_stk))


print("\n" + "=" * 70)
print("Phase 3: 首次归因 v1 — ALT_001 应按 v1 别名映射解析为 CANON_001")
print("=" * 70)
r1 = run_attribution()
check("v1归因成功", r1["success"], str(r1))
check("v1新增差异=2", r1.get("created") == 2, f"created={r1.get('created')}")

with get_conn() as conn:
    discs_v1 = get_discrepancies(conn)
print(f"  [INFO] v1差异列表: {[(d['id'], d['store_id'], d['barcode'], d['diff_qty']) for d in discs_v1]}")

check("v1差异数=2", len(discs_v1) == 2, f"实际={len(discs_v1)}")

d1_alt = next((d for d in discs_v1 if d["barcode"] == "CANON_001"), None)
check("ALT_001 解析后生成 CANON_001 差异", d1_alt is not None,
      f"差异条码列表={[d['barcode'] for d in discs_v1]}")

if d1_alt:
    sample_id = d1_alt["id"]
    check("CANON_001 差异正确 diff=10",
          abs(d1_alt["diff_qty"] - 10) < 0.001, f"diff={d1_alt['diff_qty']}")
    check("CANON_001 差异规则版本=1",
          d1_alt.get("rule_ver") == 1, f"rule_ver={d1_alt.get('rule_ver')}")


print("\n" + "=" * 70)
print("Phase 4: 给 CANON_001 差异加备注 + 状态流转（模拟人工复核）")
print("=" * 70)
NOTE_V1 = "v1已复核: ALT_001→CANON_001，差异10属于正常损耗"
with get_conn() as conn:
    update_review_note(conn, sample_id, NOTE_V1)
    transition_status(conn, sample_id, STATUS_CONFIRMED, note="v1复核确认")
    transition_status(conn, sample_id, STATUS_CLOSED, note="确认关闭")

with get_conn() as conn:
    d_after = next(d for d in get_discrepancies(conn) if d["id"] == sample_id)
    logs_after = get_status_log(conn, sample_id)

check("备注保存成功", d_after.get("review_note") == NOTE_V1)
check("状态=closed", d_after["status"] == STATUS_CLOSED)
check("2条流转日志", len(logs_after) == 2)

v1_diff_count = len(discs_v1)
v1_diff_ids = {d["id"] for d in discs_v1}
v1_diff_barcodes = {d["barcode"] for d in discs_v1}
v1_canon_001_id = sample_id
print(f"  [INFO] v1差异ID: {sorted(v1_diff_ids)}")
print(f"  [INFO] v1差异条码: {sorted(v1_diff_barcodes)}")


print("\n" + "=" * 70)
print("Phase 5: 规则变更 v2 — 别名映射改为 ALT_001 → CANON_999（bug触发点）")
print("=" * 70)
cfg_v2 = {
    "loss_threshold_pct": 2.0,
    "loss_threshold_abs": 3.0,
    "transfer_delay_days": 3,
    "aliases": {"ALT_001": "CANON_999"},  # ← 别名映射变了
}
r_save2 = save_rule_config(cfg_v2)
check("v2规则保存成功", r_save2["success"], str(r_save2))
check("v2规则版本=2", r_save2.get("version") == 2, str(r_save2))


print("\n" + "=" * 70)
print("Phase 6: 核心断言 — 同一批旧数据不应重生新差异")
print("=" * 70)

with get_conn() as conn:
    discs_v2 = get_discrepancies(conn)
v2_diff_count = len(discs_v2)
v2_diff_ids = {d["id"] for d in discs_v2}
v2_diff_barcodes = {d["barcode"] for d in discs_v2}
print(f"  [INFO] v2差异ID: {sorted(v2_diff_ids)}")
print(f"  [INFO] v2差异条码: {sorted(v2_diff_barcodes)}")

check("差异总数仍为2（不应因别名变更产生新差异）",
      v2_diff_count == v1_diff_count,
      f"v1={v1_diff_count}, v2={v2_diff_count}, 新增了={v2_diff_count - v1_diff_count}条")

check("差异ID集合不变（不应生成新差异ID）",
      v2_diff_ids == v1_diff_ids,
      f"v1={v1_diff_ids}, v2={v2_diff_ids}")

check("差异条码集合不变（不应出现新条码 CANON_999）",
      v2_diff_barcodes == v1_diff_barcodes,
      f"v1={v2_diff_barcodes - v1_diff_barcodes}, 新增条码={v2_diff_barcodes - v1_diff_barcodes}")

check("不应出现 CANON_999 差异（旧数据不会按新别名重生）",
      "CANON_999" not in v2_diff_barcodes,
      f"出现了不该有的 CANON_999 差异: {[d for d in discs_v2 if d['barcode'] == 'CANON_999']}")

check("CANON_001 差异仍然存在（旧差异不消失）",
      "CANON_001" in v2_diff_barcodes,
      f"CANON_001 消失了！")

with get_conn() as conn:
    d_canon_001 = next(d for d in get_discrepancies(conn) if d["id"] == v1_canon_001_id)
    snap = get_snapshot_for_discrepancy(conn, v1_canon_001_id)
    steps = get_calc_steps_for_discrepancy(conn, v1_canon_001_id)
    logs = get_status_log(conn, v1_canon_001_id)

check("CANON_001 差异规则版本仍为1（不被覆盖）",
      d_canon_001.get("rule_ver") == 1, f"rule_ver={d_canon_001.get('rule_ver')}")
check("CANON_001 差异 diff 仍为10（不被重算）",
      abs(d_canon_001["diff_qty"] - 10) < 0.001, f"diff={d_canon_001['diff_qty']}")
check("CANON_001 差异状态仍为closed",
      d_canon_001["status"] == STATUS_CLOSED, d_canon_001["status"])
check("CANON_001 差异备注仍完整保留",
      d_canon_001.get("review_note") == NOTE_V1, d_canon_001.get("review_note"))
check("CANON_001 流转日志仍2条",
      len(logs) == 2, f"日志数={len(logs)}")


print("\n" + "=" * 70)
print("Phase 7: 用新别名导入一批新数据 — 新数据应按新别名解析")
print("=" * 70)
inv_csv2 = make_csv("""store_id,barcode,sku_name,system_qty
S002,ALT_001,测试商品A,200
S002,CANON_002,测试商品B,80
""")
stk_csv2 = make_csv("""store_id,barcode,sku_name,actual_qty
S002,ALT_001,测试商品A,170
S002,CANON_002,测试商品B,75
""")
r_inv2 = import_csv("inventory", "inv2.csv", inv_csv2)
check("新库存导入成功", r_inv2["success"], str(r_inv2))
r_stk2 = import_csv("stocktake", "stk2.csv", stk_csv2)
check("新盘点导入成功", r_stk2["success"], str(r_stk2))

r_attr2 = run_attribution()
check("新数据归因成功", r_attr2["success"], str(r_attr2))
check("新数据新增差异=2（旧数据被跳过）",
      r_attr2.get("created") == 2, f"created={r_attr2.get('created')}, skipped={r_attr2.get('skipped')}")

with get_conn() as conn:
    discs_v3 = get_discrepancies(conn)
v3_diff_barcodes = {(d["store_id"], d["barcode"]): d["diff_qty"] for d in discs_v3}
print(f"  [INFO] 最终差异: {v3_diff_barcodes}")

check("S001的CANON_001仍存在（旧数据）",
      ("S001", "CANON_001") in v3_diff_barcodes)
check("S002的ALT_001按v2别名解析为CANON_999（新数据）",
      ("S002", "CANON_999") in v3_diff_barcodes,
      f"期望 (S002, CANON_999), 实际S002条码={[(d['store_id'], d['barcode']) for d in discs_v3 if d['store_id']=='S002']}")

d_s002_999 = next(d for d in discs_v3 if d["store_id"] == "S002" and d["barcode"] == "CANON_999")
check("S002 CANON_999 diff=30（正确）",
      abs(d_s002_999["diff_qty"] - 30) < 0.001, f"diff={d_s002_999['diff_qty']}")
check("S002 CANON_999 规则版本=2（新数据用新规则）",
      d_s002_999.get("rule_ver") == 2, f"rule_ver={d_s002_999.get('rule_ver')}")


print("\n" + "=" * 70)
print("Phase 8: CSV/JSON导出检查 — 旧数据不串口径")
print("=" * 70)
with get_conn() as conn:
    export_discs = get_discrepancies(conn)
    for d in export_discs:
        d["snapshot"] = get_snapshot_for_discrepancy(conn, d["id"])
        d["calc_steps"] = get_calc_steps_for_discrepancy(conn, d["id"])

d_exp_old = next(d for d in export_discs if d["id"] == v1_canon_001_id)
d_exp_new = next(d for d in export_discs if d["id"] == d_s002_999["id"])

if d_exp_old.get("snapshot"):
    cfg_old = d_exp_old["snapshot"].get("rule_config_snapshot", {}) or {}
    check("旧差异导出快照别名映射为 ALT_001→CANON_001（不串v2）",
          cfg_old.get("aliases", {}).get("ALT_001") == "CANON_001",
          f"aliases={cfg_old.get('aliases', {})}")
    check("旧差异导出快照规则版本ID指向v1",
          d_exp_old["snapshot"]["rule_version_id"] == 1,
          f"rule_version_id={d_exp_old['snapshot']['rule_version_id']}")

if d_exp_new.get("snapshot"):
    cfg_new = d_exp_new["snapshot"].get("rule_config_snapshot", {}) or {}
    check("新差异导出快照别名映射为 ALT_001→CANON_999（正确）",
          cfg_new.get("aliases", {}).get("ALT_001") == "CANON_999",
          f"aliases={cfg_new.get('aliases', {})}")
    check("新差异导出快照规则版本ID指向v2",
          d_exp_new["snapshot"]["rule_version_id"] == 2,
          f"rule_version_id={d_exp_new['snapshot']['rule_version_id']}")


print("\n" + "=" * 70)
print("Phase 9: 模拟重启 — 数据一致性保持")
print("=" * 70)
import importlib
import db as db_mod
importlib.reload(db_mod)

with db_mod.get_conn() as conn:
    discs_rc = db_mod.get_discrepancies(conn)
    d_old_rc = next(d for d in discs_rc if d["id"] == v1_canon_001_id)
    d_new_rc = next(d for d in discs_rc if d["id"] == d_s002_999["id"])

check(f"重连后差异总数={len(discs_rc)} == 4", len(discs_rc) == 4)
check("重连后旧差异条码还是CANON_001", d_old_rc["barcode"] == "CANON_001")
check("重连后旧差异规则版本还是v1", d_old_rc.get("rule_ver") == 1)
check("重连后旧差异状态还是closed", d_old_rc["status"] == STATUS_CLOSED)
check("重连后新差异条码是CANON_999", d_new_rc["barcode"] == "CANON_999")
check("重连后新差异规则版本是v2", d_new_rc.get("rule_ver") == 2)


# ── 汇总 ──
print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 别名映射不可变回归测试全部通过!")
    print("   [OK] 导入时canonical_barcode永久固化")
    print("   [OK] 别名变更后旧数据不重生新差异")
    print("   [OK] 旧差异快照/证据/状态/备注/流转日志不变")
    print("   [OK] 新导入数据按新别名解析")
    print("   [OK] CSV/JSON导出不串口径")
    print("   [OK] 重启后一致性保持")
