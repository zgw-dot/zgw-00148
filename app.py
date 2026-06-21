import streamlit as st
import pandas as pd
import json
import io
import os
import tempfile
import uuid

from db import (
    init_db, get_conn, STATUS_LABELS, STATUS_PENDING_REVIEW, STATUS_CONFIRMED,
    STATUS_PENDING_ACCOUNTABILITY, STATUS_CLOSED, VALID_TRANSITIONS, IMPORT_TYPES,
    get_discrepancies, get_evidence_for_discrepancy, get_status_log,
    get_stores, get_import_records, transition_status, update_review_note,
    get_active_rule_version, now_iso, get_snapshot_for_discrepancy,
    get_calc_steps_for_discrepancy, get_discrepancies_extended,
    get_discrepancy_versions, get_all_rule_versions_with_labels,
    get_import_records_with_rule_version, save_ui_state, load_ui_state,
    get_store_list, get_barcode_list, get_date_range,
    save_review_scheme, get_review_schemes, get_review_scheme_by_id,
    get_review_scheme_by_name, update_review_scheme_name, delete_review_scheme,
    copy_review_scheme, mark_scheme_used, get_last_used_scheme,
    check_data_date_range_changed, get_scheme_operation_logs, log_scheme_operation,
    export_scheme_package, validate_scheme_package, import_scheme_package,
    preview_scheme_package_import, confirm_scheme_package_import,
    save_import_preview_context, load_import_preview_context,
    load_last_import_policy, clear_import_preview_context,
    SCHEME_ACTION_LABELS, SCHEME_ACTION_CREATED, SCHEME_ACTION_OVERWRITTEN,
    SCHEME_ACTION_RENAMED, SCHEME_ACTION_KEPT,
    compute_scheme_diff, save_import_batch, load_import_batch,
    list_pending_batches, update_batch_selection, clear_import_batch,
    mark_batch_completed, confirm_partial_scheme_import,
    export_scheme_manifest, shrink_batch_to_remaining,
    list_all_batches, get_import_decision_logs,
)
from import_service import import_csv
from engine import run_attribution, CAUSE_LABELS
from rules import save_rule_config, get_current_config, get_version_history
from sample_data import generate_sample_data, SAMPLE_DIR

st.set_page_config(page_title="门店盘点差异复盘工具", page_icon="📦", layout="wide")

init_db()

if "sample_generated" not in st.session_state:
    generate_sample_data()
    st.session_state.sample_generated = True


def _status_badge(status):
    colors = {
        STATUS_PENDING_REVIEW: "#FFA500",
        STATUS_CONFIRMED: "#4169E1",
        STATUS_PENDING_ACCOUNTABILITY: "#DC143C",
        STATUS_CLOSED: "#2E8B57",
    }
    label = STATUS_LABELS.get(status, status)
    color = colors.get(status, "#888")
    return f'<span style="background:{color};color:white;padding:2px 10px;border-radius:10px;font-size:13px">{label}</span>'


st.title("📦 门店盘点差异复盘工具")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📥 数据导入", "🔍 差异归因", "📋 差异列表", "⚙️ 规则配置", "📤 导出", "📊 差异复盘对比",
])

# ── Tab 1: 数据导入 ──
with tab1:
    st.header("导入 CSV 数据")
    st.markdown("支持四种数据类型：**库存**、**销售**、**调拨**、**盘点**")

    col_a, col_b = st.columns([2, 1])
    with col_a:
        import_type = st.selectbox("选择数据类型", IMPORT_TYPES,
                                   format_func=lambda x: {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}.get(x, x))
        uploaded = st.file_uploader(f"上传 {import_type} CSV 文件", type=["csv"], key=f"upload_{import_type}")

        if uploaded:
            content = uploaded.read()
            dup_key = f"dup_confirm_{import_type}_{uploaded.name}"

            if st.session_state.get(dup_key):
                result = import_csv(import_type, uploaded.name, content, allow_different_rule_version=True)
                del st.session_state[dup_key]
            else:
                result = import_csv(import_type, uploaded.name, content)

            if result["success"]:
                rv = result.get("rule_version", "-")
                st.success(f"✅ 导入成功！规则 v{rv}，有效行: {result['valid_rows']}，总行: {result['total_rows']}")
                if result.get("error_rows"):
                    st.warning(f"⚠️ 有 {result['error_rows']} 行被跳过：")
                    for e in result.get("detail_errors", []):
                        st.error(e)
            else:
                if result.get("duplicate"):
                    dup_type = result.get("duplicate_type")
                    if dup_type == "different_rule_version":
                        st.warning(result["error"])
                        col1, col2 = st.columns(2)
                        with col1:
                            if st.button("✅ 确认继续导入（分开存储）", key=f"confirm_{dup_key}"):
                                st.session_state[dup_key] = True
                                st.rerun()
                        with col2:
                            if st.button("❌ 取消", key=f"cancel_{dup_key}"):
                                st.rerun()
                    else:
                        st.warning(f"⚠️ {result['error']}")
                else:
                    st.error(f"❌ {result['error']}")
                    for e in result.get("detail_errors", []):
                        st.error(e)

    with col_b:
        st.subheader("快速导入样例数据")
        sample_files = {
            "inventory": "inventory.csv",
            "sales": "sales.csv",
            "transfer": "transfer.csv",
            "stocktake": "stocktake.csv",
        }
        for itype, fname in sample_files.items():
            fpath = os.path.join(SAMPLE_DIR, fname)
            label = {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}[itype]
            if os.path.exists(fpath):
                with open(fpath, "rb") as f:
                    content = f.read()
                btn_key = f"sample_{itype}"
                dup_key = f"sample_dup_confirm_{itype}"

                if st.session_state.get(dup_key):
                    result = import_csv(itype, fname, content, allow_different_rule_version=True)
                    del st.session_state[dup_key]
                    if result["success"]:
                        rv = result.get("rule_version", "-")
                        st.success(f"✅ 样例{label}导入成功！规则 v{rv}，有效行: {result['valid_rows']}")
                        if result.get("error_rows"):
                            st.warning(f"⚠️ {result['error_rows']} 行被跳过")
                    else:
                        if result.get("duplicate"):
                            dup_type = result.get("duplicate_type")
                            if dup_type == "different_rule_version":
                                st.warning(result["error"])
                                col1, col2 = st.columns(2)
                                with col1:
                                    if st.button("✅ 确认继续导入", key=f"sample_confirm_{dup_key}"):
                                        st.session_state[dup_key] = True
                                        st.rerun()
                                with col2:
                                    if st.button("❌ 取消", key=f"sample_cancel_{dup_key}"):
                                        st.rerun()
                            else:
                                st.warning(f"⚠️ {result['error']}")
                        else:
                            st.error(f"❌ {result['error']}")
                else:
                    if st.button(f"📁 导入样例{label}", key=btn_key):
                        result = import_csv(itype, fname, content)
                        if result["success"]:
                            rv = result.get("rule_version", "-")
                            st.success(f"✅ 样例{label}导入成功！规则 v{rv}，有效行: {result['valid_rows']}")
                            if result.get("error_rows"):
                                st.warning(f"⚠️ {result['error_rows']} 行被跳过")
                        else:
                            if result.get("duplicate"):
                                dup_type = result.get("duplicate_type")
                                if dup_type == "different_rule_version":
                                    st.warning(result["error"])
                                    col1, col2 = st.columns(2)
                                    with col1:
                                        if st.button("✅ 确认继续导入", key=f"sample_confirm_{dup_key}"):
                                            st.session_state[dup_key] = True
                                            st.rerun()
                                    with col2:
                                        if st.button("❌ 取消", key=f"sample_cancel_{dup_key}"):
                                            st.rerun()
                                else:
                                    st.warning(f"⚠️ {result['error']}")
                            else:
                                st.error(f"❌ {result['error']}")

        st.divider()
        st.subheader("测试坏行导入")
        bad_path = os.path.join(SAMPLE_DIR, "inventory_with_bad_rows.csv")
        bad_sales_path = os.path.join(SAMPLE_DIR, "sales_with_bad_rows.csv")
        if os.path.exists(bad_path):
            with open(bad_path, "rb") as f:
                bad_content = f.read()
            if st.button("🧪 导入坏行库存测试文件", key="bad_test"):
                result = import_csv("inventory", "inventory_with_bad_rows.csv", bad_content)
                if result["success"]:
                    st.success(f"✅ 部分成功: 有效 {result['valid_rows']} 行, 跳过 {result['error_rows']} 行")
                    for e in result.get("detail_errors", []):
                        st.error(e)
                else:
                    st.error(f"❌ {result['error']}")
                    for e in result.get("detail_errors", []):
                        st.error(e)
        if os.path.exists(bad_sales_path):
            with open(bad_sales_path, "rb") as f:
                bad_sales_content = f.read()
            if st.button("🧪 导入坏行销售测试文件", key="bad_sales_test"):
                result = import_csv("sales", "sales_with_bad_rows.csv", bad_sales_content)
                if result["success"]:
                    st.success(f"✅ 部分成功: 有效 {result['valid_rows']} 行, 跳过 {result['error_rows']} 行")
                    for e in result.get("detail_errors", []):
                        st.error(e)
                else:
                    st.error(f"❌ {result['error']}")
                    for e in result.get("detail_errors", []):
                        st.error(e)

    st.divider()
    st.subheader("导入记录")
    with get_conn() as conn:
        records = get_import_records_with_rule_version(conn)
    if records:
        df_import = pd.DataFrame(records)
        df_import["import_type"] = df_import["import_type"].map(
            {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}
        )
        df_import["rule_ver"] = df_import["rule_ver"].apply(lambda x: f"v{x}" if x else "-")
        df_import = df_import[["file_name", "import_type", "rule_ver", "imported_at", "row_count", "error_count"]]
        df_import.columns = ["文件名", "类型", "规则版本", "导入时间", "有效行", "错误行"]
        st.dataframe(df_import, use_container_width=True, hide_index=True)
    else:
        st.info("暂无导入记录")

# ── Tab 2: 差异归因 ──
with tab2:
    st.header("运行差异归因")
    st.markdown("基于当前规则版本，对已导入的库存和盘点数据进行比对，归因差异原因。")

    with get_conn() as conn:
        current_cfg, current_ver = get_current_config()

    col_info1, col_info2, col_info3 = st.columns(3)
    with col_info1:
        st.metric("当前规则版本", f"v{current_ver}" if current_ver else "未配置")
    with col_info2:
        st.metric("损耗阈值(%)", f"{current_cfg.get('loss_threshold_pct', '-')}%")
    with col_info3:
        st.metric("调拨延迟窗口", f"{current_cfg.get('transfer_delay_days', '-')}天")

    if st.button("🚀 运行归因分析", type="primary"):
        with st.spinner("正在归因分析..."):
            result = run_attribution()
        if result["success"]:
            msg = f"✅ 归因完成！新增差异 {result['created']} 条，使用规则 v{result['rule_version']}"
            if result.get("skipped", 0) > 0:
                msg += f"（已有 {result['skipped']} 条差异保留原归因快照不变）"
            st.success(msg)
        else:
            st.error(f"❌ {result['error']}")

