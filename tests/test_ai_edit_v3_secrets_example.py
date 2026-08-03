from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "deploy" / "huangque-secrets.env.example"


class V3SecretsExampleTests(unittest.TestCase):
    def test_v3_section_has_one_example_only_elevenlabs_key(self):
        text = EXAMPLE.read_text(encoding="utf-8")
        match = re.search(
            r"^### /etc/huangque/ai-edit-v3\.env\n(?P<body>[\s\S]*?)(?=^### |\Z)",
            text,
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(match)
        section = match.group("body")
        self.assertEqual(
            ["ELEVENLABS_API_KEY=replace-with-elevenlabs-key"],
            [line for line in section.splitlines() if line.startswith("ELEVENLABS_API_KEY=")],
        )
        self.assertIn("music_v2", section)
        self.assertIn("eleven_text_to_sound_v2", section)
        self.assertNotIn("ELEVENLABS_BASE", section)
        self.assertNotIn("https://api.elevenlabs.io", section)


if __name__ == "__main__":
    unittest.main()
