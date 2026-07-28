import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "ai_edit_v2_secret_scan.py"


class SecretScannerTests(unittest.TestCase):
    def _repo(self):
        temp = tempfile.TemporaryDirectory(prefix="secret-scan-")
        root = Path(temp.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return temp, root

    def _scan(self, root):
        return subprocess.run(
            [sys.executable, str(SCANNER), "--root", str(root)],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )

    def test_scans_tracked_and_untracked_fixtures_but_honors_standard_ignores(self):
        temp, root = self._repo()
        self.addCleanup(temp.cleanup)
        (root / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
        tracked_secret = "sk-" + "proj-" + "abcdefghijklmnopqrstuvwxyz123456"
        (root / "tracked.env").write_text(f"OPENAI_API_KEY={tracked_secret}\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "tracked.env"], cwd=root, check=True)
        fixture = root / "tests" / "fixtures"
        fixture.mkdir(parents=True)
        untracked_secret = "shotstack_live_abcdefghijklmnopqrstuvwxyz"
        (fixture / "provider.json").write_text(
            f'{{"SHOTSTACK_API_KEY":"{untracked_secret}"}}', encoding="utf-8"
        )
        ignored_secret = "xi_ignored_abcdefghijklmnopqrstuvwxyz"
        (root / "ignored.env").write_text(
            f"ELEVENLABS_API_KEY={ignored_secret}\n", encoding="utf-8"
        )

        completed = self._scan(root)

        self.assertEqual(completed.returncode, 1, completed)
        self.assertIn("tracked.env:openai", completed.stdout)
        self.assertIn("tests/fixtures/provider.json:shotstack", completed.stdout)
        self.assertNotIn("ignored.env", completed.stdout)
        self.assertNotIn(tracked_secret, completed.stdout + completed.stderr)
        self.assertNotIn(untracked_secret, completed.stdout + completed.stderr)
        self.assertNotIn(ignored_secret, completed.stdout + completed.stderr)

    def test_provider_canaries_report_only_path_and_type(self):
        temp, root = self._repo()
        self.addCleanup(temp.cleanup)
        values = {
            "dashscope": "dashscope_live_abcdefghijklmnopqrstuvwxyz",
            "cos_secret_id": "AKIDabcdefghijklmnopqrstuvwxyz",
            "cos_secret_key": "cos_live_abcdefghijklmnopqrstuvwxyz",
            "elevenlabs": "xi_live_abcdefghijklmnopqrstuvwxyz",
        }
        text = "\n".join([
            f"DASHSCOPE_API_KEY={values['dashscope']}",
            f"AI_EDIT_V2_COS_SECRET_ID={values['cos_secret_id']}",
            f"AI_EDIT_V2_COS_SECRET_KEY={values['cos_secret_key']}",
            f"ELEVENLABS_API_KEY={values['elevenlabs']}",
        ])
        (root / "canaries.env").write_text(text, encoding="utf-8")

        completed = self._scan(root)

        self.assertEqual(completed.returncode, 1, completed)
        for secret_type, value in values.items():
            self.assertIn(f"canaries.env:{secret_type}", completed.stdout)
            self.assertNotIn(value, completed.stdout + completed.stderr)

    def test_placeholders_are_clean(self):
        temp, root = self._repo()
        self.addCleanup(temp.cleanup)
        (root / "example.env").write_text(
            "OPENAI_API_KEY=test-placeholder\nSHOTSTACK_API_KEY=<replace-me>\n",
            encoding="utf-8",
        )
        completed = self._scan(root)
        self.assertEqual(completed.returncode, 0, completed)
        self.assertEqual(completed.stdout.strip(), "secret_scan=clean")


if __name__ == "__main__":
    unittest.main()