# ── Tab 3: 差异列表 ──
with tab3:
    st.header("差异列表")

    with get_conn() as conn:
        stores = get_stores(conn)

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        filter_store = st.selectbox("按门店筛选", ["全部"] + stores, key="filter_store")
    with col_f2:
        filter_status = st.selectbox(
            "按状态筛选",
            ["全部"] + list(STATUS_LABELS.keys()),
            format_func=lambda x: "全部" if x == "全部" else STATUS_LABELS.get(x, x),
            key="filter_status",
        )

    with get_conn() as conn:
        store_param = None if filter_store == "全部" else filter_store
        status_param = None if filter_status == "全部" else filter_status
        discs = get_discrepancies(conn, store_id=store_param, status=status_param)

    if discs:
        for d in discs:
            cause_label = CAUSE_LABELS.get(d["attributed_cause"], d["attributed_cause"] or "未归因")
            with st.expander(
                f"[{STATUS_LABELS.get(d['status'], d['status'])}] "
                f"{d['store_id']} | {d['sku_name'] or d['barcode']} | "
                f"差异: {d['diff_qty']:+.1f} ({cause_label})"
            ):
                col_d1, col_d2 = st.columns([3, 2])

                with col_d1:
                    st.markdown(f"**条码**: {d['barcode']}")
                    st.markdown(f"**系统数量**: {d['system_qty']:.1f} → **实际数量**: {d['actual_qty']:.1f}")
                    st.markdown(f"**差异**: {d['diff_qty']:+.1f}")
                    st.markdown(f"**归因**: {cause_label}")
                    st.markdown(f"**归因详情**: {d.get('cause_detail', '-')}")
                    st.markdown(f"**规则版本**: v{d.get('rule_ver', '-')}")
                    st.markdown(f"**创建时间**: {d['created_at']}")
                    st.markdown(_status_badge(d["status"]), unsafe_allow_html=True)

                    note_key = f"note_{d['id']}"
                    current_note = d.get("review_note") or ""
                    new_note = st.text_area("复核备注", value=current_note, key=note_key)
                    if st.button("💾 保存备注", key=f"save_note_{d['id']}"):
                        with get_conn() as conn:
                            update_review_note(conn, d["id"], new_note)
                        st.success("备注已保存")
                        st.rerun()

                with col_d2:
                    st.markdown("**状态流转**")
                    valid_next = VALID_TRANSITIONS.get(d["status"], [])
                    if valid_next:
                        for next_s in valid_next:
                            label = STATUS_LABELS.get(next_s, next_s)
                            if st.button(f"→ {label}", key=f"trans_{d['id']}_{next_s}"):
                                try:
                                    with get_conn() as conn:
                                        transition_status(conn, d["id"], next_s)
                                    st.success(f"已流转到: {label}")
                                    st.rerun()
                                except ValueError as e:
                                    st.error(str(e))
                    else:
                        st.info("已关闭，不可再流转")

                    st.divider()
                    st.markdown("**来源证据**")
                    with get_conn() as conn:
                        evidences = get_evidence_for_discrepancy(conn, d["id"])
                    for ev in evidences:
                        type_label = {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}.get(
                            ev["source_type"], ev["source_type"]
                        )
                        st.markdown(f"- [{type_label}] 行{ev['source_line']}: {ev.get('description', ev.get('raw_row', ''))}")

                    st.divider()
                    st.markdown("**流转日志**")
                    with get_conn() as conn:
                        logs = get_status_log(conn, d["id"])
                    for log in logs:
                        from_l = STATUS_LABELS.get(log["from_status"], log["from_status"] or "新建")
                        to_l = STATUS_LABELS.get(log["to_status"], log["to_status"])
                        st.markdown(f"- {log['changed_at'][:19]}: {from_l} → {to_l}" + (f" ({log.get('note', '')})" if log.get("note") else ""))

                st.divider()
                st.markdown("### 🧾 归因快照（可回放解释链路）")
                with get_conn() as conn:
                    snap = get_snapshot_for_discrepancy(conn, d["id"])
                    calc_steps = get_calc_steps_for_discrepancy(conn, d["id"])

                if snap:
                    snap_col1, snap_col2 = st.columns(2)
                    with snap_col1:
                        st.markdown("**规则配置快照（当时生效）**")
                        cfg = snap.get("rule_config_snapshot", {}) or {}
                        cfg_display = {
                            "损耗阈值(%)": cfg.get("loss_threshold_pct", "-"),
                            "损耗阈值(绝对值)": cfg.get("loss_threshold_abs", "-"),
                            "调拨延迟窗口(天)": cfg.get("transfer_delay_days", "-"),
                            "条码别名映射": cfg.get("aliases", {}) if cfg.get("aliases") else "(无)",
                        }
                        st.json(cfg_display, expanded=False)

                    with snap_col2:
                        st.markdown("**别名映射与命中ID**")
                        if snap.get("alias_before"):
                            st.markdown(f"- **映射前条码**: `{snap['alias_before']}`")
                            st.markdown(f"- **映射后规范条码**: `{snap['alias_after']}`")
                        else:
                            st.markdown("- 别名映射: 无（直接使用原始条码）")
                        st.markdown(f"- 命中库存原始ID: `{snap.get('raw_inventory_ids', [])}`")
                        st.markdown(f"- 命中盘点原始ID: `{snap.get('raw_stocktake_ids', [])}`")
                        st.markdown(f"- 命中销售原始ID: `{snap.get('raw_sales_ids', [])}`")
                        st.markdown(f"- 命中调拨原始ID: `{snap.get('raw_transfer_ids', [])}`")
                        st.markdown(f"- 快照生成时间: `{snap.get('created_at', '')[:19]}`")

                    st.markdown("**📊 计算步骤回放（从初始差异到最终归因）**")
                    if calc_steps:
                        for cs in calc_steps:
                            step_type_label = {
                                "init": "🔢 初始计算",
                                "sales": "🛒 销售扣减",
                                "transfer_out": "📤 调拨出库扣减",
                                "transfer_in": "📥 调拨入库扣减",
                                "normal_loss": "⚖️ 正常损耗判定",
                                "unknown_loss": "❓ 未知缺失",
                                "unknown_surplus": "📈 盘盈",
                            }.get(cs["step_type"], cs["step_type"])
                            with st.container():
                                sc1, sc2, sc3, sc4 = st.columns([2, 3, 2, 2])
                                with sc1:
                                    st.markdown(f"**{step_type_label}**")
                                with sc2:
                                    st.markdown(cs["step_description"])
                                with sc3:
                                    if cs["step_type"] != "init":
                                        st.markdown(f"扣减: **{cs['amount_applied']:+.1f}**")
                                    else:
                                        st.markdown("—")
                                with sc4:
                                    if cs["step_type"] != "init":
                                        st.markdown(f"剩余: {cs['remaining_before']:.1f} → **{cs['remaining_after']:.1f}**")
                                    else:
                                        st.markdown(f"初始: **{cs['remaining_after']:+.1f}**")
                                if cs.get("raw_data_ids"):
                                    with st.expander(f"  🔗 关联原始数据ID ({len(cs['raw_data_ids'])}条)", expanded=False):
                                        st.markdown(f"原始ID列表: `{cs['raw_data_ids']}`")
                    else:
                        st.info("该差异暂无计算步骤记录（可能为旧版数据，建议重新归因）")
                else:
                    st.info("该差异暂无归因快照（可能为旧版数据，建议重新归因生成）")
    else:
        st.info("暂无差异记录，请先导入数据并运行归因分析")

# ── Tab 4: 规则配置 ──
with tab4:
    st.header("规则配置")
    st.markdown("修改归因规则参数。**校验失败时旧规则保留，不会冲掉旧数据。**")

    current_cfg, current_ver = get_current_config()

    col_r1, col_r2 = st.columns(2)
    with col_r1:
        loss_pct = st.number_input("损耗阈值(%)", min_value=0.0, max_value=100.0,
                                   value=float(current_cfg.get("loss_threshold_pct", 2.0)), step=0.5)
        loss_abs = st.number_input("损耗阈值(绝对值)", min_value=0.0,
                                   value=float(current_cfg.get("loss_threshold_abs", 3.0)), step=0.5)
        delay_days = st.number_input("调拨延迟窗口(天)", min_value=0,
                                    value=int(current_cfg.get("transfer_delay_days", 3)), step=1)

    with col_r2:
        st.markdown("**条码别名映射** (alias_barcode → canonical_barcode)")
        aliases = current_cfg.get("aliases", {})
        alias_text = st.text_area(
            "每行一个映射，格式: alias=canonical",
            value="\n".join(f"{k}={v}" for k, v in aliases.items()),
            height=150,
            key="alias_text",
        )

        parsed_aliases = {}
        alias_errors = []
        for line in alias_text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if "=" not in line:
                alias_errors.append(f"格式错误: '{line}'，应为 alias=canonical")
                continue
            parts = line.split("=", 1)
            parsed_aliases[parts[0].strip()] = parts[1].strip()

        if alias_errors:
            for e in alias_errors:
                st.error(e)

    if st.button("💾 保存规则", type="primary"):
        new_config = {
            "loss_threshold_pct": loss_pct,
            "loss_threshold_abs": loss_abs,
            "transfer_delay_days": delay_days,
            "aliases": parsed_aliases,
        }
        result = save_rule_config(new_config)
        if result["success"]:
            st.success(result["message"])
            st.rerun()
        else:
            st.error(result["message"])
            for e in result.get("errors", []):
                st.error(e)

    st.divider()
    st.subheader("规则版本历史")
    versions = get_version_history()
    if versions:
        for v in versions:
            cfg_display = json.loads(v["config_json"])
            with st.expander(f"v{v['version']} — {v['created_at'][:19]} {'✅ 当前' if v['is_active'] else ''}"):
                st.json(cfg_display, expanded=False)
    else:
        st.info("暂无规则版本")

