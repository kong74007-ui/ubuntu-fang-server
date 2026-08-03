import pathlib
import unittest


ASSETS_HTML = pathlib.Path(__file__).resolve().parents[1] / "site/workbench/assets.html"


class AiEditV2AssetPreviewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = ASSETS_HTML.read_text(encoding="utf-8")

    def test_v2_action_refreshes_owner_scoped_url_without_cache(self):
        self.assertIn("function refreshVideoAssetUrl(x){", self.html)
        block = self.html.split("function refreshVideoAssetUrl(x){", 1)[1].split(
            "function previewVideoAsset", 1
        )[0]
        self.assertIn("['ai_edit_v2','ai_edit_v3'].indexOf(x.mode)<0", block)
        self.assertIn("/api/gen/video/assets?limit=120", block)
        self.assertIn("cache:'no-store'", block)
        self.assertIn("Authorization:'Bearer '+tok", block)
        self.assertIn("String(item.id)===String(x.id)", block)
        self.assertIn("item.mode===x.mode", block)
        self.assertIn("x.video_url=fresh", block)

    def test_preview_and_download_wait_for_a_fresh_url(self):
        self.assertIn("function previewVideoAsset(x,title,imageUrl){", self.html)
        self.assertIn("function downloadVideoAsset(x,button){", self.html)
        card = self.html.split("function videoCard(x){", 1)[1].split(
            "function escapeHtml", 1
        )[0]
        self.assertIn("previewVideoAsset(x,title,imageUrl)", card)
        self.assertIn("downloadVideoAsset(x,dl)", card)
        self.assertNotIn("openAssetVideoModal(videoUrl,title,imageUrl)", card)
        self.assertNotIn("downloadAsset(videoUrl,'huangque-video.mp4',dl)", card)

    def test_refresh_failure_never_reuses_the_expired_url(self):
        preview = self.html.split("function previewVideoAsset(x,title,imageUrl){", 1)[1].split(
            "function downloadVideoAsset", 1
        )[0]
        download = self.html.split("function downloadVideoAsset(x,button){", 1)[1].split(
            "function openAssetImageModal", 1
        )[0]
        self.assertIn("refreshVideoAssetUrl(x).then", preview)
        self.assertIn("refreshVideoAssetUrl(x).then", download)
        self.assertIn("无法刷新视频地址，请重试", preview)
        self.assertIn("无法刷新视频地址，请重试", download)


if __name__ == "__main__":
    unittest.main()
