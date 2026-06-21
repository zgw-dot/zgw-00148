import sqlite3
import json
import os
import uuid
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_diff.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS import_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT NOT NULL,
    file_name TEXT NOT NULL,
    import_type TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    row_count INTEGER,
    error_count INTEGER,
    rule_version_id INTEGER,
    FOREIGN KEY (rule_version_id) REFERENCES rule_versions(id)
);

CREATE TABLE IF NOT EXISTS ui_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state_key TEXT NOT NULL UNIQUE,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    store_id TEXT,
    barcode TEXT NOT NULL,
    canonical_barcode TEXT NOT NULL,
    sku_name TEXT,
    system_qty REAL,
    actual_qty REAL,
    sale_qty REAL,
    sale_date TEXT,
    transfer_qty REAL,
    transfer_date TEXT,
    store_id_from TEXT,
    store_id_to TEXT,
    raw_row TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES import_records(id)
);

CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS discrepancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    barcode TEXT NOT NULL,
    sku_name TEXT,
    system_qty REAL NOT NULL,
    actual_qty REAL NOT NULL,
    diff_qty REAL NOT NULL,
    attributed_cause TEXT,
    cause_detail TEXT,
    rule_version_id INTEGER,
    import_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending_review',
    review_note TEXT,
    reviewed_at TEXT,
    reviewed_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (rule_version_id) REFERENCES rule_versions(id),
    FOREIGN KEY (import_id) REFERENCES import_records(id)
);

CREATE TABLE IF NOT EXISTS evidence_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_id INTEGER NOT NULL,
    raw_data_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    description TEXT,
    FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id),
    FOREIGN KEY (raw_data_id) REFERENCES raw_data(id)
);

CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS barcode_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_barcode TEXT NOT NULL UNIQUE,
    canonical_barcode TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attribution_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_id INTEGER NOT NULL,
    rule_version_id INTEGER NOT NULL,
    rule_config_snapshot TEXT NOT NULL,
    alias_before TEXT,
    alias_after TEXT,
    system_qty_snapshot REAL NOT NULL,
    actual_qty_snapshot REAL NOT NULL,
    diff_qty_snapshot REAL NOT NULL,
    raw_inventory_ids TEXT,
    raw_stocktake_ids TEXT,
    raw_sales_ids TEXT,
    raw_transfer_ids TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id),
    FOREIGN KEY (rule_version_id) REFERENCES rule_versions(id)
);

CREATE TABLE IF NOT EXISTS discrepancy_calc_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    step_description TEXT NOT NULL,
    amount_applied REAL NOT NULL,
    remaining_before REAL NOT NULL,
    remaining_after REAL NOT NULL,
    raw_data_ids TEXT,
    FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id)
);

CREATE TABLE IF NOT EXISTS review_schemes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    filter_state_json TEXT NOT NULL,
    data_date_range_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS review_scheme_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme_id INTEGER,
    scheme_name TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    operation_detail TEXT,
    operated_at TEXT NOT NULL,
    operator TEXT DEFAULT 'user',
    FOREIGN KEY (scheme_id) REFERENCES review_schemes(id)
);

CREATE TABLE IF NOT EXISTS scheme_import_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    package_json TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    selected_indices_json TEXT NOT NULL DEFAULT '[]',
    item_decisions_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

STATUS_PENDING_REVIEW = "pending_review"
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING_ACCOUNTABILITY = "pending_accountability"
STATUS_CLOSED = "closed"

STATUS_LABELS = {
    STATUS_PENDING_REVIEW: "待复核",
    STATUS_CONFIRMED: "已确认",
    STATUS_PENDING_ACCOUNTABILITY: "待追责",
    STATUS_CLOSED: "误差关闭",
}

VALID_TRANSITIONS = {
    STATUS_PENDING_REVIEW: [STATUS_CONFIRMED, STATUS_CLOSED],
    STATUS_CONFIRMED: [STATUS_PENDING_ACCOUNTABILITY, STATUS_CLOSED],
    STATUS_PENDING_ACCOUNTABILITY: [STATUS_CLOSED],
    STATUS_CLOSED: [],
}

IMPORT_TYPES = ["inventory", "sales", "transfer", "stocktake"]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_db(conn)


def _migrate_db(conn):
    cols = conn.execute("PRAGMA table_info(import_records)").fetchall()
    col_names = [c["name"] for c in cols]

    has_rule_ver = "rule_version_id" in col_names

    existing_unique = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='import_records'"
    ).fetchone()
    table_sql = existing_unique["sql"] if existing_unique else ""
    has_old_unique = "UNIQUE" in table_sql.upper() and "rule_version_id" not in table_sql

    if not has_rule_ver or has_old_unique:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_records_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash TEXT NOT NULL,
                file_name TEXT NOT NULL,
                import_type TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER,
                error_count INTEGER,
                rule_version_id INTEGER,
                FOREIGN KEY (rule_version_id) REFERENCES rule_versions(id)
            )
        """)

        old_cols = ", ".join([c["name"] for c in cols])
        new_cols = old_cols + (", rule_version_id" if not has_rule_ver else "")
        if has_rule_ver:
            conn.execute(f"""
                INSERT INTO import_records_new ({old_cols})
                SELECT {old_cols} FROM import_records
            """)
        else:
            conn.execute(f"""
                INSERT INTO import_records_new ({old_cols}, rule_version_id)
                SELECT {old_cols}, NULL FROM import_records
            """)

        conn.execute("DROP TABLE import_records")
        conn.execute("ALTER TABLE import_records_new RENAME TO import_records")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_import_unique_hash_type_rule "
        "ON import_records(file_hash, import_type, rule_version_id)"
    )

    ui_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ui_state'"
    ).fetchone()
    if not ui_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ui_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state_key TEXT NOT NULL UNIQUE,
                state_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

    for table_name, create_sql in [
        ("review_schemes", """
            CREATE TABLE IF NOT EXISTS review_schemes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT,
                filter_state_json TEXT NOT NULL,
                data_date_range_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT
            )
        """),
        ("review_scheme_operations", """
            CREATE TABLE IF NOT EXISTS review_scheme_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scheme_id INTEGER,
                scheme_name TEXT NOT NULL,
                operation_type TEXT NOT NULL,
                operation_detail TEXT,
                operated_at TEXT NOT NULL,
                operator TEXT DEFAULT 'user',
                FOREIGN KEY (scheme_id) REFERENCES review_schemes(id)
            )
        """),
    ]:
        exists = conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'"
        ).fetchone()
        if not exists:
            conn.execute(create_sql)
        else:
            cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            col_names = [c["name"] for c in cols]
            if table_name == "review_schemes":
                if "description" not in col_names:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN description TEXT")
                if "data_date_range_json" not in col_names:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN data_date_range_json TEXT")
                if "last_used_at" not in col_names:
                    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN last_used_at TEXT")

    batch_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='scheme_import_batches'"
    ).fetchone()
    if not batch_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheme_import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                package_json TEXT NOT NULL,
                preview_json TEXT NOT NULL,
                selected_indices_json TEXT NOT NULL DEFAULT '[]',
                item_decisions_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

    _migrate_work_orders(conn)
    _migrate_handover_packages(conn)


def now_iso():
    return datetime.now().isoformat()


def get_active_rule_version(conn):
    row = conn.execute(
        "SELECT * FROM rule_versions WHERE is_active = 1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row:
        return dict(row)
    return None


def get_all_rule_versions(conn):
    rows = conn.execute(
        "SELECT * FROM rule_versions ORDER BY version DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def insert_rule_version(conn, config_dict):
    latest = conn.execute(
        "SELECT MAX(version) as mv FROM rule_versions"
    ).fetchone()
    next_ver = (latest["mv"] or 0) + 1
    now = now_iso()
    conn.execute(
        "INSERT INTO rule_versions (version, config_json, created_at, is_active) VALUES (?, ?, ?, 1)",
        (next_ver, json.dumps(config_dict, ensure_ascii=False), now),
    )
    return next_ver


def validate_rule_config(config_dict):
    errors = []
    if not isinstance(config_dict, dict):
        return ["配置必须为 JSON 对象"]
    if "loss_threshold_pct" in config_dict:
        v = config_dict["loss_threshold_pct"]
        if not isinstance(v, (int, float)) or v < 0 or v > 100:
            errors.append("损耗阈值百分比必须为 0-100 的数值")
    if "loss_threshold_abs" in config_dict:
        v = config_dict["loss_threshold_abs"]
        if not isinstance(v, (int, float)) or v < 0:
            errors.append("损耗阈值绝对值必须为非负数值")
    if "transfer_delay_days" in config_dict:
        v = config_dict["transfer_delay_days"]
        if not isinstance(v, (int, float)) or v < 0:
            errors.append("调拨延迟窗口天数必须为非负数值")
    if "aliases" in config_dict:
        aliases = config_dict["aliases"]
        if not isinstance(aliases, dict):
            errors.append("别名映射必须为对象")
        else:
            for k, v in aliases.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    errors.append(f"别名映射的键值必须为字符串: {k}->{v}")
                    break
    return errors


def transition_status(conn, discrepancy_id, to_status, changed_by="user", note=None):
    disc = conn.execute(
        "SELECT * FROM discrepancies WHERE id = ?", (discrepancy_id,)
    ).fetchone()
    if not disc:
        raise ValueError(f"差异记录不存在: {discrepancy_id}")
    disc = dict(disc)
    from_status = disc["status"]
    if to_status not in VALID_TRANSITIONS.get(from_status, []):
        raise ValueError(
            f"不允许从 '{STATUS_LABELS.get(from_status, from_status)}' 转换到 '{STATUS_LABELS.get(to_status, to_status)}'"
        )
    now = now_iso()
    conn.execute(
        "UPDATE discrepancies SET status = ?, updated_at = ? WHERE id = ?",
        (to_status, now, discrepancy_id),
    )
    conn.execute(
        "INSERT INTO status_log (discrepancy_id, from_status, to_status, changed_at, changed_by, note) VALUES (?, ?, ?, ?, ?, ?)",
        (discrepancy_id, from_status, to_status, now, changed_by, note),
    )


def update_review_note(conn, discrepancy_id, note):
    now = now_iso()
    conn.execute(
        "UPDATE discrepancies SET review_note = ?, reviewed_at = ?, updated_at = ? WHERE id = ?",
        (note, now, now, discrepancy_id),
    )


def get_discrepancies(conn, store_id=None, status=None):
    sql = "SELECT d.*, rv.version as rule_ver FROM discrepancies d LEFT JOIN rule_versions rv ON d.rule_version_id = rv.id WHERE 1=1"
    params = []
    if store_id:
        sql += " AND d.store_id = ?"
        params.append(store_id)
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    sql += " ORDER BY d.created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_evidence_for_discrepancy(conn, discrepancy_id):
    rows = conn.execute(
        """SELECT el.*, rd.source_type, rd.source_line, rd.raw_row, rd.store_id, rd.barcode
           FROM evidence_lines el
           JOIN raw_data rd ON el.raw_data_id = rd.id
           WHERE el.discrepancy_id = ?""",
        (discrepancy_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_status_log(conn, discrepancy_id):
    rows = conn.execute(
        "SELECT * FROM status_log WHERE discrepancy_id = ? ORDER BY changed_at",
        (discrepancy_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_stores(conn):
    rows = conn.execute("SELECT DISTINCT store_id FROM discrepancies ORDER BY store_id").fetchall()
    return [r["store_id"] for r in rows if r["store_id"]]


def get_import_records(conn):
    rows = conn.execute(
        "SELECT * FROM import_records ORDER BY imported_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def clear_all_discrepancies(conn):
    conn.execute("DELETE FROM status_log")
    conn.execute("DELETE FROM evidence_lines")
    conn.execute("DELETE FROM discrepancy_calc_steps")
    conn.execute("DELETE FROM attribution_snapshots")
    conn.execute("DELETE FROM discrepancies")


def get_discrepancy_by_business_key(conn, store_id, barcode, rule_version_id=None):
    sql = "SELECT * FROM discrepancies WHERE store_id = ? AND barcode = ?"
    params = [store_id, barcode]
    if rule_version_id is not None:
        sql += " AND rule_version_id = ?"
        params.append(rule_version_id)
    sql += " LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row:
        return dict(row)
    return None


def delete_evidence_for_discrepancy(conn, discrepancy_id):
    conn.execute("DELETE FROM evidence_lines WHERE discrepancy_id = ?", (discrepancy_id,))


def delete_snapshots_for_discrepancy(conn, discrepancy_id):
    conn.execute("DELETE FROM discrepancy_calc_steps WHERE discrepancy_id = ?", (discrepancy_id,))
    conn.execute("DELETE FROM attribution_snapshots WHERE discrepancy_id = ?", (discrepancy_id,))


def insert_attribution_snapshot(conn, discrepancy_id, rule_version_id, rule_config_snapshot,
                                alias_before, alias_after, system_qty_snapshot, actual_qty_snapshot,
                                diff_qty_snapshot, raw_inventory_ids, raw_stocktake_ids,
                                raw_sales_ids, raw_transfer_ids):
    now = now_iso()
    conn.execute(
        """INSERT INTO attribution_snapshots
           (discrepancy_id, rule_version_id, rule_config_snapshot, alias_before, alias_after,
            system_qty_snapshot, actual_qty_snapshot, diff_qty_snapshot,
            raw_inventory_ids, raw_stocktake_ids, raw_sales_ids, raw_transfer_ids, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            discrepancy_id, rule_version_id,
            json.dumps(rule_config_snapshot, ensure_ascii=False),
            alias_before, alias_after,
            system_qty_snapshot, actual_qty_snapshot, diff_qty_snapshot,
            json.dumps(raw_inventory_ids or []) if raw_inventory_ids else "[]",
            json.dumps(raw_stocktake_ids or []) if raw_stocktake_ids else "[]",
            json.dumps(raw_sales_ids or []) if raw_sales_ids else "[]",
            json.dumps(raw_transfer_ids or []) if raw_transfer_ids else "[]",
            now,
        ),
    )


