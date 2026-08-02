from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from server.content_domains.ai_edit_v3.catalog import load_template_catalog
from server.content_domains.ai_edit_v3.store import StoreConflictError, V3Store


class TemplateCatalogTests(unittest.TestCase):
    def test_catalog_has_four_published_immutable_real_previews(self):
        catalog = load_template_catalog()
        self.assertEqual(4, len(catalog))
        self.assertEqual(2, sum(item.ratio == "16:9" for item in catalog))
        self.assertEqual(2, sum(item.ratio == "9:16" for item in catalog))
        self.assertEqual({"commercial_diagnostic", "editorial_explainer"}, {item.creative_direction for item in catalog})
        for item in catalog:
            self.assertEqual("1", item.version)
            self.assertEqual("published", item.status)
            self.assertTrue(item.title.strip())
            self.assertTrue(item.category.strip())
            self.assertGreaterEqual(len(item.allowed_layouts), 2)
            self.assertTrue(item.capabilities)
            self.assertEqual((item.ratio,), item.supported_ratios)
            self.assertTrue(item.preview_path.is_file())
            self.assertEqual(item.preview_sha256, hashlib.sha256(item.preview_path.read_bytes()).hexdigest())
            self.assertRegex(item.sha256, r"^[0-9a-f]{64}$")

    def test_seed_is_idempotent_and_rejects_same_version_drift(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            v2 = root / "v2.db"
            v2.touch()
            store = V3Store(root / "v3.db", v2_db_path=v2)
            templates = load_template_catalog()
            store.seed_template_versions(templates, now_ms=1000)
            store.seed_template_versions(templates, now_ms=2000)
            for template in templates:
                self.assertEqual(1, len(store.list_template_versions(template.template_id)))
            drift = list(templates)
            drift[0] = drift[0].with_changes(title="被篡改标题")
            with self.assertRaises(StoreConflictError):
                store.seed_template_versions(drift, now_ms=3000)

    def test_unpublished_or_ratio_mismatched_catalog_cannot_seed_active(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            v2 = root / "v2.db"
            v2.touch()
            store = V3Store(root / "v3.db", v2_db_path=v2)
            template = load_template_catalog()[0]
            with self.assertRaises(StoreConflictError):
                store.seed_template_versions([template.with_changes(status="draft")], now_ms=1000)
            with self.assertRaises(StoreConflictError):
                store.seed_template_versions([replace(template, supported_ratios=("9:16",))], now_ms=1000)


if __name__ == "__main__":
    unittest.main()
