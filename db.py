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

    saved_range = scheme.get("data_date_range")
    current_range = get_date_range(conn)

    if not saved_range or not current_range:
        return {"changed": False, "saved": saved_range, "current": current_range}

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
                        f"导入方案包跳过(状态变化): '{target_name}'",
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
                    f"导入方案包跳过(保留原方案): '{original_name}'",
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
                    f"导入方案包覆盖: '{original_name}'",
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
                    f"导入方案包改名: '{original_name}' -> '{target_name}'",
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
                    f"导入方案包改名: '{original_name}' -> '{target_name}'",
                )
            else:
                results.append({
                    "name": target_name,
                    "action": SCHEME_ACTION_CREATED,
                    "scheme_id": scheme_id,
                })
                log_scheme_operation(
                    conn, scheme_id, target_name, "import_scheme",
                    f"导入方案包新建: '{target_name}'",
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
    for i, pr in enumerate(preview_results):
        if i not in selected_set:
            continue

        decision = item_decisions.get(str(i), {}) if item_decisions else {}
        override_action = decision.get("action")
        note = decision.get("note", "")

        item = dict(pr)
        if override_action and override_action in (
            SCHEME_ACTION_CREATED, SCHEME_ACTION_OVERWRITTEN,
            SCHEME_ACTION_RENAMED, SCHEME_ACTION_KEPT
        ):
            original_name = pr.get("original_name", pr["name"])
            if override_action == SCHEME_ACTION_RENAMED and pr["action"] != SCHEME_ACTION_RENAMED:
                rename_suffix = decision.get("rename_suffix", "(导入)")
                new_name = _compute_rename_target(conn, original_name, rename_suffix)
                item["name"] = new_name
            elif override_action == SCHEME_ACTION_OVERWRITTEN:
                existing = conn.execute(
                    "SELECT * FROM review_schemes WHERE name = ?", (original_name,)
                ).fetchone()
                if existing:
                    item["existing_id"] = dict(existing)["id"]
            item["action"] = override_action

        item["note"] = note
        items_to_import.append(item)

    result = _execute_scheme_import(conn, items_to_import)

    if result["success"]:
        for r in result["results"]:
            idx = None
            for i, pr in enumerate(preview_results):
                if i in selected_set and pr.get("original_name", pr["name"]) == r.get("original_name", r["name"]):
                    idx = i
                    break
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

    return result


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
