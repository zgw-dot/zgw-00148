import json
from datetime import datetime, timedelta
from db import (
    get_conn, now_iso, get_active_rule_version, clear_all_discrepancies,
    get_discrepancy_by_business_key, insert_attribution_snapshot, insert_calc_step,
    resolve_barcode, DEFAULT_RULES,
)
from import_service import get_inventory_data, get_stocktake_data, get_sales_data, get_transfer_data

CAUSE_UNRECORDED_SALE = "unrecorded_sale"
CAUSE_TRANSFER_IN_TRANSIT = "transfer_in_transit"
CAUSE_NORMAL_LOSS = "normal_loss"
CAUSE_UNKNOWN_LOSS = "unknown_loss"
CAUSE_UNKNOWN_SURPLUS = "unknown_surplus"
CAUSE_TRANSFER_IN_PENDING = "transfer_in_pending"

CAUSE_LABELS = {
    CAUSE_UNRECORDED_SALE: "销售未入账",
    CAUSE_TRANSFER_IN_TRANSIT: "调拨在途(出)",
    CAUSE_NORMAL_LOSS: "正常损耗",
    CAUSE_UNKNOWN_LOSS: "未知差异(缺失)",
    CAUSE_UNKNOWN_SURPLUS: "盘盈(多出)",
    CAUSE_TRANSFER_IN_PENDING: "调拨入库在途",
}


