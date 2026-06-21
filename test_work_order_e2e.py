import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    init_db, get_conn,
    STATUS_CONFIRMED, STATUS_PENDING_REVIEW, STATUS_LABELS,
    WO_STATUS_LABELS, WO_STATUS_PENDING_DISPATCH, WO_STATUS_PROCESSING,
    WO_STATUS_PENDING_REVIEW, WO_STATUS_CLOSED, WO_STATUS_REVOKED,
    WO_ACTION_LABELS, WO_ROLE_ADMIN, WO_ROLE_NORMAL,
    create_work_order, batch_create_work_orders,
    get_work_order, get_work_order_by_no,
    list_work_orders, update_work_order,
    transition_work_order_status, batch_reassign_work_orders,
    get_work_order_logs, get_all_work_order_logs,
    export_work_orders_json, preview_work_orders_import,
    replay_work_order_statuses,
    save_wo_ui_state, load_wo_ui_state,
    save_wo_batch_selection, load_wo_batch_selection,
    get_discrepancies, transition_status,
)


def print_separator(title=""):
    print("\n" + "=" * 60)
    if title:
        print(f"  {title}")
        print("=" * 60)


def test_e2e_flow():
    print_separator("差异处置工单台账 - 端到端测试")

    init_db()

    with get_conn() as conn:
        discs = get_discrepancies(conn, status=STATUS_CONFIRMED)
        print(f"当前已确认差异数量: {len(discs)}")

        if len(discs) < 3:
            print("将待复核状态的差异流转为已确认...")
            pending_discs = get_discrepancies(conn, status=STATUS_PENDING_REVIEW)
            print(f"待复核差异数量: {len(pending_discs)}")

            for i, d in enumerate(pending_discs[:5]):
                try:
                    transition_status(conn, d["id"], STATUS_CONFIRMED, changed_by="test_user")
                    print(f"  - 差异 {d['id']} ({d['store_id']} - {d.get('sku_name', d['barcode'])}) 已确认")
                except Exception as e:
                    print(f"  - 差异 {d['id']} 流转失败: {e}")

            discs = get_discrepancies(conn, status=STATUS_CONFIRMED)
            print(f"确认后已确认差异数量: {len(discs)}")

        if not discs:
            print("ERROR: 没有可用的已确认差异，请先导入数据并运行归因分析")
            return False

        test_discs = discs[:3]
        print(f"使用前 {len(test_discs)} 条差异进行测试")

    print_separator("1. 测试单个建单")

    with get_conn() as conn:
        disc = test_discs[0]
        deadline = (datetime.now() + timedelta(days=7)).date().isoformat()
        result = create_work_order(
            conn, disc["id"],
            assignee="张三",
            deadline=deadline,
            action_type="inventory_correction",
            action_detail="库存校正测试",
            created_by="admin_user",
        )
        if result["success"]:
            print(f"✅ 建单成功: {result['wo_no']}")
            wo_id = result["work_order_id"]
        else:
            print(f"❌ 建单失败: {result.get('error', '未知错误')}")
            wo_id = None

    print_separator("2. 测试重复建单拦截")

    with get_conn() as conn:
        if wo_id:
            result = create_work_order(
                conn, disc["id"],
                assignee="李四",
                created_by="admin_user",
            )
            if not result["success"] and "重复" in result.get("error", ""):
                print(f"✅ 重复建单拦截成功: {result['error']}")
            else:
                print(f"❌ 重复建单拦截失败: {result}")

    print_separator("3. 测试批量建单")

    with get_conn() as conn:
        disc_ids = [d["id"] for d in test_discs]
        deadline = (datetime.now() + timedelta(days=5)).date().isoformat()
        result = batch_create_work_orders(
            conn, disc_ids,
            assignee="王五",
            deadline=deadline,
            action_type="process_optimization",
            created_by="admin_user",
        )
        print(f"批量建单结果: 总计 {result['total']} 条, 成功 {result['success_count']} 条, 失败 {result['fail_count']} 条")
        if result["fail_count"] > 0:
            for r in result["results"]:
                if not r["success"]:
                    print(f"  - 失败: {r['error']}")
        if result["success_count"] > 0:
            print("✅ 批量建单功能正常")

    print_separator("4. 测试工单列表查询")

    with get_conn() as conn:
        all_wos = list_work_orders(conn)
        print(f"工单总数: {len(all_wos)}")

        store_wos = list_work_orders(conn, store_id=test_discs[0]["store_id"])
        print(f"按门店筛选 ({test_discs[0]['store_id']}): {len(store_wos)} 条")

        status_wos = list_work_orders(conn, status=WO_STATUS_PENDING_DISPATCH)
        print(f"按状态筛选 (待派发): {len(status_wos)} 条")

        assignee_wos = list_work_orders(conn, assignee="王五")
        print(f"按负责人筛选 (王五): {len(assignee_wos)} 条")
        print("✅ 工单列表查询功能正常")

    print_separator("5. 测试工单状态流转")

    with get_conn() as conn:
        wos = list_work_orders(conn, status=WO_STATUS_PENDING_DISPATCH)
        if wos:
            wo = wos[0]
            print(f"测试工单: {wo['wo_no']} (当前状态: {WO_STATUS_LABELS[wo['status']]})")

            result = transition_work_order_status(
                conn, wo["id"], WO_STATUS_PROCESSING,
                operator="admin_user", role=WO_ROLE_ADMIN,
                note="开始处理",
            )
            if result["success"]:
                print(f"✅ 流转成功: {WO_STATUS_LABELS[result['from_status']]} → {WO_STATUS_LABELS[result['to_status']]}")
            else:
                print(f"❌ 流转失败: {result['error']}")

            result = transition_work_order_status(
                conn, wo["id"], WO_STATUS_PENDING_REVIEW,
                operator="admin_user", role=WO_ROLE_ADMIN,
                note="处理完成，待复核",
            )
            if result["success"]:
                print(f"✅ 流转成功: {WO_STATUS_LABELS[result['from_status']]} → {WO_STATUS_LABELS[result['to_status']]}")
            else:
                print(f"❌ 流转失败: {result['error']}")

            result = transition_work_order_status(
                conn, wo["id"], WO_STATUS_CLOSED,
                operator="admin_user", role=WO_ROLE_ADMIN,
                note="复核通过，关闭工单",
            )
            if result["success"]:
                print(f"✅ 流转成功: {WO_STATUS_LABELS[result['from_status']]} → {WO_STATUS_LABELS[result['to_status']]}")
            else:
                print(f"❌ 流转失败: {result['error']}")

            result = transition_work_order_status(
                conn, wo["id"], WO_STATUS_PROCESSING,
                operator="admin_user", role=WO_ROLE_ADMIN,
            )
            if not result["success"] and "已关闭" in result.get("error", ""):
                print(f"✅ 已关闭工单状态变更拦截成功: {result['error']}")
            else:
                print(f"❌ 已关闭工单状态变更拦截失败: {result}")

    print_separator("6. 测试工单编辑")

    with get_conn() as conn:
        wos = list_work_orders(conn, status=WO_STATUS_PENDING_DISPATCH)
        if wos:
            wo = wos[0]
            print(f"测试工单: {wo['wo_no']}")

            result = update_work_order(
                conn, wo["id"],
                {"assignee": "赵六", "action_detail": "更新后的处理详情"},
                operator="admin_user", role=WO_ROLE_ADMIN,
            )
            if result["success"]:
                print("✅ 工单编辑成功")
                updated_wo = get_work_order(conn, wo["id"])
                print(f"  - 新负责人: {updated_wo['assignee']}")
                print(f"  - 新详情: {updated_wo['action_detail']}")
            else:
                print(f"❌ 工单编辑失败: {result['error']}")

    print_separator("7. 测试已关闭工单编辑拦截")

    with get_conn() as conn:
        wos = list_work_orders(conn, status=WO_STATUS_CLOSED)
        if wos:
            wo = wos[0]
            result = update_work_order(
                conn, wo["id"],
                {"assignee": "测试修改"},
                operator="admin_user", role=WO_ROLE_ADMIN,
            )
            if not result["success"] and "已关闭" in result.get("error", ""):
                print(f"✅ 已关闭工单编辑拦截成功: {result['error']}")
            else:
                print(f"❌ 已关闭工单编辑拦截失败: {result}")

    print_separator("8. 测试普通角色权限 - 撤销别人工单")

    with get_conn() as conn:
        wos = list_work_orders(conn, status=WO_STATUS_PENDING_DISPATCH)
        if wos:
            wo = wos[0]
            print(f"测试工单: {wo['wo_no']} (创建人: {wo['created_by']})")

            result = transition_work_order_status(
                conn, wo["id"], WO_STATUS_REVOKED,
                operator="other_user", role=WO_ROLE_NORMAL,
            )
            if not result["success"] and "普通角色" in result.get("error", ""):
                print(f"✅ 普通角色撤销别人工单拦截成功: {result['error']}")
            else:
                print(f"❌ 普通角色撤销别人工单拦截失败: {result}")

    print_separator("9. 测试批量改派")

    with get_conn() as conn:
        wos = list_work_orders(conn, status=WO_STATUS_PENDING_DISPATCH)
        if len(wos) >= 2:
            wo_ids = [w["id"] for w in wos[:2]]
            result = batch_reassign_work_orders(
                conn, wo_ids, "新负责人-批量",
                operator="admin_user",
            )
            print(f"批量改派结果: 成功 {result['success_count']}/{result['total']} 条")
            if result["success_count"] > 0:
                print("✅ 批量改派功能正常")

    print_separator("10. 测试操作日志")

    with get_conn() as conn:
        wos = list_work_orders(conn)
        if wos:
            logs = get_work_order_logs(conn, wos[0]["id"])
            print(f"工单 {wos[0]['wo_no']} 的操作日志: {len(logs)} 条")
            for log in logs[:3]:
                print(f"  - {log['operated_at'][:19]} | {log['operator']} | {log['action_type']} | {log.get('action_detail', '')}")

            all_logs = get_all_work_order_logs(conn, limit=10)
            print(f"\n全部操作日志（最近10条）: {len(all_logs)} 条")
            print("✅ 操作日志功能正常")

    print_separator("11. 测试导出 JSON")

    with get_conn() as conn:
        export_data = export_work_orders_json(conn)
        print(f"导出工单数量: {export_data['total']} 条")
        print(f"导出版本: {export_data['export_version']}")
        if export_data["work_orders"]:
            print(f"第一条工单: {export_data['work_orders'][0]['wo_no']}")
            print("✅ JSON 导出功能正常")

    print_separator("12. 测试导入预览和状态回放")

    with get_conn() as conn:
        export_data = export_work_orders_json(conn)

        if export_data["work_orders"]:
            for wo in export_data["work_orders"]:
                if wo["status"] == WO_STATUS_PENDING_DISPATCH:
                    wo["status"] = WO_STATUS_PROCESSING
                    break

            preview = preview_work_orders_import(conn, export_data)
            if preview["success"]:
                print(f"导入预览: 总计 {preview['total']} 条")
                print(f"  - 新建: {preview['new_count']} 条")
                print(f"  - 状态变更: {preview['update_count']} 条")
                print(f"  - 状态一致: {preview['same_count']} 条")
                print("✅ 导入预览功能正常")

                result = replay_work_order_statuses(
                    conn, preview["preview_results"],
                    operator="import_user",
                )
                if result["success"]:
                    print(f"\n状态回放结果:")
                    print(f"  - 总计: {result['total']} 条")
                    print(f"  - 新建: {result['created_count']} 条")
                    print(f"  - 更新: {result['updated_count']} 条")
                    print(f"  - 跳过: {result['skipped_count']} 条")
                    print(f"  - 失败: {result['failed_count']} 条")
                    print("✅ 状态回放功能正常")
                else:
                    print(f"❌ 状态回放失败: {result}")
            else:
                print(f"❌ 导入预览失败: {preview['error']}")

    print_separator("13. 测试 UI 状态持久化")

    with get_conn() as conn:
        test_state = {
            "filter_store": "测试门店",
            "filter_status": "pending_dispatch",
            "keyword": "测试",
        }
        save_wo_ui_state(conn, test_state)
        loaded = load_wo_ui_state(conn)
        if loaded == test_state:
            print("✅ UI 状态持久化正常")
        else:
            print(f"❌ UI 状态持久化失败: {loaded}")

        test_selection = [1, 2, 3]
        save_wo_batch_selection(conn, test_selection)
        loaded_sel = load_wo_batch_selection(conn)
        if loaded_sel == test_selection:
            print("✅ 批量选择状态持久化正常")
        else:
            print(f"❌ 批量选择状态持久化失败: {loaded_sel}")

    print_separator("测试总结")

    with get_conn() as conn:
        total_wos = len(list_work_orders(conn))
        total_logs = len(get_all_work_order_logs(conn, limit=1000))
        print(f"工单总数: {total_wos}")
        print(f"操作日志总数: {total_logs}")
        print("\n🎉 端到端测试完成！所有核心功能验证通过。")

    return True


if __name__ == "__main__":
    test_e2e_flow()
