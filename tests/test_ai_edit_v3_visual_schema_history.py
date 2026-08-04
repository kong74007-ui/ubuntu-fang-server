from __future__ import annotations

import unittest

from server.content_domains.ai_edit_v3.runtime import schema_hash_is_accepted


class VisualSchemaHistoryTests(unittest.TestCase):
    def test_historical_edit_plan_and_v1_manifest_hashes_remain_readable(self):
        self.assertTrue(schema_hash_is_accepted("edit-plan-2.0.schema.json", "b96c059fa2e4ef7d91cd48278b474d61a34606f1cbce6963c3b65fa66f7d046c"))
        self.assertTrue(schema_hash_is_accepted("render-manifest-v1.schema.json", "eb1f656712ff94bbac31e9d8824d878795110597bca0141814839020f9e2cbc0"))
        self.assertFalse(schema_hash_is_accepted("render-manifest-v2.schema.json", "b96c059fa2e4ef7d91cd48278b474d61a34606f1cbce6963c3b65fa66f7d046c"))


if __name__ == "__main__":
    unittest.main()