def run_attribution():
    with get_conn() as conn:
        rule_rec = get_active_rule_version(conn)
        if not rule_rec:
            cfg = DEFAULT_RULES.copy()
            version_id = _ensure_default_rule(conn)
            rule_rec = get_active_rule_version(conn)
        else:
            cfg = json.loads(rule_rec["config_json"])

        loss_pct = cfg.get("loss_threshold_pct", 2.0)
        loss_abs = cfg.get("loss_threshold_abs", 3.0)
        delay_days = cfg.get("transfer_delay_days", 3)
        inv_data = get_inventory_data(conn)
        stk_data = get_stocktake_data(conn)
        sal_data = get_sales_data(conn)
        tra_data = get_transfer_data(conn)

        if not inv_data or not stk_data:
            return {"success": False, "error": "需要至少导入一份库存文件和一份盘点文件才能归因"}

        inv_map = {}
        inv_raw_barcodes = {}
        for r in inv_data:
            raw_bc = r["barcode"]
            cb = r["canonical_barcode"]
            key = (r["store_id"], cb)
            if key not in inv_map:
                inv_map[key] = {"system_qty": 0, "sku_name": r["sku_name"] or "", "raw_ids": []}
            inv_map[key]["system_qty"] += r["system_qty"] or 0
            if not inv_map[key]["sku_name"] and r["sku_name"]:
                inv_map[key]["sku_name"] = r["sku_name"]
            inv_map[key]["raw_ids"].append(r["id"])
            inv_raw_barcodes.setdefault(key, set()).add(raw_bc)

        stk_map = {}
        stk_raw_barcodes = {}
        for r in stk_data:
            raw_bc = r["barcode"]
            cb = r["canonical_barcode"]
            key = (r["store_id"], cb)
            if key not in stk_map:
                stk_map[key] = {"actual_qty": 0, "sku_name": r["sku_name"] or "", "raw_ids": []}
            stk_map[key]["actual_qty"] += r["actual_qty"] or 0
            if not stk_map[key]["sku_name"] and r["sku_name"]:
                stk_map[key]["sku_name"] = r["sku_name"]
            stk_map[key]["raw_ids"].append(r["id"])
            stk_raw_barcodes.setdefault(key, set()).add(raw_bc)

        sales_by_key = {}
        for r in sal_data:
            raw_bc = r["barcode"]
            cb = r["canonical_barcode"]
            key = (r["store_id"], cb)
            if key not in sales_by_key:
                sales_by_key[key] = {"total_sale": 0, "raw_ids": []}
            sales_by_key[key]["total_sale"] += r["sale_qty"] or 0
            sales_by_key[key]["raw_ids"].append(r["id"])

        transfers_by_key = {}
        now = datetime.now()
        cutoff = now - timedelta(days=delay_days)
        for r in tra_data:
            raw_bc = r["barcode"]
            cb = r["canonical_barcode"]
            try:
                tdate = datetime.fromisoformat(r["transfer_date"])
                within_window = tdate >= cutoff
            except (ValueError, TypeError):
                within_window = True

            key_out = (r["store_id_from"], cb)
            key_in = (r["store_id_to"], cb)

            if key_out not in transfers_by_key:
                transfers_by_key[key_out] = {"out_qty": 0, "in_qty": 0, "out_raw_ids": [], "in_raw_ids": []}
            if key_in not in transfers_by_key:
                transfers_by_key[key_in] = {"out_qty": 0, "in_qty": 0, "out_raw_ids": [], "in_raw_ids": []}

            if within_window:
                transfers_by_key[key_out]["out_qty"] += r["transfer_qty"] or 0
                transfers_by_key[key_out]["out_raw_ids"].append(r["id"])
                transfers_by_key[key_in]["in_qty"] += r["transfer_qty"] or 0
                transfers_by_key[key_in]["in_raw_ids"].append(r["id"])

        all_keys = set(inv_map.keys()) | set(stk_map.keys())
        created = 0
        skipped = 0
        now_str = now_iso()

        for key in all_keys:
            store_id, barcode = key
            sys_qty = inv_map.get(key, {}).get("system_qty", 0)
            act_qty = stk_map.get(key, {}).get("actual_qty", 0)
            sku_name = inv_map.get(key, {}).get("sku_name") or stk_map.get(key, {}).get("sku_name", "")
            diff = sys_qty - act_qty

            if abs(diff) < 0.001:
                continue

            all_raw_bc = set()
            all_raw_bc.update(inv_raw_barcodes.get(key, set()))
            all_raw_bc.update(stk_raw_barcodes.get(key, set()))
            alias_before = ", ".join(sorted([b for b in all_raw_bc if b != barcode])) or None
            alias_after = barcode if alias_before else None

            existing = get_discrepancy_by_business_key(conn, store_id, barcode, rule_rec["id"])
            if existing:
                skipped += 1
                continue

            cause, cause_detail, evidence, calc_steps = _attribute(
                key, diff, sys_qty, act_qty, loss_pct, loss_abs,
                sales_by_key.get(key), transfers_by_key.get(key),
                inv_map.get(key, {}).get("raw_ids", []),
                stk_map.get(key, {}).get("raw_ids", []),
            )

            conn.execute(
                """INSERT INTO discrepancies
                   (store_id, barcode, sku_name, system_qty, actual_qty, diff_qty,
                    attributed_cause, cause_detail, rule_version_id, import_id, status,
                    review_note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    store_id, barcode, sku_name, sys_qty, act_qty, diff,
                    cause, cause_detail, rule_rec["id"], None,
                    "pending_review", None, now_str, now_str,
                ),
            )
            disc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            created += 1

            for ev in evidence:
                conn.execute(
                    "INSERT INTO evidence_lines (discrepancy_id, raw_data_id, evidence_type, description) VALUES (?, ?, ?, ?)",
                    (disc_id, ev["raw_data_id"], ev["evidence_type"], ev["description"]),
                )

            sales_info = sales_by_key.get(key)
            transfer_info = transfers_by_key.get(key)
            all_transfer_raw_ids = []
            if transfer_info:
                all_transfer_raw_ids.extend(transfer_info.get("out_raw_ids", []))
                all_transfer_raw_ids.extend(transfer_info.get("in_raw_ids", []))
            insert_attribution_snapshot(
                conn, disc_id, rule_rec["id"], cfg,
                alias_before, alias_after, sys_qty, act_qty, diff,
                inv_map.get(key, {}).get("raw_ids", []),
                stk_map.get(key, {}).get("raw_ids", []),
                sales_info.get("raw_ids", []) if sales_info else [],
                all_transfer_raw_ids,
            )

            for idx, cs in enumerate(calc_steps):
                insert_calc_step(
                    conn, disc_id, idx, cs["step_type"], cs["step_description"],
                    cs["amount_applied"], cs["remaining_before"], cs["remaining_after"],
                    cs.get("raw_data_ids", []),
                )

        return {
            "success": True,
            "created": created,
            "skipped": skipped,
            "total_keys": len(all_keys),
            "rule_version": rule_rec["version"],
        }


def _attribute(key, diff, sys_qty, act_qty, loss_pct, loss_abs, sales_info, transfer_info,
               inv_raw_ids, stk_raw_ids):
    evidence = []
    calc_steps = []
    store_id, barcode = key

    for rid in inv_raw_ids:
        evidence.append({"raw_data_id": rid, "evidence_type": "inventory", "description": f"库存记录: 系统数量 {sys_qty}"})
    for rid in stk_raw_ids:
        evidence.append({"raw_data_id": rid, "evidence_type": "stocktake", "description": f"盘点记录: 实际数量 {act_qty}"})

    abs_diff = abs(diff)
    scenario_label = "缺失(系统多)" if diff > 0 else "盘盈(实际多)"
    calc_steps.append({
        "step_type": "init",
        "step_description": f"初始差异[{scenario_label}]: 系统 {sys_qty} - 实际 {act_qty} = {diff:+.1f} (绝对值 {abs_diff:.1f})",
        "amount_applied": 0,
        "remaining_before": abs_diff,
        "remaining_after": abs_diff,
        "raw_data_ids": inv_raw_ids + stk_raw_ids,
    })

    if diff > 0:
        remaining = abs_diff
        causes = []
        sale_amount = 0
        transfer_out_amount = 0

        if sales_info and sales_info["total_sale"] > 0:
            sale_amount = min(sales_info["total_sale"], remaining)
            old_remaining = remaining
            remaining -= sale_amount
            causes.append(f"销售未入账 {sale_amount:.1f}")
            for rid in sales_info["raw_ids"]:
                evidence.append({"raw_data_id": rid, "evidence_type": "sales", "description": f"销售记录: 销量 {sales_info['total_sale']}"})
            calc_steps.append({
                "step_type": "sales",
                "step_description": f"匹配销售记录: 总销量 {sales_info['total_sale']}，扣减 {sale_amount:.1f}",
                "amount_applied": sale_amount,
                "remaining_before": old_remaining,
                "remaining_after": remaining,
                "raw_data_ids": sales_info["raw_ids"],
            })

        if transfer_info and transfer_info["out_qty"] > 0:
            transfer_out_amount = min(transfer_info["out_qty"], remaining)
            old_remaining = remaining
            remaining -= transfer_out_amount
            causes.append(f"调拨在途出 {transfer_out_amount:.1f}")
            for rid in transfer_info["out_raw_ids"]:
                evidence.append({"raw_data_id": rid, "evidence_type": "transfer", "description": f"调拨出记录: 数量 {transfer_info['out_qty']}"})
            calc_steps.append({
                "step_type": "transfer_out",
                "step_description": f"匹配调拨出库: 出库数 {transfer_info['out_qty']}，扣减 {transfer_out_amount:.1f}",
                "amount_applied": transfer_out_amount,
                "remaining_before": old_remaining,
                "remaining_after": remaining,
                "raw_data_ids": transfer_info["out_raw_ids"],
            })

        threshold = max(sys_qty * loss_pct / 100, loss_abs)
        if remaining > 0 and remaining <= threshold:
            old_remaining = remaining
            causes.append(f"正常损耗 {remaining:.1f} (阈值 {threshold:.1f})")
            calc_steps.append({
                "step_type": "normal_loss",
                "step_description": f"判定为正常损耗: 剩余 {remaining:.1f} ≤ 阈值 {threshold:.1f}",
                "amount_applied": remaining,
                "remaining_before": old_remaining,
                "remaining_after": 0,
                "raw_data_ids": [],
            })
            remaining = 0
            cause = CAUSE_NORMAL_LOSS
        elif remaining > 0:
            old_remaining = remaining
            cause = CAUSE_UNKNOWN_LOSS
            causes.append(f"未知缺失 {remaining:.1f}")
            calc_steps.append({
                "step_type": "unknown_loss",
                "step_description": f"剩余 {remaining:.1f} > 阈值 {threshold:.1f}，判定为未知缺失",
                "amount_applied": remaining,
                "remaining_before": old_remaining,
                "remaining_after": 0,
                "raw_data_ids": [],
            })
            remaining = 0
        else:
            if sale_amount > 0 and transfer_out_amount > 0:
                cause = CAUSE_UNRECORDED_SALE
            elif sale_amount > 0:
                cause = CAUSE_UNRECORDED_SALE
            elif transfer_out_amount > 0:
                cause = CAUSE_TRANSFER_IN_TRANSIT
            else:
                cause = CAUSE_NORMAL_LOSS

        cause_detail = "; ".join(causes) if causes else f"差异 {diff:.1f}"
        return cause, cause_detail, evidence, calc_steps

    else:
        remaining = abs_diff
        causes = []

        if transfer_info and transfer_info["in_qty"] > 0:
            transfer_in_amount = min(transfer_info["in_qty"], remaining)
            old_remaining = remaining
            remaining -= transfer_in_amount
            causes.append(f"调拨入库在途 {transfer_in_amount:.1f}")
            for rid in transfer_info["in_raw_ids"]:
                evidence.append({"raw_data_id": rid, "evidence_type": "transfer", "description": f"调拨入记录: 数量 {transfer_info['in_qty']}"})
            calc_steps.append({
                "step_type": "transfer_in",
                "step_description": f"匹配调拨入库: 入库数 {transfer_info['in_qty']}，扣减 {transfer_in_amount:.1f}",
                "amount_applied": transfer_in_amount,
                "remaining_before": old_remaining,
                "remaining_after": remaining,
                "raw_data_ids": transfer_info["in_raw_ids"],
            })

        if remaining > 0:
            old_remaining = remaining
            cause = CAUSE_UNKNOWN_SURPLUS
            causes.append(f"盘盈 {remaining:.1f}")
            calc_steps.append({
                "step_type": "unknown_surplus",
                "step_description": f"剩余 {remaining:.1f} 无可解释来源，判定为盘盈",
                "amount_applied": remaining,
                "remaining_before": old_remaining,
                "remaining_after": 0,
                "raw_data_ids": [],
            })
            remaining = 0
        else:
            cause = CAUSE_TRANSFER_IN_PENDING

        cause_detail = "; ".join(causes) if causes else f"盘盈 {abs(diff):.1f}"
        return cause, cause_detail, evidence, calc_steps


def _ensure_default_rule(conn):
    from db import insert_rule_version, validate_rule_config
    errors = validate_rule_config(DEFAULT_RULES)
    if errors:
        raise ValueError(f"默认规则配置无效: {errors}")
    return insert_rule_version(conn, DEFAULT_RULES)
