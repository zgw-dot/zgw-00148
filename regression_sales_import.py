"""
门店盘点差异复盘工具 — 销售导入链路收紧回归测试
覆盖:
  1. 根因定位：sku_name 空/全空格、barcode 空/全空格、sale_qty 空/全空格/非数值、sale_date 空
     — 全部必须在 validate_row 层被拦截
  2. 入库前防御校验：即使上游漏过，_defensive_check 也会拦
  3. 混合导入（空sku+缺数量+正常行）：
     - success/valid_rows/error_rows 精确
     - 行级报错带行号和字段名
     - DB 中只有正常行落库
  4. 坏行不进入差异归因、不进入 CSV/JSON 导出
  5. 正常门店（S001/S002）结果不受影响
"""
import os
import sys
import json
import sqlite3

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
)
from import_service import (
    import_csv, validate_row, compute_file_hash, REQUIRED_FIELDS, NUMERIC_FIELDS,
    _normalize_val, _defensive_check,
)
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config, get_version_history
from sample_data import generate_sample_data, SAMPLE_DIR

init_db()
generate_sample_data()

errors_total = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        errors_total.append((name, detail))


def read_sample(fname):
    with open(os.path.join(SAMPLE_DIR, fname), "rb") as f:
        return f.read()


# =============================================================
print("\n" + "=" * 70)
print("测试 0: GBK 终端输出兼容性（默认 Windows PowerShell 环境）")
print("=" * 70)
import io as _io

GBK_SAMPLE_STRINGS = [
    "[清理] 已清空旧数据库",
    "  [OK] 测试通过样例",
    "  [FAIL] 测试失败样例",
    "测试 X: 标题样例 — 中文说明",
    "第 3 行: 缺少必填字段 'sku_name'",
    "第 5 行: 字段 'sale_qty' 的值 'abc' 不是有效数值",
    "S003 无差异（坏行未入库，无法归因）",
    "复核备注 + 状态流转 + 流转日志（正常流程）",
    "坏行不进入 CSV/JSON 导出",
    "门店筛选 — S001/S002/S003 严格隔离",
    "库存导入成功: valid=10",
    "销售导入成功: valid=3",
    "差异总数>0",
    "[FAIL] 共 3 项失败:",
    "[全部通过] 全部销售导入链路收紧回归测试通过!",
    "store_id, barcode, sku_name, sale_qty, sale_date",
    "正常商品C, 空数量商品, 全空格数量, 正常商品D",
]
_gbk_ok = True
_gbk_errors = []
for _i, _s in enumerate(GBK_SAMPLE_STRINGS):
    try:
        _s.encode("gbk")
    except UnicodeEncodeError as _e:
        _gbk_ok = False
        _gbk_errors.append(f"字符串#{_i}: {_e}")
check("所有输出字符串都能用 GBK 编码（默认 Windows 终端兼容）", _gbk_ok,
      "; ".join(_gbk_errors) if _gbk_errors else "")

_gbk_buf = _io.BytesIO()
_gbk_out = _io.TextIOWrapper(_gbk_buf, encoding="gbk", errors="strict")
try:
    for _s in GBK_SAMPLE_STRINGS:
        _gbk_out.write(_s + "\n")
    _gbk_out.flush()
    _gbk_write_ok = True
    _gbk_write_err = ""
except UnicodeEncodeError as _e:
    _gbk_write_ok = False
    _gbk_write_err = str(_e)
check("模拟 GBK 终端流式写入不触发 UnicodeEncodeError", _gbk_write_ok, _gbk_write_err)

del _gbk_buf, _gbk_out, _io

# =============================================================
print("\n" + "=" * 70)
print("测试 1: REQUIRED_FIELDS 四类导入均包含 sku_name（根因修复）")
print("=" * 70)
for itype in ["inventory", "sales", "transfer", "stocktake"]:
    check(f"{itype} REQUIRED_FIELDS 含 sku_name",
          "sku_name" in REQUIRED_FIELDS.get(itype, []),
          str(REQUIRED_FIELDS.get(itype)))

