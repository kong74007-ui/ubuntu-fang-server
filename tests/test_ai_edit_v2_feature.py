import os
import unittest
from unittest.mock import patch

from server.content_domains import ai_edit_v2_feature as feature


class FeatureCapabilityTests(unittest.TestCase):
    @staticmethod
    def _configured_environment():
        return {
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

    def test_capability_disables_advanced_renderers(self):
        with patch.dict(os.environ, {"AI_EDIT_V2_ENABLED": "1"}, clear=True):
            value = feature.capability()

        self.assertFalse(value["renderers"]["remotion"])
        self.assertFalse(value["renderers"]["hyperframes"])
        self.assertFalse(value["generation"]["ai_video"])

    def test_shotstack_requires_fully_configured_stable_runtime(self):
        configured = self._configured_environment()
        with patch.dict(os.environ, configured, clear=True), patch.object(
            feature, "runtime_ready", return_value=True
        ), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
            value = feature.capability()

        self.assertTrue(value["renderers"]["shotstack"])

    def test_example_placeholders_leave_shotstack_disabled(self):
        example = {
            "AI_EDIT_V2_ENABLED": "1",
            "DASHSCOPE_API_KEY": "replace-with-dashscope-key",
            "OPENAI_API_KEY": "replace-with-openai-key",
            "ELEVENLABS_API_KEY": "replace-with-elevenlabs-key",
            "SHOTSTACK_API_KEY": "replace-with-shotstack-key",
            "AI_EDIT_V2_COS_SECRET_ID": "replace-with-v2-cos-secret-id",
            "AI_EDIT_V2_COS_SECRET_KEY": "replace-with-v2-cos-secret-key",
            "AI_EDIT_V2_COS_REGION": "ap-guangzhou",
            "AI_EDIT_V2_COS_BUCKET": "replace-with-private-bucket-name",
        }
        with patch.dict(os.environ, example, clear=True), patch.object(
            feature, "runtime_ready", return_value=True
        ), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
            value = feature.capability()

        self.assertFalse(value["renderers"]["shotstack"])

    def test_each_required_stable_component_keeps_shotstack_disabled_when_missing(self):
        required_variables = (
            "DASHSCOPE_API_KEY",
            "OPENAI_API_KEY",
            "ELEVENLABS_API_KEY",
            "SHOTSTACK_API_KEY",
            "AI_EDIT_V2_COS_SECRET_ID",
            "AI_EDIT_V2_COS_SECRET_KEY",
            "AI_EDIT_V2_COS_REGION",
            "AI_EDIT_V2_COS_BUCKET",
        )
        for missing in required_variables:
            with self.subTest(missing=missing):
                configured = self._configured_environment()
                del configured[missing]
                with patch.dict(os.environ, configured, clear=True), patch.object(
                    feature, "runtime_ready", return_value=True
                ), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
                    value = feature.capability()
                self.assertFalse(value["renderers"]["shotstack"])

        for unavailable_tool in ("ffmpeg", "ffprobe"):
            with self.subTest(unavailable_tool=unavailable_tool), patch.dict(
                os.environ, self._configured_environment(), clear=True
            ), patch.object(feature, "runtime_ready", return_value=True), patch.object(
                feature.shutil,
                "which",
                side_effect=lambda command: None
                if command == unavailable_tool
                else "/usr/bin/tool",
            ):
                value = feature.capability()
            self.assertFalse(value["renderers"]["shotstack"])


if __name__ == "__main__":
    unittest.main()
