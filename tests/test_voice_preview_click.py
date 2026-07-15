import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class VoicePreviewClickTests(unittest.TestCase):
    def test_public_voice_preview_starts_before_async_asset_resolution(self):
        pages = {
            "video": (ROOT / "site/workbench/video.html").read_text(encoding="utf-8"),
            "audio": (ROOT / "site/workbench/audio.html").read_text(encoding="utf-8"),
        }
        for page, html in pages.items():
            with self.subTest(page=page):
                block = html[html.index("function playPreview(url,btn){"):]
                block = block[:block.index("\n  }")]
                self.assertIn("start(fresh(url))", block)
                self.assertIn("activePreview.play()", block)
                self.assertIn("activePreview.onerror=fail", block)
                self.assertLess(block.index("start(fresh(url))"), block.index(".then(start)"))


if __name__ == "__main__":
    unittest.main()