# ── Tab 5: 导出 ──
with tab5:
    st.header("导出数据")
    st.markdown("导出包含**差异明细、复核备注、来源证据行、状态流转日志、归因快照（规则+别名+计算步骤）**，可独立复盘。")

    with get_conn() as conn:
        stores = get_stores(conn)

    export_store = st.selectbox("按门店筛选导出", ["全部"] + stores, key="export_store")
    export_format = st.radio("导出格式", ["CSV", "JSON"], horizontal=True, key="export_format")

    with get_conn() as conn:
        store_param = None if export_store == "全部" else export_store
        discs = get_discrepancies(conn, store_id=store_param)

    if discs:
        with get_conn() as conn:
            for d in discs:
                d["evidence_lines"] = get_evidence_for_discrepancy(conn, d["id"])
                d["status_logs"] = get_status_log(conn, d["id"])
                d["snapshot"] = get_snapshot_for_discrepancy(conn, d["id"])
                d["calc_steps"] = get_calc_steps_for_discrepancy(conn, d["id"])

        for d in discs:
            d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
            d["cause_label"] = CAUSE_LABELS.get(d["attributed_cause"], d["attributed_cause"] or "未归因")
            ev_parts = []
            for ev in d["evidence_lines"]:
                tl = {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}.get(
                    ev.get("source_type", ""), ev.get("source_type", "")
                )
                ev_parts.append(f"[{tl}] 行{ev.get('source_line', '?')}: {ev.get('description', '') or json.loads(ev.get('raw_row', '{}'))}")
            d["evidence_summary"] = " | ".join(ev_parts) if ev_parts else ""
            log_parts = []
            for lg in d["status_logs"]:
                from_l = STATUS_LABELS.get(lg["from_status"], lg["from_status"] or "新建")
                to_l = STATUS_LABELS.get(lg["to_status"], lg["to_status"])
                note = f" ({lg.get('note', '')})" if lg.get("note") else ""
                log_parts.append(f"{lg['changed_at'][:19]}: {from_l} → {to_l}{note}")
            d["status_log_summary"] = " | ".join(log_parts) if log_parts else ""

            snap = d.get("snapshot")
            if snap:
                cfg = snap.get("rule_config_snapshot", {}) or {}
                alias_info = ""
                if snap.get("alias_before"):
                    alias_info = f"{snap['alias_before']} → {snap['alias_after']}"
                d["snapshot_alias"] = alias_info or "(无别名映射)"
                d["snapshot_rule_config"] = json.dumps(cfg, ensure_ascii=False)
                d["snapshot_created_at"] = snap.get("created_at", "")[:19] if snap.get("created_at") else ""
                d["snapshot_inv_ids"] = json.dumps(snap.get("raw_inventory_ids", []), ensure_ascii=False)
                d["snapshot_stk_ids"] = json.dumps(snap.get("raw_stocktake_ids", []), ensure_ascii=False)
                d["snapshot_sal_ids"] = json.dumps(snap.get("raw_sales_ids", []), ensure_ascii=False)
                d["snapshot_tra_ids"] = json.dumps(snap.get("raw_transfer_ids", []), ensure_ascii=False)
            else:
                d["snapshot_alias"] = "(无快照，建议重新归因)"
                d["snapshot_rule_config"] = ""
                d["snapshot_created_at"] = ""
                d["snapshot_inv_ids"] = "[]"
                d["snapshot_stk_ids"] = "[]"
                d["snapshot_sal_ids"] = "[]"
                d["snapshot_tra_ids"] = "[]"

            calc_steps = d.get("calc_steps", [])
            if calc_steps:
                step_labels_map = {
                    "init": "初始计算", "sales": "销售扣减", "transfer_out": "调拨出库扣减",
                    "transfer_in": "调拨入库扣减", "normal_loss": "正常损耗判定",
                    "unknown_loss": "未知缺失", "unknown_surplus": "盘盈",
                }
                calc_summary_parts = []
                for idx, cs in enumerate(calc_steps):
                    st_label = step_labels_map.get(cs["step_type"], cs["step_type"])
                    if cs["step_type"] == "init":
                        calc_summary_parts.append(
                            f"[{idx+1}]{st_label}: {cs['step_description']}"
                        )
                    else:
                        calc_summary_parts.append(
                            f"[{idx+1}]{st_label}: {cs['step_description']} "
                            f"(扣减{cs['amount_applied']:+.1f}, 剩{cs['remaining_before']:.1f}→{cs['remaining_after']:.1f})"
                        )
                d["calc_steps_summary"] = " || ".join(calc_summary_parts)
                d["calc_steps_json"] = json.dumps(
                    [
                        {
                            "step_index": cs["step_index"],
                            "step_type": cs["step_type"],
                            "step_description": cs["step_description"],
                            "amount_applied": cs["amount_applied"],
                            "remaining_before": cs["remaining_before"],
                            "remaining_after": cs["remaining_after"],
                            "raw_data_ids": cs.get("raw_data_ids", []),
                        }
                        for cs in calc_steps
                    ],
                    ensure_ascii=False,
                )
            else:
                d["calc_steps_summary"] = "(无计算步骤，建议重新归因)"
                d["calc_steps_json"] = "[]"

        export_cols = [
            "id", "store_id", "barcode", "sku_name", "system_qty", "actual_qty",
            "diff_qty", "attributed_cause", "cause_label", "cause_detail",
            "rule_ver", "status", "status_label",
            "review_note", "reviewed_at", "created_at", "updated_at",
            "evidence_summary", "status_log_summary",
            "snapshot_alias", "snapshot_rule_config", "snapshot_created_at",
            "snapshot_inv_ids", "snapshot_stk_ids", "snapshot_sal_ids", "snapshot_tra_ids",
            "calc_steps_summary", "calc_steps_json",
        ]

        df_export = pd.DataFrame(discs)
        existing_cols = [c for c in export_cols if c in df_export.columns]
        df_display = df_export[existing_cols].copy()
        df_display.columns = [
            "差异ID", "门店", "条码", "商品名称", "系统数量", "实际数量",
            "差异数量", "归因编码", "归因", "归因详情",
            "规则版本", "状态编码", "状态",
            "复核备注", "复核时间", "创建时间", "更新时间",
            "来源证据", "状态流转",
            "快照-别名映射", "快照-当时规则配置(JSON)", "快照-生成时间",
            "快照-库存原始ID", "快照-盘点原始ID", "快照-销售原始ID", "快照-调拨原始ID",
            "计算步骤(文本)", "计算步骤(JSON)",
        ]

        st.dataframe(df_display, use_container_width=True, hide_index=True)

        if export_format == "CSV":
            csv_buf = io.StringIO()
            df_display.to_csv(csv_buf, index=False, encoding="utf-8-sig")
            st.download_button(
                "⬇️ 下载 CSV（含完整证据+流转+快照+计算步骤）",
                data=csv_buf.getvalue().encode("utf-8-sig"),
                file_name=f"discrepancies_full_{now_iso()[:10]}.csv",
                mime="text/csv",
            )
        else:
            json_obj = []
            for d in discs:
                snap = d.get("snapshot")
                snap_obj = None
                if snap:
                    snap_obj = {
                        "alias_before": snap.get("alias_before"),
                        "alias_after": snap.get("alias_after"),
                        "rule_config_snapshot": snap.get("rule_config_snapshot", {}),
                        "system_qty_snapshot": snap.get("system_qty_snapshot"),
                        "actual_qty_snapshot": snap.get("actual_qty_snapshot"),
                        "diff_qty_snapshot": snap.get("diff_qty_snapshot"),
                        "raw_inventory_ids": snap.get("raw_inventory_ids", []),
                        "raw_stocktake_ids": snap.get("raw_stocktake_ids", []),
                        "raw_sales_ids": snap.get("raw_sales_ids", []),
                        "raw_transfer_ids": snap.get("raw_transfer_ids", []),
                        "created_at": snap.get("created_at"),
                    }
                calc_steps = d.get("calc_steps", [])
                json_obj.append({
                    "id": d["id"],
                    "store_id": d["store_id"],
                    "barcode": d["barcode"],
                    "sku_name": d["sku_name"],
                    "system_qty": d["system_qty"],
                    "actual_qty": d["actual_qty"],
                    "diff_qty": d["diff_qty"],
                    "attributed_cause": d["attributed_cause"],
                    "cause_label": d["cause_label"],
                    "cause_detail": d.get("cause_detail"),
                    "rule_version": d.get("rule_ver"),
                    "status": d["status"],
                    "status_label": d["status_label"],
                    "review_note": d.get("review_note"),
                    "reviewed_at": d.get("reviewed_at"),
                    "created_at": d.get("created_at"),
                    "updated_at": d.get("updated_at"),
                    "evidence_lines": [
                        {
                            "source_type": ev.get("source_type"),
                            "source_line": ev.get("source_line"),
                            "evidence_type": ev.get("evidence_type"),
                            "description": ev.get("description"),
                            "raw_row": json.loads(ev["raw_row"]) if ev.get("raw_row") else None,
                        }
                        for ev in d["evidence_lines"]
                    ],
                    "status_logs": [
                        {
                            "from_status": lg.get("from_status"),
                            "from_status_label": STATUS_LABELS.get(lg["from_status"], lg["from_status"] or "新建"),
                            "to_status": lg.get("to_status"),
                            "to_status_label": STATUS_LABELS.get(lg["to_status"], lg["to_status"]),
                            "changed_at": lg.get("changed_at"),
                            "changed_by": lg.get("changed_by"),
                            "note": lg.get("note"),
                        }
                        for lg in d["status_logs"]
                    ],
                    "attribution_snapshot": snap_obj,
                    "calculation_steps": [
                        {
                            "step_index": cs["step_index"],
                            "step_type": cs["step_type"],
                            "step_type_label": {
                                "init": "初始计算", "sales": "销售扣减",
                                "transfer_out": "调拨出库扣减", "transfer_in": "调拨入库扣减",
                                "normal_loss": "正常损耗判定", "unknown_loss": "未知缺失",
                                "unknown_surplus": "盘盈",
                            }.get(cs["step_type"], cs["step_type"]),
                            "step_description": cs["step_description"],
                            "amount_applied": cs["amount_applied"],
                            "remaining_before": cs["remaining_before"],
                            "remaining_after": cs["remaining_after"],
                            "raw_data_ids": cs.get("raw_data_ids", []),
                        }
                        for cs in calc_steps
                    ],
                })
            json_str = json.dumps(json_obj, ensure_ascii=False, indent=2, default=str)
            st.download_button(
                "⬇️ 下载 JSON（嵌套证据+流转+快照+计算步骤，可独立复盘）",
                data=json_str.encode("utf-8"),
                file_name=f"discrepancies_full_{now_iso()[:10]}.json",
                mime="application/json",
            )
    else:
        st.info("暂无差异数据可导出")


