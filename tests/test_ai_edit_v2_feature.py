import os
import unittest
from unittest.mock import patch

from server.content_domains import ai_edit_v2_feature as feature


class FeatureCapabilityTests(unittest.TestCase):
    def test_capability_uses_constructed_production_dependency_readiness(self):
        with patch.dict(os.environ, {"AI_EDIT_V2_ENABLED": "1"}, clear=False), \
             patch.object(feature.runtime, "production_dependencies", return_value={
                 "readiness_errors": lambda: ["AI_EDIT_V2_REPAIR_PROVIDER"]
             }):
            state = feature.capability()

        self.assertFalse(state["accepts_submissions"])
        self.assertFalse(state["stable_runtime_ready"])
        self.assertEqual(state["readiness_errors"], ["AI_EDIT_V2_REPAIR_PROVIDER"])
    @staticmethod
    def _configured_environment():
        return {
            "AI_EDIT_V2_ENABLED": "1",
            "DASHSCOPE_API_KEY": "test-dashscope-key",
            "OPENAI_API_KEY": "test-openai-key",
            "ELEVENLABS_API_KEY": "test-elevenlabs-key",
            "SHOTSTACK_API_KEY": "test-shotstack-key",
            "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": (
                "https://app.example.test/api/v2/edit/webhooks/shotstack"
            ),
            "AI_EDIT_V2_WEBHOOK_SECRET": "test-shotstack-webhook-secret",
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
        ), patch.object(feature.runtime, "production_dependencies", return_value={
            "readiness_errors": lambda: []
        }), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
            value = feature.capability()

        self.assertTrue(value["renderers"]["shotstack"])
        self.assertTrue(value["stable_runtime_ready"])
        self.assertTrue(value["accepts_submissions"])

    def test_missing_quality_runtime_dependency_rejects_submissions(self):
        with patch.dict(os.environ, self._configured_environment(), clear=True), patch.object(
            feature, "runtime_ready", return_value=False
        ), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
            value = feature.capability()
            rejection = feature.rejection()

        self.assertFalse(value["stable_runtime_ready"])
        self.assertFalse(value["accepts_submissions"])
        self.assertEqual(rejection, (
            503, {"code": "ai_edit_v2_not_ready", "detail": "ai_edit_v2_not_ready"}
        ))

    def test_example_placeholders_leave_shotstack_disabled(self):
        example = {
            "AI_EDIT_V2_ENABLED": "1",
            "DASHSCOPE_API_KEY": "replace-with-dashscope-key",
            "OPENAI_API_KEY": "replace-with-openai-key",
            "ELEVENLABS_API_KEY": "replace-with-elevenlabs-key",
            "SHOTSTACK_API_KEY": "replace-with-shotstack-key",
            "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": (
                "https://replace-with-public-host/api/v2/edit/webhooks/shotstack"
            ),
            "AI_EDIT_V2_WEBHOOK_SECRET": "replace-with-random-v2-webhook-secret",
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
            "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL",
            "AI_EDIT_V2_WEBHOOK_SECRET",
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
                self.assertFalse(value["accepts_submissions"])

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
            self.assertFalse(value["accepts_submissions"])

    def test_shotstack_callback_must_be_https(self):
        for invalid_url in (
            "http://app.example.test/api/v2/edit/webhooks/shotstack",
            "not-a-url",
            "https://replace-with-public-host/api/v2/edit/webhooks/shotstack",
        ):
            with self.subTest(invalid_url=invalid_url):
                configured = self._configured_environment()
                configured["AI_EDIT_V2_SHOTSTACK_CALLBACK_URL"] = invalid_url
                with patch.dict(os.environ, configured, clear=True), patch.object(
                    feature, "runtime_ready", return_value=True
                ), patch.object(feature.shutil, "which", return_value="/usr/bin/tool"):
                    value = feature.capability()
                self.assertFalse(value["renderers"]["shotstack"])
                self.assertFalse(value["accepts_submissions"])


if __name__ == "__main__":
    unittest.main()
