import sqlite3
import json
from datetime import datetime

DB_PATH = "d:\\workSpace\\AI__SPACE\\02-label\\zgw-00148\\inventory_diff.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

now = datetime.now().isoformat()

print("补全测试数据...")

# 1. 更新可乐330ml的复核备注
update_sql = """
UPDATE discrepancies 
SET review_note = ?, reviewed_at = ?, updated_at = ?
WHERE id = 1
"""
note = "复核确认：可乐330ml正常损耗+7.0，已核实销售5和调拨记录，差异在阈值范围内，无需追责。"
conn.execute(update_sql, (note, now, now))
print("✅ 已更新差异1（可乐330ml）的复核备注")

# 2. 将差异1流转到"已确认"
conn.execute("""
    UPDATE discrepancies SET status = 'confirmed', updated_at = ? WHERE id = 1
""", (now,))
conn.execute("""
    INSERT INTO status_log (discrepancy_id, from_status, to_status, changed_at, changed_by, note)
    VALUES (?, ?, ?, ?, ?, ?)
""", (1, 'pending_review', 'confirmed', now, 'user', '复核完成，确认是正常损耗'))
print("✅ 已将差异1流转到'已确认'")

# 3. 保存一个新的规则版本v2
latest_ver = conn.execute("SELECT MAX(version) as mv FROM rule_versions").fetchone()["mv"]
next_ver = latest_ver + 1
new_config = {
    "loss_threshold_pct": 3.5,
    "loss_threshold_abs": 5.0,
    "transfer_delay_days": 5,
    "aliases": {"ALT123": "6901234567890"}
}
conn.execute("UPDATE rule_versions SET is_active = 0 WHERE is_active = 1")
conn.execute("""
    INSERT INTO rule_versions (version, config_json, created_at, is_active)
    VALUES (?, ?, ?, 1)
""", (next_ver, json.dumps(new_config, ensure_ascii=False), now))
print(f"✅ 已保存新规则版本 v{next_ver}")

# 4. 验证导出数据
discs = conn.execute("""
    SELECT d.*, rv.version as rule_ver
    FROM discrepancies d LEFT JOIN rule_versions rv ON d.rule_version_id = rv.id
    ORDER BY d.id
""").fetchall()
cols = [d[0] for d in conn.execute("SELECT * FROM discrepancies LIMIT 1").description]
print(f"\n可导出自段: {cols}")
print(f"\n差异数据统计:")
print(f"  总计: {len(discs)} 条")
print(f"  待复核: {len([d for d in discs if d['status'] == 'pending_review'])}")
print(f"  已确认: {len([d for d in discs if d['status'] == 'confirmed'])}")
print(f"  待追责: {len([d for d in discs if d['status'] == 'pending_accountability'])}")
print(f"  误差关闭: {len([d for d in discs if d['status'] == 'closed'])}")
print(f"  门店S001: {len([d for d in discs if d['store_id'] == 'S001'])} 条")
print(f"  门店S002: {len([d for d in discs if d['store_id'] == 'S002'])} 条")

conn.commit()
conn.close()
print("\n🎉 测试数据补全完成！")
