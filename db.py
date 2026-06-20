import sqlite3
import json
import os
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
    return dict(row) if row else None
