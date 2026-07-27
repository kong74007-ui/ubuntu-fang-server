import json
import pathlib
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import server.admin_api as admin_api


class AdminPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = pathlib.Path(self.temp_dir.name) / "admin.db"
        self.db_patch = patch.object(admin_api, "ADMIN_DB", self.db_path)
        self.db_patch.start()
        admin_api.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_draft_preview_publish_and_audit(self):
        config = admin_api.ai_edit_v2_billing.default_price_config()
        config["base_points"] = 29
        preview = admin_api.preview_ai_edit_v2_pricing({"config": config})
        self.assertEqual(len(preview["scenarios"]), 3)
        self.assertEqual(preview["scenarios"][0]["breakdown"]["base"], 29)

        saved = admin_api.save_ai_edit_v2_price_draft(
            "strong", {"version": "price-admin-v2", "config": config}
        )
        self.assertEqual(saved["status"], "draft")
        with self.assertRaisesRegex(ValueError, "publish_confirmation_required"):
            admin_api.publish_ai_edit_v2_price(
                "strong", {"version": "price-admin-v2", "confirmation": "确认"}
            )
        published = admin_api.publish_ai_edit_v2_price(
            "strong",
            {"version": "price-admin-v2", "confirmation": "发布 price-admin-v2"},
        )
        self.assertEqual(published["status"], "published")
        listing = admin_api.load_ai_edit_v2_pricing()
        self.assertEqual(listing["active_version"], "price-admin-v2")

        with closing(admin_api.db()) as conn:
            actions = [
                row["action"]
                for row in conn.execute(
                    "SELECT action FROM admin_audit ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(
            actions,
            ["ai_edit_v2_price_draft_created", "ai_edit_v2_price_published"],
        )

    def test_admin_page_exposes_preview_and_second_confirmation(self):
        page = (
            pathlib.Path(__file__).resolve().parents[1]
            / "site" / "admin" / "ai-edit-v2-pricing.html"
        ).read_text(encoding="utf-8")
        self.assertIn("价格表预览", page)
        self.assertIn("发布 VERSION", page)
        self.assertIn("/api/admin/ai-edit-v2/pricing/preview", page)
        self.assertIn("/api/admin/ai-edit-v2/pricing/publish", page)


if __name__ == "__main__":
    unittest.main()
