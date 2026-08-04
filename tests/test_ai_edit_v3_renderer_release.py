from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.renderers.release import (
    RendererReleaseError,
    verify_renderer_release,
)


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "server" / "ai_edit_v3_renderer"


class RendererReleaseTests(unittest.TestCase):
    def test_ci_runs_renderer_tests_and_lock_check_under_node_22(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('node-version: "22"', workflow)
        self.assertIn("working-directory: server/ai_edit_v3_renderer", workflow)
        self.assertIn("npm run release:lock:check", workflow)

    def test_committed_release_has_exact_pins_and_measured_fonts(self) -> None:
        report = verify_renderer_release(RELEASE)
        self.assertEqual(report.node_major, 22)
        self.assertEqual(report.hyperframes_version, "0.7.84")
        self.assertEqual(report.gsap_version, "3.15.0")
        lock = json.loads((RELEASE / "renderer-release.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["schema_version"], 2)
        self.assertEqual(report.renderer_build_id, lock["renderer_build_id"])

    def test_one_byte_lock_or_font_change_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "release"
            import shutil
            shutil.copytree(RELEASE, copied, ignore=shutil.ignore_patterns("node_modules"))
            font = copied / "assets" / "fonts" / "NotoSansSC-Regular.woff2"
            font.write_bytes(font.read_bytes() + b"x")
            with self.assertRaisesRegex(RendererReleaseError, "renderer_release_tree_hash_mismatch"):
                verify_renderer_release(copied)

            shutil.rmtree(copied)
            shutil.copytree(RELEASE, copied, ignore=shutil.ignore_patterns("node_modules"))
            lock = copied / "renderer-release.lock.json"
            payload = json.loads(lock.read_text(encoding="utf-8"))
            payload["schema_version"] = "1"
            lock.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RendererReleaseError, "renderer_schema_version_invalid"):
                verify_renderer_release(copied)


if __name__ == "__main__":
    unittest.main()