# =============================================================
print("\n" + "=" * 70)
print("测试 2: validate_row 单元 — 所有空商品标识/数量组合被拦截")
print("=" * 70)
cases_pass = [
    ("销售-正常行", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "测试商品", "sale_qty": "5", "sale_date": "2025-06-20"}),
]
cases_block = [
    ("销售-sku_name空串", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "", "sale_qty": "5", "sale_date": "2025-06-20"},
     "sku_name"),
    ("销售-sku_name全空格", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "   ", "sale_qty": "5", "sale_date": "2025-06-20"},
     "sku_name"),
    ("销售-barcode空串", "sales",
     {"store_id": "S001", "barcode": "", "sku_name": "测试", "sale_qty": "5", "sale_date": "2025-06-20"},
     "barcode"),
    ("销售-barcode全空格", "sales",
     {"store_id": "S001", "barcode": "   ", "sku_name": "测试", "sale_qty": "5", "sale_date": "2025-06-20"},
     "barcode"),
    ("销售-sale_qty空串", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "测试", "sale_qty": "", "sale_date": "2025-06-20"},
     "sale_qty"),
    ("销售-sale_qty全空格", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "测试", "sale_qty": "   ", "sale_date": "2025-06-20"},
     "sale_qty"),
    ("销售-sale_qty非数值", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "测试", "sale_qty": "abc", "sale_date": "2025-06-20"},
     "sale_qty"),
    ("销售-sale_date空串", "sales",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "测试", "sale_qty": "5", "sale_date": ""},
     "sale_date"),
    ("库存-sku_name空", "inventory",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "", "system_qty": "10"},
     "sku_name"),
    ("库存-system_qty空", "inventory",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "测试", "system_qty": ""},
     "system_qty"),
    ("调拨-barcode空", "transfer",
     {"store_id_from": "S001", "store_id_to": "S002", "barcode": "", "sku_name": "X", "transfer_qty": "5", "transfer_date": "2025-06-20"},
     "barcode"),
    ("盘点-actual_qty非数值", "stocktake",
     {"store_id": "S001", "barcode": "6900000000001", "sku_name": "X", "actual_qty": "x"},
     "actual_qty"),
]
for name, it, row in cases_pass:
    errs = validate_row(it, row, 2)
    check(f"{name}: 无错误通过", len(errs) == 0, str(errs))
for name, it, row, field in cases_block:
    errs = validate_row(it, row, 2)
    check(f"{name}: 被拦截", len(errs) > 0 and any(field in e for e in errs),
          f"errs={errs}")

# =============================================================
print("\n" + "=" * 70)
print("测试 3: _defensive_check 入库前最终防御（即使上游漏过也能拦）")
print("=" * 70)
check("_defensive_check 正常行返回 None",
      _defensive_check("sales", 99,
          {"store_id": "S001", "barcode": "6900000000001", "sku_name": "商品", "sale_qty": "5", "sale_date": "2025-06-20"}) is None)
check("_defensive_check sku_name空 被拦",
      _defensive_check("sales", 99,
          {"store_id": "S001", "barcode": "6900000000001", "sku_name": "", "sale_qty": "5", "sale_date": "2025-06-20"}) is not None)
check("_defensive_check barcode空 被拦",
      _defensive_check("sales", 99,
          {"store_id": "S001", "barcode": "   ", "sku_name": "X", "sale_qty": "5", "sale_date": "2025-06-20"}) is not None)
check("_defensive_check sale_qty空 被拦",
      _defensive_check("sales", 99,
          {"store_id": "S001", "barcode": "6900000000001", "sku_name": "X", "sale_qty": "", "sale_date": "2025-06-20"}) is not None)
check("_defensive_check sale_qty非数值 被拦",
      _defensive_check("sales", 99,
          {"store_id": "S001", "barcode": "6900000000001", "sku_name": "X", "sale_qty": "abc", "sale_date": "2025-06-20"}) is not None)

# =============================================================
print("\n" + "=" * 70)
print("测试 4: 销售坏行混合 CSV 真实字节流导入")
print("=" * 70)
csv_mixed = """store_id,barcode,sku_name,sale_qty,sale_date
S003,6900000000100,正常商品C,10,2025-06-20
S003,6900000000101, ,5,2025-06-20
S003,6900000000102,,3,2025-06-20
S003,6900000000103,空数量商品,,2025-06-20
S003,6900000000104,全空格数量,   ,2025-06-20
S003,6900000000105,正常商品D,7,2025-06-20
S003,,空条码商品,2,2025-06-20
S003,   ,全空格条码,4,2025-06-20
S003,6900000000106,sku空+qty空,,2025-06-20
S003,6900000000107,非数值数量,abc,2025-06-20
S003,6900000000108,缺日期商品,5,
"""
r = import_csv("sales", "mixed_bad_sales.csv", csv_mixed.encode("utf-8-sig"))
check(f"导入返回 success=True", r["success"] is True, str(r))
check(f"valid_rows=2（仅正常商品C和D）", r.get("valid_rows") == 2, f"valid_rows={r.get('valid_rows')}")
check(f"error_rows=9", r.get("error_rows") == 9, f"error_rows={r.get('error_rows')}")
errs = r.get("detail_errors", [])
check("错误共 9 条", len(errs) == 9, str(errs))
for ln in [3, 4, 5, 6, 8, 9, 10, 11, 12]:
    check(f"错误含精确行号 '第 {ln} 行'", any(f"第 {ln} 行" in e for e in errs), str(errs))
