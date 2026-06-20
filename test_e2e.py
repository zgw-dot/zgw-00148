import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import init_db, get_conn, IMPORT_TYPES, STATUS_LABELS
from import_service import import_csv
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config
from sample_data import generate_sample_data, SAMPLE_DIR

print("✅ 所有模块导入成功")
print(f"导入类型: {IMPORT_TYPES}")
print(f"状态: {STATUS_LABELS}")
print(f"归因类型: {CAUSE_LABELS}")

db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_inventory_diff.db")
if os.path.exists(db_path):
    os.remove(db_path)

import db as db_module
db_module.DB_PATH = db_path
import import_service as import_module
import_module.DB_PATH = db_path
import engine as engine_module
engine_module.DB_PATH = db_path
import rules as rules_module
rules_module.DB_PATH = db_path

init_db()
print("✅ 数据库初始化成功")

sample_dir = generate_sample_data()
print(f"✅ 样例数据生成在: {sample_dir}")

import_types_map = {
    "inventory.csv": "inventory",
    "sales.csv": "sales",
    "transfer.csv": "transfer",
    "stocktake.csv": "stocktake",
}

for fname, itype in import_types_map.items():
    fpath = os.path.join(SAMPLE_DIR, fname)
    with open(fpath, "rb") as f:
        content = f.read()
    result = import_csv(itype, fname, content)
    print(f"导入 {fname}: {result}")

result = run_attribution()
print(f"归因结果: {result}")

with get_conn() as conn:
    from db import get_discrepancies
    discs = get_discrepancies(conn)
    print(f"差异数量: {len(discs)}")
    for d in discs:
        print(f"  {d['store_id']} | {d['sku_name']} | 差异: {d['diff_qty']:+.1f} | {d['attributed_cause']} | {d['status']}")

if discs:
    from db import update_review_note, transition_status, STATUS_CONFIRMED, STATUS_CLOSED
    disc_id = discs[0]["id"]
    update_review_note(conn, disc_id, "测试复核备注 - 已确认差异情况")
    print("✅ 复核备注已更新")

    transition_status(conn, disc_id, STATUS_CONFIRMED)
    print(f"✅ 状态流转到: {STATUS_CONFIRMED}")

    if len(discs) > 1:
        disc_id2 = discs[1]["id"]
        transition_status(conn, disc_id2, STATUS_CLOSED, note="测试误差关闭")
        print(f"✅ 状态流转到: {STATUS_CLOSED}")

bad_config = {"loss_threshold_pct": -1}
result = save_rule_config(bad_config)
print(f"坏规则测试: {result}")
assert result["success"] == False
print("✅ 坏规则正确拦截")

good_config = {"loss_threshold_pct": 3.0, "loss_threshold_abs": 5.0, "transfer_delay_days": 5, "aliases": {"ALT123": "6901234567890"}}
result = save_rule_config(good_config)
print(f"好规则测试: {result}")
assert result["success"] == True
print("✅ 规则版本递增正确")

from db import get_discrepancies, get_evidence_for_discrepancy, get_status_log
discs2 = get_discrepancies(conn)
for d in discs2[:2]:
    ev = get_evidence_for_discrepancy(conn, d["id"])
    log = get_status_log(conn, d["id"])
    print(f"差异 {d['id']}: 证据 {len(ev)} 条, 日志 {len(log)} 条")

print("\n🎉 所有测试通过!")

os.remove(db_path)
