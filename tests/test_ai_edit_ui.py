# -*- coding: utf-8 -*-
import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")


class AiEditUiTests(unittest.TestCase):
    def test_tab_and_panel_exist(self):
        self.assertIn('data-video-function="ai-edit"', HTML)
        self.assertIn('id="aiEditPanel"', HTML)

    def test_controls_have_stable_ids(self):
        for control_id in (
            "aiEditSourceType", "aiEditSourceSelect", "aiEditUpload", "aiEditStyle",
            "aiEditMaterials", "aiEditCaptions", "aiEditAutoAssets",
            "aiEditRatio", "aiEditCost", "aiEditSubmit", "aiEditStatus",
        ):
            self.assertIn('id="{}"'.format(control_id), HTML)

    def test_uses_only_v1_edit_endpoints(self):
        panel_script = HTML.split("function loadAiEditAssets", 1)[1]
        for path in ("/api/v1/edit/styles", "/api/v1/edit/uploads", "/api/v1/edit/jobs"):
            self.assertIn(path, panel_script)
        self.assertNotIn("api.shotstack.io", HTML)
        self.assertNotIn("SHOTSTACK_API_KEY", HTML)

    def test_submission_uses_idempotency_key(self):
        self.assertIn("Idempotency-Key", HTML)
        self.assertIn("crypto.randomUUID", HTML)

    def test_upload_urls_are_not_persisted(self):
        script = HTML.split("function loadAiEditAssets", 1)[1]
        self.assertNotRegex(script, r"localStorage\.(?:setItem|getItem)[^\n]*(?:put_url|video_url|signed)")

    def test_inline_script_is_valid_javascript(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", HTML)
        self.assertTrue(scripts)
        result = subprocess.run(
            ["node", "--check", "-"], input=scripts[-1], text=True,
            encoding="utf-8", capture_output=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