# ── Tab 6: 差异复盘对比 ──
with tab6:
    st.header("📊 差异复盘对比（按规则版本回看）")
    st.markdown("按**门店、时间、商品、规则版本**筛选，将旧记录、新记录、别名变化和归因快照并排对比，无需来回翻导出文件。")

    with get_conn() as conn:
        stores = get_store_list(conn)
        barcodes = get_barcode_list(conn)
        rule_versions = get_all_rule_versions_with_labels(conn)
        date_range = get_date_range(conn)
        all_schemes = get_review_schemes(conn)
        last_used_scheme = get_last_used_scheme(conn)
        operation_logs = get_scheme_operation_logs(conn, limit=50)

    saved_state = None
    with get_conn() as conn:
        saved_state = load_ui_state(conn, "review_filter_state")

    if "review_filter_init" not in st.session_state:
        init_state = saved_state
        if last_used_scheme and last_used_scheme.get("filter_state"):
            init_state = last_used_scheme["filter_state"]
            st.session_state.current_scheme_id = last_used_scheme["id"]
            st.session_state.current_scheme_name = last_used_scheme["name"]
        if init_state:
            st.session_state.review_store = init_state.get("store_id", "全部")
            st.session_state.review_barcode = init_state.get("barcode", "")
            st.session_state.review_rule_ver_a = init_state.get("rule_ver_a", 0)
            st.session_state.review_rule_ver_b = init_state.get("rule_ver_b", 0)
            st.session_state.review_status = init_state.get("status", "全部")
            st.session_state.review_date_from = init_state.get("date_from", "")
            st.session_state.review_date_to = init_state.get("date_to", "")
        st.session_state.review_filter_init = True

    st.subheader("📋 复盘方案管理")

    scheme_col1, scheme_col2, scheme_col3 = st.columns([2, 2, 1])
    with scheme_col1:
        if all_schemes:
            scheme_options = [(0, "--- 选择方案载入 ---")] + [
                (s["id"], f"{s['name']} {'(最近使用)' if last_used_scheme and s['id'] == last_used_scheme.get('id') else ''}")
                for s in all_schemes
            ]
            scheme_display = {k: v for k, v in scheme_options}
            selected_scheme_id = st.selectbox(
                "选择复盘方案",
                list(scheme_display.keys()),
                format_func=lambda x: scheme_display[x],
                key="selected_scheme_id",
            )
            if selected_scheme_id > 0:
                col_load, col_clear = st.columns(2)
                with col_load:
                    if st.button("📥 载入方案", type="primary", key="load_scheme_btn"):
                        with get_conn() as conn:
                            scheme = get_review_scheme_by_id(conn, selected_scheme_id)
                        if scheme:
                            fs = scheme["filter_state"]
                            st.session_state.review_store = fs.get("store_id", "全部")
                            st.session_state.review_barcode = fs.get("barcode", "")
                            st.session_state.review_rule_ver_a = fs.get("rule_ver_a", 0)
                            st.session_state.review_rule_ver_b = fs.get("rule_ver_b", 0)
                            st.session_state.review_status = fs.get("status", "全部")
                            st.session_state.review_date_from = fs.get("date_from", "")
                            st.session_state.review_date_to = fs.get("date_to", "")
                            st.session_state.current_scheme_id = scheme["id"]
                            st.session_state.current_scheme_name = scheme["name"]
                            with get_conn() as conn:
                                mark_scheme_used(conn, scheme["id"])
                                range_check = check_data_date_range_changed(conn, scheme["id"])
                            if range_check.get("changed"):
                                st.warning(
                                    f"⚠️ 底层数据时间范围已变化！\n\n"
                                    f"方案保存时: {range_check['saved'].get('min_date','')[:10]} ~ {range_check['saved'].get('max_date','')[:10]}\n"
                                    f"当前数据: {range_check['current'].get('min_date','')[:10]} ~ {range_check['current'].get('max_date','')[:10]}"
                                )
                            st.success(f"✅ 已载入方案 '{scheme['name']}'")
                            st.rerun()
                with col_clear:
                    if st.button("🔄 清除当前", key="clear_current_scheme"):
                        for k in ["current_scheme_id", "current_scheme_name"]:
                            if k in st.session_state:
                                del st.session_state[k]
                        st.rerun()
        else:
            st.info("暂无保存的方案，调整筛选条件后可保存为新方案")

    with scheme_col2:
        new_scheme_name = st.text_input(
            "方案名称",
            value=st.session_state.get("current_scheme_name", ""),
            placeholder="输入方案名称以保存",
            key="new_scheme_name",
        )
        new_scheme_desc = st.text_area(
            "方案描述（可选）",
            placeholder="简述这个方案的用途，如'618大促前后对比'",
            key="new_scheme_desc",
            height=60,
        )
        col_save, col_saveas = st.columns(2)
        with col_save:
            if st.button("💾 保存方案", key="save_scheme_btn"):
                if not new_scheme_name.strip():
                    st.error("请输入方案名称")
                else:
                    current_filters = {
                        "store_id": st.session_state.get("review_store", "全部"),
                        "barcode": st.session_state.get("review_barcode", ""),
                        "rule_ver_a": st.session_state.get("review_rule_ver_a", 0),
                        "rule_ver_b": st.session_state.get("review_rule_ver_b", 0),
                        "status": st.session_state.get("review_status", "全部"),
                        "date_from": st.session_state.get("review_date_from", ""),
                        "date_to": st.session_state.get("review_date_to", ""),
                        "saved_at": now_iso(),
                    }
                    confirm_key = f"save_confirm_{new_scheme_name.strip()}"
                    if st.session_state.get(confirm_key):
                        with get_conn() as conn:
                            result = save_review_scheme(
                                conn, new_scheme_name.strip(), current_filters,
                                description=new_scheme_desc.strip() or None,
                                overwrite=True,
                                data_date_range=date_range,
                            )
                        if result["success"]:
                            st.session_state.current_scheme_id = result["scheme_id"]
                            st.session_state.current_scheme_name = result["name"]
                            del st.session_state[confirm_key]
                            action = "覆盖更新" if result["overwritten"] else "新建"
                            st.success(f"✅ {action}方案成功: '{result['name']}'")
                            st.rerun()
                    else:
                        with get_conn() as conn:
                            result = save_review_scheme(
                                conn, new_scheme_name.strip(), current_filters,
                                description=new_scheme_desc.strip() or None,
                                overwrite=False,
                                data_date_range=date_range,
                            )
                        if result["success"]:
                            st.session_state.current_scheme_id = result["scheme_id"]
                            st.session_state.current_scheme_name = result["name"]
                            st.success(f"✅ 新建方案成功: '{result['name']}'")
                            st.rerun()
                        elif result.get("needs_confirm"):
                            st.warning(f"⚠️ 方案名 '{new_scheme_name.strip()}' 已存在，是否覆盖？")
                            col_yes, col_no = st.columns(2)
                            with col_yes:
                                if st.button("✅ 确认覆盖", key=f"confirm_overwrite_{new_scheme_name.strip()}"):
                                    st.session_state[confirm_key] = True
                                    st.rerun()
                            with col_no:
                                if st.button("❌ 取消", key=f"cancel_overwrite_{new_scheme_name.strip()}"):
                                    st.rerun()
                        else:
                            st.error(f"❌ {result.get('error', '保存失败')}")
        with col_saveas:
            if st.button("📋 另存为新", key="saveas_scheme_btn"):
                if not new_scheme_name.strip():
                    st.error("请输入新方案名称")
                else:
                    current_filters = {
                        "store_id": st.session_state.get("review_store", "全部"),
                        "barcode": st.session_state.get("review_barcode", ""),
                        "rule_ver_a": st.session_state.get("review_rule_ver_a", 0),
                        "rule_ver_b": st.session_state.get("review_rule_ver_b", 0),
                        "status": st.session_state.get("review_status", "全部"),
                        "date_from": st.session_state.get("review_date_from", ""),
                        "date_to": st.session_state.get("review_date_to", ""),
                        "saved_at": now_iso(),
                    }
                    with get_conn() as conn:
                        result = save_review_scheme(
                            conn, new_scheme_name.strip(), current_filters,
                            description=new_scheme_desc.strip() or None,
                            overwrite=False,
                            data_date_range=date_range,
                        )
                    if result["success"]:
                        st.session_state.current_scheme_id = result["scheme_id"]
                        st.session_state.current_scheme_name = result["name"]
                        st.success(f"✅ 另存为新方案成功: '{result['name']}'")
                        st.rerun()
                    elif result.get("needs_confirm"):
                        st.error(f"❌ 方案名 '{new_scheme_name.strip()}' 已存在，请换一个名称")
                    else:
                        st.error(f"❌ {result.get('error', '保存失败')}")

    with scheme_col3:
        current_scheme_id = st.session_state.get("current_scheme_id")
        current_scheme_name = st.session_state.get("current_scheme_name")
        if current_scheme_id and current_scheme_name:
            st.info(f"📌 当前方案:\n**{current_scheme_name}**")
            col_rename, col_copy, col_del = st.columns(3)
            with col_rename:
                if st.button("✏️", key="rename_scheme_btn", help="改名"):
                    st.session_state.show_rename_dialog = True
            with col_copy:
                if st.button("📋", key="copy_scheme_btn", help="复制后改"):
                    st.session_state.show_copy_dialog = True
            with col_del:
                if st.button("🗑️", key="delete_scheme_btn", help="删除"):
                    st.session_state.show_delete_dialog = True

            if st.session_state.get("show_rename_dialog"):
                rename_new_name = st.text_input("新名称", value=current_scheme_name, key="rename_new_name")
                col_confirm, col_cancel = st.columns(2)
                with col_confirm:
                    if st.button("✅ 确认改名", key="confirm_rename"):
                        if rename_new_name.strip() and rename_new_name.strip() != current_scheme_name:
                            with get_conn() as conn:
                                result = update_review_scheme_name(conn, current_scheme_id, rename_new_name.strip())
                            if result["success"]:
                                st.session_state.current_scheme_name = result["new_name"]
                                st.session_state.new_scheme_name = result["new_name"]
                                del st.session_state.show_rename_dialog
                                st.success(f"✅ 已改名为: '{result['new_name']}'")
                                st.rerun()
                            else:
                                st.error(f"❌ {result.get('error', '改名失败')}")
                        else:
                            st.error("请输入不同的新名称")
                with col_cancel:
                    if st.button("❌ 取消", key="cancel_rename"):
                        del st.session_state.show_rename_dialog
                        st.rerun()

            if st.session_state.get("show_copy_dialog"):
                copy_new_name = st.text_input("新方案名称", value=f"{current_scheme_name} 副本", key="copy_new_name")
                copy_new_desc = st.text_area("描述（可选）", key="copy_new_desc", height=50)
                col_confirm, col_cancel = st.columns(2)
                with col_confirm:
                    if st.button("✅ 确认复制", key="confirm_copy"):
                        if copy_new_name.strip():
                            with get_conn() as conn:
                                result = copy_review_scheme(
                                    conn, current_scheme_id, copy_new_name.strip(),
                                    new_description=copy_new_desc.strip() or None,
                                )
                            if result["success"]:
                                st.session_state.current_scheme_id = result["new_scheme_id"]
                                st.session_state.current_scheme_name = result["new_name"]
                                st.session_state.new_scheme_name = result["new_name"]
                                del st.session_state.show_copy_dialog
                                st.success(f"✅ 已复制为: '{result['new_name']}'")
                                st.rerun()
                            elif result.get("needs_confirm"):
                                st.error(f"❌ 名称 '{copy_new_name.strip()}' 已存在")
                            else:
                                st.error(f"❌ {result.get('error', '复制失败')}")
                        else:
                            st.error("请输入新方案名称")
                with col_cancel:
                    if st.button("❌ 取消", key="cancel_copy"):
                        del st.session_state.show_copy_dialog
                        st.rerun()

            if st.session_state.get("show_delete_dialog"):
                st.warning(f"⚠️ 确定要删除方案 '{current_scheme_name}' 吗？")
                col_confirm, col_cancel = st.columns(2)
                with col_confirm:
                    if st.button("✅ 确认删除", type="primary", key="confirm_delete"):
                        with get_conn() as conn:
                            result = delete_review_scheme(conn, current_scheme_id)
                        if result["success"]:
                            for k in ["current_scheme_id", "current_scheme_name", "show_delete_dialog"]:
                                if k in st.session_state:
                                    del st.session_state[k]
                            st.success(f"✅ 已删除方案: '{result['name']}'")
                            st.rerun()
                        else:
                            st.error(f"❌ {result.get('error', '删除失败')}")
                with col_cancel:
                    if st.button("❌ 取消", key="cancel_delete"):
                        del st.session_state.show_delete_dialog
                        st.rerun()

    st.divider()
    st.subheader("📦 方案包导入/导出")

    pkg_col1, pkg_col2 = st.columns(2)
    with pkg_col1:
        st.markdown("**导出方案包**")
        with get_conn() as conn:
            exportable_schemes = get_review_schemes(conn)
        if exportable_schemes:
            export_mode = st.radio(
                "导出模式",
                ["快速导出", "清单导出（支持筛选）"],
                horizontal=True,
                key="export_scheme_mode",
            )

            if export_mode == "快速导出":
                export_scope = st.radio(
                    "导出范围",
                    ["当前方案", "全部方案"],
                    horizontal=True,
                    key="export_scheme_scope",
                )
                export_scheme_ids = None
                if export_scope == "当前方案":
                    cid = st.session_state.get("current_scheme_id")
                    if cid:
                        export_scheme_ids = [cid]
                    else:
                        st.warning("当前未选择方案，请先选择或载入一个方案")
                if export_scope == "全部方案" or (export_scope == "当前方案" and export_scheme_ids):
                    with get_conn() as conn:
                        pkg = export_scheme_package(conn, scheme_ids=export_scheme_ids)
                    pkg_json = json.dumps(pkg, ensure_ascii=False, indent=2, default=str)
                    scope_label = export_scope
                    if export_scope == "当前方案":
                        scope_label = st.session_state.get("current_scheme_name", "当前方案")
                    pkg_file_name = f"scheme_package_{scope_label}_{now_iso()[:10]}.json"
                    pkg_file_name = pkg_file_name.replace(" ", "_").replace("/", "_")
                    st.download_button(
                        "⬇️ 下载方案包 JSON",
                        data=pkg_json.encode("utf-8"),
                        file_name=pkg_file_name,
                        mime="application/json",
                        key="download_scheme_package",
                    )
                    st.caption(f"包含 {pkg['scheme_count']} 个方案，导出时间: {pkg['exported_at'][:19]}")
            else:
                st.markdown("筛选条件")
                manifest_name_filter = st.text_input(
                    "方案名包含",
                    value="",
                    key="manifest_name_filter",
                    placeholder="如：L3、冲突、复盘"
                )

                if exportable_schemes:
                    all_updated = [s["updated_at"] for s in exportable_schemes if s.get("updated_at")]
                    if all_updated:
                        max_updated = max(all_updated)[:10]
                        min_updated = min(all_updated)[:10]
                    else:
                        max_updated = now_iso()[:10]
                        min_updated = "2026-01-01"
                else:
                    max_updated = now_iso()[:10]
                    min_updated = "2026-01-01"

                st.caption(f"方案更新时间范围：{min_updated} ~ {max_updated}")
                date_preset = st.selectbox(
                    "时间快捷筛选",
                    ["不筛选", "最近7天", "最近30天", "最近90天", "2026年之后", "自定义"],
                    key="manifest_date_preset",
                    index=0,
                )
                custom_updated_after = ""
                if date_preset == "自定义":
                    custom_updated_after = st.text_input(
                        "自定义起始日期（YYYY-MM-DD）",
                        value="",
                        key="manifest_custom_date",
                        placeholder=f"如：{min_updated}"
                    )
                elif date_preset == "不筛选":
                    custom_updated_after = ""
                elif date_preset == "最近7天":
                    from datetime import timedelta
                    today = datetime.now()
                    week_ago = today - timedelta(days=7)
                    custom_updated_after = week_ago.isoformat()[:10]
                elif date_preset == "最近30天":
                    from datetime import timedelta
                    today = datetime.now()
                    month_ago = today - timedelta(days=30)
                    custom_updated_after = month_ago.isoformat()[:10]
                elif date_preset == "最近90天":
                    from datetime import timedelta
                    today = datetime.now()
                    qtr_ago = today - timedelta(days=90)
                    custom_updated_after = qtr_ago.isoformat()[:10]
                elif date_preset == "2026年之后":
                    custom_updated_after = "2026-01-01"

                manifest_updated_after = custom_updated_after

                scheme_opts = [(s["id"], s["name"]) for s in exportable_schemes]
                selected_export_names = st.multiselect(
                    "选择要导出的方案",
                    options=[s[1] for s in scheme_opts],
                    default=[s[1] for s in scheme_opts],
                    key="manifest_scheme_select",
                )
                export_scheme_ids = [
                    s[0] for s in scheme_opts if s[1] in selected_export_names
                ]

                if export_scheme_ids:
                    with get_conn() as conn:
                        manifest = export_scheme_manifest(
                            conn,
                            scheme_ids=export_scheme_ids,
                            name_filter=manifest_name_filter or None,
                            updated_after=manifest_updated_after or None,
                        )
                    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2, default=str)
                    pkg_file_name = f"scheme_manifest_{now_iso()[:10]}.json"
                    st.download_button(
                        "⬇️ 下载导出清单 JSON",
                        data=manifest_json.encode("utf-8"),
                        file_name=pkg_file_name,
                        mime="application/json",
                        key="download_scheme_manifest",
                    )
                    st.caption(
                        f"清单包含 {manifest['total_schemes']} 个方案，"
                        f"导出时间: {manifest['exported_at'][:19]}"
                    )
                    with st.expander("清单摘要", expanded=False):
                        st.json({
                            "total_schemes": manifest["total_schemes"],
                            "date_range": manifest["date_range"],
                            "filter_applied": manifest["filter_applied"],
                        })

                    export_pkg_data = None
                    if manifest["schemes"]:
                        pkg_schemes = []
                        for s in manifest["schemes"]:
                            pkg_schemes.append({
                                "name": s["name"],
                                "description": s.get("description"),
                                "filter_state": s["filter_state"],
                                "data_date_range": s.get("data_date_range"),
                                "created_at": s.get("created_at"),
                                "updated_at": s.get("updated_at"),
                            })
                        export_pkg = {
                            "version": "1.0",
                            "exported_at": manifest["exported_at"],
                            "scheme_count": len(pkg_schemes),
                            "schemes": pkg_schemes,
                        }
                        export_pkg_json = json.dumps(export_pkg, ensure_ascii=False, indent=2, default=str)
                        pkg_file_name2 = f"scheme_package_filtered_{now_iso()[:10]}.json"
                        st.download_button(
                            "⬇️ 同时下载可导入的方案包 JSON",
                            data=export_pkg_json.encode("utf-8"),
                            file_name=pkg_file_name2,
                            mime="application/json",
                            key="download_scheme_package_filtered",
                        )
                else:
                    st.info("请至少选择一个方案")
        else:
            st.info("暂无方案可导出")

    with pkg_col2:
        st.markdown("**导入方案包**")

        with get_conn() as conn:
            saved_preview = load_import_preview_context(conn)
            last_policy = load_last_import_policy(conn)
            pending_batches = list_pending_batches(conn)

        if pending_batches:
            only_one = len(pending_batches) == 1
            auto_restored = False
            if only_one and "batch_id" not in st.session_state and not parsed_pkg and not saved_preview:
                auto_batch = load_import_batch(conn, pending_batches[0]["batch_id"])
                if auto_batch:
                    st.session_state.parsed_pkg = auto_batch["package"]
                    st.session_state.preview_result = auto_batch["preview"]
                    st.session_state.batch_id = auto_batch["batch_id"]
                    st.session_state.selected_indices = auto_batch.get("selected_indices", [])
                    st.session_state.item_decisions = auto_batch.get("item_decisions", {})
                    auto_policy = auto_batch["preview"].get("summary", {}).get("conflict_policy")
                    auto_suffix = auto_batch["preview"].get("summary", {}).get("rename_suffix")
                    if auto_policy:
                        st.session_state.selected_policy = auto_policy
                    if auto_suffix:
                        st.session_state.rename_suffix = auto_suffix
                    auto_restored = True
                    st.success(
                        f"💡 自动恢复上次剩余批次（{auto_batch['batch_id'][:12]}...），"
                        f"共 {len(auto_batch.get('selected_indices', []))} 项已勾选，"
                        f"{len(auto_batch.get('item_decisions', {}))} 项有决策备注"
                    )

            if not auto_restored:
                st.warning(f"📋 有 {len(pending_batches)} 个待处理批次")
            batch_options = [
                f"{b['batch_id'][:8]}... ({b['scheme_count']}个方案, {b['created_at'][:19]})"
                for b in pending_batches
            ]
            batch_options.append("（不恢复）")
            selected_batch_idx = st.selectbox(
                "恢复待处理批次（或切换批次）",
                range(len(batch_options)),
                format_func=lambda i: batch_options[i],
                key="select_pending_batch",
            )
            if selected_batch_idx < len(pending_batches):
                if st.button("📂 恢复此批次", key="restore_batch_btn"):
                    batch = load_import_batch(conn, pending_batches[selected_batch_idx]["batch_id"])
                    if batch:
                        st.session_state.parsed_pkg = batch["package"]
                        st.session_state.preview_result = batch["preview"]
                        st.session_state.batch_id = batch["batch_id"]
                        st.session_state.selected_indices = batch.get("selected_indices", [])
                        st.session_state.item_decisions = batch.get("item_decisions", {})
                        bpol = batch["preview"].get("summary", {}).get("conflict_policy")
                        bsuf = batch["preview"].get("summary", {}).get("rename_suffix")
                        if bpol:
                            st.session_state.selected_policy = bpol
                        if bsuf:
                            st.session_state.rename_suffix = bsuf
                        st.rerun()
            if st.button("🗑️ 清除所有待处理批次", key="clear_all_batches"):
                with get_conn() as conn:
                    for b in pending_batches:
                        clear_import_batch(conn, b["batch_id"])
                for k in ["batch_id", "parsed_pkg", "preview_result",
                          "selected_indices", "item_decisions",
                          "selected_policy", "rename_suffix"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
            st.divider()

        if saved_preview and saved_preview.get("success"):
            st.info("💡 检测到未完成的导入预览，已自动恢复")
            if st.button("🔄 清除预览，重新上传", key="clear_saved_preview"):
                with get_conn() as conn:
                    clear_import_preview_context(conn)
                for k in ["parsed_pkg", "preview_result", "selected_policy", "rename_suffix",
                          "batch_id", "selected_indices", "item_decisions"]:
                    if k in st.session_state:
                        del st.session_state[k]
                st.rerun()
            st.divider()

        default_policy = "keep"
        default_suffix = "(导入)"
        if last_policy:
            default_policy = last_policy.get("conflict_policy", "keep")
            default_suffix = last_policy.get("rename_suffix", "(导入)")

        uploaded_pkg = st.file_uploader("上传方案包 JSON 文件", type=["json"], key="upload_scheme_package")

        parsed_pkg = st.session_state.get("parsed_pkg")
        preview_result = st.session_state.get("preview_result")
        batch_id = st.session_state.get("batch_id")

        if saved_preview and not parsed_pkg and not batch_id:
            parsed_pkg = saved_preview.get("package")
            preview_result = saved_preview
            st.session_state.parsed_pkg = parsed_pkg
            st.session_state.preview_result = preview_result
            saved_policy = saved_preview.get("summary", {})
            st.session_state.selected_policy = saved_policy.get("conflict_policy", default_policy)
            st.session_state.rename_suffix = saved_policy.get("rename_suffix", default_suffix)

        if uploaded_pkg and not parsed_pkg:
            try:
                pkg_content = uploaded_pkg.read().decode("utf-8")
                parsed_pkg = json.loads(pkg_content)
                st.session_state.parsed_pkg = parsed_pkg
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                parsed_pkg = None
                st.error(f"❌ 文件解析失败: {e}")

        if parsed_pkg is not None:
            validation = validate_scheme_package(parsed_pkg)
            if not validation["valid"]:
                st.error(f"❌ 方案包校验失败: {validation['error']}")
                if "parsed_pkg" in st.session_state:
                    del st.session_state.parsed_pkg
            else:
                pkg_names = [s["name"] for s in parsed_pkg["schemes"]]
                with get_conn() as conn:
                    existing_names = set(s["name"] for s in get_review_schemes(conn))
                conflicts = [n for n in pkg_names if n in existing_names]

                policy_display_map = {
                    "keep": "保留原方案（跳过）",
                    "overwrite": "覆盖已有方案",
                    "rename": "改名导入",
                }
                policy_reverse_map = {v: k for k, v in policy_display_map.items()}

                current_policy = st.session_state.get("selected_policy", default_policy)

                if conflicts:
                    st.warning(f"⚠️ 以下方案名已存在: {', '.join(conflicts)}")
                    conflict_policy_label = st.radio(
                        "重名冲突处理方式",
                        ["保留原方案（跳过）", "覆盖已有方案", "改名导入"],
                        index=list(policy_reverse_map.keys()).index(policy_display_map.get(current_policy, "保留原方案（跳过）")),
                        key="import_conflict_policy_ui",
                    )
                    selected_policy = policy_reverse_map[conflict_policy_label]
                    st.session_state.selected_policy = selected_policy
                else:
                    selected_policy = current_policy
                    st.session_state.selected_policy = selected_policy

                rename_suffix = st.session_state.get("rename_suffix", default_suffix)
                if selected_policy == "rename":
                    rename_suffix = st.text_input(
                        "改名后缀",
                        value=rename_suffix,
                        key="import_rename_suffix_ui",
                    )
                    st.session_state.rename_suffix = rename_suffix

                if st.button("🔍 预检导入结果", type="secondary", key="preview_import_btn"):
                    with get_conn() as conn:
                        before_schemes = get_review_schemes(conn)
                        before_count = len(before_schemes)
                        before_logs = get_scheme_operation_logs(conn, limit=100)
                        before_import_logs = [
                            l for l in before_logs if l["operation_type"] == "import_scheme"
                        ]

                        preview_result = preview_scheme_package_import(
                            conn, parsed_pkg,
                            conflict_policy=selected_policy,
                            rename_suffix=rename_suffix,
                        )

                        after_schemes = get_review_schemes(conn)
                        after_count = len(after_schemes)
                        after_logs = get_scheme_operation_logs(conn, limit=100)
                        after_import_logs = [
                            l for l in after_logs if l["operation_type"] == "import_scheme"
                        ]

                    if after_count != before_count:
                        st.error("❌ 预检异常：预检阶段修改了数据库，请联系开发人员")
                    elif len(after_import_logs) != len(before_import_logs):
                        st.error("❌ 预检异常：预检阶段留下了操作日志，请联系开发人员")
                    elif preview_result["success"]:
                        st.session_state.preview_result = preview_result
                        st.session_state.batch_id = str(uuid.uuid4())
                        st.session_state.selected_indices = list(range(len(preview_result["preview_results"])))
                        st.session_state.item_decisions = {}
                        with get_conn() as conn:
                            save_import_preview_context(conn, preview_result)
                            save_import_batch(
                                conn,
                                st.session_state.batch_id,
                                parsed_pkg,
                                preview_result,
                                selected_indices=st.session_state.selected_indices,
                                item_decisions=st.session_state.item_decisions,
                            )
                        st.success("✅ 预检完成，以下是导入预览（未写入数据库）")
                    else:
                        st.error(f"❌ 预检失败: {preview_result.get('error', '未知错误')}")

                if preview_result and preview_result.get("success"):
                    summary = preview_result["summary"]
                    preview_items = preview_result["preview_results"]
                    pkg_schemes = preview_result.get("package", {}).get("schemes", [])

                    st.subheader("📋 导入预览")

                    col_sum1, col_sum2, col_sum3, col_sum4 = st.columns(4)
                    with col_sum1:
                        st.metric("方案总数", summary["scheme_count"])
                    with col_sum2:
                        st.metric("新建", summary["created_count"],
                                  help="数据库中不存在，将新建")
                    with col_sum3:
                        st.metric("覆盖", summary["overwritten_count"],
                                  help="数据库中已存在，将覆盖")
                    with col_sum4:
                        st.metric("改名", summary["renamed_count"],
                                  help="数据库中已存在，将改名导入")

                    col_sum5, col_sum6, _ = st.columns([1, 1, 2])
                    with col_sum5:
                        st.metric("保留原方案", summary["kept_count"],
                                  help="数据库中已存在，将跳过")
                    with col_sum6:
                        if summary.get("exported_at"):
                            st.caption(f"📅 包导出时间: {summary['exported_at'][:19]}")
                        if summary.get("min_date") and summary.get("max_date"):
                            st.caption(f"📊 数据时间范围: {summary['min_date'][:10]} ~ {summary['max_date'][:10]}")

                    with st.expander("🔍 回放预检状态对拍（已入库/待处理/冲突结果一览）", expanded=False):
                        st.caption("逐个核对：当前DB状态 ↔ 包内版本 ↔ 预检处理结果")
                        for i, item in enumerate(preview_items):
                            original_name = item.get("original_name", item["name"])
                            target_name = item["name"]
                            action = item["action"]
                            label = SCHEME_ACTION_LABELS.get(action, action)
                            with get_conn() as conn:
                                exists = get_review_scheme_by_name(conn, original_name)
                            status_badge = "✅ 已入库" if exists else "🆕 未入库"
                            conflict = "⚠️ 冲突" if exists and action != SCHEME_ACTION_CREATED else "—"
                            result_map = {
                                SCHEME_ACTION_CREATED: "→ 新建入库",
                                SCHEME_ACTION_OVERWRITTEN: "→ 覆盖已有",
                                SCHEME_ACTION_RENAMED: f"→ 改名导入为 '{target_name}'",
                                SCHEME_ACTION_KEPT: "→ 保留原方案（跳过）",
                            }
                            outcome = result_map.get(action, "—")
                            st.markdown(
                                f"**[{i+1}] '{original_name}'**  |  DB状态: {status_badge}  |  "
                                f"冲突: {conflict}  |  预检结果: **{label}** {outcome}"
                            )
                        st.divider()
                        st.markdown(f"**汇总：** 方案总数 {summary['scheme_count']}，"
                                    f"新建🆕 {summary['created_count']}，"
                                    f"覆盖♻️ {summary['overwritten_count']}，"
                                    f"改名✏️ {summary['renamed_count']}，"
                                    f"保留跳过⏭️ {summary['kept_count']}")

                    st.markdown("**勾选本次要导入的方案（未勾选的留在待处理批次）：**")

                    selected_indices = st.session_state.get("selected_indices",
                                                           list(range(len(preview_items))))
                    item_decisions = st.session_state.get("item_decisions", {})

                    new_selected = []
                    new_decisions = dict(item_decisions)

                    emoji_map = {
                        SCHEME_ACTION_CREATED: "🆕",
                        SCHEME_ACTION_OVERWRITTEN: "♻️",
                        SCHEME_ACTION_RENAMED: "✏️",
                        SCHEME_ACTION_KEPT: "⏭️",
                    }

                    for i, item in enumerate(preview_items):
                        action = item["action"]
                        label = SCHEME_ACTION_LABELS.get(action, action)
                        name = item["name"]
                        original_name = item.get("original_name", name)
                        emoji = emoji_map.get(action, "📦")

                        incoming_scheme = pkg_schemes[i] if i < len(pkg_schemes) else {}
                        with get_conn() as conn:
                            local_scheme = get_review_scheme_by_name(conn, original_name)
                        diffs = compute_scheme_diff(local_scheme, incoming_scheme)

                        checked = i in selected_indices
                        col_check, col_detail = st.columns([1, 5])
                        with col_check:
                            is_selected = st.checkbox(
                                f"导入",
                                value=checked,
                                key=f"import_select_{i}",
                            )
                        with col_detail:
                            header = f"{emoji} '{original_name}'"
                            if action == SCHEME_ACTION_RENAMED:
                                header += f" → '{name}'"
                            header += f" （{label}）"
                            st.markdown(f"**{header}**")

                            if diffs:
                                diff_rows = []
                                for d in diffs:
                                    diff_rows.append(f"| {d['field']} | {d['local']} | {d['incoming']} |")
                                diff_table = "| 差异项 | 本地 | 待导入 |\n|---|---|---|\n" + "\n".join(diff_rows)
                                st.markdown(diff_table)
                            elif local_scheme:
                                st.caption("✅ 与本地版本一致，无差异")
                            else:
                                st.caption("🆕 本地不存在此方案")

                            if action in (SCHEME_ACTION_OVERWRITTEN, SCHEME_ACTION_KEPT, SCHEME_ACTION_RENAMED):
                                per_action_options = ["默认（跟随全局策略）", "保留原方案", "覆盖已有方案", "改名导入"]
                                per_action_values = [None, "kept", "overwritten", "renamed"]
                                current_per = new_decisions.get(str(i), {}).get("action")
                                per_idx = 0
                                if current_per in per_action_values:
                                    per_idx = per_action_values.index(current_per)
                                per_choice = st.selectbox(
                                    "冲突处理",
                                    per_action_options,
                                    index=per_idx,
                                    key=f"per_action_{i}",
                                )
                                chosen_action = per_action_values[per_action_options.index(per_choice)]

                                rename_suffix_val = rename_suffix
                                if chosen_action == "renamed":
                                    rename_suffix_val = st.text_input(
                                        "改名后缀",
                                        value=new_decisions.get(str(i), {}).get("rename_suffix", rename_suffix),
                                        key=f"per_rename_{i}",
                                    )

                                note = st.text_input(
                                    "备注",
                                    value=new_decisions.get(str(i), {}).get("note", ""),
                                    key=f"per_note_{i}",
                                )

                                dec = {}
                                if chosen_action:
                                    dec["action"] = chosen_action
                                if chosen_action == "renamed":
                                    dec["rename_suffix"] = rename_suffix_val
                                if note:
                                    dec["note"] = note
                                new_decisions[str(i)] = dec
                            else:
                                note = st.text_input(
                                    "备注",
                                    value=new_decisions.get(str(i), {}).get("note", ""),
                                    key=f"per_note_{i}",
                                )
                                if note:
                                    new_decisions[str(i)] = {"note": note}

                        if is_selected:
                            new_selected.append(i)

                    st.session_state.selected_indices = new_selected
                    st.session_state.item_decisions = new_decisions

                    if batch_id:
                        with get_conn() as conn:
                            update_batch_selection(conn, batch_id, new_selected, new_decisions)

                    col_confirm, col_partial, col_cancel = st.columns(3)
                    with col_confirm:
                        if st.button("✅ 全部确认导入", type="primary", key="confirm_import_scheme"):
                            all_indices = list(range(len(preview_items)))
                            with get_conn() as conn:
                                result = confirm_partial_scheme_import(
                                    conn, preview_result, all_indices, item_decisions
                                )
                            if result["success"]:
                                processed = result.get("processed_indices", all_indices)
                                st.success(
                                    f"✅ 导入完成！成功 {result['imported_count']} 个，"
                                    f"跳过 {result['skipped_count']} 个，共 {result['total']} 个"
                                )
                                for r in result["results"]:
                                    lbl = SCHEME_ACTION_LABELS.get(r["action"], r["action"])
                                    emj = emoji_map.get(r["action"], "📦")
                                    if r["action"] == SCHEME_ACTION_RENAMED:
                                        st.info(f"{emj} '{r.get('original_name', r['name'])}' → '{r['name']}' ({lbl})")
                                    else:
                                        st.info(f"{emj} '{r['name']}' ({lbl})")
                                with get_conn() as conn:
                                    clear_import_preview_context(conn)
                                    if batch_id:
                                        shrink_result = shrink_batch_to_remaining(
                                            conn, batch_id, processed,
                                            conflict_policy=result.get("conflict_policy"),
                                            rename_suffix=result.get("rename_suffix_global"),
                                        )
                                        if not shrink_result.get("success"):
                                            st.warning(
                                                f"批次收口异常: {shrink_result.get('error','')}，"
                                                f"将手动标记完成"
                                            )
                                            mark_batch_completed(conn, batch_id)
                                for k in ["parsed_pkg", "preview_result", "selected_policy",
                                          "rename_suffix", "batch_id", "selected_indices",
                                          "item_decisions"]:
                                    if k in st.session_state:
                                        del st.session_state[k]
                                st.rerun()
                            else:
                                st.error(f"❌ 导入失败: {result['error']}")

                    with col_partial:
                        if st.button("☑️ 导入选中项", type="secondary", key="partial_import_scheme"):
                            if not new_selected:
                                st.warning("请至少勾选一个方案")
                            else:
                                shrink_result = None
                                with get_conn() as conn:
                                    result = confirm_partial_scheme_import(
                                        conn, preview_result, new_selected, new_decisions
                                    )
                                if result["success"]:
                                    processed = result.get("processed_indices", list(new_selected))
                                    remaining_count = result.get("remaining_count", 0)
                                    st.success(
                                        f"✅ 已导入 {result['imported_count']} 个方案，"
                                        f"跳过 {result['skipped_count']} 个"
                                    )
                                    for r in result["results"]:
                                        lbl = SCHEME_ACTION_LABELS.get(r["action"], r["action"])
                                        emj = emoji_map.get(r["action"], "📦")
                                        if r["action"] == SCHEME_ACTION_RENAMED:
                                            st.info(f"{emj} '{r.get('original_name', r['name'])}' → '{r['name']}' ({lbl})")
                                        else:
                                            st.info(f"{emj} '{r['name']}' ({lbl})")

                                    if batch_id:
                                        with get_conn() as conn:
                                            shrink_result = shrink_batch_to_remaining(
                                                conn, batch_id, processed,
                                                conflict_policy=result.get("conflict_policy"),
                                                rename_suffix=result.get("rename_suffix_global"),
                                            )
                                        if shrink_result and shrink_result.get("success"):
                                            if shrink_result.get("all_completed"):
                                                st.success("✅ 本批次所有方案已全部处理完成，批次已收口")
                                            else:
                                                st.info(
                                                    f"📋 还有 {shrink_result['remaining_count']} 个方案留在待处理批次中，"
                                                    f"新批次ID: {shrink_result['new_batch_id'][:16]}...，"
                                                    f"下次打开应用会自动恢复。"
                                                )
                                                new_remain_dec = shrink_result.get("remaining_item_decisions", {})
                                                new_remain_sel = shrink_result.get("remaining_selected_indices", [])
                                                if new_remain_dec or new_remain_sel:
                                                    st.caption(
                                                        f"已保留: {len(new_remain_sel)} 项勾选，"
                                                        f"{len(new_remain_dec)} 项决策/备注"
                                                    )
                                        elif shrink_result and not shrink_result.get("success"):
                                            st.error(
                                                f"批次收口失败: {shrink_result.get('error','未知错误')}，"
                                                f"将改用兼容方式手动重建剩余批次"
                                            )

                                    with get_conn() as conn:
                                        clear_import_preview_context(conn)

                                    for k in ["parsed_pkg", "preview_result", "selected_policy",
                                              "rename_suffix", "selected_indices", "item_decisions"]:
                                        if k in st.session_state:
                                            del st.session_state[k]
                                    if "batch_id" in st.session_state:
                                        del st.session_state["batch_id"]
                                    if remaining_count > 0:
                                        st.rerun()
                                else:
                                    st.error(f"❌ 导入失败: {result['error']}")

                    with col_cancel:
                        if st.button("❌ 取消导入", key="cancel_import_scheme"):
                            with get_conn() as conn:
                                clear_import_preview_context(conn)
                                if batch_id:
                                    update_batch_selection(
                                        conn, batch_id,
                                        st.session_state.get("selected_indices", []),
                                        st.session_state.get("item_decisions", {}),
                                    )

                            for k in ["parsed_pkg", "preview_result", "selected_policy",
                                      "rename_suffix", "selected_indices", "item_decisions"]:
                                if k in st.session_state:
                                    del st.session_state[k]

                            st.success("✅ 已取消导入，数据库未做任何修改。勾选状态已保存到待处理批次。")
                            st.rerun()

    with st.expander("📜 导入决策日志与操作日志（可查询筛选）", expanded=False):
        st.markdown("**导入决策日志查询（保留/覆盖/改名等决策均可查）**")
        log_col1, log_col2, log_col3 = st.columns(3)
        with log_col1:
            log_scheme_name = st.text_input(
                "按方案名搜索",
                value="",
                key="log_scheme_name_filter",
                placeholder="输入方案名关键词，如：存在冲突A"
            )
        with log_col2:
            log_type_choices = [
                ("全部（含批次操作）", "import_scheme,import_scheme_note,batch_shrink,batch_create_remaining,batch_close,create,update,rename,copy,delete,load,export_scheme"),
                ("仅导入决策（保留/覆盖/改名/新建）", "import_scheme"),
                ("仅导入备注", "import_scheme_note"),
                ("导入决策+备注", "import_scheme,import_scheme_note"),
                ("仅批次收口/拆分操作", "batch_shrink,batch_create_remaining,batch_close"),
                ("仅方案管理操作（新建/更新/改名/复制/删除）", "create,update,rename,copy,delete"),
            ]
            log_type_labels = [c[0] for c in log_type_choices]
            log_type_values = [c[1] for c in log_type_choices]
            log_type_choice = st.selectbox(
                "日志类型",
                range(len(log_type_choices)),
                format_func=lambda i: log_type_labels[i],
                index=2,
                key="log_type_filter",
            )
            selected_log_type = log_type_values[log_type_choice]
        with log_col3:
            log_limit = st.number_input(
                "返回条数",
                min_value=10, max_value=1000, value=100, step=50,
                key="log_limit_filter"
            )

        with get_conn() as conn:
            filtered_logs = get_import_decision_logs(
                conn,
                scheme_name=log_scheme_name or None,
                operation_type=selected_log_type,
                limit=int(log_limit),
            )

        if filtered_logs:
            log_df = pd.DataFrame(filtered_logs)
            log_df = log_df[["operated_at", "scheme_name", "operation_type", "operation_detail", "operator"]]
            log_df.columns = ["操作时间", "方案名称", "操作类型", "操作详情", "操作人"]
            log_df["操作时间"] = log_df["操作时间"].str[:19]
            type_labels = {
                "create": "新建方案", "update": "更新方案", "rename": "改名方案",
                "copy": "复制方案", "delete": "删除方案", "load": "载入方案",
                "export_scheme": "导出方案包",
                "import_scheme": "导入决策",
                "import_scheme_note": "导入备注",
                "batch_shrink": "批次收口",
                "batch_create_remaining": "分出剩余批次",
                "batch_close": "批次完成关闭",
            }
            log_df["操作类型"] = log_df["操作类型"].map(type_labels).fillna(log_df["操作类型"])
            st.dataframe(log_df, use_container_width=True, hide_index=True)
            st.caption(f"匹配 {len(filtered_logs)} 条日志")
        else:
            st.info("暂无匹配的日志记录")

        st.divider()
        st.markdown("**全部操作日志（最近50条原始记录）**")
        if operation_logs:
            log_all_df = pd.DataFrame(operation_logs)
            log_all_df = log_all_df[["operated_at", "scheme_name", "operation_type", "operation_detail", "operator"]]
            log_all_df.columns = ["操作时间", "方案名称", "操作类型", "操作详情", "操作人"]
            log_all_df["操作时间"] = log_all_df["操作时间"].str[:19]
            log_all_df["操作类型"] = log_all_df["操作类型"].map(type_labels).fillna(log_all_df["操作类型"])
            st.dataframe(log_all_df, use_container_width=True, hide_index=True)
        else:
            st.info("暂无操作日志")

    st.divider()
    st.subheader("🔍 筛选条件")

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        filter_store = st.selectbox(
            "按门店筛选",
            ["全部"] + stores,
            key="review_store",
        )
        filter_barcode = st.text_input(
            "按商品条码/名称搜索",
            value=st.session_state.get("review_barcode", ""),
            key="review_barcode",
            placeholder="输入条码或商品名称关键词",
        )
        filter_status = st.selectbox(
            "按状态筛选",
            ["全部"] + list(STATUS_LABELS.keys()),
            format_func=lambda x: "全部" if x == "全部" else STATUS_LABELS.get(x, x),
            key="review_status",
        )

    with col_f2:
        rv_options = [(0, "全部规则版本")] + [(v["version"], f'v{v["version"]} ({v["disc_count"]}条差异)') for v in rule_versions]
        rv_display_a = {k: v for k, v in rv_options}
        rv_display_b = {k: v for k, v in rv_options}
        filter_rule_a = st.selectbox(
            "对比版本 A（旧规则）",
            list(rv_display_a.keys()),
            format_func=lambda x: rv_display_a[x],
            key="review_rule_ver_a",
        )
        filter_rule_b = st.selectbox(
            "对比版本 B（新规则）",
            list(rv_display_b.keys()),
            format_func=lambda x: rv_display_b[x],
            key="review_rule_ver_b",
        )
        date_from_val = st.text_input(
            "开始时间 (YYYY-MM-DD或ISO格式，留空不限)",
            value=st.session_state.get("review_date_from", ""),
            key="review_date_from",
            placeholder="如 2026-01-01 或 2026-01-01T00:00:00",
        )
        date_to_val = st.text_input(
            "结束时间 (YYYY-MM-DD或ISO格式，留空不限)",
            value=st.session_state.get("review_date_to", ""),
            key="review_date_to",
            placeholder="如 2026-12-31 或 2026-12-31T23:59:59",
        )
        if date_range:
            st.caption(f"📅 数据时间范围: {date_range.get('min_date','')[:10]} ~ {date_range.get('max_date','')[:10]}")

    col_btn1, col_btn2, _ = st.columns([1, 1, 3])
    with col_btn1:
        if st.button("💾 记住当前筛选", type="secondary", key="save_filter"):
            state_to_save = {
                "store_id": filter_store,
                "barcode": filter_barcode,
                "rule_ver_a": filter_rule_a,
                "rule_ver_b": filter_rule_b,
                "status": filter_status,
                "date_from": date_from_val,
                "date_to": date_to_val,
                "saved_at": now_iso(),
            }
            with get_conn() as conn:
                save_ui_state(conn, "review_filter_state", state_to_save)
            st.success("✅ 筛选条件已保存，重启后可自动恢复")
    with col_btn2:
        if st.button("🔄 重置筛选", key="reset_filter"):
            if "review_filter_init" in st.session_state:
                del st.session_state.review_filter_init
            for k in ["review_store", "review_barcode", "review_rule_ver_a", "review_rule_ver_b", "review_status", "review_date_from", "review_date_to"]:
                if k in st.session_state:
                    del st.session_state[k]
            with get_conn() as conn:
                save_ui_state(conn, "review_filter_state", None)
            st.rerun()

    if saved_state:
        st.info(f"💡 已恢复上次筛选组合（保存于 {saved_state.get('saved_at', '')[:19]}）")

    st.divider()

    store_param = None if filter_store == "全部" else filter_store
    status_param = None if filter_status == "全部" else filter_status
    rule_param_a = None if filter_rule_a == 0 else filter_rule_a
    rule_param_b = None if filter_rule_b == 0 else filter_rule_b
    barcode_param = filter_barcode if filter_barcode else None
    date_from_param = date_from_val if date_from_val and date_from_val.strip() else None
    date_to_param = date_to_val if date_to_val and date_to_val.strip() else None

    with get_conn() as conn:
        discs_a = []
        discs_b = []
        if rule_param_a:
            discs_a = get_discrepancies_extended(
                conn, store_id=store_param, status=status_param,
                rule_version=rule_param_a, barcode=barcode_param,
                date_from=date_from_param, date_to=date_to_param,
            )
        if rule_param_b:
            discs_b = get_discrepancies_extended(
                conn, store_id=store_param, status=status_param,
                rule_version=rule_param_b, barcode=barcode_param,
                date_from=date_from_param, date_to=date_to_param,
            )
        if not rule_param_a and not rule_param_b:
            discs_a = get_discrepancies_extended(
                conn, store_id=store_param, status=status_param,
                barcode=barcode_param,
                date_from=date_from_param, date_to=date_to_param,
            )

    def _build_key_map(discs):
        m = {}
        for d in discs:
            key = (d["store_id"], d["barcode"])
            if key not in m:
                m[key] = []
            m[key].append(d)
        return m

    map_a = _build_key_map(discs_a)
    map_b = _build_key_map(discs_b)
    all_keys = sorted(set(map_a.keys()) | set(map_b.keys()))

    if not all_keys:
        st.info("暂无符合筛选条件的差异记录，请调整筛选条件或先导入数据并运行归因")
    else:
        summary_cols = st.columns(4)
        with summary_cols[0]:
            st.metric(f"版本A {'v'+str(filter_rule_a) if filter_rule_a else '(全部)'}", f"{len(discs_a)} 条差异")
        with summary_cols[1]:
            st.metric(f"版本B {'v'+str(filter_rule_b) if filter_rule_b else '(全部)'}", f"{len(discs_b)} 条差异")
        with summary_cols[2]:
            only_a = len(set(map_a.keys()) - set(map_b.keys()))
            st.metric("仅版本A有", f"{only_a} 个商品")
        with summary_cols[3]:
            only_b = len(set(map_b.keys()) - set(map_a.keys()))
            st.metric("仅版本B有", f"{only_b} 个商品")

        if rule_param_a and rule_param_b:
            diff_total = 0
            changed_causes = 0
            for key in all_keys:
                if key in map_a and key in map_b:
                    da = map_a[key][0]
                    db = map_b[key][0]
                    if abs(da["diff_qty"] - db["diff_qty"]) > 0.001:
                        diff_total += 1
                    if da.get("attributed_cause") != db.get("attributed_cause"):
                        changed_causes += 1
            sum_col2 = st.columns(2)
            with sum_col2[0]:
                st.metric("差异数量变化的商品", f"{diff_total} 个")
            with sum_col2[1]:
                st.metric("归因结果变化的商品", f"{changed_causes} 个")

        st.divider()
        st.subheader("📋 并排对比详情")

        for key in all_keys:
            store_id, barcode = key
            list_a = map_a.get(key, [])
            list_b = map_b.get(key, [])
            da = list_a[0] if list_a else None
            db = list_b[0] if list_b else None

            sku_name = ""
            if da:
                sku_name = da.get("sku_name", "")
            elif db:
                sku_name = db.get("sku_name", "")

            badge_a = ""
            badge_b = ""
            if da and db:
                if abs(da["diff_qty"] - db["diff_qty"]) > 0.001:
                    badge_a = " ⚠️ 差异量变化"
                if da.get("attributed_cause") != db.get("attributed_cause"):
                    badge_b = " ⚠️ 归因变化"
            if da and not db:
                badge_a = " 📌 仅A有"
            if db and not da:
                badge_b = " 🆕 仅B有"

            with st.expander(
                f"[{store_id}] {sku_name or barcode} "
                f"{'| 版本A' + badge_a if da else ''} "
                f"{'| 版本B' + badge_b if db else ''}"
            ):
                col_hdr1, col_hdr2 = st.columns(2)
                with col_hdr1:
                    label_a = f"版本A - 规则 v{da['rule_ver']}" if da else "版本A - 无记录"
                    st.markdown(f"### {label_a}")
                with col_hdr2:
                    label_b = f"版本B - 规则 v{db['rule_ver']}" if db else "版本B - 无记录"
                    st.markdown(f"### {label_b}")

                col_alias1, col_alias2 = st.columns(2)
                with col_alias1:
                    if da:
                        with get_conn() as conn:
                            snap_a = get_snapshot_for_discrepancy(conn, da["id"])
                        st.markdown("**🏷️ 别名映射**")
                        if snap_a and snap_a.get("alias_before"):
                            st.markdown(f"映射前: `{snap_a['alias_before']}` → 映射后: `{snap_a['alias_after']}`")
                        else:
                            st.markdown("无别名映射")
                    else:
                        st.markdown("—")
                with col_alias2:
                    if db:
                        with get_conn() as conn:
                            snap_b = get_snapshot_for_discrepancy(conn, db["id"])
                        st.markdown("**🏷️ 别名映射**")
                        if snap_b and snap_b.get("alias_before"):
                            st.markdown(f"映射前: `{snap_b['alias_before']}` → 映射后: `{snap_b['alias_after']}`")
                        else:
                            st.markdown("无别名映射")
                    else:
                        st.markdown("—")

                col1, col2 = st.columns(2)
                with col1:
                    if da:
                        cause_a = CAUSE_LABELS.get(da["attributed_cause"], da["attributed_cause"] or "未归因")
                        st.markdown(f"**系统数量**: {da['system_qty']:.1f}")
                        st.markdown(f"**实际数量**: {da['actual_qty']:.1f}")
                        diff_style = "color: red" if da['diff_qty'] > 0 else "color: green"
                        st.markdown(f"**差异数量**: <span style='{diff_style}'>{da['diff_qty']:+.1f}</span>", unsafe_allow_html=True)
                        st.markdown(f"**归因**: {cause_a}")
                        st.markdown(f"**归因详情**: {da.get('cause_detail', '-')}")
                        st.markdown(f"**状态**: {STATUS_LABELS.get(da['status'], da['status'])}")
                        st.markdown(f"**规则版本**: v{da.get('rule_ver', '-')}")
                        if da.get("review_note"):
                            st.markdown(f"**复核备注**: {da['review_note']}")
                        st.markdown(f"**创建时间**: {da['created_at'][:19]}")
                        st.markdown(_status_badge(da["status"]), unsafe_allow_html=True)
                    else:
                        st.markdown("*此版本无该商品记录*")
                with col2:
                    if db:
                        cause_b = CAUSE_LABELS.get(db["attributed_cause"], db["attributed_cause"] or "未归因")
                        st.markdown(f"**系统数量**: {db['system_qty']:.1f}")
                        st.markdown(f"**实际数量**: {db['actual_qty']:.1f}")
                        diff_style = "color: red" if db['diff_qty'] > 0 else "color: green"
                        st.markdown(f"**差异数量**: <span style='{diff_style}'>{db['diff_qty']:+.1f}</span>", unsafe_allow_html=True)
                        st.markdown(f"**归因**: {cause_b}")
                        st.markdown(f"**归因详情**: {db.get('cause_detail', '-')}")
                        st.markdown(f"**状态**: {STATUS_LABELS.get(db['status'], db['status'])}")
                        st.markdown(f"**规则版本**: v{db.get('rule_ver', '-')}")
                        if db.get("review_note"):
                            st.markdown(f"**复核备注**: {db['review_note']}")
                        st.markdown(f"**创建时间**: {db['created_at'][:19]}")
                        st.markdown(_status_badge(db["status"]), unsafe_allow_html=True)
                    else:
                        st.markdown("*此版本无该商品记录*")

                if da and db:
                    if abs(da["diff_qty"] - db["diff_qty"]) > 0.001 or da.get("attributed_cause") != db.get("attributed_cause"):
                        st.warning("⚠️ 两个版本间存在差异")
                        delta_qty = db["diff_qty"] - da["diff_qty"]
                        st.markdown(f"- **差异量变化**: {da['diff_qty']:+.1f} → {db['diff_qty']:+.1f} (Δ {delta_qty:+.1f})")
                        if da.get("attributed_cause") != db.get("attributed_cause"):
                            st.markdown(f"- **归因变化**: {CAUSE_LABELS.get(da['attributed_cause'], '未归因')} → {CAUSE_LABELS.get(db['attributed_cause'], '未归因')}")

                st.divider()
                st.markdown("**🧾 归因快照对比（当时生效规则）**")
                col_snap1, col_snap2 = st.columns(2)
                with col_snap1:
                    if da:
                        with get_conn() as conn:
                            snap_a = get_snapshot_for_discrepancy(conn, da["id"])
                            steps_a = get_calc_steps_for_discrepancy(conn, da["id"])
                        if snap_a:
                            cfg_a = snap_a.get("rule_config_snapshot", {}) or {}
                            st.markdown(f"**损耗阈值**: {cfg_a.get('loss_threshold_pct', '-')}% / 绝对值 {cfg_a.get('loss_threshold_abs', '-')}")
                            st.markdown(f"**调拨延迟窗口**: {cfg_a.get('transfer_delay_days', '-')} 天")
                            aliases_a = cfg_a.get("aliases", {})
                            if aliases_a:
                                st.markdown(f"**别名映射规则**: `{json.dumps(aliases_a, ensure_ascii=False)}`")
                            st.markdown(f"**快照生成时间**: {snap_a.get('created_at', '')[:19]}")
                            with st.expander(f"📊 计算步骤 ({len(steps_a)} 步)", expanded=False):
                                for cs in steps_a:
                                    step_label = {
                                        "init": "🔢 初始", "sales": "🛒 销售", "transfer_out": "📤 调出",
                                        "transfer_in": "📥 调入", "normal_loss": "⚖️ 损耗",
                                        "unknown_loss": "❓ 缺失", "unknown_surplus": "📈 盘盈",
                                    }.get(cs["step_type"], cs["step_type"])
                                    if cs["step_type"] == "init":
                                        st.markdown(f"{step_label}: {cs['step_description']}")
                                    else:
                                        st.markdown(f"{step_label}: {cs['step_description']} (扣{cs['amount_applied']:+.1f}, 剩{cs['remaining_after']:.1f})")
                        else:
                            st.markdown("*无快照*")
                    else:
                        st.markdown("—")
                with col_snap2:
                    if db:
                        with get_conn() as conn:
                            snap_b = get_snapshot_for_discrepancy(conn, db["id"])
                            steps_b = get_calc_steps_for_discrepancy(conn, db["id"])
                        if snap_b:
                            cfg_b = snap_b.get("rule_config_snapshot", {}) or {}
                            st.markdown(f"**损耗阈值**: {cfg_b.get('loss_threshold_pct', '-')}% / 绝对值 {cfg_b.get('loss_threshold_abs', '-')}")
                            st.markdown(f"**调拨延迟窗口**: {cfg_b.get('transfer_delay_days', '-')} 天")
                            aliases_b = cfg_b.get("aliases", {})
                            if aliases_b:
                                st.markdown(f"**别名映射规则**: `{json.dumps(aliases_b, ensure_ascii=False)}`")
                            st.markdown(f"**快照生成时间**: {snap_b.get('created_at', '')[:19]}")
                            with st.expander(f"📊 计算步骤 ({len(steps_b)} 步)", expanded=False):
                                for cs in steps_b:
                                    step_label = {
                                        "init": "🔢 初始", "sales": "🛒 销售", "transfer_out": "📤 调出",
                                        "transfer_in": "📥 调入", "normal_loss": "⚖️ 损耗",
                                        "unknown_loss": "❓ 缺失", "unknown_surplus": "📈 盘盈",
                                    }.get(cs["step_type"], cs["step_type"])
                                    if cs["step_type"] == "init":
                                        st.markdown(f"{step_label}: {cs['step_description']}")
                                    else:
                                        st.markdown(f"{step_label}: {cs['step_description']} (扣{cs['amount_applied']:+.1f}, 剩{cs['remaining_after']:.1f})")
                        else:
                            st.markdown("*无快照*")
                    else:
                        st.markdown("—")

        st.divider()
        st.subheader("📤 导出对比结果（含筛选条件+对比摘要）")

        export_format_rv = st.radio("导出格式", ["CSV", "JSON"], horizontal=True, key="export_format_rv")

        all_export_data = []
        for key in all_keys:
            store_id, barcode = key
            list_a = map_a.get(key, [])
            list_b = map_b.get(key, [])
            da = list_a[0] if list_a else None
            db = list_b[0] if list_b else None

            row = {
                "store_id": store_id,
                "barcode": barcode,
                "sku_name": "",
            }

            if da:
                row["sku_name"] = da.get("sku_name", "")
                row["v_a_rule_ver"] = da.get("rule_ver", "")
                row["v_a_system_qty"] = da["system_qty"]
                row["v_a_actual_qty"] = da["actual_qty"]
                row["v_a_diff_qty"] = da["diff_qty"]
                row["v_a_cause"] = CAUSE_LABELS.get(da["attributed_cause"], da["attributed_cause"] or "未归因")
                row["v_a_cause_detail"] = da.get("cause_detail", "")
                row["v_a_status"] = STATUS_LABELS.get(da["status"], da["status"])
                row["v_a_review_note"] = da.get("review_note", "")
                row["v_a_created_at"] = da["created_at"]
            else:
                row["v_a_rule_ver"] = ""
                row["v_a_system_qty"] = ""
                row["v_a_actual_qty"] = ""
                row["v_a_diff_qty"] = ""
                row["v_a_cause"] = "无记录"
                row["v_a_cause_detail"] = ""
                row["v_a_status"] = ""
                row["v_a_review_note"] = ""
                row["v_a_created_at"] = ""

            if db:
                if not row["sku_name"]:
                    row["sku_name"] = db.get("sku_name", "")
                row["v_b_rule_ver"] = db.get("rule_ver", "")
                row["v_b_system_qty"] = db["system_qty"]
                row["v_b_actual_qty"] = db["actual_qty"]
                row["v_b_diff_qty"] = db["diff_qty"]
                row["v_b_cause"] = CAUSE_LABELS.get(db["attributed_cause"], db["attributed_cause"] or "未归因")
                row["v_b_cause_detail"] = db.get("cause_detail", "")
                row["v_b_status"] = STATUS_LABELS.get(db["status"], db["status"])
                row["v_b_review_note"] = db.get("review_note", "")
                row["v_b_created_at"] = db["created_at"]
            else:
                row["v_b_rule_ver"] = ""
                row["v_b_system_qty"] = ""
                row["v_b_actual_qty"] = ""
                row["v_b_diff_qty"] = ""
                row["v_b_cause"] = "无记录"
                row["v_b_cause_detail"] = ""
                row["v_b_status"] = ""
                row["v_b_review_note"] = ""
                row["v_b_created_at"] = ""

            if da and db:
                row["diff_qty_change"] = db["diff_qty"] - da["diff_qty"]
                row["cause_changed"] = "是" if da.get("attributed_cause") != db.get("attributed_cause") else "否"
            else:
                row["diff_qty_change"] = ""
                row["cause_changed"] = ""

            all_export_data.append(row)

        current_scheme_id = st.session_state.get("current_scheme_id")
        current_scheme_name = st.session_state.get("current_scheme_name", "")

        rule_a_label = f"v{filter_rule_a}" if filter_rule_a else "全部"
        rule_b_label = f"v{filter_rule_b}" if filter_rule_b else "全部"
        if rule_versions:
            rv_map = {v["version"]: v for v in rule_versions}
            if filter_rule_a and filter_rule_a in rv_map:
                rv_a = rv_map[filter_rule_a]
                cfg_a = json.loads(rv_a["config_json"]) if rv_a.get("config_json") else {}
                rule_a_label += f" (损耗阈值{cfg_a.get('loss_threshold_pct', '-')}%)"
            if filter_rule_b and filter_rule_b in rv_map:
                rv_b = rv_map[filter_rule_b]
                cfg_b = json.loads(rv_b["config_json"]) if rv_b.get("config_json") else {}
                rule_b_label += f" (损耗阈值{cfg_b.get('loss_threshold_pct', '-')}%)"

        filter_summary = {
            "exported_at": now_iso(),
            "scheme_name": current_scheme_name or "",
            "scheme_id": current_scheme_id or "",
            "filter_store": filter_store,
            "filter_barcode": filter_barcode,
            "filter_status": filter_status,
            "filter_rule_a": filter_rule_a,
            "filter_rule_b": filter_rule_b,
            "filter_rule_a_label": rule_a_label,
            "filter_rule_b_label": rule_b_label,
            "filter_date_from": date_from_param or "",
            "filter_date_to": date_to_param or "",
            "version_summary": {
                "rule_a": rule_a_label,
                "rule_b": rule_b_label,
            },
            "summary": {
                "total_items": len(all_keys),
                "count_version_a": len(discs_a),
                "count_version_b": len(discs_b),
                "only_in_a": len(set(map_a.keys()) - set(map_b.keys())),
                "only_in_b": len(set(map_b.keys()) - set(map_a.keys())),
            }
        }

        if rule_param_a and rule_param_b:
            diff_qty_count = sum(1 for r in all_export_data if r.get("diff_qty_change") and abs(r["diff_qty_change"]) > 0.001)
            cause_change_count = sum(1 for r in all_export_data if r.get("cause_changed") == "是")
            filter_summary["summary"]["diff_qty_changed"] = diff_qty_count
            filter_summary["summary"]["cause_changed"] = cause_change_count

        if current_scheme_id and current_scheme_name:
            with get_conn() as conn:
                log_scheme_operation(
                    conn, current_scheme_id, current_scheme_name, "export",
                    f"导出对比结果，共{len(all_keys)}条商品，格式:{export_format_rv}"
                )

        if all_export_data:
            if export_format_rv == "CSV":
                df_rv = pd.DataFrame(all_export_data)
                df_rv.insert(0, "scheme_name", current_scheme_name or "")
                df_rv.insert(1, "filter_store", filter_store)
                df_rv.insert(2, "filter_barcode", filter_barcode)
                df_rv.insert(3, "filter_rule_a", rule_a_label)
                df_rv.insert(4, "filter_rule_b", rule_b_label)
                df_rv.insert(5, "filter_status", filter_status)
                df_rv.insert(6, "filter_date_from", date_from_param or "不限")
                df_rv.insert(7, "filter_date_to", date_to_param or "不限")

                col_map = {
                    "scheme_name": "方案名称",
                    "filter_store": "筛选-门店",
                    "filter_barcode": "筛选-商品",
                    "filter_rule_a": "筛选-规则版本A",
                    "filter_rule_b": "筛选-规则版本B",
                    "filter_status": "筛选-状态",
                    "filter_date_from": "筛选-开始时间",
                    "filter_date_to": "筛选-结束时间",
                    "store_id": "门店",
                    "barcode": "条码",
                    "sku_name": "商品名称",
                    "v_a_rule_ver": "版本A-规则版本",
                    "v_a_system_qty": "版本A-系统数量",
                    "v_a_actual_qty": "版本A-实际数量",
                    "v_a_diff_qty": "版本A-差异数量",
                    "v_a_cause": "版本A-归因",
                    "v_a_cause_detail": "版本A-归因详情",
                    "v_a_status": "版本A-状态",
                    "v_a_review_note": "版本A-复核备注",
                    "v_a_created_at": "版本A-创建时间",
                    "v_b_rule_ver": "版本B-规则版本",
                    "v_b_system_qty": "版本B-系统数量",
                    "v_b_actual_qty": "版本B-实际数量",
                    "v_b_diff_qty": "版本B-差异数量",
                    "v_b_cause": "版本B-归因",
                    "v_b_cause_detail": "版本B-归因详情",
                    "v_b_status": "版本B-状态",
                    "v_b_review_note": "版本B-复核备注",
                    "v_b_created_at": "版本B-创建时间",
                    "diff_qty_change": "差异量变化",
                    "cause_changed": "归因是否变化",
                }
                existing = [c for c in col_map if c in df_rv.columns]
                df_rv = df_rv[existing]
                df_rv.columns = [col_map[c] for c in existing]

                csv_buf = io.StringIO()
                df_rv.to_csv(csv_buf, index=False, encoding="utf-8-sig")
                csv_content = csv_buf.getvalue()

                date_from_display = filter_summary.get("filter_date_from") or "不限"
                date_to_display = filter_summary.get("filter_date_to") or "不限"
                scheme_display = current_scheme_name or "(未命名方案)"
                summary_lines = [
                    "# 差异复盘对比导出",
                    f"# 方案名称: {scheme_display}",
                    f"# 导出时间: {filter_summary['exported_at']}",
                    f"# 时间条件: {date_from_display} ~ {date_to_display}",
                    f"# 版本摘要: 版本A={rule_a_label}, 版本B={rule_b_label}",
                    f"# 筛选条件: 门店={filter_summary['filter_store']}, 商品={filter_summary['filter_barcode']}, "
                    f"状态={filter_summary['filter_status']}",
                    f"# 对比摘要: {json.dumps(filter_summary['summary'], ensure_ascii=False)}",
                    "#",
                ]
                full_csv = "\n".join(summary_lines) + csv_content

                csv_file_name = f"discrepancy_compare_{current_scheme_name or 'unnamed'}_{now_iso()[:10]}.csv"
                csv_file_name = csv_file_name.replace(" ", "_").replace("/", "_")

                st.download_button(
                    "⬇️ 下载 CSV（含方案名+时间条件+版本摘要+对比数据）",
                    data=full_csv.encode("utf-8-sig"),
                    file_name=csv_file_name,
                    mime="text/csv",
                )

                st.dataframe(df_rv, use_container_width=True, hide_index=True)
            else:
                json_export = {
                    "export_metadata": filter_summary,
                    "scheme_info": {
                        "scheme_id": current_scheme_id or "",
                        "scheme_name": current_scheme_name or "",
                        "time_condition": {
                            "date_from": date_from_param or "不限",
                            "date_to": date_to_param or "不限",
                        },
                        "version_summary": {
                            "rule_a": rule_a_label,
                            "rule_b": rule_b_label,
                        },
                    },
                    "comparison_data": all_export_data,
                    "rule_versions_info": [
                        {"version": v["version"], "config": json.loads(v["config_json"]), "created_at": v["created_at"]}
                        for v in rule_versions
                    ],
                }
                json_str = json.dumps(json_export, ensure_ascii=False, indent=2, default=str)

                json_file_name = f"discrepancy_compare_{current_scheme_name or 'unnamed'}_{now_iso()[:10]}.json"
                json_file_name = json_file_name.replace(" ", "_").replace("/", "_")

                st.download_button(
                    "⬇️ 下载 JSON（含方案名+时间条件+版本摘要+完整数据）",
                    data=json_str.encode("utf-8"),
                    file_name=json_file_name,
                    mime="application/json",
                )
                with st.expander("📋 预览导出内容（含筛选条件和对比摘要）", expanded=False):
                    st.json(json_export)
        else:
            st.info("暂无数据可导出")
