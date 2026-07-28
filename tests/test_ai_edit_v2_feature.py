import os
import unittest
from unittest.mock import patch

from server.content_domains import ai_edit_v2_feature as feature


class FeatureCapabilityTests(unittest.TestCase):
    def test_capability_disables_advanced_renderers(self):
        with patch.dict(os.environ, {"AI_EDIT_V2_ENABLED": "1"}, clear=True):
            value = feature.capability()

        self.assertFalse(value["renderers"]["remotion"])
        self.assertFalse(value["renderers"]["hyperframes"])
        self.assertFalse(value["generation"]["ai_video"])

    def test_shotstack_requires_fully_configured_stable_runtime(self):
        configured = {
            "AI_EDIT_V2_ENABLED": "1",
            "DASHSCOPE_API_KEY": "test-dashscope-key",
            "OPENAI_API_KEY": "test-openai-key",
            "ELEVENLABS_API_KEY": "test-elevenlabs-key",
            "SHOTSTACK_API_KEY": "test-shotstack-key",
            "AI_EDIT_V2_COS_SECRET_ID": "test-cos-id",
            "AI_EDIT_V2_COS_SECRET_KEY": "test-cos-key",
            "AI_EDIT_V2_COS_REGION": "ap-guangzhou",
            "AI_EDIT_V2_COS_BUCKET": "test-private-bucket",
        }
        with patch.dict(os.environ, configured, clear=True), patch.object(
            feature, "runtime_ready", return_value=True
        ), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
            value = feature.capability()

        self.assertTrue(value["renderers"]["shotstack"])


if __name__ == "__main__":
    unittest.main()
