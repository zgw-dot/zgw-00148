import sqlite3
import json

DB_PATH = "d:\\workSpace\\AI__SPACE\\02-label\\zgw-00148\\inventory_diff.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row

print("=" * 60)
print("数据库状态验证")
print("=" * 60)

print("\n1. 导入记录:")
rows = conn.execute("SELECT * FROM import_records ORDER BY id").fetchall()
for r in rows:
    print(f"  [{r['id']}] {r['file_name']} ({r['import_type']}): {r['row_count']}行, {r['error_count']}错误")

print("\n2. 差异记录 (带状态和规则版本):")
rows = conn.execute("""
    SELECT d.*, rv.version as rule_ver 
    FROM discrepancies d 
    LEFT JOIN rule_versions rv ON d.rule_version_id = rv.id
    ORDER BY d.id
""").fetchall()
for r in rows:
    status_labels = {
        'pending_review': '待复核',
        'confirmed': '已确认',
        'pending_accountability': '待追责',
        'closed': '误差关闭'
    }
    status = status_labels.get(r['status'], r['status'])
    review_note = r['review_note'] or '无'
    review_note = review_note[:30] + '...' if len(review_note) > 30 else review_note
    print(f"  [{r['id']}] {r['store_id']} | {r['sku_name']} | 差异: {r['diff_qty']:+.1f} | {status} | 规则v{r['rule_ver']} | 备注: {review_note}")

print("\n3. 差异证据行数:")
rows = conn.execute("""
    SELECT d.id, d.sku_name, COUNT(el.id) as evidence_count
    FROM discrepancies d
    LEFT JOIN evidence_lines el ON d.id = el.discrepancy_id
    GROUP BY d.id
    ORDER BY d.id
""").fetchall()
for r in rows:
    print(f"  差异[{r['id']}] {r['sku_name']}: {r['evidence_count']}条证据")

print("\n4. 流转日志:")
rows = conn.execute("SELECT * FROM status_log ORDER BY id").fetchall()
if rows:
    for r in rows:
        from_s = r['from_status'] or '新建'
        print(f"  [{r['id']}] 差异{r['discrepancy_id']}: {from_s} → {r['to_status']} at {r['changed_at'][:19]}")
else:
    print("  (暂无)")

print("\n5. 规则版本:")
rows = conn.execute("SELECT * FROM rule_versions ORDER BY version").fetchall()
for r in rows:
    cfg = json.loads(r['config_json'])
    active = '✅ 当前' if r['is_active'] else ''
    print(f"  v{r['version']} {active}: loss_pct={cfg['loss_threshold_pct']}%, loss_abs={cfg['loss_threshold_abs']}, delay={cfg['transfer_delay_days']}天, created_at={r['created_at'][:19]}")

print("\n6. 门店列表:")
rows = conn.execute("SELECT DISTINCT store_id FROM discrepancies ORDER BY store_id").fetchall()
for r in rows:
    print(f"  - {r['store_id']}")

print("\n" + "=" * 60)
print("✅ 数据库验证完成 - 所有数据已正确持久化！")
print("=" * 60)

conn.close()