check("错误含 'sku_name'", any("sku_name" in e for e in errs), str(errs))
check("错误含 'barcode'", any("barcode" in e for e in errs), str(errs))
check("错误含 'sale_qty'", any("sale_qty" in e for e in errs), str(errs))
check("错误含 'sale_date'", any("sale_date" in e for e in errs), str(errs))

with get_conn() as conn:
    raw = conn.execute("SELECT * FROM raw_data WHERE source_type='sales' AND store_id='S003' ORDER BY source_line").fetchall()
check(f"DB中仅落库 2 条 S003 销售行（坏行 0 条落库）", len(raw) == 2,
      f"实际 {len(raw)} 条: {[dict(r) for r in raw]}")
skus = {r["sku_name"] for r in raw}
check(f"落库的是正常商品C/D: {skus}", skus == {"正常商品C", "正常商品D"}, str(skus))
check(f"落库的 2 条 sku_name 均非空", all(r["sku_name"] and r["sku_name"].strip() for r in raw),
      str([r["sku_name"] for r in raw]))
check(f"落库的 2 条 barcode 均非空", all(r["barcode"] and r["barcode"].strip() for r in raw),
      str([r["barcode"] for r in raw]))
check(f"落库的 2 条 sale_qty 均有值", all(r["sale_qty"] is not None for r in raw),
      str([r["sale_qty"] for r in raw]))

# =============================================================
print("\n" + "=" * 70)
print("测试 5: 导入四类正式样例数据 — 正常数据无误伤")
print("=" * 70)
r_inv = import_csv("inventory", "inventory.csv", read_sample("inventory.csv"))
check(f"库存导入成功: valid={r_inv.get('valid_rows')}", r_inv["success"] and r_inv.get("valid_rows") == 10, str(r_inv))
r_sal = import_csv("sales", "sales.csv", read_sample("sales.csv"))
check(f"销售导入成功: valid={r_sal.get('valid_rows')}", r_sal["success"] and r_sal.get("valid_rows") == 3, str(r_sal))
r_tra = import_csv("transfer", "transfer.csv", read_sample("transfer.csv"))
check(f"调拨导入成功: valid={r_tra.get('valid_rows')}", r_tra["success"] and r_tra.get("valid_rows") == 2, str(r_tra))
r_stk = import_csv("stocktake", "stocktake.csv", read_sample("stocktake.csv"))
check(f"盘点导入成功: valid={r_stk.get('valid_rows')}", r_stk["success"] and r_stk.get("valid_rows") == 10, str(r_stk))

with get_conn() as conn:
    raw_inv = conn.execute("SELECT COUNT(*) c FROM raw_data WHERE source_type='inventory'").fetchone()["c"]
    raw_sal = conn.execute("SELECT COUNT(*) c FROM raw_data WHERE source_type='sales' AND store_id IN ('S001','S002')").fetchone()["c"]
    raw_tra = conn.execute("SELECT COUNT(*) c FROM raw_data WHERE source_type='transfer'").fetchone()["c"]
    raw_stk = conn.execute("SELECT COUNT(*) c FROM raw_data WHERE source_type='stocktake'").fetchone()["c"]
check(f"raw_data 库存行=10", raw_inv == 10)
check(f"raw_data 销售行=3（不含S003坏行）", raw_sal == 3)
check(f"raw_data 调拨行=2", raw_tra == 2)
check(f"raw_data 盘点行=10", raw_stk == 10)

# =============================================================
print("\n" + "=" * 70)
print("测试 6: 差异归因 — 坏门店 S003 无差异（坏行未入库）")
print("=" * 70)
r_attr = run_attribution()
check(f"归因成功 created={r_attr.get('created')}", r_attr["success"] and r_attr.get("created", 0) > 0)

with get_conn() as conn:
    discs = get_discrepancies(conn)
    discs_s003 = get_discrepancies(conn, store_id="S003")
    discs_s001 = get_discrepancies(conn, store_id="S001")
    discs_s002 = get_discrepancies(conn, store_id="S002")
check(f"差异总数>0", len(discs) > 0, str(len(discs)))
check(f"S003 无差异（坏行未入库，无法归因）", len(discs_s003) == 0,
      f"S003有 {len(discs_s003)} 条差异: {[d['barcode'] for d in discs_s003]}")
check(f"S001 有差异", len(discs_s001) > 0)
check(f"S002 有差异", len(discs_s002) > 0)

