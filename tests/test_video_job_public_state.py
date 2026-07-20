import pathlib
import sqlite3
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import core


class VideoJobPublicStateTests(unittest.TestCase):
    def _row(self, status, refunded=0, error="upstream failed"):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 id, 'video' kind, 'fang' username, 20 cost, ? status, "
            "NULL result, ? error, 1 created_at, 2 updated_at, ? refunded",
            (status, error, refunded),
        ).fetchone()
        conn.close()
        return row

    def test_failed_job_overrides_stale_downloading_phase_and_exposes_refund(self):
        public = core._job_public_dict(self._row("failed", refunded=1), "downloading")
        self.assertEqual("failed", public["phase"])
        self.assertIs(True, public["refunded"])

    def test_done_job_has_done_phase(self):
        public = core._job_public_dict(self._row("done"), "downloading")
        self.assertEqual("done", public["phase"])
        self.assertIs(False, public["refunded"])

    def test_heygen_failure_returns_friendly_content_audit_message(self):
        raw = (
            '剧情视频已提交 HeyGen(video_id=private-id，已扣费)，后续失败: '
            'HeyGen视频生成失败: {"id": "private-id", "status": "failed"}'
        )
        row = self._row("error", refunded=1, error=raw)

        public = core._job_public_dict(row, "processing")

        self.assertEqual(
            "内容审核失败，请尝试更换其他图片，视频或提示词。\n点数已退还。",
            public["error"],
        )
        self.assertNotIn("HeyGen", public["error"])
        self.assertNotIn("private-id", public["error"])
        self.assertEqual(raw, row["error"], "数据库原始错误应保留供后台排查")

    def test_non_heygen_failure_is_not_mislabeled_as_content_audit(self):
        public = core._job_public_dict(
            self._row("error", refunded=1, error="The read operation timed out"),
            "processing",
        )

        self.assertEqual("The read operation timed out", public["error"])


if __name__ == "__main__":
    unittest.main()
