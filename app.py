import streamlit as st
import pandas as pd
import json
import io
import os
import tempfile

from db import (
    init_db, get_conn, STATUS_LABELS, STATUS_PENDING_REVIEW, STATUS_CONFIRMED,
    STATUS_PENDING_ACCOUNTABILITY, STATUS_CLOSED, VALID_TRANSITIONS, IMPORT_TYPES,
    get_discrepancies, get_evidence_for_discrepancy, get_status_log,
    get_stores, get_import_records, transition_status, update_review_note,
    get_active_rule_version, now_iso,
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

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📥 数据导入", "🔍 差异归因", "📋 差异列表", "⚙️ 规则配置", "📤 导出",
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
            result = import_csv(import_type, uploaded.name, content)
            if result["success"]:
                st.success(f"✅ 导入成功！有效行: {result['valid_rows']}，总行: {result['total_rows']}")
                if result.get("error_rows"):
                    st.warning(f"⚠️ 有 {result['error_rows']} 行被跳过：")
                    for e in result.get("detail_errors", []):
                        st.error(e)
            else:
                if result.get("duplicate"):
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
                if st.button(f"📁 导入样例{label}", key=btn_key):
                    result = import_csv(itype, fname, content)
                    if result["success"]:
                        st.success(f"✅ 样例{label}导入成功！有效行: {result['valid_rows']}")
                        if result.get("error_rows"):
                            st.warning(f"⚠️ {result['error_rows']} 行被跳过")
                    else:
                        if result.get("duplicate"):
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
        records = get_import_records(conn)
    if records:
        df_import = pd.DataFrame(records)
        df_import["import_type"] = df_import["import_type"].map(
            {"inventory": "库存", "sales": "销售", "transfer": "调拨", "stocktake": "盘点"}
        )
        df_import = df_import[["file_name", "import_type", "imported_at", "row_count", "error_count"]]
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
            st.success(f"✅ 归因完成！新建差异 {result['created']} 条，使用规则 v{result['rule_version']}")
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
    st.markdown("导出包含**差异明细、复核备注、来源证据行、状态流转日志**，可独立复盘。")

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

        export_cols = [
            "id", "store_id", "barcode", "sku_name", "system_qty", "actual_qty",
            "diff_qty", "attributed_cause", "cause_label", "cause_detail",
            "rule_ver", "status", "status_label",
            "review_note", "reviewed_at", "created_at", "updated_at",
            "evidence_summary", "status_log_summary",
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
        ]

        st.dataframe(df_display, use_container_width=True, hide_index=True)

        if export_format == "CSV":
            csv_buf = io.StringIO()
            df_display.to_csv(csv_buf, index=False, encoding="utf-8-sig")
            st.download_button(
                "⬇️ 下载 CSV（含证据+流转+备注）",
                data=csv_buf.getvalue().encode("utf-8-sig"),
                file_name=f"discrepancies_full_{now_iso()[:10]}.csv",
                mime="text/csv",
            )
        else:
            json_obj = []
            for d in discs:
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
                })
            json_str = json.dumps(json_obj, ensure_ascii=False, indent=2, default=str)
            st.download_button(
                "⬇️ 下载 JSON（嵌套证据+流转+备注）",
                data=json_str.encode("utf-8"),
                file_name=f"discrepancies_full_{now_iso()[:10]}.json",
                mime="application/json",
            )
    else:
        st.info("暂无差异数据可导出")