# =============================================================
print("\n" + "=" * 70)
print("测试 7: 复核备注 + 状态流转 + 流转日志（正常流程）")
print("=" * 70)
with get_conn() as conn:
    d1 = discs_s001[0]
    NOTE = "销售导入收紧回归测试: 该差异经复核属于正常损耗"
    update_review_note(conn, d1["id"], NOTE)
    transition_status(conn, d1["id"], STATUS_CONFIRMED, note="流转到已确认")
    transition_status(conn, d1["id"], STATUS_CLOSED, note="确认无误关闭")
with get_conn() as conn:
    d1_upd = next(d for d in get_discrepancies(conn) if d["id"] == d1["id"])
    logs = get_status_log(conn, d1["id"])
check("复核备注保存", d1_upd.get("review_note") == NOTE, d1_upd.get("review_note"))
check("状态流转到 closed", d1_upd["status"] == STATUS_CLOSED, d1_upd["status"])
check("流转日志 2 条", len(logs) == 2, str(logs))

# =============================================================
print("\n" + "=" * 70)
print("测试 8: 坏行不进入 CSV/JSON 导出")
print("=" * 70)
with get_conn() as conn:
    all_discs = get_discrepancies(conn)
    for d in all_discs:
        d["evidence_lines"] = get_evidence_for_discrepancy(conn, d["id"])
        d["status_logs"] = get_status_log(conn, d["id"])

csv_rows = []
for d in all_discs:
    csv_rows.append({
        "store_id": d["store_id"],
        "barcode": d["barcode"],
        "sku_name": d.get("sku_name", ""),
        "review_note": d.get("review_note", ""),
        "evidence_cnt": len(d["evidence_lines"]),
        "log_cnt": len(d["status_logs"]),
    })
check("CSV 数据无 S003 门店", all(r["store_id"] != "S003" for r in csv_rows),
      str([r for r in csv_rows if r["store_id"] == "S003"]))
check("CSV 每行 barcode 非空", all(r["barcode"] and r["barcode"].strip() for r in csv_rows))
check("CSV 每行 sku_name 非空", all(r["sku_name"] and r["sku_name"].strip() for r in csv_rows))
with_note = [r for r in csv_rows if r["review_note"] == NOTE]
check("CSV 含复核备注行", len(with_note) == 1, str(with_note))
check("CSV 该行含证据行计数", with_note[0]["evidence_cnt"] >= 2 if with_note else False)
check("CSV 该行含流转日志计数", with_note[0]["log_cnt"] == 2 if with_note else False)

json_obj = []
for d in all_discs:
    json_obj.append({
        "store_id": d["store_id"],
        "barcode": d["barcode"],
        "sku_name": d.get("sku_name", ""),
        "review_note": d.get("review_note"),
        "evidence_lines": [dict(e) for e in d["evidence_lines"]],
        "status_logs": [dict(lg) for lg in d["status_logs"]],
    })
check("JSON 无 S003 门店", all(j["store_id"] != "S003" for j in json_obj))
json_note = next((j for j in json_obj if j["review_note"] == NOTE), None)
check("JSON 嵌套含复核备注", json_note is not None)
if json_note:
    check("JSON 嵌套 evidence_lines 数组有内容", len(json_note["evidence_lines"]) >= 2)
    check("JSON 证据含 source_line 可追溯", all(e.get("source_line") is not None for e in json_note["evidence_lines"]))
    check("JSON 嵌套 status_logs 有内容", len(json_note["status_logs"]) >= 2)
    check("JSON 日志含流转备注", any("确认无误关闭" in str(lg.get("note", "")) for lg in json_note["status_logs"]))

# =============================================================
print("\n" + "=" * 70)
print("测试 9: 门店筛选 — S001/S002/S003 严格隔离")
print("=" * 70)
with get_conn() as conn:
    stores = get_stores(conn)
check(f"可选门店: {stores}", set(stores) == {"S001", "S002"}, str(stores))
with get_conn() as conn:
    d1_sel = get_discrepancies(conn, store_id="S001")
    d2_sel = get_discrepancies(conn, store_id="S002")
check("筛选S001只有S001", all(d["store_id"] == "S001" for d in d1_sel))
check("筛选S002只有S002", all(d["store_id"] == "S002" for d in d2_sel))
check("S001+S002 = 总数", len(d1_sel) + len(d2_sel) == len(all_discs))

# =============================================================
print("\n" + "=" * 70)
if errors_total:
    print(f"[FAIL] 共 {len(errors_total)} 项失败:")
    for n, d in errors_total:
        print(f"   - {n}: {d}")
    sys.exit(1)
else:
    print("[全部通过] 全部销售导入链路收紧回归测试通过!")
