from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from unittest import mock
import json
import shutil
import subprocess
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import matrix_material_adaptation as adaptation


class AdaptationTests(unittest.TestCase):
    def plan(self, duration, count=2, kind="video"):
        items = [{"sha256": "a"*64, "media_type": kind}]
        scenes = [{"scene_id": f"media_{i:02}", "clip_duration_seconds": 4.867} for i in range(count)]
        return adaptation.plan(items, scenes, lambda item: duration)

    def test_short_source_loops_and_fills_missing_slots(self):
        plan = self.plan(4.608)
        self.assertEqual(len(plan), 2)
        self.assertEqual(plan[0]["adaptation"]["mode"], "loop")
        self.assertTrue(plan[1]["adaptation"]["reused"])

    def test_tiny_source_freezes(self):
        self.assertEqual(self.plan(.9)[0]["adaptation"]["mode"], "freeze")

    def test_images_become_timed_video(self):
        self.assertEqual(self.plan(0, kind="image")[0]["adaptation"]["mode"], "image")

    def test_long_sources_use_entire_duration_deterministically(self):
        first = self.plan(30.)
        self.assertEqual(first, self.plan(30.))
        self.assertGreater(first[0]["clip_start_seconds"], 0.)
        self.assertNotEqual(first[0]["clip_start_seconds"], first[1]["clip_start_seconds"])

    def test_bad_source_skipped_and_all_bad_fails(self):
        items = [{"sha256": "a"*64, "media_type": "video"}, {"sha256": "b"*64, "media_type": "video"}]
        scenes = [{"scene_id": "one", "clip_duration_seconds": 5}]
        inspect = Mock(side_effect=[ValueError("decode"), 6.])
        output = adaptation.plan(items, scenes, inspect)
        self.assertEqual(output[0]["sha256"], "b"*64)
        self.assertEqual(output[0]["adaptation"]["skipped_indices"], [0])
        with self.assertRaisesRegex(ValueError, "MATERIAL_UNAVAILABLE"):
            adaptation.plan(items, scenes, Mock(side_effect=ValueError("bad")))

    def test_invalid_start_is_repaired(self):
        item = {"sha256": "a"*64, "media_type": "video", "clip_start_seconds": 50}
        result = adaptation.plan([item], [{"scene_id": "one", "clip_duration_seconds": 5}], lambda _: 9.)
        self.assertLessEqual(result[0]["clip_start_seconds"], 3.85)


class AdmissionIntegrationTests(unittest.TestCase):
    from tests.test_matrix_owned_admission import OwnedReferenceAdmissionTests as Fixture
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    TOP, BOTTOM = Fixture.TOP, Fixture.BOTTOM
    _width = staticmethod(Fixture._width)
    _semantic, _raw, materials = Fixture._semantic, Fixture._raw, Fixture.materials

    def test_adaptive_reference_freezes_count_and_reuses_sources(self):
        from server import matrix_template_api as matrix
        self.service.enforce_user_materials = True
        raw = dict(self._raw(materials=self.materials(1), policy="owned_public"), material_adaptation="auto-v1")
        with mock.patch.object(self.service, "_inspect_user_asset", return_value=.9):
            first = self.service.submit(raw, "adaptive-source-01")
            frozen = json.loads(self.service.store.get(first["job_id"])["payload"])
            selected = self.service._select_materials(frozen, first["job_id"])
            self.assertGreaterEqual(len(selected), 3)
            self.assertEqual(selected, self.service._select_materials(frozen, first["job_id"]))
            self.assertEqual(first["job_id"], self.service.submit(raw, "adaptive-source-01")["job_id"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg needed")
class RealMediaTests(unittest.TestCase):
    def test_loop_freeze_and_image_produce_full_length_no_black_tail(self):
        from server import matrix_template_api as matrix
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "short.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=160x240:rate=30:duration=0.7", "-c:v", "libx264", str(source)], check=True)
            image = root / "still.png"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-frames:v", "1", str(image)], check=True)
            service = Mock()
            service.gpu_runtime = None
            service._source_color = matrix.MatrixTemplateService._source_color
            service._clip_color_encoding = matrix.MatrixTemplateService._clip_color_encoding
            service._reference_video_duration = matrix.MatrixTemplateService._reference_video_duration

            def run(command, **kwargs):
                result = subprocess.run(command, capture_output=True, timeout=kwargs["timeout_seconds"])
                return result.returncode, result.stdout, result.stderr
            service._run_tracked_process = run
            for mode in ("loop", "freeze", "image"):
                item = {"adaptation": {"mode": mode}, "clip_duration_seconds": 2.0}
                target = root / (mode + ".mp4")
                adaptation.prepare(service, item, image if mode == "image" else source, target, time.time()+30)
                self.assertGreaterEqual(service._reference_video_duration(target), 2.27)
                result = subprocess.run(["ffmpeg", "-v", "info", "-i", str(target), "-vf", "blackdetect=d=0.1", "-f", "null", "-"], capture_output=True)
                self.assertNotIn(b"black_start", result.stderr)


if __name__ == "__main__":
    unittest.main()
