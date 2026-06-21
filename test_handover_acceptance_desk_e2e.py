import sys
import os
import json
import unittest
import sqlite3
import time
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import (
    init_db, get_conn,
    HP_STATUS_PENDING_HANDOVER, HP_STATUS_RECEIVED, HP_STATUS_PROCESSING,
    HP_STATUS_COMPLETED, HP_STATUS_WITHDRAWN,
    HP_STATUS_LABELS, HP_VALID_TRANSITIONS, HP_ROLE_ADMIN, HP_ROLE_NORMAL,
    HP_ACTION_LABELS,
    create_handover_package, get_handover_package, get_handover_package_by_no,
    list_handover_packages, get_handover_package_items,
    transition_handover_status, update_handover_package,
    get_handover_package_logs, get_all_handover_logs,
    get_hp_stores, get_hp_receivers,
    export_handover_packages_json, preview_handover_packages_import,
    confirm_handover_packages_import, generate_import_receipt_scheme,
    save_hp_ui_state, load_hp_ui_state,
    save_hp_draft, load_hp_draft, clear_hp_draft,
    save_hp_batch_selection, load_hp_batch_selection,
    save_hp_import_preview, load_hp_import_preview,
    save_hp_import_session, load_hp_import_session, clear_hp_import_session,
    generate_handover_sample_data,
    group_conflicts_by_type, export_conflicts_json, export_conflicts_csv,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_LABELS,
    get_discrepancies, now_iso,
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_handover_acceptance.db")


def _insert_sample_discrepancies(conn, count=5, store_id="S001", created_by="test_user"):
    now = now_iso()
    ids = []
    for i in range(count):
        conn.execute(
            """INSERT INTO discrepancies
               (store_id, barcode, sku_name, system_qty, actual_qty, diff_qty,
                attributed_cause, status, review_note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (store_id, f"BC{i:04d}", f"SKU-{i}", 100.0 + i, 90.0 + i,
             -10.0, "normal_loss", STATUS_PENDING_REVIEW,
             f"备注{i}", now, now),
        )
        ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return ids


class TestHandoverAcceptanceDeskE2E(unittest.TestCase):

    def setUp(self):
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()

    def _get_conn(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_conn_ctx(self):
        return get_conn()

    def test_01_create_draft_complete(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            draft_data = {
                "title": "早班交接包-2026-06-21",
                "receiver": "night_shift_zhang",
                "handover_note": "请夜班同事跟进这3条差异，重点关注SKU-0的损耗原因",
                "description": "早班待复核差异交接",
                "filter_snapshot": {
                    "store_id": "S001",
                    "status": STATUS_PENDING_REVIEW,
                    "keyword": "损耗",
                },
                "selected_ids": disc_ids,
            }

            result = save_hp_draft(conn, draft_data)
            self.assertTrue(result["success"])
            self.assertIn("draft", result)

            loaded_draft = load_hp_draft(conn)
            self.assertIsNotNone(loaded_draft)
            self.assertEqual(loaded_draft["title"], "早班交接包-2026-06-21")
            self.assertEqual(loaded_draft["receiver"], "night_shift_zhang")
            self.assertEqual(loaded_draft["handover_note"], "请夜班同事跟进这3条差异，重点关注SKU-0的损耗原因")
            self.assertEqual(loaded_draft["description"], "早班待复核差异交接")
            self.assertEqual(loaded_draft["selected_ids"], disc_ids)
            self.assertEqual(loaded_draft["filter_snapshot"]["store_id"], "S001")
            self.assertIn("evidence_summary", loaded_draft)
            self.assertEqual(len(loaded_draft["evidence_summary"]), 3)

            for disc_id in disc_ids:
                self.assertIn(str(disc_id), loaded_draft["evidence_summary"])
                ev = loaded_draft["evidence_summary"][str(disc_id)]
                self.assertIn("store_id", ev)
                self.assertIn("barcode", ev)
                self.assertIn("sku_name", ev)
                self.assertIn("diff_qty", ev)
                self.assertIn("evidence_summary", ev)
                self.assertIn("disc_status", ev)

    def test_02_draft_validation(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 2)

            with self.assertRaises(ValueError) as ctx:
                save_hp_draft(conn, {"selected_ids": disc_ids})
            self.assertIn("缺少必填字段: title", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx:
                save_hp_draft(conn, {"title": "", "selected_ids": disc_ids})
            self.assertIn("请输入交接包标题", str(ctx.exception))

            with self.assertRaises(ValueError) as ctx:
                save_hp_draft(conn, {"title": "测试包", "selected_ids": []})
            self.assertIn("请至少选择一条差异记录", str(ctx.exception))

    def test_03_cross_restart_persistence(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            draft_data = {
                "title": "重启测试交接包",
                "receiver": "user_restart",
                "handover_note": "测试重启恢复",
                "description": "重启测试",
                "filter_snapshot": {"store_id": "S001", "status": STATUS_PENDING_REVIEW},
                "selected_ids": disc_ids,
            }
            save_hp_draft(conn, draft_data)

            ui_state = {
                "filter_store": "S001",
                "filter_status": HP_STATUS_PENDING_HANDOVER,
                "keyword": "测试",
                "sort_by": "created_at",
            }
            save_hp_ui_state(conn, ui_state)

            batch_selection = [disc_ids[0], disc_ids[2]]
            save_hp_batch_selection(conn, batch_selection)

        with self._get_conn_ctx() as conn:
            loaded_draft = load_hp_draft(conn)
            self.assertIsNotNone(loaded_draft)
            self.assertEqual(loaded_draft["title"], "重启测试交接包")
            self.assertEqual(loaded_draft["receiver"], "user_restart")
            self.assertEqual(loaded_draft["selected_ids"], disc_ids)
            self.assertIn("evidence_summary", loaded_draft)

            loaded_ui = load_hp_ui_state(conn)
            self.assertEqual(loaded_ui["filter_store"], "S001")
            self.assertEqual(loaded_ui["keyword"], "测试")

            loaded_sel = load_hp_batch_selection(conn)
            self.assertEqual(loaded_sel, batch_selection)

            result = clear_hp_draft(conn)
            self.assertTrue(result["success"])
            cleared = load_hp_draft(conn)
            self.assertIsNone(cleared)

    def test_04_export_snapshot_complete(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            result = create_handover_package(
                conn, title="导出测试完整包", discrepancy_ids=disc_ids,
                receiver="export_user", handover_note="导出测试备注",
                description="导出测试描述",
                filter_snapshot={"store_id": "S001", "status": STATUS_PENDING_REVIEW},
                created_by="creator_export",
            )
            self.assertTrue(result["success"])
            pkg_id = result["package_id"]
            pkg_no = result["pkg_no"]

            transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="export_user", role=HP_ROLE_ADMIN,
            )

            export_data = export_handover_packages_json(conn, package_ids=[pkg_id])
            self.assertEqual(export_data["export_version"], "2.0")
            self.assertEqual(export_data["export_type"], "handover_packages")
            self.assertEqual(export_data["generator"], "handover_acceptance_desk")
            self.assertEqual(export_data["total"], 1)
            self.assertEqual(len(export_data["handover_packages"]), 1)

            pkg = export_data["handover_packages"][0]
            self.assertEqual(pkg["pkg_no"], pkg_no)
            self.assertEqual(pkg["title"], "导出测试完整包")
            self.assertEqual(pkg["receiver"], "export_user")
            self.assertEqual(pkg["status"], HP_STATUS_RECEIVED)
            self.assertIn("items", pkg)
            self.assertEqual(len(pkg["items"]), 3)
            self.assertIn("logs", pkg)
            self.assertIn("filter_snapshot", pkg)
            self.assertIsInstance(pkg["filter_snapshot"], dict)
            self.assertEqual(pkg["filter_snapshot"]["store_id"], "S001")
            self.assertIn("selected_ids_json", pkg)
            self.assertEqual(pkg["selected_ids_json"], disc_ids)

            for item in pkg["items"]:
                self.assertIn("evidence_snapshot", item)
                self.assertIsInstance(item["evidence_snapshot"], list)

    def test_05_preview_import_with_receipt_scheme(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            result = create_handover_package(
                conn, title="预检测试包", discrepancy_ids=disc_ids,
                receiver="preview_user", handover_note="预检测试",
                created_by="creator_preview",
            )
            pkg_id = result["package_id"]

            export_data = export_handover_packages_json(conn, package_ids=[pkg_id])

            conn.execute("DELETE FROM handover_package_logs WHERE package_id = ?", (pkg_id,))
            conn.execute("DELETE FROM handover_package_items WHERE package_id = ?", (pkg_id,))
            conn.execute("DELETE FROM handover_packages WHERE id = ?", (pkg_id,))

            preview = preview_handover_packages_import(conn, export_data)
            self.assertTrue(preview["success"])
            self.assertEqual(preview["new_count"], 1)
            self.assertEqual(preview["safe_count"], 0)
            self.assertEqual(preview["conflict_count"], 0)
            self.assertIn("grouped_conflicts", preview)

            receipt_scheme = generate_import_receipt_scheme(
                conn, preview["preview_results"], export_data
            )
            self.assertTrue(receipt_scheme["success"])
            self.assertEqual(receipt_scheme["can_import_count"], 1)

            scheme_item = receipt_scheme["scheme_items"][0]
            self.assertEqual(scheme_item["action"], "create")
            self.assertTrue(scheme_item["can_import"])
            self.assertEqual(scheme_item["details"]["discrepancy_count"], 3)

    def test_06_confirm_import_with_precheck_guarantee(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            result = create_handover_package(
                conn, title="确认导回测试包", discrepancy_ids=disc_ids,
                receiver="confirm_user", handover_note="确认导回测试",
                created_by="creator_confirm",
            )
            pkg_id = result["package_id"]
            original_pkg_no = result["pkg_no"]

            export_data = export_handover_packages_json(conn, package_ids=[pkg_id])
            export_data["handover_packages"][0]["status"] = HP_STATUS_RECEIVED
            export_data["handover_packages"][0]["received_by"] = "remote_user"
            export_data["handover_packages"][0]["received_at"] = "2026-01-01T12:00:00"

            preview = preview_handover_packages_import(conn, export_data)
            self.assertEqual(preview["safe_count"], 1)

            receipt_scheme = generate_import_receipt_scheme(
                conn, preview["preview_results"], export_data
            )
            self.assertTrue(receipt_scheme["success"])
            scheme_item = receipt_scheme["scheme_items"][0]
            self.assertEqual(scheme_item["action"], "update")
            self.assertTrue(scheme_item["can_import"])

            confirm_result = confirm_handover_packages_import(
                conn, preview["preview_results"], export_data,
                operator="import_confirm_user",
            )
            self.assertTrue(confirm_result["success"])
            self.assertEqual(confirm_result["received_count"], 1)
            self.assertEqual(confirm_result["failed_count"], 0)

            imported_pkg = get_handover_package_by_no(conn, original_pkg_no)
            self.assertIsNotNone(imported_pkg)
            self.assertEqual(imported_pkg["title"], "确认导回测试包")
            self.assertEqual(imported_pkg["status"], HP_STATUS_RECEIVED)
            self.assertEqual(imported_pkg["received_by"], "import_confirm_user")

            items = get_handover_package_items(conn, imported_pkg["id"])
            self.assertEqual(len(items), 3)

            logs = get_handover_package_logs(conn, imported_pkg["id"])
            self.assertTrue(any(l["action_type"] == "create" for l in logs))
            self.assertTrue(any(l["action_type"] == "receive" for l in logs))

    def test_07_conflict_detection_grouped(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 4)

            result = create_handover_package(
                conn, title="冲突测试包", discrepancy_ids=disc_ids[:3],
                receiver="conflict_user", handover_note="冲突测试",
                created_by="creator_conflict",
            )
            pkg_id = result["package_id"]
            pkg_no = result["pkg_no"]

            transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="conflict_user", role=HP_ROLE_ADMIN,
            )

            export_data = export_handover_packages_json(conn, package_ids=[pkg_id])

            time.sleep(0.01)
            conn.execute(
                "UPDATE discrepancies SET review_note = ? WHERE id = ?",
                ("修改后的备注-已被本地修改", disc_ids[0]),
            )
            conn.execute(
                "UPDATE discrepancies SET updated_at = ? WHERE id = ?",
                (now_iso(), disc_ids[0]),
            )

            conn.execute(
                "UPDATE discrepancies SET status = ?, updated_at = ? WHERE id = ?",
                (STATUS_CONFIRMED, now_iso(), disc_ids[1]),
            )

            preview = preview_handover_packages_import(conn, export_data)
            self.assertTrue(preview["success"])
            self.assertEqual(preview["conflict_count"], 1)

            grouped = preview["grouped_conflicts"]
            self.assertIsNotNone(grouped)
            self.assertIn("groups", grouped)
            self.assertIn("summary", grouped)

            self.assertGreaterEqual(grouped["summary"]["note_changed"], 1)
            self.assertGreaterEqual(grouped["summary"]["status_changed"], 1)
            self.assertGreaterEqual(grouped["summary"]["local_newer"], 1)

            conflict_item = preview["preview_results"][0]
            self.assertEqual(conflict_item["import_status"], "conflict")
            self.assertTrue(conflict_item["has_blocking"])

            conflict_types = [c["type"] for c in conflict_item["conflicts"]]
            self.assertIn("note_changed", conflict_types)
            self.assertIn("status_changed", conflict_types)
            self.assertIn("local_newer", conflict_types)

    def test_08_export_conflicts_json_csv(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            result = create_handover_package(
                conn, title="冲突导出测试包", discrepancy_ids=disc_ids,
                receiver="export_conflict_user", created_by="creator_export",
            )
            pkg_id = result["package_id"]

            export_data = export_handover_packages_json(conn, package_ids=[pkg_id])

            time.sleep(0.01)
            conn.execute(
                "UPDATE discrepancies SET review_note = ? WHERE id = ?",
                ("本地修改的备注", disc_ids[0]),
            )
            conn.execute(
                "UPDATE discrepancies SET status = ?, updated_at = ? WHERE id = ?",
                (STATUS_CONFIRMED, now_iso(), disc_ids[1]),
            )

            preview = preview_handover_packages_import(conn, export_data)
            self.assertEqual(preview["conflict_count"], 1)

            json_output = export_conflicts_json(preview["preview_results"])
            json_data = json.loads(json_output)
            self.assertEqual(json_data["export_type"], "handover_conflicts")
            self.assertIn("conflict_groups", json_data)
            self.assertIn("total_conflicts", json_data)
            self.assertGreater(json_data["total_conflicts"], 0)

            csv_output = export_conflicts_csv(preview["preview_results"])
            self.assertIn("冲突类型", csv_output)
            self.assertIn("交接包编号", csv_output)
            self.assertIn("交接包标题", csv_output)
            self.assertIn("复核备注已修改", csv_output)
            self.assertIn("状态不一致", csv_output)

    def test_09_permission_control_normal_role(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            result = create_handover_package(
                conn, title="权限测试包", discrepancy_ids=disc_ids,
                receiver="receiver_a", created_by="creator_a",
            )
            pkg_id = result["package_id"]

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="receiver_a", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_PROCESSING,
                operator="other_user", role=HP_ROLE_NORMAL,
            )
            self.assertFalse(r["success"])
            self.assertIn("不能处理别人已接收的包", r["error"])

            logs = get_handover_package_logs(conn, pkg_id)
            self.assertTrue(any(
                l["action_type"] == "blocked_operation" for l in logs
            ))

            r = update_handover_package(
                conn, pkg_id, {"title": "被篡改的标题"},
                operator="other_user", role=HP_ROLE_NORMAL,
            )
            self.assertFalse(r["success"])
            self.assertIn("不能编辑别人已接收的交接包", r["error"])

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_PROCESSING,
                operator="receiver_a", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_COMPLETED,
                operator="receiver_a", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = update_handover_package(
                conn, pkg_id, {"title": "修改已完成包"},
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("已完成", r["error"])

            logs = get_handover_package_logs(conn, pkg_id)
            blocked_logs = [l for l in logs if l["action_type"] == "blocked_operation"]
            self.assertGreaterEqual(len(blocked_logs), 2)

    def test_10_withdraw_and_modify_block(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 2)

            result = create_handover_package(
                conn, title="撤回测试包", discrepancy_ids=disc_ids,
                created_by="creator_withdraw",
            )
            pkg_id = result["package_id"]

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_WITHDRAWN,
                operator="creator_withdraw", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("已撤回", r["error"])

            r = update_handover_package(
                conn, pkg_id, {"title": "修改已撤回包"},
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("已撤回", r["error"])

            logs = get_handover_package_logs(conn, pkg_id)
            blocked_logs = [l for l in logs if l["action_type"] == "blocked_operation"]
            self.assertEqual(len(blocked_logs), 2)

    def test_11_normal_cannot_change_receiver(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 2)

            result = create_handover_package(
                conn, title="接手人修改测试包", discrepancy_ids=disc_ids,
                receiver="original_receiver", created_by="creator_test",
            )
            pkg_id = result["package_id"]

            r = update_handover_package(
                conn, pkg_id, {"receiver": "new_receiver"},
                operator="other_user", role=HP_ROLE_NORMAL,
            )
            self.assertFalse(r["success"])
            self.assertIn("不能修改已指定接手人的交接包的接手人", r["error"])

            r = update_handover_package(
                conn, pkg_id, {"receiver": "admin_new_receiver"},
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertTrue(r["success"])

    def test_12_full_workflow_replay(self):
        with self._get_conn_ctx() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 5)

            draft_data = {
                "title": "完整流程测试交接包",
                "receiver": "workflow_user",
                "handover_note": "请按流程处理这些差异",
                "description": "完整流程测试",
                "filter_snapshot": {
                    "store_id": "S001",
                    "status": STATUS_PENDING_REVIEW,
                },
                "selected_ids": disc_ids[:4],
            }
            draft_result = save_hp_draft(conn, draft_data)
            self.assertTrue(draft_result["success"])

            loaded_draft = load_hp_draft(conn)
            self.assertIsNotNone(loaded_draft)
            self.assertEqual(len(loaded_draft["evidence_summary"]), 4)

            result = create_handover_package(
                conn,
                title=loaded_draft["title"],
                discrepancy_ids=loaded_draft["selected_ids"],
                receiver=loaded_draft["receiver"],
                handover_note=loaded_draft["handover_note"],
                description=loaded_draft["description"],
                filter_snapshot=loaded_draft["filter_snapshot"],
                created_by="workflow_creator",
            )
            self.assertTrue(result["success"])
            pkg_id = result["package_id"]
            pkg_no = result["pkg_no"]

            clear_hp_draft(conn)

            export_data = export_handover_packages_json(conn, package_ids=[pkg_id])

        with self._get_conn_ctx() as conn2:
            preview = preview_handover_packages_import(conn2, export_data)
            self.assertTrue(preview["success"])
            self.assertEqual(preview["safe_count"], 1)

            receipt_scheme = generate_import_receipt_scheme(
                conn2, preview["preview_results"], export_data
            )
            self.assertTrue(receipt_scheme["success"])
            self.assertEqual(receipt_scheme["can_import_count"], 1)

            confirm_result = confirm_handover_packages_import(
                conn2, preview["preview_results"], export_data,
                operator="workflow_importer",
            )
            self.assertTrue(confirm_result["success"])
            self.assertEqual(confirm_result["failed_count"], 0)

            imported_pkg = get_handover_package_by_no(conn2, pkg_no)
            self.assertIsNotNone(imported_pkg)
            self.assertEqual(imported_pkg["status"], HP_STATUS_PENDING_HANDOVER)

            r = transition_handover_status(
                conn2, imported_pkg["id"], HP_STATUS_RECEIVED,
                operator="workflow_user", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = transition_handover_status(
                conn2, imported_pkg["id"], HP_STATUS_PROCESSING,
                operator="workflow_user", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = transition_handover_status(
                conn2, imported_pkg["id"], HP_STATUS_COMPLETED,
                operator="workflow_user", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            final_pkg = get_handover_package(conn2, imported_pkg["id"])
            self.assertEqual(final_pkg["status"], HP_STATUS_COMPLETED)
            self.assertIsNotNone(final_pkg["completed_at"])

            all_logs = get_handover_package_logs(conn2, imported_pkg["id"])
            action_types = [l["action_type"] for l in all_logs]
            self.assertIn("create", action_types)
            self.assertIn("receive", action_types)
            self.assertIn("status_change", action_types)
            self.assertIn("complete", action_types)

            export_after = export_handover_packages_json(
                conn2, package_ids=[imported_pkg["id"]]
            )
            self.assertEqual(export_after["total"], 1)
            pkg_after = export_after["handover_packages"][0]
            self.assertEqual(pkg_after["status"], HP_STATUS_COMPLETED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
