import csv
import hashlib
import io
import json
from db import get_conn, now_iso

REQUIRED_FIELDS = {
    "inventory": ["store_id", "barcode", "sku_name", "system_qty"],
    "sales": ["store_id", "barcode", "sku_name", "sale_qty", "sale_date"],
    "transfer": ["store_id_from", "store_id_to", "barcode", "sku_name", "transfer_qty", "transfer_date"],
    "stocktake": ["store_id", "barcode", "sku_name", "actual_qty"],
}

NUMERIC_FIELDS = {
    "inventory": ["system_qty"],
    "sales": ["sale_qty"],
    "transfer": ["transfer_qty"],
    "stocktake": ["actual_qty"],
}


def compute_file_hash(content_bytes):
    return hashlib.sha256(content_bytes).hexdigest()


def _normalize_val(val):
    if val is None:
        return None
    if isinstance(val, str):
        stripped = val.strip()
        return stripped if stripped != "" else None
    return val


def validate_row(import_type, row, line_num):
    errors = []
    required = REQUIRED_FIELDS.get(import_type, [])
    for field in required:
        val = _normalize_val(row.get(field))
        if val is None:
            errors.append(f"第 {line_num} 行: 缺少必填字段 '{field}'")

    numeric_fields = NUMERIC_FIELDS.get(import_type, [])
    for field in numeric_fields:
        raw = _normalize_val(row.get(field))
        if raw is None:
            continue
        try:
            float(raw)
        except (ValueError, TypeError):
            errors.append(f"第 {line_num} 行: 字段 '{field}' 的值 '{raw}' 不是有效数值")

    return errors


def _defensive_check(import_type, line_num, row):
    barcode = _normalize_val(row.get("barcode"))
    if barcode is None:
        return f"第 {line_num} 行: 商品标识 barcode 归一化后为空，拦截入库"

    sku_name = _normalize_val(row.get("sku_name"))
    if sku_name is None:
        return f"第 {line_num} 行: 商品名称 sku_name 归一化后为空，拦截入库"

    qty_fields = NUMERIC_FIELDS.get(import_type, [])
    for qf in qty_fields:
        raw = _normalize_val(row.get(qf))
        if raw is None:
            return f"第 {line_num} 行: 数量字段 '{qf}' 归一化后为空，拦截入库"
        try:
            float(raw)
        except (ValueError, TypeError):
            return f"第 {line_num} 行: 数量字段 '{qf}'='{raw}' 不是有效数值，拦截入库"
    return None


def import_csv(import_type, file_name, content_bytes):
    if isinstance(content_bytes, str):
        content_bytes = content_bytes.encode("utf-8")

    file_hash = compute_file_hash(content_bytes)

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM import_records WHERE file_hash = ? AND import_type = ?",
            (file_hash, import_type),
        ).fetchone()
        if existing:
            return {
                "success": False,
                "error": f"文件 '{file_name}' 已导入过（哈希: {file_hash[:8]}...），不会重复生成差异",
                "duplicate": True,
            }

        text = content_bytes.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)

        if not rows:
            return {"success": False, "error": "文件为空或无有效行"}

        available_fields = set(rows[0].keys())
        required = REQUIRED_FIELDS.get(import_type, [])
        missing_cols = [f for f in required if f not in available_fields]
        if missing_cols:
            return {
                "success": False,
                "error": f"文件缺少必要列: {', '.join(missing_cols)}。已有列: {', '.join(available_fields)}",
            }

        valid_rows = []
        all_errors = []

        for i, row in enumerate(rows, start=2):
            row_errors = validate_row(import_type, row, i)
            if row_errors:
                all_errors.extend(row_errors)
            else:
                clean = {}
                for k, v in row.items():
                    clean[k] = v.strip() if isinstance(v, str) else v
                valid_rows.append((i, clean))

        if all_errors and not valid_rows:
            return {
                "success": False,
                "error": "所有行均有校验错误，无法导入",
                "detail_errors": all_errors,
            }

        now = now_iso()
        conn.execute(
            "INSERT INTO import_records (file_hash, file_name, import_type, imported_at, row_count, error_count) VALUES (?, ?, ?, ?, ?, ?)",
            (file_hash, file_name, import_type, now, len(valid_rows), len(all_errors)),
        )
        import_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        insert_count = 0
        for line_num, row in valid_rows:
            defensive_err = _defensive_check(import_type, line_num, row)
            if defensive_err:
                all_errors.append(defensive_err)
                continue

            raw_row = json.dumps(row, ensure_ascii=False)
            system_qty = _safe_float(row.get("system_qty"))
            actual_qty = _safe_float(row.get("actual_qty"))
            sale_qty = _safe_float(row.get("sale_qty"))
            transfer_qty = _safe_float(row.get("transfer_qty"))

            conn.execute(
                """INSERT INTO raw_data
                   (import_id, source_type, source_line, store_id, barcode, sku_name,
                    system_qty, actual_qty, sale_qty, sale_date,
                    transfer_qty, transfer_date, store_id_from, store_id_to, raw_row)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    import_id,
                    import_type,
                    line_num,
                    row.get("store_id", ""),
                    row.get("barcode", ""),
                    row.get("sku_name", ""),
                    system_qty,
                    actual_qty,
                    sale_qty,
                    row.get("sale_date", ""),
                    transfer_qty,
                    row.get("transfer_date", ""),
                    row.get("store_id_from", ""),
                    row.get("store_id_to", ""),
                    raw_row,
                ),
            )
            insert_count += 1

        if insert_count == 0 and all_errors:
            conn.execute("DELETE FROM import_records WHERE id = ?", (import_id,))
            return {
                "success": False,
                "error": "所有行在入库前防御校验中均被拦截，无法导入",
                "detail_errors": all_errors,
            }

        conn.execute(
            "UPDATE import_records SET row_count = ?, error_count = ? WHERE id = ?",
            (insert_count, len(all_errors), import_id),
        )

        return {
            "success": True,
            "import_id": import_id,
            "total_rows": len(rows),
            "valid_rows": insert_count,
            "error_rows": len(all_errors),
            "detail_errors": all_errors if all_errors else None,
        }


def _safe_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def get_inventory_data(conn, import_id=None):
    sql = "SELECT * FROM raw_data WHERE source_type = 'inventory'"
    params = []
    if import_id:
        sql += " AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_stocktake_data(conn, import_id=None):
    sql = "SELECT * FROM raw_data WHERE source_type = 'stocktake'"
    params = []
    if import_id:
        sql += " AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_sales_data(conn, import_id=None):
    sql = "SELECT * FROM raw_data WHERE source_type = 'sales'"
    params = []
    if import_id:
        sql += " AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_transfer_data(conn, import_id=None):
    sql = "SELECT * FROM raw_data WHERE source_type = 'transfer'"
    params = []
    if import_id:
        sql += " AND import_id = ?"
        params.append(import_id)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
