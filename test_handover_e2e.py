import sys
import os
import json
import unittest
import sqlite3

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
    confirm_handover_packages_import,
    save_hp_ui_state, load_hp_ui_state,
    save_hp_draft, load_hp_draft, clear_hp_draft,
    save_hp_batch_selection, load_hp_batch_selection,
    generate_handover_sample_data,
    create_work_order, batch_create_work_orders,
    STATUS_PENDING_REVIEW, STATUS_CONFIRMED, STATUS_LABELS,
    get_discrepancies, now_iso, save_ui_state, load_ui_state,
    get_stores,
)
from engine import CAUSE_LABELS

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory_diff.db")


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


class TestHandoverE2E(unittest.TestCase):

    def setUp(self):
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()

    def _get_conn(self):
        return get_conn()

    def test_A_basic_crud(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)

            result = create_handover_package(
                conn, title="测试交接包", discrepancy_ids=disc_ids,
                receiver="user_a", handover_note="请尽快处理",
                description="描述", filter_snapshot={"store_id": "S001"},
                created_by="creator1",
            )
            self.assertTrue(result["success"])
            pkg_id = result["package_id"]
            pkg_no = result["pkg_no"]

            pkgs = list_handover_packages(conn)
            self.assertTrue(any(p["id"] == pkg_id for p in pkgs))

            pkg = get_handover_package(conn, pkg_id)
            self.assertIsNotNone(pkg)
            self.assertEqual(pkg["title"], "测试交接包")
            self.assertEqual(pkg["status"], HP_STATUS_PENDING_HANDOVER)
            self.assertEqual(len(pkg["items"]), 3)

            pkg_by_no = get_handover_package_by_no(conn, pkg_no)
            self.assertIsNotNone(pkg_by_no)
            self.assertEqual(pkg_by_no["pkg_no"], pkg_no)

            items = get_handover_package_items(conn, pkg_id)
            self.assertEqual(len(items), 3)

    def test_B_status_transitions(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 2)
            result = create_handover_package(
                conn, title="流转测试", discrepancy_ids=disc_ids,
                created_by="creator1",
            )
            pkg_id = result["package_id"]

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="user_a", role=HP_ROLE_ADMIN,
            )
            self.assertTrue(r["success"])
            self.assertEqual(r["from_status"], HP_STATUS_PENDING_HANDOVER)
            self.assertEqual(r["to_status"], HP_STATUS_RECEIVED)

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_PROCESSING,
                operator="user_a", role=HP_ROLE_ADMIN,
            )
            self.assertTrue(r["success"])
            self.assertEqual(r["to_status"], HP_STATUS_PROCESSING)

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_COMPLETED,
                operator="user_a", role=HP_ROLE_ADMIN,
            )
            self.assertTrue(r["success"])
            self.assertEqual(r["to_status"], HP_STATUS_COMPLETED)

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("已完成", r["error"])

            disc_ids2 = _insert_sample_discrepancies(conn, 2, store_id="S002")
            result2 = create_handover_package(
                conn, title="撤回测试", discrepancy_ids=disc_ids2,
                created_by="creator2",
            )
            pkg_id2 = result2["package_id"]

            r = transition_handover_status(
                conn, pkg_id2, HP_STATUS_WITHDRAWN,
                operator="creator2", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])
            self.assertEqual(r["to_status"], HP_STATUS_WITHDRAWN)

            r = transition_handover_status(
                conn, pkg_id2, HP_STATUS_RECEIVED,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("已撤回", r["error"])

            disc_ids3 = _insert_sample_discrepancies(conn, 2, store_id="S003")
            result3 = create_handover_package(
                conn, title="非法跳转", discrepancy_ids=disc_ids3,
                created_by="creator3",
            )
            pkg_id3 = result3["package_id"]

            r = transition_handover_status(
                conn, pkg_id3, HP_STATUS_COMPLETED,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("不允许", r["error"])

    def test_C_permission_controls(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)
            result = create_handover_package(
                conn, title="权限测试包", discrepancy_ids=disc_ids,
                receiver="user_a", created_by="creator1",
            )
            pkg_id = result["package_id"]

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_WITHDRAWN,
                operator="other_user", role=HP_ROLE_NORMAL,
            )
            self.assertFalse(r["success"])
            self.assertIn("普通角色只能撤回自己创建", r["error"])

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="wrong_receiver", role=HP_ROLE_NORMAL,
            )
            self.assertFalse(r["success"])
            self.assertIn("无权接收", r["error"])

            r = transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="user_a", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            r = update_handover_package(
                conn, pkg_id, {"title": "被篡改"},
                operator="other_user", role=HP_ROLE_NORMAL,
            )
            self.assertFalse(r["success"])
            self.assertIn("普通角色不能编辑别人已接收", r["error"])

            r = update_handover_package(
                conn, pkg_id, {"title": "合法修改"},
                operator="user_a", role=HP_ROLE_NORMAL,
            )
            self.assertTrue(r["success"])

            disc_ids2 = _insert_sample_discrepancies(conn, 2, store_id="S005")
            result2 = create_handover_package(
                conn, title="完成包", discrepancy_ids=disc_ids2,
                created_by="creator1",
            )
            pkg_id2 = result2["package_id"]
            transition_handover_status(
                conn, pkg_id2, HP_STATUS_RECEIVED,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            transition_handover_status(
                conn, pkg_id2, HP_STATUS_PROCESSING,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            transition_handover_status(
                conn, pkg_id2, HP_STATUS_COMPLETED,
                operator="admin_user", role=HP_ROLE_ADMIN,
            )

            r = update_handover_package(
                conn, pkg_id2, {"title": "修改已完成包"},
                operator="admin_user", role=HP_ROLE_ADMIN,
            )
            self.assertFalse(r["success"])
            self.assertIn("已完成", r["error"])

    def test_D_export(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)
            create_handover_package(
                conn, title="导出测试包", discrepancy_ids=disc_ids,
                receiver="user_b", created_by="creator1",
            )

            export_data = export_handover_packages_json(conn)
            self.assertEqual(export_data["export_type"], "handover_packages")
            self.assertEqual(export_data["total"], 1)
            self.assertIn("export_version", export_data)
            self.assertIn("exported_at", export_data)
            self.assertEqual(len(export_data["handover_packages"]), 1)

            pkg = export_data["handover_packages"][0]
            self.assertEqual(pkg["title"], "导出测试包")
            self.assertIn("items", pkg)
            self.assertEqual(len(pkg["items"]), 3)

    def test_E_import_conflict_precheck(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)
            result = create_handover_package(
                conn, title="导入预检包", discrepancy_ids=disc_ids,
                receiver="user_c", created_by="creator1",
            )
            pkg_id = result["package_id"]

            export_data = export_handover_packages_json(conn)

            disc_ids2 = _insert_sample_discrepancies(conn, 2, store_id="S099")
            result2 = create_handover_package(
                conn, title="新包预检", discrepancy_ids=disc_ids2,
                created_by="creator2",
            )
            export_data2 = export_handover_packages_json(conn, store_id="S099")

            for pkg in export_data2["handover_packages"]:
                pkg["pkg_no"] = "HP_EXTERNAL_0001"

            preview = preview_handover_packages_import(conn, export_data2)
            self.assertTrue(preview["success"])
            self.assertEqual(preview["new_count"], 1)
            self.assertEqual(preview["safe_count"], 0)
            self.assertEqual(preview["conflict_count"], 0)

            safe_preview = preview_handover_packages_import(conn, export_data)
            self.assertTrue(safe_preview["success"])
            self.assertTrue(any(
                r["import_status"] == "safe" for r in safe_preview["preview_results"]
            ))

            conn.execute(
                "UPDATE discrepancies SET review_note = ? WHERE id = ?",
                ("修改后的备注", disc_ids[0]),
            )
            conn.execute(
                "UPDATE discrepancies SET updated_at = ? WHERE id = ?",
                (now_iso(), disc_ids[0]),
            )

            conflict_preview = preview_handover_packages_import(conn, export_data)
            self.assertTrue(conflict_preview["success"])
            conflict_item = conflict_preview["preview_results"][0]
            self.assertEqual(conflict_item["import_status"], "conflict")
            conflict_types = [c["type"] for c in conflict_item["conflicts"]]
            self.assertIn("note_changed", conflict_types)

        with self._get_conn() as conn:
            disc_ids3 = _insert_sample_discrepancies(conn, 2, store_id="S100")
            result3 = create_handover_package(
                conn, title="状态冲突测试", discrepancy_ids=disc_ids3,
                created_by="creator3",
            )
            pkg_id3 = result3["package_id"]

            export_data3 = export_handover_packages_json(conn, store_id="S100")

            conn.execute(
                "UPDATE discrepancies SET status = ?, updated_at = ? WHERE id = ?",
                (STATUS_CONFIRMED, now_iso(), disc_ids3[0]),
            )

            conflict_preview3 = preview_handover_packages_import(conn, export_data3)
            self.assertTrue(conflict_preview3["success"])
            conflict_item3 = conflict_preview3["preview_results"][0]
            self.assertEqual(conflict_item3["import_status"], "conflict")
            conflict_types3 = [c["type"] for c in conflict_item3["conflicts"]]
            self.assertIn("status_changed", conflict_types3)

        with self._get_conn() as conn:
            disc_ids4 = _insert_sample_discrepancies(conn, 2, store_id="S101")
            result4 = create_handover_package(
                conn, title="本地更新冲突", discrepancy_ids=disc_ids4,
                created_by="creator4",
            )
            pkg_id4 = result4["package_id"]

            export_data4 = export_handover_packages_json(conn, store_id="S101")

            import copy
            export_snapshot = copy.deepcopy(export_data4)
            for pkg in export_snapshot["handover_packages"]:
                for item in pkg.get("items", []):
                    old_updated = item.get("disc_updated_at", "")
                    if old_updated:
                        item["disc_updated_at"] = old_updated[:10] + "T00:00:00.000000"

            conn.execute(
                "UPDATE discrepancies SET updated_at = ? WHERE id = ?",
                (now_iso(), disc_ids4[0]),
            )

            newer_preview = preview_handover_packages_import(conn, export_snapshot)
            self.assertTrue(newer_preview["success"])
            newer_item = newer_preview["preview_results"][0]
            if newer_item["import_status"] == "conflict":
                newer_types = [c["type"] for c in newer_item["conflicts"]]
                self.assertIn("local_newer", newer_types)

    def test_F_cross_restart_persistence(self):
        with self._get_conn() as conn:
            test_state = {
                "filter_store": "S001",
                "filter_status": HP_STATUS_PENDING_HANDOVER,
                "keyword": "测试",
            }
            save_hp_ui_state(conn, test_state)
            loaded_state = load_hp_ui_state(conn)
            self.assertEqual(loaded_state, test_state)

            draft = {
                "title": "未完成草稿",
                "selected_ids": [1, 2, 3],
                "note": "待后续处理",
            }
            save_hp_draft(conn, draft)
            loaded_draft = load_hp_draft(conn)
            self.assertEqual(loaded_draft, draft)

            clear_hp_draft(conn)
            cleared_draft = load_hp_draft(conn)
            self.assertIsNone(cleared_draft)

            selection = [10, 20, 30]
            save_hp_batch_selection(conn, selection)
            loaded_sel = load_hp_batch_selection(conn)
            self.assertEqual(loaded_sel, selection)

    def test_G_audit_log(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 3)
            result = create_handover_package(
                conn, title="日志审计包", discrepancy_ids=disc_ids,
                receiver="user_d", created_by="creator_log",
            )
            pkg_id = result["package_id"]

            logs = get_handover_package_logs(conn, pkg_id)
            self.assertTrue(any(l["action_type"] == "create" for l in logs))

            transition_handover_status(
                conn, pkg_id, HP_STATUS_RECEIVED,
                operator="user_d", role=HP_ROLE_ADMIN,
            )
            logs = get_handover_package_logs(conn, pkg_id)
            self.assertTrue(any(l["action_type"] == "receive" for l in logs))

            disc_ids2 = _insert_sample_discrepancies(conn, 2, store_id="S200")
            result2 = create_handover_package(
                conn, title="撤回日志包", discrepancy_ids=disc_ids2,
                created_by="creator_withdraw",
            )
            pkg_id2 = result2["package_id"]

            transition_handover_status(
                conn, pkg_id2, HP_STATUS_WITHDRAWN,
                operator="creator_withdraw", role=HP_ROLE_NORMAL,
            )
            logs2 = get_handover_package_logs(conn, pkg_id2)
            self.assertTrue(any(l["action_type"] == "withdraw" for l in logs2))

            export_data = export_handover_packages_json(conn)
            all_logs = get_all_handover_logs(conn, limit=100)
            self.assertTrue(any(l["action_type"] == "export" for l in all_logs))

            preview = preview_handover_packages_import(conn, export_data)
            self.assertTrue(preview["success"])

            confirm_result = confirm_handover_packages_import(
                conn, preview["preview_results"], export_data,
                operator="import_user",
            )
            self.assertTrue(confirm_result["success"])
            all_logs3 = get_all_handover_logs(conn, limit=200)
            self.assertTrue(any(l["action_type"] == "import_confirm" for l in all_logs3))

    def test_H_filter_and_restore_view(self):
        with self._get_conn() as conn:
            ids_s001 = _insert_sample_discrepancies(conn, 3, store_id="S001")
            ids_s002 = _insert_sample_discrepancies(conn, 2, store_id="S002")

            create_handover_package(
                conn, title="S001包A", discrepancy_ids=ids_s001[:2],
                receiver="user_x", created_by="creator1",
                filter_snapshot={"store_id": "S001", "status": HP_STATUS_PENDING_HANDOVER},
            )
            create_handover_package(
                conn, title="S002包B", discrepancy_ids=ids_s002,
                receiver="user_y", created_by="creator2",
                filter_snapshot={"store_id": "S002"},
            )
            result_c = create_handover_package(
                conn, title="S001包C", discrepancy_ids=[ids_s001[2]],
                receiver="user_z", created_by="creator1",
                filter_snapshot={"store_id": "S001"},
            )

            all_pkgs = list_handover_packages(conn)
            self.assertEqual(len(all_pkgs), 3)

            s001_pkgs = list_handover_packages(conn, store_id="S001")
            self.assertEqual(len(s001_pkgs), 2)

            pending_pkgs = list_handover_packages(conn, status=HP_STATUS_PENDING_HANDOVER)
            self.assertEqual(len(pending_pkgs), 3)

            user_x_pkgs = list_handover_packages(conn, receiver="user_x")
            self.assertEqual(len(user_x_pkgs), 1)
            self.assertEqual(user_x_pkgs[0]["title"], "S001包A")

            kw_pkgs = list_handover_packages(conn, keyword="包C")
            self.assertEqual(len(kw_pkgs), 1)
            self.assertEqual(kw_pkgs[0]["title"], "S001包C")

            pkg_c = get_handover_package(conn, result_c["package_id"])
            self.assertIsNotNone(pkg_c["filter_snapshot"])
            fs = json.loads(pkg_c["filter_snapshot"]) if isinstance(pkg_c["filter_snapshot"], str) else pkg_c["filter_snapshot"]
            self.assertEqual(fs["store_id"], "S001")

            stores = get_hp_stores(conn)
            self.assertIn("S001", stores)
            self.assertIn("S002", stores)

            receivers = get_hp_receivers(conn)
            self.assertIn("user_x", receivers)
            self.assertIn("user_y", receivers)
            self.assertIn("user_z", receivers)

    def test_I_sample_data_generation(self):
        with self._get_conn() as conn:
            disc_ids = _insert_sample_discrepancies(conn, 5)
            generate_handover_sample_data(conn)

            pkgs = list_handover_packages(conn)
            self.assertGreater(len(pkgs), 0)

            for pkg in pkgs:
                items = get_handover_package_items(conn, pkg["id"])
                self.assertGreater(len(items), 0)


if __name__ == "__main__":
    unittest.main()
