# -*- coding: utf-8 -*-
"""leadgen_api 的两处修复：

1. COS 转存的总预算（#11）
   线上 23 次转存失败全部是 "The read operation timed out"。原实现 tikhub._http_get(timeout=120)
   的 timeout 只管单次 socket 读，慢 CDN 上 read 会反复续命；再加盲目重试 2 次，最坏在转存上
   耗 240s+，把整个 collect 任务顶过 reaper 判死线 → 判死退点 → worker 又写回 done。
   现在改成分块读 + 每块检查总预算，超预算立即放弃且不再重试。

2. 退点走 auth 服务（#9）
   原 add_points 直接 UPDATE users.db，没有事务、不进 points_audit，collect/leads 的退点在
   审计里完全隐形。改为调 auth 的 refund 接口；auth 不可用时回退直写 —— 宁可少一条审计，
   也不能把用户的点吞了。
"""
import importlib, io, sys, time, unittest
from pathlib import Path


class _FakeResponse(io.BytesIO):
    """够用的 urlopen 返回体替身：支持 with、.headers、.read(n)。"""

    def __init__(self, data, headers=None, chunk_delay=0.0):
        super().__init__(data)
        self.headers = headers or {}
        self._delay = chunk_delay

    def read(self, n=-1):
        if self._delay:
            time.sleep(self._delay)
        return super().read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class CosBudgetTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        self.tikhub = importlib.import_module("tikhub")
        self._orig_opener = self.tikhub._OPENER

    def tearDown(self):
        self.tikhub._OPENER = self._orig_opener

    def _stub_opener(self, response):
        class _O:
            def open(self, req, timeout=None):
                return response
        self.tikhub._OPENER = _O()

    def test_fetch_success(self):
        self._stub_opener(_FakeResponse(b"x" * 1000))
        data = self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 30)
        self.assertEqual(len(data), 1000)

    def test_rejects_oversize_by_content_length(self):
        """Content-Length 预检：下载前就否掉，省掉整段无用等待。"""
        big = str(self.lg.COS_FETCH_MAX_BYTES + 1)
        self._stub_opener(_FakeResponse(b"", {"Content-Length": big}))
        with self.assertRaises(ValueError) as ctx:
            self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 30)
        self.assertIn("超过转存上限", str(ctx.exception))

    def test_rejects_oversize_while_streaming(self):
        """CDN 不给 Content-Length 时，边下边数，超限即停。"""
        self.lg.COS_FETCH_MAX_BYTES, orig = 4096, self.lg.COS_FETCH_MAX_BYTES
        try:
            self._stub_opener(_FakeResponse(b"x" * 100000))
            with self.assertRaises(ValueError):
                self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 30)
        finally:
            self.lg.COS_FETCH_MAX_BYTES = orig

    def test_deadline_already_expired(self):
        self._stub_opener(_FakeResponse(b"x"))
        with self.assertRaises(TimeoutError):
            self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() - 1)

    def test_deadline_exceeded_midstream(self):
        """核心回归：慢 CDN 每块都拖时间，到点必须放弃，而不是无限续命。"""
        self._stub_opener(_FakeResponse(b"x" * 1000000, chunk_delay=0.05))
        t0 = time.time()
        with self.assertRaises(TimeoutError) as ctx:
            self.lg._fetch_within_budget("http://cdn/v.mp4", time.time() + 0.2)
        self.assertLess(time.time() - t0, 2.0, "超预算后仍在继续下载")
        self.assertIn("预算", str(ctx.exception))

    def test_fallback_returns_original_url_and_does_not_raise(self):
        """转存失败必须回退原链接，绝不中断采集。"""
        class _Boom:
            def open(self, req, timeout=None):
                raise OSError("The read operation timed out")
        self.tikhub._OPENER = _Boom()
        orig_cos = sys.modules.get("content_domains.cos")
        import content_domains.cos as cos
        enabled, cos.enabled = cos.enabled, lambda: True
        try:
            out = self.lg.public_url_from_remote("http://cdn/v.mp4", "collect/douyin/1.mp4", "video/mp4")
            self.assertEqual(out, "http://cdn/v.mp4")
        finally:
            cos.enabled = enabled
            if orig_cos is not None:
                sys.modules["content_domains.cos"] = orig_cos


class RefundAuditTests(unittest.TestCase):
    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.lg = importlib.import_module("leadgen_api")
        self._orig_auth = self.lg._auth_points
        self._orig_direct = self.lg._add_points_direct
        self.direct_calls = []
        self.lg._add_points_direct = lambda u, d: (self.direct_calls.append((u, d)), True)[1]

    def tearDown(self):
        self.lg._auth_points = self._orig_auth
        self.lg._add_points_direct = self._orig_direct

    def test_uses_auth_service_when_available(self):
        calls = []
        self.lg._auth_points = lambda path, u, a: (calls.append((path, u, a)), (200, {"points": 9}))[1]
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(calls, [("/api/auth/points/refund", "u", 6)])
        self.assertEqual(self.direct_calls, [], "auth 成功时不该直写 users.db")

    def test_falls_back_to_direct_write_when_auth_fails(self):
        """auth 挂了/令牌漏配时必须仍把点退回去 —— 宁可审计缺一条，也不能吞用户的点。"""
        self.lg._auth_points = lambda path, u, a: (500, {"detail": "HQ_INTERNAL_TOKEN 未配置"})
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(self.direct_calls, [("u", 6)])

    def test_falls_back_on_http_error(self):
        self.lg._auth_points = lambda path, u, a: (403, {"detail": "forbidden"})
        self.assertTrue(self.lg.add_points("u", 6))
        self.assertEqual(self.direct_calls, [("u", 6)])

    def test_auth_points_without_token_short_circuits(self):
        token, self.lg.INTERNAL_TOKEN = self.lg.INTERNAL_TOKEN, ""
        try:
            status, data = self.lg._auth_points("/api/auth/points/refund", "u", 6)
            self.assertEqual(status, 500)
            self.assertIn("HQ_INTERNAL_TOKEN", data["detail"])
        finally:
            self.lg.INTERNAL_TOKEN = token


class PermanentUrlTests(unittest.TestCase):
    """资产库要能区分「永久直链」和「会过期的第三方 CDN 直链」。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        self.store = importlib.import_module("content_domains.assets_store")

    def test_cos_url_is_permanent(self):
        self.assertTrue(self.store._is_permanent_url(
            "https://huangque-media-1435693839.cos.ap-guangzhou.myqcloud.com/collect/douyin/1.mp4"))

    def test_third_party_cdn_is_not_permanent(self):
        for u in ("https://v5-dy-ov-experiment.zjcdn.com/abc",     # 抖音
                  "https://sns-v11.rednotecdn.com/abc",            # 小红书
                  "https://wxapp.tc.qq.com/abc"):                  # 视频号
            self.assertFalse(self.store._is_permanent_url(u), u)

    def test_empty_url_is_not_permanent(self):
        self.assertFalse(self.store._is_permanent_url(""))
        self.assertFalse(self.store._is_permanent_url(None))

    def test_collect_meta_carries_permanent_flag(self):
        _, _, url, meta = self.store._project("collect", {
            "video": {"title": "t", "play_url": "https://v5-dy-ov-experiment.zjcdn.com/x.mp4"}})
        self.assertEqual(url, "https://v5-dy-ov-experiment.zjcdn.com/x.mp4")
        self.assertFalse(meta["permanent"])


if __name__ == "__main__":
    unittest.main()
