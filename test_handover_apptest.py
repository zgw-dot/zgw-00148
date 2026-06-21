import sys
import os
import json
import unittest
import tempfile
import shutil
import sqlite3
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from db import (
    HP_STATUS_PENDING_HANDOVER, HP_STATUS_RECEIVED,
    HP_STATUS_COMPLETED, HP_STATUS_WITHDRAWN,
    HP_ROLE_ADMIN, HP_ROLE_NORMAL,
    STATUS_PENDING_REVIEW, now_iso,
)

ORIG_DB_PATH = db.DB_PATH
BACKUP_DB_PATH = ORIG_DB_PATH + ".bak"
TEST_DB_PATH = ORIG_DB_PATH


def _insert_discrepancies(conn, count=5, store_id="S001"):
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


class TestHandoverAppTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if os.path.exists(ORIG_DB_PATH):
            shutil.copy2(ORIG_DB_PATH, BACKUP_DB_PATH)
            os.remove(ORIG_DB_PATH)
        db.init_db()
        with db.get_conn() as conn:
            cls.sample_disc_ids = _insert_discrepancies(conn, 5)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(ORIG_DB_PATH):
            os.remove(ORIG_DB_PATH)
        if os.path.exists(BACKUP_DB_PATH):
            shutil.move(BACKUP_DB_PATH, ORIG_DB_PATH)

    def _get_app(self):
        from streamlit.testing.v1 import AppTest
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
        return AppTest.from_file(script_path, default_timeout=30)

    def test_01_app_starts_and_shows_tab8(self):
        at = self._get_app().run()
        self.assertFalse(at.exception, f"App failed to start: {at.exception}")

        self.assertTrue(len(at.tabs) >= 8, f"Expected at least 8 tabs, got {len(at.tabs)}")

        tab8 = at.tabs[7]
        self.assertIsNotNone(tab8, "Tab 8 should exist")

    def test_02_date_range_display_empty(self):
        at = self._get_app().run()
        self.assertFalse(at.exception)

        all_md = [str(m.value) for m in at.markdown]
        all_text = " ".join(all_md)
        self.assertIn("交接包", all_text, "Tab 8 content should be visible in app")

    def test_03_draft_persists_across_restart(self):
        from db import save_hp_draft, load_hp_draft, clear_hp_draft

        with db.get_conn() as conn:
            clear_hp_draft(conn)

        with db.get_conn() as conn:
            draft_data = {
                "title": "AppTest 草稿包",
                "receiver": "tester",
                "handover_note": "来自 AppTest 的草稿",
                "description": "测试草稿持久化",
                "filter_snapshot": {"store_id": "S001"},
                "selected_ids": self.sample_disc_ids[:2],
            }
            result = save_hp_draft(conn, draft_data)
            self.assertTrue(result["success"])

            loaded = load_hp_draft(conn)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["title"], "AppTest 草稿包")
            self.assertEqual(len(loaded["selected_ids"]), 2)

        at = self._get_app().run()
        self.assertFalse(at.exception)

    def test_04_create_package_and_verify(self):
        from db import create_handover_package, list_handover_packages, get_handover_package
        with db.get_conn() as conn:
            before_count = len(list_handover_packages(conn))
            result = create_handover_package(
                conn,
                title="AppTest 正式交接包",
                discrepancy_ids=self.sample_disc_ids[:3],
                receiver="receiver_user",
                handover_note="请处理这 3 条差异",
                filter_snapshot={"store_id": "S001"},
                created_by="apptest_user",
            )
            self.assertTrue(result["success"])
            self.assertIn("pkg_no", result)
            pkg_id = result["package_id"]
            pkg = get_handover_package(conn, pkg_id)
            self.assertIsNotNone(pkg)
            self.assertEqual(pkg["title"], "AppTest 正式交接包")
            self.assertEqual(len(pkg["items"]), 3)
            after_count = len(list_handover_packages(conn))
            self.assertEqual(after_count, before_count + 1)

        at = self._get_app().run()
        self.assertFalse(at.exception)

    def test_05_export_and_preview_import(self):
        from db import (
            create_handover_package, export_handover_packages_json,
            preview_handover_packages_import,
        )
        with db.get_conn() as conn:
            create_handover_package(
                conn,
                title="导回测试包",
                discrepancy_ids=self.sample_disc_ids[2:4],
                receiver="import_tester",
                handover_note="用于导回流程测试",
                filter_snapshot={"store_id": "S001"},
                created_by="exporter",
            )
            export_data = export_handover_packages_json(conn, status=HP_STATUS_PENDING_HANDOVER)
            self.assertIn("handover_packages", export_data)
            self.assertTrue(len(export_data["handover_packages"]) > 0)

            preview = preview_handover_packages_import(conn, export_data)
            self.assertTrue(preview.get("success", False))
            self.assertIn("preview_results", preview)
            self.assertTrue(len(preview["preview_results"]) > 0)

        at = self._get_app().run()
        self.assertFalse(at.exception)
        tab8 = at.tabs[7]
        self.assertIsNotNone(tab8)

    def test_06_prepare_view_data_paths(self):
        from db import hp_prepare_view_data, save_hp_draft, clear_hp_draft
        from db import create_handover_package

        with db.get_conn() as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM handover_package_items")
            conn.execute("DELETE FROM handover_package_logs")
            conn.execute("DELETE FROM handover_packages")
            conn.execute("DELETE FROM ui_state WHERE state_key IN (?, ?)",
                         ("handover_draft", "handover_ui_state"))
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()

        with db.get_conn() as conn:
            data = hp_prepare_view_data(conn, current_user="test", user_role=HP_ROLE_ADMIN)
            self.assertIn(data["startup_path"], ("empty", "draft_only", "has_packages"))

        with db.get_conn() as conn:
            save_hp_draft(conn, {
                "title": "路径测试草稿",
                "selected_ids": self.sample_disc_ids[:1],
            })

        with db.get_conn() as conn:
            data = hp_prepare_view_data(conn, current_user="test", user_role=HP_ROLE_ADMIN)
            self.assertTrue(data["has_draft"])
            self.assertIsNotNone(data["draft"])

        with db.get_conn() as conn:
            create_handover_package(
                conn,
                title="路径测试正式包",
                discrepancy_ids=self.sample_disc_ids[1:3],
                created_by="tester",
            )

        with db.get_conn() as conn:
            data = hp_prepare_view_data(conn, current_user="test", user_role=HP_ROLE_ADMIN)
            self.assertTrue(data["has_packages"])
            self.assertTrue(data["package_count"] > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