def insert_calc_step(conn, discrepancy_id, step_index, step_type, step_description,
                     amount_applied, remaining_before, remaining_after, raw_data_ids):
    conn.execute(
        """INSERT INTO discrepancy_calc_steps
           (discrepancy_id, step_index, step_type, step_description,
            amount_applied, remaining_before, remaining_after, raw_data_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            discrepancy_id, step_index, step_type, step_description,
            amount_applied, remaining_before, remaining_after,
            json.dumps(raw_data_ids or [], ensure_ascii=False) if raw_data_ids else "[]",
        ),
    )


def get_snapshot_for_discrepancy(conn, discrepancy_id):
    row = conn.execute(
        "SELECT * FROM attribution_snapshots WHERE discrepancy_id = ? ORDER BY id DESC LIMIT 1",
        (discrepancy_id,),
    ).fetchone()
    if not row:
        return None
    snap = dict(row)
    for k in ["rule_config_snapshot", "raw_inventory_ids", "raw_stocktake_ids",
            "raw_sales_ids", "raw_transfer_ids"]:
        try:
            snap[k] = json.loads(snap[k]) if snap.get(k) else None
        except (TypeError, json.JSONDecodeError):
            snap[k] = None
    return snap


def get_calc_steps_for_discrepancy(conn, discrepancy_id):
    rows = conn.execute(
        "SELECT * FROM discrepancy_calc_steps WHERE discrepancy_id = ? ORDER BY step_index",
        (discrepancy_id,),
    ).fetchall()
    steps = [dict(r) for r in rows]
    for s in steps:
        try:
            s["raw_data_ids"] = json.loads(s["raw_data_ids"]) if s.get("raw_data_ids") else []
        except (TypeError, json.JSONDecodeError):
            s["raw_data_ids"] = []
    return steps


DEFAULT_RULES = {
    "loss_threshold_pct": 2.0,
    "loss_threshold_abs": 3.0,
    "transfer_delay_days": 3,
    "aliases": {},
}


def resolve_barcode(barcode, aliases):
    return aliases.get(barcode, barcode)


def get_discrepancies_extended(conn, store_id=None, status=None, rule_version=None,
                               barcode=None, date_from=None, date_to=None):
    sql = "SELECT d.*, rv.version as rule_ver, rv.config_json as rule_config FROM discrepancies d LEFT JOIN rule_versions rv ON d.rule_version_id = rv.id WHERE 1=1"
    params = []
    if store_id:
        sql += " AND d.store_id = ?"
        params.append(store_id)
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    if rule_version:
        sql += " AND rv.version = ?"
        params.append(rule_version)
    if barcode:
        sql += " AND (d.barcode LIKE ? OR d.sku_name LIKE ?)"
        params.extend([f"%{barcode}%", f"%{barcode}%"])
    if date_from:
        sql += " AND d.created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND d.created_at <= ?"
        params.append(date_to)
    sql += " ORDER BY d.store_id, d.barcode, rv.version DESC, d.created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        if r.get("rule_config"):
            try:
                r["rule_config"] = json.loads(r["rule_config"])
            except (TypeError, json.JSONDecodeError):
                pass
    return results


def get_discrepancy_versions(conn, store_id, barcode):
    sql = """SELECT d.*, rv.version as rule_ver, rv.config_json as rule_config,
                    rv.created_at as rule_created_at
             FROM discrepancies d
             LEFT JOIN rule_versions rv ON d.rule_version_id = rv.id
             WHERE d.store_id = ? AND d.barcode = ?
             ORDER BY rv.version DESC, d.created_at DESC"""
    rows = conn.execute(sql, (store_id, barcode)).fetchall()
    results = [dict(r) for r in rows]
    for r in results:
        if r.get("rule_config"):
            try:
                r["rule_config"] = json.loads(r["rule_config"])
            except (TypeError, json.JSONDecodeError):
                pass
        if r.get("snapshot"):
            try:
                r["snapshot"] = json.loads(r["snapshot"])
            except (TypeError, json.JSONDecodeError):
                pass
    return results


def get_all_rule_versions_with_labels(conn):
    rows = conn.execute(
        "SELECT *, (SELECT COUNT(*) FROM discrepancies d WHERE d.rule_version_id = rv.id) as disc_count "
        "FROM rule_versions rv ORDER BY version DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_import_records_with_rule_version(conn):
    rows = conn.execute(
        """SELECT ir.*, rv.version as rule_ver
           FROM import_records ir
           LEFT JOIN rule_versions rv ON ir.rule_version_id = rv.id
           ORDER BY ir.imported_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def check_import_duplicate(conn, file_hash, import_type, rule_version_id=None):
    sql = "SELECT * FROM import_records WHERE file_hash = ? AND import_type = ?"
    params = [file_hash, import_type]
    if rule_version_id is not None:
        sql += " AND rule_version_id = ?"
        params.append(rule_version_id)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def save_ui_state(conn, state_key, state_value):
    now = now_iso()
    conn.execute(
        """INSERT INTO ui_state (state_key, state_value, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(state_key) DO UPDATE SET
               state_value = excluded.state_value,
               updated_at = excluded.updated_at""",
        (state_key, json.dumps(state_value, ensure_ascii=False), now),
    )


def load_ui_state(conn, state_key, default=None):
    row = conn.execute(
        "SELECT state_value FROM ui_state WHERE state_key = ?",
        (state_key,)
    ).fetchone()
    if row:
        try:
            return json.loads(row["state_value"])
        except (TypeError, json.JSONDecodeError):
            return default
    return default


def get_store_list(conn):
    rows = conn.execute(
        "SELECT DISTINCT store_id FROM raw_data ORDER BY store_id"
    ).fetchall()
    return [r["store_id"] for r in rows if r["store_id"]]


def get_barcode_list(conn, store_id=None):
    sql = "SELECT DISTINCT barcode, sku_name FROM raw_data WHERE 1=1"
    params = []
    if store_id:
        sql += " AND store_id = ?"
        params.append(store_id)
    sql += " ORDER BY barcode"
    rows = conn.execute(sql, params).fetchall()
    return [{"barcode": r["barcode"], "sku_name": r["sku_name"] or r["barcode"]} for r in rows if r["barcode"]]


def get_date_range(conn):
    row = conn.execute(
        "SELECT MIN(imported_at) as min_date, MAX(imported_at) as max_date FROM import_records"
    ).fetchone()
    raw = dict(row) if row else {}
    min_date = raw.get("min_date")
    max_date = raw.get("max_date")
    has_data = bool(min_date and max_date)
    return {
        "min_date": min_date,
        "max_date": max_date,
        "has_data": has_data,
        "min_date_display": min_date[:10] if min_date else "",
        "max_date_display": max_date[:10] if max_date else "",
        "range_display": f"{min_date[:10]} ~ {max_date[:10]}" if has_data else "",
    }


def get_hp_date_range(conn):
    row = conn.execute(
        "SELECT MIN(created_at) as min_date, MAX(created_at) as max_date FROM handover_packages"
    ).fetchone()
    raw = dict(row) if row else {}
    min_date = raw.get("min_date")
    max_date = raw.get("max_date")
    has_data = bool(min_date and max_date)
    return {
        "min_date": min_date,
        "max_date": max_date,
        "has_data": has_data,
        "min_date_display": min_date[:10] if min_date else "",
        "max_date_display": max_date[:10] if max_date else "",
        "range_display": f"{min_date[:10]} ~ {max_date[:10]}" if has_data else "",
    }


def save_review_scheme(conn, name, filter_state, description=None, overwrite=False,
                       data_date_range=None):
    now = now_iso()
    existing = conn.execute(
        "SELECT * FROM review_schemes WHERE name = ?", (name,)
    ).fetchone()

    if existing and not overwrite:
        return {
            "success": False,
            "error": f"方案名 '{name}' 已存在",
            "existing": dict(existing),
            "needs_confirm": True,
        }

    filter_json = json.dumps(filter_state, ensure_ascii=False)
    date_range_json = json.dumps(data_date_range, ensure_ascii=False) if data_date_range else None

    if existing:
        conn.execute(
            """UPDATE review_schemes
               SET description = ?, filter_state_json = ?, data_date_range_json = ?,
                   updated_at = ?
               WHERE name = ?""",
            (description, filter_json, date_range_json, now, name),
        )
        row = conn.execute("SELECT * FROM review_schemes WHERE name = ?", (name,)).fetchone()
        scheme_id = row["id"]
        log_scheme_operation(conn, scheme_id, name, "update",
                             f"覆盖更新方案，描述: {description or '(无)'}")
    else:
        conn.execute(
            """INSERT INTO review_schemes
               (name, description, filter_state_json, data_date_range_json,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, description, filter_json, date_range_json, now, now),
        )
        row = conn.execute("SELECT * FROM review_schemes WHERE name = ?", (name,)).fetchone()
        scheme_id = row["id"]
        log_scheme_operation(conn, scheme_id, name, "create",
                             f"新建方案，描述: {description or '(无)'}")

    return {
        "success": True,
        "scheme_id": scheme_id,
        "name": name,
        "overwritten": existing is not None,
    }


def get_review_schemes(conn):
    rows = conn.execute(
        "SELECT * FROM review_schemes ORDER BY COALESCE(last_used_at, updated_at) DESC"
    ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["filter_state"] = json.loads(d["filter_state_json"]) if d.get("filter_state_json") else {}
        except (TypeError, json.JSONDecodeError):
            d["filter_state"] = {}
        try:
            d["data_date_range"] = json.loads(d["data_date_range_json"]) if d.get("data_date_range_json") else None
        except (TypeError, json.JSONDecodeError):
            d["data_date_range"] = None
        results.append(d)
    return results


def get_review_scheme_by_id(conn, scheme_id):
    row = conn.execute(
        "SELECT * FROM review_schemes WHERE id = ?", (scheme_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["filter_state"] = json.loads(d["filter_state_json"]) if d.get("filter_state_json") else {}
    except (TypeError, json.JSONDecodeError):
        d["filter_state"] = {}
    try:
        d["data_date_range"] = json.loads(d["data_date_range_json"]) if d.get("data_date_range_json") else None
    except (TypeError, json.JSONDecodeError):
        d["data_date_range"] = None
    return d


def get_review_scheme_by_name(conn, name):
    row = conn.execute(
        "SELECT * FROM review_schemes WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["filter_state"] = json.loads(d["filter_state_json"]) if d.get("filter_state_json") else {}
    except (TypeError, json.JSONDecodeError):
        d["filter_state"] = {}
    try:
        d["data_date_range"] = json.loads(d["data_date_range_json"]) if d.get("data_date_range_json") else None
    except (TypeError, json.JSONDecodeError):
        d["data_date_range"] = None
    return d


def update_review_scheme_name(conn, scheme_id, new_name):
    existing = conn.execute(
        "SELECT * FROM review_schemes WHERE id = ?", (scheme_id,)
    ).fetchone()
    if not existing:
        return {"success": False, "error": f"方案ID {scheme_id} 不存在"}

    name_exists = conn.execute(
        "SELECT * FROM review_schemes WHERE name = ? AND id != ?", (new_name, scheme_id)
    ).fetchone()
    if name_exists:
        return {"success": False, "error": f"方案名 '{new_name}' 已存在", "needs_confirm": False}

    old_name = existing["name"]
    now = now_iso()
    conn.execute(
        "UPDATE review_schemes SET name = ?, updated_at = ? WHERE id = ?",
        (new_name, now, scheme_id),
    )
    log_scheme_operation(conn, scheme_id, new_name, "rename",
                         f"从 '{old_name}' 改名为 '{new_name}'")
    return {"success": True, "old_name": old_name, "new_name": new_name}


def delete_review_scheme(conn, scheme_id):
    row = conn.execute(
        "SELECT * FROM review_schemes WHERE id = ?", (scheme_id,)
    ).fetchone()
    if not row:
        return {"success": False, "error": f"方案ID {scheme_id} 不存在"}

    name = row["name"]
    conn.execute(
        "UPDATE review_scheme_operations SET scheme_id = NULL WHERE scheme_id = ?",
        (scheme_id,),
    )
    conn.execute("DELETE FROM review_schemes WHERE id = ?", (scheme_id,))
    log_scheme_operation(conn, None, name, "delete",
                         f"删除方案 '{name}'")
    return {"success": True, "name": name}


def copy_review_scheme(conn, scheme_id, new_name, new_description=None):
    existing = conn.execute(
        "SELECT * FROM review_schemes WHERE id = ?", (scheme_id,)
    ).fetchone()
    if not existing:
        return {"success": False, "error": f"方案ID {scheme_id} 不存在"}

    name_exists = conn.execute(
        "SELECT * FROM review_schemes WHERE name = ?", (new_name,)
    ).fetchone()
    if name_exists:
        return {"success": False, "error": f"方案名 '{new_name}' 已存在", "needs_confirm": True}

    old_name = existing["name"]
    now = now_iso()
    conn.execute(
        """INSERT INTO review_schemes
           (name, description, filter_state_json, data_date_range_json,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (new_name, new_description or existing["description"],
         existing["filter_state_json"], existing["data_date_range_json"],
         now, now),
    )
    row = conn.execute("SELECT * FROM review_schemes WHERE name = ?", (new_name,)).fetchone()
    new_scheme_id = row["id"]
    log_scheme_operation(conn, new_scheme_id, new_name, "copy",
                         f"从方案 '{old_name}' 复制为 '{new_name}'")
    return {"success": True, "new_scheme_id": new_scheme_id, "new_name": new_name}


def mark_scheme_used(conn, scheme_id):
    now = now_iso()
    row = conn.execute(
        "SELECT * FROM review_schemes WHERE id = ?", (scheme_id,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE review_schemes SET last_used_at = ? WHERE id = ?",
            (now, scheme_id),
        )
        save_ui_state(conn, "last_used_scheme_id", {"scheme_id": scheme_id, "used_at": now})
        log_scheme_operation(conn, scheme_id, row["name"], "load",
                             f"载入方案 '{row['name']}'")


def get_last_used_scheme(conn):
    last_id_state = load_ui_state(conn, "last_used_scheme_id")
    if last_id_state and last_id_state.get("scheme_id"):
        scheme = get_review_scheme_by_id(conn, last_id_state["scheme_id"])
        if scheme:
            return scheme
    row = conn.execute(
        "SELECT * FROM review_schemes ORDER BY last_used_at DESC NULLS LAST, updated_at DESC LIMIT 1"
    ).fetchone()
    if row:
        return get_review_scheme_by_id(conn, row["id"])
    return None


def check_data_date_range_changed(conn, scheme_id):
    scheme = get_review_scheme_by_id(conn, scheme_id)
    if not scheme:
        return {"changed": False}

    saved_range = scheme.get("data_date_range") or {}
    current_range = get_date_range(conn)

    changed = (saved_range.get("min_date") != current_range.get("min_date") or
               saved_range.get("max_date") != current_range.get("max_date"))

    return {
        "changed": changed,
        "saved": saved_range,
        "current": current_range,
    }


def log_scheme_operation(conn, scheme_id, scheme_name, operation_type, operation_detail=None):
    now = now_iso()
    conn.execute(
        """INSERT INTO review_scheme_operations
           (scheme_id, scheme_name, operation_type, operation_detail, operated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (scheme_id, scheme_name, operation_type, operation_detail, now),
    )


SCHEME_PACKAGE_VERSION = "1.0"

SCHEME_PACKAGE_REQUIRED_KEYS = {"version", "exported_at", "schemes"}

SCHEME_PACKAGE_SCHEME_REQUIRED_KEYS = {"name", "filter_state"}

SCHEME_IMPORT_PREVIEW_CONTEXT_KEY = "scheme_import_preview_context"
SCHEME_IMPORT_LAST_POLICY_KEY = "scheme_import_last_policy"

SCHEME_ACTION_CREATED = "created"
SCHEME_ACTION_OVERWRITTEN = "overwritten"
SCHEME_ACTION_RENAMED = "renamed"
SCHEME_ACTION_KEPT = "kept"

SCHEME_ACTION_LABELS = {
    SCHEME_ACTION_CREATED: "新建",
    SCHEME_ACTION_OVERWRITTEN: "覆盖",
    SCHEME_ACTION_RENAMED: "改名导入",
    SCHEME_ACTION_KEPT: "保留原方案",
}


def export_scheme_package(conn, scheme_ids=None):
    now = now_iso()
    if scheme_ids is None:
        rows = conn.execute("SELECT * FROM review_schemes ORDER BY COALESCE(last_used_at, updated_at) DESC").fetchall()
    else:
        placeholders = ",".join("?" for _ in scheme_ids)
        rows = conn.execute(
            f"SELECT * FROM review_schemes WHERE id IN ({placeholders})",
            list(scheme_ids),
        ).fetchall()

    schemes = []
    for r in rows:
        d = dict(r)
        try:
            filter_state = json.loads(d["filter_state_json"]) if d.get("filter_state_json") else {}
        except (TypeError, json.JSONDecodeError):
            filter_state = {}
        try:
            data_date_range = json.loads(d["data_date_range_json"]) if d.get("data_date_range_json") else None
        except (TypeError, json.JSONDecodeError):
            data_date_range = None
        schemes.append({
            "name": d["name"],
            "description": d.get("description"),
            "filter_state": filter_state,
            "data_date_range": data_date_range,
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
        })

    package = {
        "version": SCHEME_PACKAGE_VERSION,
        "exported_at": now,
        "scheme_count": len(schemes),
        "schemes": schemes,
    }

    scheme_names = [s["name"] for s in schemes]
    scheme_id_list = [dict(r)["id"] for r in rows] if rows else []
    for sid in scheme_id_list:
        log_scheme_operation(
            conn, sid, schemes[scheme_id_list.index(sid)]["name"] if scheme_id_list else "",
            "export_scheme",
            f"导出方案包，包含{len(schemes)}个方案: {', '.join(scheme_names)}",
        )

    return package


def validate_scheme_package(package):
    if not isinstance(package, dict):
        return {"valid": False, "error": "方案包必须是JSON对象"}

    missing = [k for k in SCHEME_PACKAGE_REQUIRED_KEYS if k not in package]
    if missing:
        return {"valid": False, "error": f"方案包缺少必填字段: {', '.join(missing)}"}

    version = package.get("version")
    if not isinstance(version, str):
        return {"valid": False, "error": "方案包version字段必须为字符串"}

    schemes = package.get("schemes")
    if not isinstance(schemes, list):
        return {"valid": False, "error": "方案包schemes字段必须为数组"}

    if len(schemes) == 0:
        return {"valid": False, "error": "方案包schemes不能为空"}

    if "scheme_count" in package:
        declared_count = package["scheme_count"]
        if not isinstance(declared_count, int) or declared_count != len(schemes):
            return {
                "valid": False,
                "error": f"方案包已损坏：scheme_count({declared_count}) 与实际 schemes 数量({len(schemes)}) 不一致",
            }

    for i, s in enumerate(schemes):
        if not isinstance(s, dict):
            return {"valid": False, "error": f"第{i+1}个方案必须是对象"}
        missing_s = [k for k in SCHEME_PACKAGE_SCHEME_REQUIRED_KEYS if k not in s]
        if missing_s:
            return {"valid": False, "error": f"第{i+1}个方案缺少必填字段: {', '.join(missing_s)}"}
        if not isinstance(s["name"], str) or not s["name"].strip():
            return {"valid": False, "error": f"第{i+1}个方案的name必须为非空字符串"}
        if not isinstance(s["filter_state"], dict):
            return {"valid": False, "error": f"第{i+1}个方案的filter_state必须为对象"}

    return {"valid": True, "scheme_count": len(schemes)}


def _compute_rename_target(conn, original_name, rename_suffix, used_names=None):
    if used_names is None:
        used_names = set()
    suffix = rename_suffix or "(导入)"
    candidate = f"{original_name}{suffix}"
    counter = 1
    while (candidate in used_names or
           conn.execute("SELECT 1 FROM review_schemes WHERE name = ?", (candidate,)).fetchone()):
        candidate = f"{original_name}{suffix}_{counter}"
        counter += 1
    return candidate


def _resolve_scheme_conflict(conn, scheme, conflict_policy, rename_suffix, used_names=None):
    name = scheme["name"]
    description = scheme.get("description")
    filter_state = scheme["filter_state"]
    data_date_range = scheme.get("data_date_range")

    existing = conn.execute(
        "SELECT * FROM review_schemes WHERE name = ?", (name,)
    ).fetchone()

    if existing:
        existing_dict = dict(existing)
        if conflict_policy == "keep":
            return {
                "name": name,
                "original_name": name,
                "action": SCHEME_ACTION_KEPT,
                "reason": f"方案名 '{name}' 已存在，保留原方案",
                "description": description,
                "filter_state": filter_state,
                "data_date_range": data_date_range,
                "existing_id": existing_dict["id"],
                "existing_description": existing_dict.get("description"),
            }
        elif conflict_policy == "overwrite":
            return {
                "name": name,
                "original_name": name,
                "action": SCHEME_ACTION_OVERWRITTEN,
                "description": description,
                "filter_state": filter_state,
                "data_date_range": data_date_range,
                "existing_id": existing_dict["id"],
                "existing_description": existing_dict.get("description"),
            }
        elif conflict_policy == "rename":
            new_name = _compute_rename_target(conn, name, rename_suffix, used_names)
            return {
                "name": new_name,
                "original_name": name,
                "action": SCHEME_ACTION_RENAMED,
                "description": description,
                "filter_state": filter_state,
                "data_date_range": data_date_range,
            }
        else:
            raise ValueError(f"未知的冲突处理策略: {conflict_policy}")
    else:
        return {
            "name": name,
            "original_name": name,
            "action": SCHEME_ACTION_CREATED,
            "description": description,
            "filter_state": filter_state,
            "data_date_range": data_date_range,
        }


def preview_scheme_package_import(conn, package, conflict_policy="keep", rename_suffix=None):
    validation = validate_scheme_package(package)
    if not validation["valid"]:
        return {
            "success": False,
            "error": validation["error"],
            "valid": False,
        }

    schemes = package["schemes"]
    exported_at = package.get("exported_at", "")
    scheme_count = len(schemes)

    all_names = [s["name"] for s in schemes]
    date_ranges = []
    for s in schemes:
        dr = s.get("data_date_range")
        if dr and isinstance(dr, dict):
            if dr.get("min_date"):
                date_ranges.append(dr["min_date"])
            if dr.get("max_date"):
                date_ranges.append(dr["max_date"])

    min_date = min(date_ranges) if date_ranges else None
    max_date = max(date_ranges) if date_ranges else None

    preview_results = []
    used_names_in_pkg = set()
    created_count = 0
    overwritten_count = 0
    renamed_count = 0
    kept_count = 0

    for s in schemes:
        resolution = _resolve_scheme_conflict(
            conn, s, conflict_policy, rename_suffix, used_names_in_pkg
        )
        preview_results.append(resolution)
        used_names_in_pkg.add(resolution["name"])

        if resolution["action"] == SCHEME_ACTION_CREATED:
            created_count += 1
        elif resolution["action"] == SCHEME_ACTION_OVERWRITTEN:
            overwritten_count += 1
        elif resolution["action"] == SCHEME_ACTION_RENAMED:
            renamed_count += 1
        elif resolution["action"] == SCHEME_ACTION_KEPT:
            kept_count += 1

    summary = {
        "scheme_count": scheme_count,
        "exported_at": exported_at,
        "min_date": min_date,
        "max_date": max_date,
        "created_count": created_count,
        "overwritten_count": overwritten_count,
        "renamed_count": renamed_count,
        "kept_count": kept_count,
        "conflict_policy": conflict_policy,
        "rename_suffix": rename_suffix,
    }

    return {
        "success": True,
        "valid": True,
        "summary": summary,
        "scheme_names": all_names,
        "preview_results": preview_results,
        "package": package,
    }


def save_import_preview_context(conn, preview_context):
    context_to_save = {
        "preview": preview_context,
        "saved_at": now_iso(),
    }
    save_ui_state(conn, SCHEME_IMPORT_PREVIEW_CONTEXT_KEY, context_to_save)
    policy = {
        "conflict_policy": preview_context.get("summary", {}).get("conflict_policy", "keep"),
        "rename_suffix": preview_context.get("summary", {}).get("rename_suffix"),
        "saved_at": now_iso(),
    }
    save_ui_state(conn, SCHEME_IMPORT_LAST_POLICY_KEY, policy)


def load_import_preview_context(conn):
    saved = load_ui_state(conn, SCHEME_IMPORT_PREVIEW_CONTEXT_KEY, default=None)
    if saved and isinstance(saved, dict):
        return saved.get("preview")
    return None


def load_last_import_policy(conn):
    saved = load_ui_state(conn, SCHEME_IMPORT_LAST_POLICY_KEY, default=None)
    if saved and isinstance(saved, dict):
        return saved
    return None


def clear_import_preview_context(conn):
    save_ui_state(conn, SCHEME_IMPORT_PREVIEW_CONTEXT_KEY, None)


def _execute_scheme_import(conn, preview_results):
    results = []
    imported_count = 0
    skipped_count = 0
    now = now_iso()
    used_names = set()

    for pr in preview_results:
        original_name = pr["original_name"]
        description = pr.get("description")
        filter_state = pr.get("filter_state", {})
        data_date_range = pr.get("data_date_range")
        extra_detail = pr.get("note", "")
        rename_suffix = pr.get("rename_suffix", "")

        existing = conn.execute(
            "SELECT * FROM review_schemes WHERE name = ?", (original_name,)
        ).fetchone()

        target_name = pr["name"]
        target_exists = conn.execute(
            "SELECT 1 FROM review_schemes WHERE name = ?", (target_name,)
        ).fetchone()

        if target_exists and pr["action"] in (SCHEME_ACTION_CREATED, SCHEME_ACTION_RENAMED):
            if target_name in used_names:
                target_name = _compute_rename_target(conn, original_name, "(导入)", used_names)
            else:
                skipped_count += 1
                results.append({
                    "name": original_name,
                    "action": SCHEME_ACTION_KEPT,
                    "reason": f"预检后数据库状态变化，方案名 '{target_name}' 已存在，保留原方案",
                })
                existing_rec = conn.execute(
                    "SELECT * FROM review_schemes WHERE name = ?", (target_name,)
                ).fetchone()
                if existing_rec:
                    log_scheme_operation(
                        conn, dict(existing_rec)["id"], target_name, "import_scheme",
                        f"导入决策=保留，原因=状态变化，方案='{target_name}'，备注={extra_detail or '(无)'}",
                    )
                continue

        filter_json = json.dumps(filter_state, ensure_ascii=False)
        date_range_json = json.dumps(data_date_range, ensure_ascii=False) if data_date_range else None

        if existing:
            existing_dict = dict(existing)
            if pr["action"] == SCHEME_ACTION_KEPT:
                skipped_count += 1
                results.append({
                    "name": original_name,
                    "action": SCHEME_ACTION_KEPT,
                    "scheme_id": existing_dict["id"],
                    "reason": pr.get("reason", f"方案名 '{original_name}' 已存在，保留原方案"),
                })
                log_scheme_operation(
                    conn, existing_dict["id"], original_name, "import_scheme",
                    f"导入决策=保留，原因={pr.get('reason','方案已存在')}，方案='{original_name}'，备注={extra_detail or '(无)'}",
                )
                continue
            elif pr["action"] == SCHEME_ACTION_OVERWRITTEN:
                existing_id = existing_dict["id"]
                conn.execute(
                    """UPDATE review_schemes
                       SET description = ?, filter_state_json = ?, data_date_range_json = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (description, filter_json, date_range_json, now, existing_id),
                )
                imported_count += 1
                used_names.add(original_name)
                results.append({
                    "name": original_name,
                    "original_name": original_name,
                    "action": SCHEME_ACTION_OVERWRITTEN,
                    "scheme_id": existing_id,
                })
                log_scheme_operation(
                    conn, existing_id, original_name, "import_scheme",
                    f"导入决策=覆盖，方案='{original_name}'，备注={extra_detail or '(无)'}",
                )
                continue
            elif pr["action"] == SCHEME_ACTION_RENAMED:
                used_names.add(target_name)
                conn.execute(
                    """INSERT INTO review_schemes
                       (name, description, filter_state_json, data_date_range_json,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (target_name, description, filter_json, date_range_json, now, now),
                )
                row = conn.execute("SELECT * FROM review_schemes WHERE name = ?", (target_name,)).fetchone()
                scheme_id = row["id"]
                imported_count += 1
                results.append({
                    "name": target_name,
                    "original_name": original_name,
                    "action": SCHEME_ACTION_RENAMED,
                    "scheme_id": scheme_id,
                })
                log_scheme_operation(
                    conn, scheme_id, target_name, "import_scheme",
                    f"导入决策=改名，原名='{original_name}'，新名='{target_name}'，后缀={rename_suffix or '(导入)'}，备注={extra_detail or '(无)'}",
                )
                continue
        else:
            used_names.add(target_name)
            conn.execute(
                """INSERT INTO review_schemes
                   (name, description, filter_state_json, data_date_range_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (target_name, description, filter_json, date_range_json, now, now),
            )
            row = conn.execute("SELECT * FROM review_schemes WHERE name = ?", (target_name,)).fetchone()
            scheme_id = row["id"]
            imported_count += 1

            if pr["action"] == SCHEME_ACTION_RENAMED:
                results.append({
                    "name": target_name,
                    "original_name": original_name,
                    "action": SCHEME_ACTION_RENAMED,
                    "scheme_id": scheme_id,
                })
                log_scheme_operation(
                    conn, scheme_id, target_name, "import_scheme",
                    f"导入决策=改名(新方案)，原名='{original_name}'，新名='{target_name}'，后缀={rename_suffix or '(导入)'}，备注={extra_detail or '(无)'}",
                )
            else:
                results.append({
                    "name": target_name,
                    "action": SCHEME_ACTION_CREATED,
                    "scheme_id": scheme_id,
                })
                log_scheme_operation(
                    conn, scheme_id, target_name, "import_scheme",
                    f"导入决策=新建，方案='{target_name}'，备注={extra_detail or '(无)'}",
                )
            continue

    return {
        "success": True,
        "imported_count": imported_count,
        "skipped_count": skipped_count,
        "total": len(preview_results),
        "results": results,
    }


def confirm_scheme_package_import(conn, preview_context=None):
    if preview_context is None:
        preview_context = load_import_preview_context(conn)

    if not preview_context or not isinstance(preview_context, dict):
        return {
            "success": False,
            "error": "没有可确认的导入预览，请先重新上传方案包",
        }

    if not preview_context.get("success") or not preview_context.get("valid"):
        return {
            "success": False,
            "error": "预览结果无效，请先重新上传方案包",
        }

    preview_results = preview_context.get("preview_results", [])
    if not preview_results:
        return {
            "success": False,
            "error": "预览结果为空，请先重新上传方案包",
        }

    result = _execute_scheme_import(conn, preview_results)

    if result["success"]:
        clear_import_preview_context(conn)

    return result


def import_scheme_package(conn, package, conflict_policy="keep", rename_suffix=None):
    validation = validate_scheme_package(package)
    if not validation["valid"]:
        return {"success": False, "error": validation["error"]}

    preview = preview_scheme_package_import(
        conn, package, conflict_policy=conflict_policy, rename_suffix=rename_suffix
    )
    if not preview["success"]:
        return {"success": False, "error": preview.get("error", "预检失败")}

    return _execute_scheme_import(conn, preview["preview_results"])


def get_scheme_operation_logs(conn, scheme_id=None, limit=100):
    sql = "SELECT * FROM review_scheme_operations"
    params = []
    if scheme_id is not None:
        sql += " WHERE scheme_id = ?"
        params.append(scheme_id)
    sql += " ORDER BY operated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def compute_scheme_diff(local_scheme, incoming_scheme):
    diffs = []
    if not local_scheme:
        return diffs

    local_fs = local_scheme.get("filter_state", {})
    incoming_fs = incoming_scheme.get("filter_state", {})
    fs_keys = set(list(local_fs.keys()) + list(incoming_fs.keys()))
    for k in sorted(fs_keys):
        lv = local_fs.get(k)
        iv = incoming_fs.get(k)
        if lv != iv:
            diffs.append({
                "field": f"筛选条件.{k}",
                "local": str(lv) if lv is not None else "(空)",
                "incoming": str(iv) if iv is not None else "(空)",
            })

    local_desc = local_scheme.get("description") or ""
    incoming_desc = incoming_scheme.get("description") or ""
    if local_desc != incoming_desc:
        diffs.append({
            "field": "描述",
            "local": local_desc or "(空)",
            "incoming": incoming_desc or "(空)",
        })

    local_dr = local_scheme.get("data_date_range") or {}
    incoming_dr = incoming_scheme.get("data_date_range") or {}
    for dk in ["min_date", "max_date"]:
        lv = local_dr.get(dk, "")
        iv = incoming_dr.get(dk, "")
        if lv != iv:
            label = "时间范围.起始" if dk == "min_date" else "时间范围.截止"
            diffs.append({
                "field": label,
                "local": lv or "(空)",
                "incoming": iv or "(空)",
            })

    return diffs


def save_import_batch(conn, batch_id, package, preview, selected_indices=None, item_decisions=None):
    now = now_iso()
    pj = json.dumps(package, ensure_ascii=False)
    prevj = json.dumps(preview, ensure_ascii=False)
    sel = json.dumps(selected_indices if selected_indices is not None else [], ensure_ascii=False)
    dec = json.dumps(item_decisions if item_decisions is not None else {}, ensure_ascii=False)

    existing = conn.execute(
        "SELECT id FROM scheme_import_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE scheme_import_batches
               SET package_json = ?, preview_json = ?, selected_indices_json = ?,
                   item_decisions_json = ?, status = 'pending', updated_at = ?
               WHERE batch_id = ?""",
            (pj, prevj, sel, dec, now, batch_id),
        )
    else:
        conn.execute(
            """INSERT INTO scheme_import_batches
               (batch_id, package_json, preview_json, selected_indices_json,
                item_decisions_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (batch_id, pj, prevj, sel, dec, now, now),
        )
    return batch_id


def load_import_batch(conn, batch_id):
    row = conn.execute(
        "SELECT * FROM scheme_import_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "batch_id": d["batch_id"],
        "package": json.loads(d["package_json"]),
        "preview": json.loads(d["preview_json"]),
        "selected_indices": json.loads(d["selected_indices_json"]),
        "item_decisions": json.loads(d["item_decisions_json"]),
        "status": d["status"],
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
    }


def list_pending_batches(conn):
    rows = conn.execute(
        "SELECT * FROM scheme_import_batches WHERE status = 'pending' ORDER BY updated_at DESC"
    ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        preview = json.loads(d["preview_json"])
        summary = preview.get("summary", {})
        results.append({
            "batch_id": d["batch_id"],
            "scheme_count": summary.get("scheme_count", 0),
            "created_count": summary.get("created_count", 0),
            "conflict_count": summary.get("overwritten_count", 0) + summary.get("renamed_count", 0) + summary.get("kept_count", 0),
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "status": d["status"],
        })
    return results


def update_batch_selection(conn, batch_id, selected_indices, item_decisions):
    now = now_iso()
    sel = json.dumps(selected_indices, ensure_ascii=False)
    dec = json.dumps(item_decisions, ensure_ascii=False)
    conn.execute(
        """UPDATE scheme_import_batches
           SET selected_indices_json = ?, item_decisions_json = ?, updated_at = ?
           WHERE batch_id = ?""",
        (sel, dec, now, batch_id),
    )


def clear_import_batch(conn, batch_id):
    conn.execute("DELETE FROM scheme_import_batches WHERE batch_id = ?", (batch_id,))


def mark_batch_completed(conn, batch_id):
    now = now_iso()
    conn.execute(
        "UPDATE scheme_import_batches SET status = 'completed', updated_at = ? WHERE batch_id = ?",
        (now, batch_id),
    )


def confirm_partial_scheme_import(conn, preview_context, selected_indices, item_decisions):
    if not preview_context or not isinstance(preview_context, dict):
        return {"success": False, "error": "没有可确认的导入预览"}

    if not preview_context.get("success") or not preview_context.get("valid"):
        return {"success": False, "error": "预览结果无效"}

    preview_results = preview_context.get("preview_results", [])
    if not preview_results:
        return {"success": False, "error": "预览结果为空"}

    if not selected_indices:
        return {"success": False, "error": "未选择任何方案进行导入"}

    selected_set = set(selected_indices)
    items_to_import = []
    processed_indices = []
    index_tracking = {}

    for i, pr in enumerate(preview_results):
        if i not in selected_set:
            continue

        decision = item_decisions.get(str(i), {}) if item_decisions else {}
        override_action = decision.get("action")
        note = decision.get("note", "")
        rename_suffix = decision.get("rename_suffix", "")

        item = dict(pr)
        item["note"] = note
        if rename_suffix:
            item["rename_suffix"] = rename_suffix

        if override_action and override_action in (
            SCHEME_ACTION_CREATED, SCHEME_ACTION_OVERWRITTEN,
            SCHEME_ACTION_RENAMED, SCHEME_ACTION_KEPT
        ):
            original_name = pr.get("original_name", pr["name"])
            if override_action == SCHEME_ACTION_RENAMED:
                effective_suffix = rename_suffix or "(导入)"
                if not item.get("rename_suffix"):
                    item["rename_suffix"] = effective_suffix
                new_name = _compute_rename_target(conn, original_name, effective_suffix)
                item["name"] = new_name
            elif override_action == SCHEME_ACTION_OVERWRITTEN:
                existing = conn.execute(
                    "SELECT * FROM review_schemes WHERE name = ?", (original_name,)
                ).fetchone()
                if existing:
                    item["existing_id"] = dict(existing)["id"]
            item["action"] = override_action

        processing_order = len(processed_indices)
        index_tracking[processing_order] = i
        processed_indices.append(i)
        items_to_import.append(item)

    result = _execute_scheme_import(conn, items_to_import)

    if result["success"]:
        result["processed_indices"] = processed_indices

        for processing_order, r in enumerate(result["results"]):
            idx = index_tracking.get(processing_order)
            if idx is not None:
                decision = item_decisions.get(str(idx), {}) if item_decisions else {}
                note = decision.get("note", "")
                if note:
                    log_scheme_operation(
                        conn, r.get("scheme_id"), r["name"],
                        "import_scheme_note",
                        f"导入备注: {note}",
                    )

        remaining = [pr for i, pr in enumerate(preview_results) if i not in selected_set]
        result["remaining_count"] = len(remaining)
        result["remaining_items"] = remaining

        remaining_indices = [i for i in range(len(preview_results)) if i not in selected_set]
        result["remaining_indices"] = remaining_indices

        original_pkg = preview_context.get("package", {})
        original_schemes = original_pkg.get("schemes", [])
        remaining_schemes_in_pkg = []
        for ri in remaining_indices:
            if ri < len(original_schemes):
                remaining_schemes_in_pkg.append(original_schemes[ri])
        result["remaining_package_schemes"] = remaining_schemes_in_pkg

        remaining_decisions = {}
        for old_idx in remaining_indices:
            old_key = str(old_idx)
            if old_key in (item_decisions or {}):
                remaining_decisions[old_key] = item_decisions[old_key]
        result["remaining_decisions_raw"] = remaining_decisions
        result["conflict_policy"] = preview_context.get("summary", {}).get("conflict_policy", "keep")
        result["rename_suffix_global"] = preview_context.get("summary", {}).get("rename_suffix")

    return result


def shrink_batch_to_remaining(conn, original_batch_id, processed_indices,
                               conflict_policy=None, rename_suffix=None):
    original_batch = load_import_batch(conn, original_batch_id)
    if not original_batch:
        return {"success": False, "error": f"批次不存在: {original_batch_id}"}

    preview = original_batch["preview"]
    package = original_batch["package"]
    old_preview_results = preview.get("preview_results", [])
    old_schemes = package.get("schemes", [])
    old_decisions = original_batch.get("item_decisions", {})
    old_selected = original_batch.get("selected_indices", [])

    processed_set = set(processed_indices)
    remaining_old_indices = [i for i in range(len(old_preview_results)) if i not in processed_set]

    if not remaining_old_indices:
        mark_batch_completed(conn, original_batch_id)
        log_scheme_operation(
            conn, None, f"batch:{original_batch_id[:8]}", "batch_close",
            f"批次全部处理完成，标记完成。共处理 {len(processed_set)} 个方案",
        )
        return {
            "success": True,
            "remaining_count": 0,
            "new_batch_id": None,
            "all_completed": True,
            "original_batch_id": original_batch_id,
        }

    new_index_mapping = {}
    remaining_preview_results = []
    remaining_schemes = []
    for new_idx, old_idx in enumerate(remaining_old_indices):
        new_index_mapping[old_idx] = new_idx
        if old_idx < len(old_preview_results):
            remaining_preview_results.append(old_preview_results[old_idx])
        if old_idx < len(old_schemes):
            remaining_schemes.append(old_schemes[old_idx])

    remaining_decisions = {}
    for old_idx in remaining_old_indices:
        old_key = str(old_idx)
        if old_key in old_decisions:
            new_key = str(new_index_mapping[old_idx])
            remaining_decisions[new_key] = old_decisions[old_key]

    remaining_old_selected_set = set()
    for si in old_selected:
        if si in new_index_mapping:
            remaining_old_selected_set.add(new_index_mapping[si])
    remaining_selected_indices = sorted(list(remaining_old_selected_set))

    remaining_pkg = {
        "version": package.get("version", "1.0"),
        "exported_at": package.get("exported_at", now_iso()),
        "scheme_count": len(remaining_schemes),
        "schemes": remaining_schemes,
    }

    eff_policy = conflict_policy or preview.get("summary", {}).get("conflict_policy", "keep")
    eff_suffix = rename_suffix or preview.get("summary", {}).get("rename_suffix")

    remaining_preview = preview_scheme_package_import(
        conn, remaining_pkg, conflict_policy=eff_policy, rename_suffix=eff_suffix
    )
    if not remaining_preview["success"]:
        return {
            "success": False,
            "error": f"重新生成剩余预检失败: {remaining_preview.get('error','')}",
        }

    for new_idx in range(len(remaining_preview_results)):
        if new_idx < len(remaining_preview["preview_results"]):
            rp = remaining_preview["preview_results"][new_idx]
            dec = remaining_decisions.get(str(new_idx), {})
            if "action" in dec:
                override_action = dec["action"]
                if override_action in (SCHEME_ACTION_CREATED, SCHEME_ACTION_OVERWRITTEN,
                                       SCHEME_ACTION_RENAMED, SCHEME_ACTION_KEPT):
                    rp["action"] = override_action
                    if override_action == SCHEME_ACTION_RENAMED:
                        suffix = dec.get("rename_suffix") or eff_suffix or "(导入)"
                        oname = rp.get("original_name", rp["name"])
                        rp["name"] = _compute_rename_target(conn, oname, suffix)

    new_batch_id = f"{original_batch_id.split('-part-')[0]}-part-{len(processed_set)}-{uuid.uuid4().hex[:8]}"
    if original_batch_id.startswith("batch-") and "-part-" not in original_batch_id:
        new_batch_id = f"{original_batch_id}-part-{len(processed_set)}-{uuid.uuid4().hex[:8]}"

    save_import_batch(
        conn, new_batch_id, remaining_pkg, remaining_preview,
        selected_indices=remaining_selected_indices,
        item_decisions=remaining_decisions,
    )

    mark_batch_completed(conn, original_batch_id)
    log_scheme_operation(
        conn, None, f"batch:{original_batch_id[:8]}", "batch_shrink",
        f"批次收口，已处理 {len(processed_set)} 个方案，剩余 {len(remaining_old_indices)} 个转入新批次 {new_batch_id[:16]}...",
    )
    log_scheme_operation(
        conn, None, f"batch:{new_batch_id[:8]}", "batch_create_remaining",
        f"从 {original_batch_id[:16]}... 分出剩余批次，含 {len(remaining_old_indices)} 个方案",
    )

    return {
        "success": True,
        "remaining_count": len(remaining_old_indices),
        "new_batch_id": new_batch_id,
        "all_completed": False,
        "original_batch_id": original_batch_id,
        "remaining_package": remaining_pkg,
        "remaining_preview": remaining_preview,
        "remaining_selected_indices": remaining_selected_indices,
        "remaining_item_decisions": remaining_decisions,
    }


def list_all_batches(conn, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM scheme_import_batches WHERE status = ? ORDER BY updated_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheme_import_batches ORDER BY updated_at DESC"
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        try:
            preview = json.loads(d["preview_json"]) if d.get("preview_json") else {}
        except (TypeError, json.JSONDecodeError):
            preview = {}
        summary = preview.get("summary", {})
        try:
            sel = json.loads(d["selected_indices_json"]) if d.get("selected_indices_json") else []
        except (TypeError, json.JSONDecodeError):
            sel = []
        results.append({
            "batch_id": d["batch_id"],
            "scheme_count": summary.get("scheme_count", 0),
            "created_count": summary.get("created_count", 0),
            "conflict_count": (summary.get("overwritten_count", 0) +
                               summary.get("renamed_count", 0) +
                               summary.get("kept_count", 0)),
            "selected_count": len(sel),
            "status": d["status"],
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
        })
    return results


def get_import_decision_logs(conn, scheme_name=None, batch_id=None,
                            operation_type="import_scheme", limit=200):
    sql = "SELECT * FROM review_scheme_operations WHERE 1=1"
    params = []
    if scheme_name:
        sql += " AND scheme_name LIKE ?"
        params.append(f"%{scheme_name}%")
    if batch_id:
        sql += " AND operation_detail LIKE ?"
        params.append(f"%{batch_id}%")
    if operation_type:
        if "," in operation_type:
            placeholders = ",".join("?" for _ in operation_type.split(","))
            sql += f" AND operation_type IN ({placeholders})"
            params.extend([t.strip() for t in operation_type.split(",")])
        else:
            sql += " AND operation_type = ?"
            params.append(operation_type)
    sql += " ORDER BY operated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def export_scheme_manifest(conn, scheme_ids=None, name_filter=None, updated_after=None):
    now = now_iso()
    if scheme_ids is not None:
        placeholders = ",".join("?" for _ in scheme_ids)
        rows = conn.execute(
            f"SELECT * FROM review_schemes WHERE id IN ({placeholders})",
            list(scheme_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM review_schemes ORDER BY COALESCE(last_used_at, updated_at) DESC"
        ).fetchall()

    schemes = []
    for r in rows:
        d = dict(r)
        if name_filter and name_filter.lower() not in d["name"].lower():
            continue
        if updated_after and d.get("updated_at", "") < updated_after:
            continue
        try:
            filter_state = json.loads(d["filter_state_json"]) if d.get("filter_state_json") else {}
        except (TypeError, json.JSONDecodeError):
            filter_state = {}
        try:
            data_date_range = json.loads(d["data_date_range_json"]) if d.get("data_date_range_json") else None
        except (TypeError, json.JSONDecodeError):
            data_date_range = None
        schemes.append({
            "id": d["id"],
            "name": d["name"],
            "description": d.get("description"),
            "filter_state": filter_state,
            "data_date_range": data_date_range,
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
            "last_used_at": d.get("last_used_at"),
        })

    date_ranges = []
    for s in schemes:
        dr = s.get("data_date_range")
        if dr and isinstance(dr, dict):
            if dr.get("min_date"):
                date_ranges.append(dr["min_date"])
            if dr.get("max_date"):
                date_ranges.append(dr["max_date"])

    manifest = {
        "version": SCHEME_PACKAGE_VERSION,
        "exported_at": now,
        "type": "scheme_export_manifest",
        "total_schemes": len(schemes),
        "date_range": {
            "min_date": min(date_ranges) if date_ranges else None,
            "max_date": max(date_ranges) if date_ranges else None,
        },
        "filter_applied": {
            "name_filter": name_filter,
            "updated_after": updated_after,
        },
        "schemes": schemes,
    }

    return manifest


WO_STATUS_PENDING_DISPATCH = "pending_dispatch"
WO_STATUS_PROCESSING = "processing"
WO_STATUS_PENDING_REVIEW = "pending_review"
WO_STATUS_CLOSED = "closed"
WO_STATUS_REVOKED = "revoked"

WO_STATUS_LABELS = {
    WO_STATUS_PENDING_DISPATCH: "待派发",
    WO_STATUS_PROCESSING: "处理中",
    WO_STATUS_PENDING_REVIEW: "待复核",
    WO_STATUS_CLOSED: "已关闭",
    WO_STATUS_REVOKED: "已撤销",
}

WO_VALID_TRANSITIONS = {
    WO_STATUS_PENDING_DISPATCH: [WO_STATUS_PROCESSING, WO_STATUS_REVOKED],
    WO_STATUS_PROCESSING: [WO_STATUS_PENDING_REVIEW, WO_STATUS_REVOKED],
    WO_STATUS_PENDING_REVIEW: [WO_STATUS_PROCESSING, WO_STATUS_CLOSED, WO_STATUS_REVOKED],
    WO_STATUS_CLOSED: [],
    WO_STATUS_REVOKED: [],
}

WO_ACTION_LABELS = {
    "price_adjust": "价格调整",
    "inventory_correction": "库存校正",
    "vendor_claim": "向供应商索赔",
    "staff_training": "员工培训",
    "process_optimization": "流程优化",
    "write_off": "报损处理",
    "other": "其他",
}

WO_ROLE_ADMIN = "admin"
WO_ROLE_NORMAL = "normal"


def _migrate_work_orders(conn):
    wo_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='work_orders'"
    ).fetchone()
    if not wo_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_no TEXT NOT NULL UNIQUE,
                discrepancy_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                barcode TEXT NOT NULL,
                sku_name TEXT,
                diff_qty REAL NOT NULL,
                attributed_cause TEXT,
                assignee TEXT,
                deadline TEXT,
                action_type TEXT,
                action_detail TEXT,
                follow_up_result TEXT,
                status TEXT NOT NULL DEFAULT 'pending_dispatch',
                created_by TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id)
            )
        """)

    wol_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='work_order_logs'"
    ).fetchone()
    if not wol_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS work_order_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_order_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                action_detail TEXT,
                operator TEXT NOT NULL DEFAULT 'user',
                operated_at TEXT NOT NULL,
                FOREIGN KEY (work_order_id) REFERENCES work_orders(id)
            )
        """)


def _generate_wo_no(conn):
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    prefix = f"WO{date_str}"
    row = conn.execute(
        "SELECT wo_no FROM work_orders WHERE wo_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",)
    ).fetchone()
    if row:
        last_no = row["wo_no"]
        seq = int(last_no[-4:]) + 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


def create_work_order(conn, discrepancy_id, assignee=None, deadline=None,
                      action_type=None, action_detail=None, created_by="user"):
    disc = conn.execute(
        "SELECT * FROM discrepancies WHERE id = ?", (discrepancy_id,)
    ).fetchone()
    if not disc:
        return {"success": False, "error": f"差异记录不存在: {discrepancy_id}"}
    disc = dict(disc)

    if disc["status"] != STATUS_CONFIRMED:
        return {
            "success": False,
            "error": f"只能对'已确认'状态的差异创建工单，当前状态: {STATUS_LABELS.get(disc['status'], disc['status'])}"
        }

    existing = conn.execute(
        "SELECT * FROM work_orders WHERE discrepancy_id = ? AND status != 'revoked'",
        (discrepancy_id,)
    ).fetchone()
    if existing:
        return {
            "success": False,
            "error": f"该差异已有有效工单（工单号: {existing['wo_no']}），不允许重复建单",
            "existing_wo": dict(existing),
        }

    wo_no = _generate_wo_no(conn)
    now = now_iso()
    conn.execute(
        """INSERT INTO work_orders
           (wo_no, discrepancy_id, store_id, barcode, sku_name, diff_qty,
            attributed_cause, assignee, deadline, action_type, action_detail,
            status, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_dispatch', ?, ?, ?)""",
        (
            wo_no, discrepancy_id, disc["store_id"], disc["barcode"],
            disc.get("sku_name"), disc["diff_qty"], disc.get("attributed_cause"),
            assignee, deadline, action_type, action_detail,
            created_by, now, now,
        ),
    )
    row = conn.execute("SELECT * FROM work_orders WHERE wo_no = ?", (wo_no,)).fetchone()
    wo_id = row["id"]

    _log_work_order_action(conn, wo_id, "create",
                           f"创建工单，关联差异ID: {discrepancy_id}", created_by)

    return {"success": True, "work_order_id": wo_id, "wo_no": wo_no}


def batch_create_work_orders(conn, discrepancy_ids, assignee=None, deadline=None,
                             action_type=None, created_by="user"):
    results = []
    success_count = 0
    fail_count = 0
    for did in discrepancy_ids:
        r = create_work_order(conn, did, assignee=assignee, deadline=deadline,
                              action_type=action_type, created_by=created_by)
        results.append({"discrepancy_id": did, **r})
        if r["success"]:
            success_count += 1
        else:
            fail_count += 1
    return {
        "success": True,
        "total": len(discrepancy_ids),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
    }


def get_work_order(conn, wo_id):
    row = conn.execute(
        "SELECT wo.*, d.attributed_cause as disc_cause, d.cause_detail as disc_cause_detail "
        "FROM work_orders wo LEFT JOIN discrepancies d ON wo.discrepancy_id = d.id "
        "WHERE wo.id = ?",
        (wo_id,),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def get_work_order_by_no(conn, wo_no):
    row = conn.execute(
        "SELECT * FROM work_orders WHERE wo_no = ?", (wo_no,)
    ).fetchone()
    if not row:
        return None
    return dict(row)


def list_work_orders(conn, store_id=None, status=None, assignee=None,
                     keyword=None, deadline_from=None, deadline_to=None):
    sql = "SELECT * FROM work_orders WHERE 1=1"
    params = []
    if store_id:
        sql += " AND store_id = ?"
        params.append(store_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if assignee:
        sql += " AND assignee = ?"
        params.append(assignee)
    if keyword:
        sql += " AND (wo_no LIKE ? OR barcode LIKE ? OR sku_name LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    if deadline_from:
        sql += " AND deadline >= ?"
        params.append(deadline_from)
    if deadline_to:
        sql += " AND deadline <= ?"
        params.append(deadline_to)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def update_work_order(conn, wo_id, updates, operator="user", role=WO_ROLE_ADMIN):
    wo = get_work_order(conn, wo_id)
    if not wo:
        return {"success": False, "error": f"工单不存在: {wo_id}"}

    if wo["status"] == WO_STATUS_CLOSED:
        return {"success": False, "error": "已关闭工单不允许再编辑"}

    if wo["status"] == WO_STATUS_REVOKED:
        return {"success": False, "error": "已撤销工单不允许再编辑"}

    allowed_fields = [
        "assignee", "deadline", "action_type", "action_detail",
        "follow_up_result"
    ]
    update_dict = {k: v for k, v in updates.items() if k in allowed_fields and v is not None}

    if not update_dict:
        return {"success": False, "error": "没有有效的更新字段"}

    set_clause = ", ".join(f"{k} = ?" for k in update_dict.keys())
    params = list(update_dict.values())
    params.extend([now_iso(), wo_id])

    conn.execute(
        f"UPDATE work_orders SET {set_clause}, updated_at = ? WHERE id = ?",
        params
    )

    detail = "更新字段: " + ", ".join(update_dict.keys())
    _log_work_order_action(conn, wo_id, "update", detail, operator)

    return {"success": True}


def transition_work_order_status(conn, wo_id, to_status, operator="user",
                                 role=WO_ROLE_ADMIN, note=None):
    wo = get_work_order(conn, wo_id)
    if not wo:
        return {"success": False, "error": f"工单不存在: {wo_id}"}

    from_status = wo["status"]

    if from_status == WO_STATUS_CLOSED:
        return {"success": False, "error": "已关闭工单不允许状态变更"}

    if from_status == WO_STATUS_REVOKED:
        return {"success": False, "error": "已撤销工单不允许状态变更"}

    if to_status not in WO_VALID_TRANSITIONS.get(from_status, []):
        return {
            "success": False,
            "error": f"不允许从 '{WO_STATUS_LABELS.get(from_status, from_status)}' "
                     f"转换到 '{WO_STATUS_LABELS.get(to_status, to_status)}'"
        }

    if to_status == WO_STATUS_REVOKED:
        if role != WO_ROLE_ADMIN and wo["created_by"] != operator:
            return {
                "success": False,
                "error": "普通角色只能撤销自己创建的工单",
            }

    now = now_iso()
    updates = {"status": to_status, "updated_at": now}
    if to_status == WO_STATUS_CLOSED:
        updates["closed_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = list(updates.values())
    params.append(wo_id)

    conn.execute(
        f"UPDATE work_orders SET {set_clause} WHERE id = ?",
        params
    )

    detail = f"状态变更: {WO_STATUS_LABELS.get(from_status, from_status)} → {WO_STATUS_LABELS.get(to_status, to_status)}"
    if note:
        detail += f"，备注: {note}"
    _log_work_order_action(conn, wo_id, "status_change", detail, operator)

    return {"success": True, "from_status": from_status, "to_status": to_status}


def batch_reassign_work_orders(conn, wo_ids, new_assignee, operator="user"):
    results = []
    success_count = 0
    for wid in wo_ids:
        r = update_work_order(conn, wid, {"assignee": new_assignee},
                              operator=operator, role=WO_ROLE_ADMIN)
        results.append({"work_order_id": wid, **r})
        if r["success"]:
            success_count += 1
    return {
        "success": True,
        "total": len(wo_ids),
        "success_count": success_count,
        "fail_count": len(wo_ids) - success_count,
        "results": results,
    }


def _log_work_order_action(conn, work_order_id, action_type, action_detail=None,
                           operator="user"):
    now = now_iso()
    conn.execute(
        """INSERT INTO work_order_logs
           (work_order_id, action_type, action_detail, operator, operated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (work_order_id, action_type, action_detail, operator, now),
    )


def get_work_order_logs(conn, wo_id, limit=100):
    rows = conn.execute(
        "SELECT * FROM work_order_logs WHERE work_order_id = ? ORDER BY operated_at DESC LIMIT ?",
        (wo_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_work_order_logs(conn, operator=None, action_type=None, limit=200):
    sql = "SELECT l.*, wo.wo_no FROM work_order_logs l JOIN work_orders wo ON l.work_order_id = wo.id WHERE 1=1"
    params = []
    if operator:
        sql += " AND l.operator = ?"
        params.append(operator)
    if action_type:
        sql += " AND l.action_type = ?"
        params.append(action_type)
    sql += " ORDER BY l.operated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_wo_assignees(conn):
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM work_orders WHERE assignee IS NOT NULL ORDER BY assignee"
    ).fetchall()
    return [r["assignee"] for r in rows if r["assignee"]]


def get_wo_stores(conn):
    rows = conn.execute(
        "SELECT DISTINCT store_id FROM work_orders ORDER BY store_id"
    ).fetchall()
    return [r["store_id"] for r in rows if r["store_id"]]


def export_work_orders_json(conn, store_id=None, status=None, assignee=None):
    orders = list_work_orders(conn, store_id=store_id, status=status, assignee=assignee)
    for wo in orders:
        logs = get_work_order_logs(conn, wo["id"])
        wo["logs"] = logs
    return {
        "export_version": "1.0",
        "exported_at": now_iso(),
        "filter": {
            "store_id": store_id,
            "status": status,
            "assignee": assignee,
        },
        "total": len(orders),
        "work_orders": orders,
    }


def preview_work_orders_import(conn, import_data):
    if not isinstance(import_data, dict):
        return {"success": False, "error": "导入数据格式错误"}

    orders = import_data.get("work_orders", [])
    if not orders:
        return {"success": False, "error": "导入数据中没有工单记录"}

    results = []
    for wo in orders:
        wo_no = wo.get("wo_no")
        existing = get_work_order_by_no(conn, wo_no) if wo_no else None

        status = "new"
        reason = "新建"
        if existing:
            if existing["status"] == wo.get("status"):
                status = "same"
                reason = "状态一致，跳过"
            else:
                status = "update"
                reason = f"状态变化: {WO_STATUS_LABELS.get(existing['status'], existing['status'])} → {WO_STATUS_LABELS.get(wo.get('status'), wo.get('status'))}"

        results.append({
            "wo_no": wo_no,
            "store_id": wo.get("store_id"),
            "barcode": wo.get("barcode"),
            "import_status": status,
            "reason": reason,
            "importing_data": wo,
            "existing_data": existing,
        })

    return {
        "success": True,
        "total": len(results),
        "new_count": sum(1 for r in results if r["import_status"] == "new"),
        "update_count": sum(1 for r in results if r["import_status"] == "update"),
        "same_count": sum(1 for r in results if r["import_status"] == "same"),
        "preview_results": results,
    }


def replay_work_order_statuses(conn, preview_results, operator="user"):
    updated_count = 0
    results = []

    for item in preview_results:
        if item["import_status"] == "same":
            results.append({
                "wo_no": item["wo_no"],
                "action": "skipped",
                "reason": "状态一致，跳过",
            })
            continue

        importing = item["importing_data"]
        wo_no = importing.get("wo_no")

        if item["import_status"] == "new":
            if not importing.get("discrepancy_id"):
                results.append({
                    "wo_no": wo_no,
                    "action": "skipped",
                    "reason": "新建工单需要关联差异ID，当前数据缺失",
                })
                continue
            r = create_work_order(
                conn,
                importing["discrepancy_id"],
                assignee=importing.get("assignee"),
                deadline=importing.get("deadline"),
                action_type=importing.get("action_type"),
                action_detail=importing.get("action_detail"),
                created_by=importing.get("created_by", operator),
            )
            if r["success"]:
                results.append({
                    "wo_no": r["wo_no"],
                    "action": "created",
                })
            else:
                results.append({
                    "wo_no": wo_no,
                    "action": "failed",
                    "reason": r.get("error", "创建失败"),
                })
            continue

        if item["import_status"] == "update":
            existing = item["existing_data"]
            target_status = importing.get("status")

            r = transition_work_order_status(
                conn, existing["id"], target_status,
                operator=operator, role=WO_ROLE_ADMIN,
                note=f"导入回放状态变更（来自 {importing.get('updated_at', '导入文件')}）"
            )
            if r["success"]:
                updated_count += 1
                results.append({
                    "wo_no": wo_no,
                    "action": "updated",
                    "from_status": r["from_status"],
                    "to_status": r["to_status"],
                })
            else:
                results.append({
                    "wo_no": wo_no,
                    "action": "failed",
                    "reason": r.get("error", "更新失败"),
                })

    return {
        "success": True,
        "total": len(results),
        "updated_count": updated_count,
        "created_count": sum(1 for r in results if r["action"] == "created"),
        "skipped_count": sum(1 for r in results if r["action"] == "skipped"),
        "failed_count": sum(1 for r in results if r["action"] == "failed"),
        "results": results,
    }


WO_UI_STATE_KEY = "work_order_ui_state"
WO_DRAFT_KEY = "work_order_draft"
WO_BATCH_SELECT_KEY = "work_order_batch_selection"


def save_wo_ui_state(conn, state):
    save_ui_state(conn, WO_UI_STATE_KEY, state)


def load_wo_ui_state(conn):
    return load_ui_state(conn, WO_UI_STATE_KEY, default={})


def save_wo_draft(conn, draft_data):
    save_ui_state(conn, WO_DRAFT_KEY, draft_data)


def load_wo_draft(conn):
    return load_ui_state(conn, WO_DRAFT_KEY, default=None)


def clear_wo_draft(conn):
    save_ui_state(conn, WO_DRAFT_KEY, None)


def save_wo_batch_selection(conn, selected_ids):
    save_ui_state(conn, WO_BATCH_SELECT_KEY, selected_ids)


def load_wo_batch_selection(conn):
    return load_ui_state(conn, WO_BATCH_SELECT_KEY, default=[])


HP_STATUS_PENDING_HANDOVER = "pending_handover"
HP_STATUS_RECEIVED = "received"
HP_STATUS_PROCESSING = "processing"
HP_STATUS_COMPLETED = "completed"
HP_STATUS_WITHDRAWN = "withdrawn"

HP_STATUS_LABELS = {
    HP_STATUS_PENDING_HANDOVER: "待交接",
    HP_STATUS_RECEIVED: "已接收",
    HP_STATUS_PROCESSING: "处理中",
    HP_STATUS_COMPLETED: "已完成",
    HP_STATUS_WITHDRAWN: "已撤回",
}

HP_VALID_TRANSITIONS = {
    HP_STATUS_PENDING_HANDOVER: [HP_STATUS_RECEIVED, HP_STATUS_WITHDRAWN],
    HP_STATUS_RECEIVED: [HP_STATUS_PROCESSING],
    HP_STATUS_PROCESSING: [HP_STATUS_COMPLETED],
    HP_STATUS_COMPLETED: [],
    HP_STATUS_WITHDRAWN: [],
}

HP_ROLE_ADMIN = "admin"
HP_ROLE_NORMAL = "normal"

HP_ACTION_LABELS = {
    "create": "建包",
    "receive": "接收",
    "withdraw": "撤回",
    "status_change": "状态变更",
    "export": "导出",
    "import_preview": "导回预检",
    "import_confirm": "导回确认",
    "update": "编辑",
    "complete": "完成",
    "blocked_operation": "阻止操作",
}


def _migrate_handover_packages(conn):
    hp_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='handover_packages'"
    ).fetchone()
    if not hp_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS handover_packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pkg_no TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                description TEXT,
                receiver TEXT,
                handover_note TEXT,
                status TEXT NOT NULL DEFAULT 'pending_handover',
                filter_snapshot TEXT,
                selected_ids_json TEXT,
                created_by TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                received_at TEXT,
                received_by TEXT,
                completed_at TEXT,
                withdrawn_at TEXT
            )
        """)

    hpi_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='handover_package_items'"
    ).fetchone()
    if not hpi_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS handover_package_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL,
                discrepancy_id INTEGER NOT NULL,
                store_id TEXT NOT NULL,
                barcode TEXT NOT NULL,
                sku_name TEXT,
                diff_qty REAL NOT NULL,
                attributed_cause TEXT,
                review_note TEXT,
                evidence_snapshot TEXT,
                disc_status TEXT,
                disc_updated_at TEXT,
                FOREIGN KEY (package_id) REFERENCES handover_packages(id),
                FOREIGN KEY (discrepancy_id) REFERENCES discrepancies(id)
            )
        """)

    hpl_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='handover_package_logs'"
    ).fetchone()
    if not hpl_exists:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS handover_package_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER NOT NULL DEFAULT 0,
                action_type TEXT NOT NULL,
                action_detail TEXT,
                operator TEXT NOT NULL DEFAULT 'user',
                operated_at TEXT NOT NULL
            )
        """)


def _generate_pkg_no(conn):
    now = datetime.now()
    date_str = now.strftime("%Y%m%d")
    prefix = f"HP{date_str}"
    row = conn.execute(
        "SELECT pkg_no FROM handover_packages WHERE pkg_no LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",)
    ).fetchone()
    if row:
        last_no = row["pkg_no"]
        seq = int(last_no[-4:]) + 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


def _log_handover_action(conn, package_id, action_type, action_detail=None,
                         operator="user"):
    now = now_iso()
    conn.execute(
        """INSERT INTO handover_package_logs
           (package_id, action_type, action_detail, operator, operated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (package_id, action_type, action_detail, operator, now),
    )


def create_handover_package(conn, title, discrepancy_ids, receiver=None,
                            handover_note=None, description=None,
                            filter_snapshot=None, created_by="user"):
    if not discrepancy_ids:
        return {"success": False, "error": "请至少选择一条差异记录"}

    pkg_no = _generate_pkg_no(conn)
    now = now_iso()

    selected_ids_json = json.dumps(discrepancy_ids, ensure_ascii=False)
    filter_json = json.dumps(filter_snapshot, ensure_ascii=False) if filter_snapshot else None

    conn.execute(
        """INSERT INTO handover_packages
           (pkg_no, title, description, receiver, handover_note, status,
            filter_snapshot, selected_ids_json, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'pending_handover', ?, ?, ?, ?, ?)""",
        (pkg_no, title, description, receiver, handover_note,
         filter_json, selected_ids_json, created_by, now, now),
    )
    row = conn.execute("SELECT * FROM handover_packages WHERE pkg_no = ?", (pkg_no,)).fetchone()
    pkg_id = row["id"]

    for disc_id in discrepancy_ids:
        disc = conn.execute("SELECT * FROM discrepancies WHERE id = ?", (disc_id,)).fetchone()
        if not disc:
            continue
        disc = dict(disc)

        evidence = get_evidence_for_discrepancy(conn, disc_id)
        evidence_json = json.dumps(evidence, ensure_ascii=False, default=str)

        conn.execute(
            """INSERT INTO handover_package_items
               (package_id, discrepancy_id, store_id, barcode, sku_name, diff_qty,
                attributed_cause, review_note, evidence_snapshot, disc_status, disc_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pkg_id, disc_id, disc["store_id"], disc["barcode"],
             disc.get("sku_name"), disc["diff_qty"], disc.get("attributed_cause"),
             disc.get("review_note"), evidence_json, disc["status"], disc["updated_at"]),
        )

    _log_handover_action(conn, pkg_id, "create",
                         f"创建交接包，包含 {len(discrepancy_ids)} 条差异", created_by)

    return {"success": True, "package_id": pkg_id, "pkg_no": pkg_no}


def get_handover_package(conn, pkg_id):
    row = conn.execute(
        "SELECT * FROM handover_packages WHERE id = ?", (pkg_id,)
    ).fetchone()
    if not row:
        return None
    pkg = dict(row)
    items = conn.execute(
        "SELECT * FROM handover_package_items WHERE package_id = ?", (pkg_id,)
    ).fetchall()
    pkg["items"] = [dict(it) for it in items]
    return pkg


def get_handover_package_by_no(conn, pkg_no):
    row = conn.execute(
        "SELECT * FROM handover_packages WHERE pkg_no = ?", (pkg_no,)
    ).fetchone()
    if not row:
        return None
    return dict(row)


def list_handover_packages(conn, store_id=None, status=None, receiver=None,
                           keyword=None):
    sql = "SELECT * FROM handover_packages WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if receiver:
        sql += " AND receiver = ?"
        params.append(receiver)
    if keyword:
        sql += " AND (pkg_no LIKE ? OR title LIKE ? OR receiver LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    packages = [dict(r) for r in rows]

    if store_id:
        filtered = []
        for pkg in packages:
            items = conn.execute(
                "SELECT id FROM handover_package_items WHERE package_id = ? AND store_id = ? LIMIT 1",
                (pkg["id"], store_id)
            ).fetchone()
            if items:
                filtered.append(pkg)
        packages = filtered

    return packages


def get_handover_package_items(conn, pkg_id):
    rows = conn.execute(
        "SELECT * FROM handover_package_items WHERE package_id = ?", (pkg_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def transition_handover_status(conn, pkg_id, to_status, operator="user",
                               role=HP_ROLE_ADMIN, note=None):
    pkg = get_handover_package(conn, pkg_id)
    if not pkg:
        return {"success": False, "error": f"交接包不存在: {pkg_id}"}

    from_status = pkg["status"]

    if from_status == HP_STATUS_COMPLETED:
        _log_handover_action(
            conn, pkg_id, "blocked_operation",
            f"阻止操作: 尝试修改已完成的交接包，操作=状态变更→{HP_STATUS_LABELS.get(to_status, to_status)}",
            operator
        )
        return {"success": False, "error": "已完成的交接包不允许再变更状态"}

    if from_status == HP_STATUS_WITHDRAWN:
        _log_handover_action(
            conn, pkg_id, "blocked_operation",
            f"阻止操作: 尝试修改已撤回的交接包，操作=状态变更→{HP_STATUS_LABELS.get(to_status, to_status)}",
            operator
        )
        return {"success": False, "error": "已撤回的交接包不允许再变更状态"}

    if to_status not in HP_VALID_TRANSITIONS.get(from_status, []):
        _log_handover_action(
            conn, pkg_id, "blocked_operation",
            f"阻止操作: 无效的状态跳转，{HP_STATUS_LABELS.get(from_status, from_status)}→{HP_STATUS_LABELS.get(to_status, to_status)}",
            operator
        )
        return {
            "success": False,
            "error": f"不允许从 '{HP_STATUS_LABELS.get(from_status, from_status)}' "
                     f"转换到 '{HP_STATUS_LABELS.get(to_status, to_status)}'"
        }

    if to_status == HP_STATUS_WITHDRAWN:
        if role != HP_ROLE_ADMIN and pkg["created_by"] != operator:
            _log_handover_action(
                conn, pkg_id, "blocked_operation",
                f"阻止操作: 普通角色尝试撤回他人创建的交接包，创建者={pkg['created_by']}",
                operator
            )
            return {
                "success": False,
                "error": "普通角色只能撤回自己创建的交接包",
            }

    if to_status == HP_STATUS_RECEIVED:
        if pkg.get("received_by") and pkg["received_by"] != operator and role != HP_ROLE_ADMIN:
            _log_handover_action(
                conn, pkg_id, "blocked_operation",
                f"阻止操作: 普通角色尝试接管别人已接收的包，当前接收人={pkg['received_by']}",
                operator
            )
            return {
                "success": False,
                "error": f"该交接包已由 '{pkg['received_by']}' 接收，普通角色不能接管别人已接收的包",
            }
        if role != HP_ROLE_ADMIN and pkg.get("receiver") and pkg["receiver"] != operator:
            _log_handover_action(
                conn, pkg_id, "blocked_operation",
                f"阻止操作: 无权限接收，指定接手人={pkg['receiver']}",
                operator
            )
            return {
                "success": False,
                "error": f"该交接包指定接手人为 '{pkg['receiver']}'，您无权接收",
            }

    if to_status == HP_STATUS_PROCESSING or to_status == HP_STATUS_COMPLETED:
        if pkg.get("received_by") and pkg["received_by"] != operator and role != HP_ROLE_ADMIN:
            _log_handover_action(
                conn, pkg_id, "blocked_operation",
                f"阻止操作: 普通角色尝试处理别人已接收的包，当前接收人={pkg['received_by']}",
                operator
            )
            return {
                "success": False,
                "error": f"该交接包已由 '{pkg['received_by']}' 接收，普通角色不能处理别人已接收的包",
            }

    now = now_iso()
    updates = {"status": to_status, "updated_at": now}
    if to_status == HP_STATUS_RECEIVED:
        updates["received_at"] = now
        updates["received_by"] = operator
    elif to_status == HP_STATUS_COMPLETED:
        updates["completed_at"] = now
    elif to_status == HP_STATUS_WITHDRAWN:
        updates["withdrawn_at"] = now

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    params = list(updates.values())
    params.append(pkg_id)

    conn.execute(
        f"UPDATE handover_packages SET {set_clause} WHERE id = ?",
        params
    )

    detail = f"状态变更: {HP_STATUS_LABELS.get(from_status, from_status)} → {HP_STATUS_LABELS.get(to_status, to_status)}"
    if note:
        detail += f"，备注: {note}"
    action_type = "status_change"
    if to_status == HP_STATUS_RECEIVED:
        action_type = "receive"
    elif to_status == HP_STATUS_WITHDRAWN:
        action_type = "withdraw"
    elif to_status == HP_STATUS_COMPLETED:
        action_type = "complete"
    _log_handover_action(conn, pkg_id, action_type, detail, operator)

    return {"success": True, "from_status": from_status, "to_status": to_status}


def update_handover_package(conn, pkg_id, updates, operator="user", role=HP_ROLE_ADMIN):
    pkg = get_handover_package(conn, pkg_id)
    if not pkg:
        return {"success": False, "error": f"交接包不存在: {pkg_id}"}

    if pkg["status"] == HP_STATUS_COMPLETED:
        _log_handover_action(
            conn, pkg_id, "blocked_operation",
            f"阻止操作: 尝试编辑已完成的交接包，修改字段={list(updates.keys())}",
            operator
        )
        return {"success": False, "error": "已完成的交接包不允许再编辑"}

    if pkg["status"] == HP_STATUS_WITHDRAWN:
        _log_handover_action(
            conn, pkg_id, "blocked_operation",
            f"阻止操作: 尝试编辑已撤回的交接包，修改字段={list(updates.keys())}",
            operator
        )
        return {"success": False, "error": "已撤回的交接包不允许再编辑"}

    if pkg["status"] == HP_STATUS_RECEIVED:
        if role != HP_ROLE_ADMIN and pkg.get("received_by") and pkg["received_by"] != operator:
            _log_handover_action(
                conn, pkg_id, "blocked_operation",
                f"阻止操作: 普通角色尝试编辑别人已接收的交接包，接收人={pkg['received_by']}，修改字段={list(updates.keys())}",
                operator
            )
            return {"success": False, "error": "普通角色不能编辑别人已接收的交接包"}

    if role == HP_ROLE_NORMAL and pkg.get("receiver") and pkg["receiver"] != operator:
        if pkg["status"] == HP_STATUS_PENDING_HANDOVER:
            if "receiver" in updates:
                new_receiver = updates.get("receiver")
                if new_receiver and new_receiver != pkg["receiver"]:
                    _log_handover_action(
                        conn, pkg_id, "blocked_operation",
                        f"阻止操作: 普通角色尝试修改指定接手人的交接包，原接手人={pkg['receiver']}，新接手人={new_receiver}",
                        operator
                    )
                    return {"success": False, "error": "普通角色不能修改已指定接手人的交接包的接手人"}

    allowed_fields = ["title", "description", "receiver", "handover_note"]
    update_dict = {k: v for k, v in updates.items() if k in allowed_fields and v is not None}

    if not update_dict:
        return {"success": False, "error": "没有有效的更新字段"}

    old_values = {k: pkg.get(k) for k in update_dict.keys()}
    set_clause = ", ".join(f"{k} = ?" for k in update_dict.keys())
    params = list(update_dict.values())
    params.extend([now_iso(), pkg_id])

    conn.execute(
        f"UPDATE handover_packages SET {set_clause}, updated_at = ? WHERE id = ?",
        params
    )

    change_details = []
    for k in update_dict.keys():
        old_v = old_values.get(k)
        new_v = update_dict.get(k)
        if old_v != new_v:
            change_details.append(f"{k}: '{old_v}' → '{new_v}'")

    detail = "更新字段: " + ", ".join(update_dict.keys())
    if change_details:
        detail += "，变更详情: " + "; ".join(change_details)
    _log_handover_action(conn, pkg_id, "update", detail, operator)

    return {"success": True, "updated_fields": list(update_dict.keys()), "changes": change_details}


def get_handover_package_logs(conn, pkg_id, limit=100):
    rows = conn.execute(
        "SELECT * FROM handover_package_logs WHERE package_id = ? ORDER BY operated_at DESC LIMIT ?",
        (pkg_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_handover_logs(conn, operator=None, action_type=None, limit=200):
    sql = "SELECT l.*, hp.pkg_no FROM handover_package_logs l LEFT JOIN handover_packages hp ON l.package_id = hp.id WHERE 1=1"
    params = []
    if operator:
        sql += " AND l.operator = ?"
        params.append(operator)
    if action_type:
        sql += " AND l.action_type = ?"
        params.append(action_type)
    sql += " ORDER BY l.operated_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_hp_stores(conn):
    rows = conn.execute(
        "SELECT DISTINCT store_id FROM handover_package_items ORDER BY store_id"
    ).fetchall()
    return [r["store_id"] for r in rows if r["store_id"]]


def get_hp_receivers(conn):
    rows = conn.execute(
        "SELECT DISTINCT receiver FROM handover_packages WHERE receiver IS NOT NULL ORDER BY receiver"
    ).fetchall()
    return [r["receiver"] for r in rows if r["receiver"]]


def export_handover_packages_json(conn, store_id=None, status=None, receiver=None, package_ids=None):
    if package_ids is not None:
        packages = []
        for pid in package_ids:
            pkg = get_handover_package(conn, pid)
            if pkg:
                packages.append(pkg)
    else:
        packages = list_handover_packages(conn, store_id=store_id, status=status, receiver=receiver)

    export_packages = []
    for pkg in packages:
        items = get_handover_package_items(conn, pkg["id"])
        for item in items:
            try:
                if item.get("evidence_snapshot") and isinstance(item["evidence_snapshot"], str):
                    item["evidence_snapshot"] = json.loads(item["evidence_snapshot"])
            except (TypeError, json.JSONDecodeError):
                pass

        pkg_copy = dict(pkg)
        pkg_copy["items"] = items
        logs = get_handover_package_logs(conn, pkg["id"])
        pkg_copy["logs"] = logs

        try:
            if pkg_copy.get("filter_snapshot") and isinstance(pkg_copy["filter_snapshot"], str):
                pkg_copy["filter_snapshot"] = json.loads(pkg_copy["filter_snapshot"])
        except (TypeError, json.JSONDecodeError):
            pass
        try:
            if pkg_copy.get("selected_ids_json") and isinstance(pkg_copy["selected_ids_json"], str):
                pkg_copy["selected_ids_json"] = json.loads(pkg_copy["selected_ids_json"])
        except (TypeError, json.JSONDecodeError):
            pass

        export_packages.append(pkg_copy)

    now = now_iso()
    detail = f"导出交接包: 共 {len(export_packages)} 个"
    conn.execute(
        """INSERT INTO handover_package_logs
           (package_id, action_type, action_detail, operator, operated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (0, "export", detail, "system", now),
    )
    return {
        "export_version": "2.0",
        "export_type": "handover_packages",
        "exported_at": now,
        "generator": "handover_acceptance_desk",
        "filter": {
            "store_id": store_id,
            "status": status,
            "receiver": receiver,
            "package_ids": package_ids,
        },
        "total": len(export_packages),
        "handover_packages": export_packages,
    }


def group_conflicts_by_type(preview_results):
    grouped = {
        "local_newer": [],
        "receiver_mismatch": [],
        "note_changed": [],
        "status_changed": [],
        "already_completed": [],
        "already_withdrawn": [],
        "discrepancy_missing": [],
        "other": [],
    }

    for result in preview_results:
        if result.get("import_status") != "conflict":
            continue
        for conflict in result.get("conflicts", []):
            ctype = conflict.get("type", "other")
            conflict_entry = {
                "pkg_no": result.get("pkg_no"),
                "pkg_title": result.get("title"),
                "discrepancy_id": conflict.get("discrepancy_id"),
                "message": conflict.get("message"),
                "conflict_type": ctype,
            }
            if ctype in grouped:
                grouped[ctype].append(conflict_entry)
            else:
                grouped["other"].append(conflict_entry)

    summary = {}
    for k, v in grouped.items():
        summary[k] = len(v)

    return {
        "groups": grouped,
        "summary": summary,
        "total_conflicts": sum(len(v) for v in grouped.values()),
    }


def export_conflicts_json(preview_results, grouped=None):
    if grouped is None:
        grouped = group_conflicts_by_type(preview_results)

    export_data = {
        "export_version": "1.0",
        "export_type": "handover_conflicts",
        "exported_at": now_iso(),
        "conflict_groups": grouped["groups"],
        "summary": grouped["summary"],
        "total_conflicts": grouped["total_conflicts"],
        "raw_preview_results": preview_results,
    }
    return json.dumps(export_data, ensure_ascii=False, indent=2)


def export_conflicts_csv(preview_results, grouped=None):
    import csv
    import io

    if grouped is None:
        grouped = group_conflicts_by_type(preview_results)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "冲突类型", "交接包编号", "交接包标题", "差异记录ID", "冲突详情"
    ])

    type_labels = {
        "local_newer": "本地状态更新",
        "receiver_mismatch": "接手人不一致",
        "note_changed": "复核备注已修改",
        "status_changed": "状态不一致",
        "already_completed": "本地已完成",
        "already_withdrawn": "本地已撤回",
        "discrepancy_missing": "差异记录不存在",
        "other": "其他冲突",
    }

    for ctype, entries in grouped["groups"].items():
        for entry in entries:
            writer.writerow([
                type_labels.get(ctype, ctype),
                entry.get("pkg_no", ""),
                entry.get("pkg_title", ""),
                entry.get("discrepancy_id", ""),
                entry.get("message", ""),
            ])

    output.seek(0)
    return output.read()


def preview_handover_packages_import(conn, import_data):
    if not isinstance(import_data, dict):
        return {"success": False, "error": "导入数据格式错误"}

    if import_data.get("export_type") != "handover_packages":
        return {"success": False, "error": "不是交接包导出文件"}

    packages = import_data.get("handover_packages", [])
    if not packages:
        return {"success": False, "error": "导入数据中没有交接包记录"}

    results = []
    for pkg in packages:
        pkg_no = pkg.get("pkg_no")
        existing = get_handover_package_by_no(conn, pkg_no) if pkg_no else None

        if not existing:
            results.append({
                "pkg_no": pkg_no,
                "title": pkg.get("title"),
                "import_status": "new",
                "conflicts": [],
                "reason": "新建",
                "package_data": pkg,
            })
            continue

        conflicts = []

        if existing["status"] == HP_STATUS_COMPLETED:
            conflicts.append({
                "type": "already_completed",
                "message": f"本地已为「已完成」状态，不允许覆盖",
                "blocking": True,
            })

        if existing["status"] == HP_STATUS_WITHDRAWN:
            conflicts.append({
                "type": "already_withdrawn",
                "message": f"本地已为「已撤回」状态，不允许覆盖",
                "blocking": True,
            })

        import_items = pkg.get("items", [])
        for imp_item in import_items:
            disc_id = imp_item.get("discrepancy_id")
            if not disc_id:
                continue
            local_disc = conn.execute(
                "SELECT * FROM discrepancies WHERE id = ?", (disc_id,)
            ).fetchone()
            if not local_disc:
                conflicts.append({
                    "type": "discrepancy_missing",
                    "message": f"差异记录 {disc_id} 在本地不存在",
                    "discrepancy_id": disc_id,
                    "blocking": True,
                })
                continue

            local_disc = dict(local_disc)
            snap_updated = imp_item.get("disc_updated_at")
            if snap_updated and local_disc["updated_at"] > snap_updated:
                conflicts.append({
                    "type": "local_newer",
                    "message": f"差异 {disc_id} 本地状态更晚（本地: {local_disc['updated_at'][:19]}，快照: {snap_updated[:19]}）",
                    "discrepancy_id": disc_id,
                    "blocking": True,
                    "local_value": local_disc["updated_at"],
                    "import_value": snap_updated,
                })

            snap_note = imp_item.get("review_note")
            if snap_note and local_disc.get("review_note") and snap_note != local_disc["review_note"]:
                conflicts.append({
                    "type": "note_changed",
                    "message": f"差异 {disc_id} 复核备注已被修改",
                    "discrepancy_id": disc_id,
                    "blocking": False,
                    "local_value": local_disc.get("review_note", ""),
                    "import_value": snap_note,
                })

            snap_status = imp_item.get("disc_status")
            if snap_status and local_disc["status"] != snap_status:
                conflicts.append({
                    "type": "status_changed",
                    "message": f"差异 {disc_id} 状态不一致（本地: {STATUS_LABELS.get(local_disc['status'], local_disc['status'])}，快照: {STATUS_LABELS.get(snap_status, snap_status)}）",
                    "discrepancy_id": disc_id,
                    "blocking": True,
                    "local_value": local_disc["status"],
                    "import_value": snap_status,
                })

        if existing.get("receiver") and pkg.get("receiver") and existing["receiver"] != pkg.get("receiver"):
            conflicts.append({
                "type": "receiver_mismatch",
                "message": f"接手人不一致（本地: {existing['receiver']}，导入: {pkg.get('receiver', '无')}）",
                "blocking": False,
                "local_value": existing["receiver"],
                "import_value": pkg.get("receiver"),
            })

        has_blocking = any(c.get("blocking", True) for c in conflicts)

        if conflicts:
            results.append({
                "pkg_no": pkg_no,
                "title": pkg.get("title"),
                "import_status": "conflict",
                "conflicts": conflicts,
                "reason": f"存在 {len(conflicts)} 个冲突（{sum(1 for c in conflicts if c.get('blocking', True))} 个阻塞）",
                "has_blocking": has_blocking,
                "package_data": pkg,
                "local_package": existing,
            })
        else:
            results.append({
                "pkg_no": pkg_no,
                "title": pkg.get("title"),
                "import_status": "safe",
                "conflicts": [],
                "reason": "可安全接收",
                "has_blocking": False,
                "package_data": pkg,
                "local_package": existing,
            })

    grouped = group_conflicts_by_type(results)

    save_hp_import_preview(conn, {
        "preview_results": results,
        "import_data": import_data,
        "grouped_conflicts": grouped,
        "previewed_at": now_iso(),
    })

    return {
        "success": True,
        "total": len(results),
        "new_count": sum(1 for r in results if r["import_status"] == "new"),
        "safe_count": sum(1 for r in results if r["import_status"] == "safe"),
        "conflict_count": sum(1 for r in results if r["import_status"] == "conflict"),
        "preview_results": results,
        "import_data": import_data,
        "grouped_conflicts": grouped,
    }


def generate_import_receipt_scheme(conn, preview_results, import_data):
    packages = import_data.get("handover_packages", [])
    pkg_map = {p.get("pkg_no"): p for p in packages}

    scheme_items = []
    for item in preview_results:
        pkg_no = item["pkg_no"]
        imp_status = item["import_status"]
        pkg_data = pkg_map.get(pkg_no)

        scheme_item = {
            "pkg_no": pkg_no,
            "title": item.get("title"),
            "import_status": imp_status,
            "action": None,
            "reason": None,
            "can_import": False,
            "details": {},
        }

        if imp_status == "new":
            disc_ids = [it.get("discrepancy_id") for it in (pkg_data or {}).get("items", []) if it.get("discrepancy_id")]
            valid_disc_ids = []
            missing_discs = []
            for did in disc_ids:
                exists = conn.execute("SELECT 1 FROM discrepancies WHERE id = ?", (did,)).fetchone()
                if exists:
                    valid_disc_ids.append(did)
                else:
                    missing_discs.append(did)

            if valid_disc_ids and not missing_discs:
                scheme_item["action"] = "create"
                scheme_item["can_import"] = True
                scheme_item["reason"] = f"新建交接包，包含 {len(valid_disc_ids)} 条差异"
                scheme_item["details"] = {
                    "discrepancy_count": len(valid_disc_ids),
                    "discrepancy_ids": valid_disc_ids,
                    "target_status": HP_STATUS_PENDING_HANDOVER,
                }
            else:
                scheme_item["action"] = "skip"
                scheme_item["can_import"] = False
                scheme_item["reason"] = f"差异记录缺失: {missing_discs}" if missing_discs else "没有有效的差异记录"
                scheme_item["details"] = {
                    "missing_discrepancies": missing_discs,
                }

        elif imp_status == "safe":
            existing = item.get("local_package")
            if existing:
                target_status = (pkg_data or {}).get("status")
                if target_status and target_status != existing["status"]:
                    if target_status in HP_VALID_TRANSITIONS.get(existing["status"], []):
                        scheme_item["action"] = "update"
                        scheme_item["can_import"] = True
                        scheme_item["reason"] = f"状态变更: {HP_STATUS_LABELS.get(existing['status'], existing['status'])} → {HP_STATUS_LABELS.get(target_status, target_status)}"
                        scheme_item["details"] = {
                            "from_status": existing["status"],
                            "to_status": target_status,
                            "package_id": existing["id"],
                        }
                    else:
                        scheme_item["action"] = "skip"
                        scheme_item["can_import"] = False
                        scheme_item["reason"] = f"无效的状态跳转: {HP_STATUS_LABELS.get(existing['status'], existing['status'])} → {HP_STATUS_LABELS.get(target_status, target_status)}"
                else:
                    scheme_item["action"] = "skip"
                    scheme_item["can_import"] = True
                    scheme_item["reason"] = "状态一致，无需变更"
            else:
                scheme_item["action"] = "create"
                scheme_item["can_import"] = True
                scheme_item["reason"] = "本地不存在，按新建处理"

        elif imp_status == "conflict":
            has_blocking = any(c.get("blocking", True) for c in item.get("conflicts", []))
            scheme_item["action"] = "skip"
            scheme_item["can_import"] = not has_blocking
            scheme_item["reason"] = item.get("reason", "存在冲突")
            scheme_item["details"] = {
                "conflict_count": len(item.get("conflicts", [])),
                "blocking_count": sum(1 for c in item.get("conflicts", []) if c.get("blocking", True)),
                "conflicts": item.get("conflicts", []),
            }

        scheme_items.append(scheme_item)

    return {
        "success": True,
        "total": len(scheme_items),
        "can_import_count": sum(1 for s in scheme_items if s["can_import"]),
        "scheme_items": scheme_items,
    }


def confirm_handover_packages_import(conn, preview_results, import_data, operator="user", selected_indices=None):
    packages = import_data.get("handover_packages", [])
    pkg_map = {p.get("pkg_no"): p for p in packages}

    receipt_scheme = generate_import_receipt_scheme(conn, preview_results, import_data)
    if not receipt_scheme["success"]:
        return receipt_scheme

    scheme_items = receipt_scheme["scheme_items"]

    if selected_indices is not None:
        selected_set = set(selected_indices)
        scheme_items = [s for i, s in enumerate(scheme_items) if i in selected_set]

    created_count = 0
    received_count = 0
    skipped_count = 0
    failed_count = 0
    results = []

    for scheme_item in scheme_items:
        pkg_no = scheme_item["pkg_no"]

        if not scheme_item["can_import"]:
            results.append({
                "pkg_no": pkg_no,
                "action": "skipped",
                "reason": scheme_item["reason"],
            })
            skipped_count += 1
            continue

        action = scheme_item["action"]
        pkg_data = pkg_map.get(pkg_no)

        if action == "create":
            disc_ids = scheme_item["details"].get("discrepancy_ids", [])
            filter_snap = (pkg_data or {}).get("filter_snapshot")
            if isinstance(filter_snap, str):
                try:
                    filter_snap = json.loads(filter_snap)
                except (TypeError, json.JSONDecodeError):
                    filter_snap = None

            r = create_handover_package(
                conn,
                title=scheme_item.get("title") or pkg_data.get("title", pkg_no),
                discrepancy_ids=disc_ids,
                receiver=(pkg_data or {}).get("receiver"),
                handover_note=(pkg_data or {}).get("handover_note"),
                description=(pkg_data or {}).get("description"),
                filter_snapshot=filter_snap,
                created_by=(pkg_data or {}).get("created_by", operator),
            )
            if r["success"]:
                created_count += 1
                results.append({
                    "pkg_no": pkg_no,
                    "action": "created",
                    "new_pkg_no": r["pkg_no"],
                    "package_id": r["package_id"],
                })

                pkg_import_status = (pkg_data or {}).get("status")
                if pkg_import_status and pkg_import_status != HP_STATUS_PENDING_HANDOVER:
                    r2 = transition_handover_status(
                        conn, r["package_id"], pkg_import_status,
                        operator=operator, role=HP_ROLE_ADMIN,
                        note=f"导回确认，同步原始状态",
                    )
                    if r2["success"]:
                        results[-1]["status_transition"] = f"{HP_STATUS_PENDING_HANDOVER} → {pkg_import_status}"
            else:
                failed_count += 1
                results.append({
                    "pkg_no": pkg_no,
                    "action": "failed",
                    "reason": r.get("error", "创建失败"),
                })
            continue

        if action == "update":
            package_id = scheme_item["details"].get("package_id")
            target_status = scheme_item["details"].get("to_status")
            if not package_id or not target_status:
                results.append({
                    "pkg_no": pkg_no,
                    "action": "skipped",
                    "reason": "缺少更新参数",
                })
                skipped_count += 1
                continue

            r = transition_handover_status(
                conn, package_id, target_status,
                operator=operator, role=HP_ROLE_ADMIN,
                note=f"导回确认状态变更（接收方案预校验通过）",
            )
            if r["success"]:
                received_count += 1
                results.append({
                    "pkg_no": pkg_no,
                    "action": "updated",
                    "from_status": r["from_status"],
                    "to_status": r["to_status"],
                    "package_id": package_id,
                })
            else:
                failed_count += 1
                results.append({
                    "pkg_no": pkg_no,
                    "action": "failed",
                    "reason": f"状态跳转失败: {r.get('error', '未知错误')}",
                })
            continue

        if action == "skip":
            results.append({
                "pkg_no": pkg_no,
                "action": "skipped",
                "reason": scheme_item["reason"],
            })
            skipped_count += 1
            continue

    _log_handover_action_import_summary(conn, operator, created_count, received_count,
                                        skipped_count, failed_count)

    clear_hp_import_session(conn)

    return {
        "success": True,
        "total": len(results),
        "created_count": created_count,
        "received_count": received_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "results": results,
        "receipt_scheme": receipt_scheme,
    }


def _log_handover_action_import_summary(conn, operator, created, received, skipped, failed):
    now = now_iso()
    detail = f"导回结果: 新建 {created}, 接收 {received}, 跳过 {skipped}, 失败 {failed}"
    conn.execute(
        """INSERT INTO handover_package_logs
           (package_id, action_type, action_detail, operator, operated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (0, "import_confirm", detail, operator, now),
    )


HP_UI_STATE_KEY = "handover_ui_state"
HP_DRAFT_KEY = "handover_draft"
HP_BATCH_SELECT_KEY = "handover_batch_selection"
HP_LAST_IMPORT_PREVIEW_KEY = "handover_last_import_preview"
HP_IMPORT_SESSION_KEY = "handover_import_session"


def save_hp_ui_state(conn, state):
    save_ui_state(conn, HP_UI_STATE_KEY, state)


def load_hp_ui_state(conn):
    return load_ui_state(conn, HP_UI_STATE_KEY, default={})


def save_hp_draft(conn, draft_data, strict=True):
    if not isinstance(draft_data, dict):
        raise ValueError("草稿数据必须为字典类型")

    has_title = bool(draft_data.get("title", "").strip())
    has_selections = bool(draft_data.get("selected_ids"))

    if strict:
        required_fields = ["title", "selected_ids"]
        for field in required_fields:
            if field not in draft_data:
                raise ValueError(f"草稿缺少必填字段: {field}")
        if not draft_data.get("title", "").strip():
            raise ValueError("请输入交接包标题")
        if not has_selections:
            raise ValueError("请至少选择一条差异记录")
    else:
        if has_title and not has_selections:
            raise ValueError("请至少选择一条差异记录")

    now = now_iso()
    existing_draft = load_hp_draft(conn) or {}
    draft_to_save = {
        "title": draft_data.get("title", existing_draft.get("title", "")).strip(),
        "receiver": draft_data.get("receiver", existing_draft.get("receiver")),
        "handover_note": draft_data.get("handover_note", existing_draft.get("handover_note", "")),
        "description": draft_data.get("description", existing_draft.get("description", "")),
        "filter_snapshot": draft_data.get("filter_snapshot", existing_draft.get("filter_snapshot", {})),
        "selected_ids": draft_data.get("selected_ids", existing_draft.get("selected_ids", [])),
        "evidence_summary": draft_data.get("evidence_summary", existing_draft.get("evidence_summary", {})),
        "created_at": draft_data.get("created_at", existing_draft.get("created_at", now)),
        "updated_at": now,
    }

    disc_ids = draft_to_save["selected_ids"]
    evidence_summary = {}
    for disc_id in disc_ids:
        disc = conn.execute("SELECT * FROM discrepancies WHERE id = ?", (disc_id,)).fetchone()
        if disc:
            disc = dict(disc)
            evidence = get_evidence_for_discrepancy(conn, disc_id)
            ev_parts = []
            for ev in evidence:
                tl = {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}.get(
                    ev.get("source_type", ""), ev.get("source_type", "")
                )
                ev_parts.append(f"[{tl}] 行{ev.get('source_line', '?')}: {ev.get('description', '')}")
            evidence_summary[str(disc_id)] = {
                "store_id": disc["store_id"],
                "barcode": disc["barcode"],
                "sku_name": disc.get("sku_name"),
                "diff_qty": disc["diff_qty"],
                "attributed_cause": disc.get("attributed_cause"),
                "review_note": disc.get("review_note"),
                "evidence_count": len(evidence),
                "evidence_summary": " | ".join(ev_parts) if ev_parts else "",
                "disc_status": disc["status"],
                "disc_updated_at": disc["updated_at"],
            }
    draft_to_save["evidence_summary"] = evidence_summary

    save_ui_state(conn, HP_DRAFT_KEY, draft_to_save)
    return {"success": True, "draft": draft_to_save}


def load_hp_draft(conn):
    draft = load_ui_state(conn, HP_DRAFT_KEY, default=None)
    if draft and isinstance(draft, dict):
        return draft
    return None


def clear_hp_draft(conn):
    save_ui_state(conn, HP_DRAFT_KEY, None)
    return {"success": True}


def save_hp_batch_selection(conn, selected_ids):
    save_ui_state(conn, HP_BATCH_SELECT_KEY, selected_ids)


def load_hp_batch_selection(conn):
    return load_ui_state(conn, HP_BATCH_SELECT_KEY, default=[])


def save_hp_import_preview(conn, preview_data):
    save_ui_state(conn, HP_LAST_IMPORT_PREVIEW_KEY, preview_data)


def load_hp_import_preview(conn):
    return load_ui_state(conn, HP_LAST_IMPORT_PREVIEW_KEY, default=None)


def save_hp_import_session(conn, session_data):
    save_ui_state(conn, HP_IMPORT_SESSION_KEY, session_data)


def load_hp_import_session(conn):
    return load_ui_state(conn, HP_IMPORT_SESSION_KEY, default=None)


def clear_hp_import_session(conn):
    save_ui_state(conn, HP_IMPORT_SESSION_KEY, None)


def generate_handover_sample_data(conn):
    existing = list_handover_packages(conn)
    if existing:
        return

    discs = get_discrepancies(conn, status=STATUS_PENDING_REVIEW)
    if len(discs) < 2:
        discs = get_discrepancies(conn)
    if len(discs) < 2:
        return

    sample_ids = [d["id"] for d in discs[:3]]

    filter_snap = {
        "store_id": discs[0]["store_id"] if discs else None,
        "status": STATUS_PENDING_REVIEW,
    }

    r = create_handover_package(
        conn,
        title="早班遗留差异交接",
        discrepancy_ids=sample_ids[:2],
        receiver="night_shift_user",
        handover_note="早班未处理完的差异，请夜班继续跟进",
        description="早班遗留待复核差异",
        filter_snapshot=filter_snap,
        created_by="morning_shift_user",
    )
    if r["success"]:
        transition_handover_status(
            conn, r["package_id"], HP_STATUS_RECEIVED,
            operator="night_shift_user", role=HP_ROLE_ADMIN,
        )

    if len(sample_ids) >= 3:
        create_handover_package(
            conn,
            title="门店S001异常盘点交接",
            discrepancy_ids=[sample_ids[2]],
            receiver="day_shift_user",
            handover_note="需要日班确认处理",
            filter_snapshot={"store_id": "S001"},
            created_by="morning_shift_user",
        )


# ── Handover Package Shared Validation Layer ──

HP_VALIDATION_BLOCKED = "blocked"
HP_VALIDATION_WARNING = "warning"
HP_VALIDATION_OK = "ok"


def hp_check_terminal_status(pkg):
    return pkg.get("status") in (HP_STATUS_COMPLETED, HP_STATUS_WITHDRAWN)


def hp_validate_edit(pkg, operator=None, role=None):
    issues = []
    status = pkg.get("status")

    if status == HP_STATUS_COMPLETED:
        issues.append({
            "level": HP_VALIDATION_BLOCKED,
            "type": "already_completed",
            "message": "已完成的交接包不允许再编辑",
        })
        return {"allowed": False, "issues": issues}

    if status == HP_STATUS_WITHDRAWN:
        issues.append({
            "level": HP_VALIDATION_BLOCKED,
            "type": "already_withdrawn",
            "message": "已撤回的交接包不允许再编辑",
        })
        return {"allowed": False, "issues": issues}

    if role == HP_ROLE_NORMAL and status == HP_STATUS_RECEIVED:
        if pkg.get("received_by") and pkg["received_by"] != operator:
            issues.append({
                "level": HP_VALIDATION_BLOCKED,
                "type": "not_receiver",
                "message": "普通角色不能编辑别人已接收的交接包",
            })
            return {"allowed": False, "issues": issues}

    return {"allowed": True, "issues": issues}


def hp_validate_receiver_change(pkg, new_receiver, operator=None, role=None):
    issues = []
    if role == HP_ROLE_NORMAL and pkg.get("receiver") and new_receiver and new_receiver != pkg["receiver"]:
        if pkg["status"] == HP_STATUS_PENDING_HANDOVER:
            issues.append({
                "level": HP_VALIDATION_BLOCKED,
                "type": "receiver_immutable",
                "message": "普通角色不能修改已指定接手人的交接包的接手人",
            })
            return {"allowed": False, "issues": issues}
    return {"allowed": True, "issues": issues}


def hp_validate_transition(pkg, to_status, operator=None, role=None):
    issues = []
    from_status = pkg.get("status")

    if from_status == HP_STATUS_COMPLETED:
        issues.append({
            "level": HP_VALIDATION_BLOCKED,
            "type": "already_completed",
            "message": "已完成的交接包不允许再变更状态",
        })
        return {"allowed": False, "issues": issues}

    if from_status == HP_STATUS_WITHDRAWN:
        issues.append({
            "level": HP_VALIDATION_BLOCKED,
            "type": "already_withdrawn",
            "message": "已撤回的交接包不允许再变更状态",
        })
        return {"allowed": False, "issues": issues}

    if to_status not in HP_VALID_TRANSITIONS.get(from_status, []):
        issues.append({
            "level": HP_VALIDATION_BLOCKED,
            "type": "invalid_transition",
            "message": "不允许从 '{}' 转换到 '{}'".format(
                HP_STATUS_LABELS.get(from_status, from_status),
                HP_STATUS_LABELS.get(to_status, to_status),
            ),
        })
        return {"allowed": False, "issues": issues}

    if to_status == HP_STATUS_WITHDRAWN:
        if role != HP_ROLE_ADMIN and pkg.get("created_by") != operator:
            issues.append({
                "level": HP_VALIDATION_BLOCKED,
                "type": "not_creator",
                "message": "普通角色只能撤回自己创建的交接包",
            })
            return {"allowed": False, "issues": issues}

    if to_status == HP_STATUS_RECEIVED:
        if pkg.get("received_by") and pkg["received_by"] != operator and role != HP_ROLE_ADMIN:
            issues.append({
                "level": HP_VALIDATION_BLOCKED,
                "type": "already_received_other",
                "message": "普通角色不能接管别人已接收的包",
            })
            return {"allowed": False, "issues": issues}

        if role == HP_ROLE_NORMAL and pkg.get("receiver") and pkg["receiver"] != operator:
            issues.append({
                "level": HP_VALIDATION_BLOCKED,
                "type": "not_assigned_receiver",
                "message": "该交接包指定接手人为 '{}'，您无权接收".format(pkg["receiver"]),
            })
            return {"allowed": False, "issues": issues}

    if to_status in (HP_STATUS_PROCESSING, HP_STATUS_COMPLETED):
        if pkg.get("received_by") and pkg["received_by"] != operator and role != HP_ROLE_ADMIN:
            issues.append({
                "level": HP_VALIDATION_BLOCKED,
                "type": "not_receiver_process",
                "message": "普通角色不能处理别人已接收的包",
            })
            return {"allowed": False, "issues": issues}

    return {"allowed": True, "issues": issues}


def hp_validate_discrepancy_conflicts(conn, import_items, local_package=None):
    conflicts = []

    for imp_item in import_items:
        disc_id = imp_item.get("discrepancy_id")
        if not disc_id:
            continue

        local_disc = conn.execute(
            "SELECT * FROM discrepancies WHERE id = ?", (disc_id,)
        ).fetchone()
        if not local_disc:
            conflicts.append({
                "type": "discrepancy_missing",
                "message": "差异记录 {} 在本地不存在".format(disc_id),
                "discrepancy_id": disc_id,
                "blocking": True,
            })
            continue

        local_disc = dict(local_disc)
        snap_updated = imp_item.get("disc_updated_at")
        if snap_updated and local_disc["updated_at"] > snap_updated:
            conflicts.append({
                "type": "local_newer",
                "message": "差异 {} 本地状态更晚".format(disc_id),
                "discrepancy_id": disc_id,
                "blocking": True,
                "local_value": local_disc["updated_at"],
                "import_value": snap_updated,
            })

        snap_note = imp_item.get("review_note")
        if snap_note and local_disc.get("review_note") and snap_note != local_disc["review_note"]:
            conflicts.append({
                "type": "note_changed",
                "message": "差异 {} 复核备注已被修改".format(disc_id),
                "discrepancy_id": disc_id,
                "blocking": False,
                "local_value": local_disc.get("review_note"),
                "import_value": snap_note,
            })

        snap_status = imp_item.get("disc_status")
        if snap_status and local_disc["status"] != snap_status:
            conflicts.append({
                "type": "status_changed",
                "message": "差异 {} 状态不一致".format(disc_id),
                "discrepancy_id": disc_id,
                "blocking": True,
                "local_value": local_disc["status"],
                "import_value": snap_status,
            })

    return conflicts


def hp_validate_package_import_conflict(conn, existing_package, import_package):
    conflicts = []

    if existing_package:
        if existing_package["status"] == HP_STATUS_COMPLETED:
            conflicts.append({
                "type": "already_completed",
                "message": "本地已为「已完成」状态，不允许覆盖",
                "blocking": True,
            })
        if existing_package["status"] == HP_STATUS_WITHDRAWN:
            conflicts.append({
                "type": "already_withdrawn",
                "message": "本地已为「已撤回」状态，不允许覆盖",
                "blocking": True,
            })
        if existing_package.get("receiver") and import_package.get("receiver") and existing_package["receiver"] != import_package.get("receiver"):
            conflicts.append({
                "type": "receiver_mismatch",
                "message": "接手人不一致",
                "blocking": False,
            })

    import_items = import_package.get("items", [])
    conflicts.extend(hp_validate_discrepancy_conflicts(conn, import_items))

    return conflicts


def hp_prepare_view_data(conn, current_user=None, user_role=None):
    packages = list_handover_packages(conn)
    draft = load_hp_draft(conn)
    ui_state = load_hp_ui_state(conn)
    batch_selection = load_hp_batch_selection(conn)
    hp_date_range = get_hp_date_range(conn)
    data_date_range = get_date_range(conn)
    stores = get_hp_stores(conn)
    receivers = get_hp_receivers(conn)
    discrepancies = get_discrepancies(conn)

    has_packages = len(packages) > 0
    has_draft = draft is not None and len(draft.get("selected_ids", []))
    has_discrepancies = len(discrepancies) > 0

    if not has_packages and not has_discrepancies:
        startup_path = "empty"
    elif not has_packages and has_draft:
        startup_path = "draft_only"
    else:
        startup_path = "has_packages"

    pending_packages = [
        p for p in packages
        if p["status"] in (HP_STATUS_PENDING_HANDOVER, HP_STATUS_RECEIVED, HP_STATUS_PROCESSING)
        and (p.get("receiver") == current_user or user_role == HP_ROLE_ADMIN)
    ]

    return {
        "startup_path": startup_path,
        "packages": packages,
        "draft": draft,
        "ui_state": ui_state,
        "batch_selection": batch_selection,
        "hp_date_range": hp_date_range,
        "data_date_range": data_date_range,
        "stores": stores,
        "receivers": receivers,
        "discrepancies": discrepancies,
        "has_packages": has_packages,
        "has_draft": has_draft,
        "has_discrepancies": has_discrepancies,
        "pending_packages": pending_packages,
        "pending_count": len(pending_packages),
        "package_count": len(packages),
        "discrepancy_count": len(discrepancies),
    }


def hp_build_receipt_action(conn, preview_item, import_package):
    pkg_no = preview_item.get("pkg_no")
    imp_status = preview_item.get("import_status")
    pkg_map = {p.get("pkg_no"): p for p in import_package.get("handover_packages", [])}
    pkg_data = pkg_map.get(pkg_no)

    result = {
        "pkg_no": pkg_no,
        "title": preview_item.get("title"),
        "import_status": imp_status,
        "action": None,
        "reason": None,
        "can_import": False,
        "details": {},
    }

    if imp_status == "new":
        disc_ids = [
            it.get("discrepancy_id") for it in (pkg_data or {}).get("items", []) if it.get("discrepancy_id")
        ]
        valid_disc_ids = [did for did in disc_ids if conn.execute("SELECT 1 FROM discrepancies WHERE id = ?", (did,)).fetchone()]
        missing = [did for did in disc_ids if did not in valid_disc_ids]

        if valid_disc_ids and not missing:
            result["action"] = "create"
            result["can_import"] = True
            result["reason"] = "新建交接包，包含 {} 条差异".format(len(valid_disc_ids))
            result["details"] = {
                "discrepancy_count": len(valid_disc_ids),
                "discrepancy_ids": valid_disc_ids,
                "target_status": HP_STATUS_PENDING_HANDOVER,
            }
        else:
            result["action"] = "skip"
            result["can_import"] = False
            result["reason"] = "差异记录缺失" if missing else "没有有效的差异记录"
            result["details"] = {"missing_discrepancies": missing}

    elif imp_status == "safe":
        existing = preview_item.get("local_package")
        if existing:
            target_status = (pkg_data or {}).get("status")
            if target_status and target_status != existing["status"]:
                if target_status in HP_VALID_TRANSITIONS.get(existing["status"], []):
                    result["action"] = "update"
                    result["can_import"] = True
                    result["reason"] = "状态变更"
                    result["details"] = {
                        "from_status": existing["status"],
                        "to_status": target_status,
                        "package_id": existing["id"],
                    }
                else:
                    result["action"] = "skip"
                    result["can_import"] = False
                    result["reason"] = "无效的状态跳转"
            else:
                result["action"] = "skip"
                result["can_import"] = True
                result["reason"] = "状态一致，无需变更"
        else:
            result["action"] = "create"
            result["can_import"] = True
            result["reason"] = "本地不存在，按新建处理"

    elif imp_status == "conflict":
        has_blocking = any(c.get("blocking", True) for c in preview_item.get("conflicts", []))
        result["action"] = "skip"
        result["can_import"] = not has_blocking
        result["reason"] = preview_item.get("reason", "存在冲突")
        result["details"] = {
            "conflict_count": len(preview_item.get("conflicts", [])),
            "blocking_count": sum(1 for c in preview_item.get("conflicts", []) if c.get("blocking", True)),
            "conflicts": preview_item.get("conflicts", []),
        }

    return result
