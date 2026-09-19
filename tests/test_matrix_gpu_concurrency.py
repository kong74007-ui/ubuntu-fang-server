import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import matrix_template_api as api


class MatrixGpuConcurrencyTests(unittest.TestCase):
    def service(self, root, slots, gpu):
        runtime = SimpleNamespace(templates=set(), public=lambda _: {'ready': True})
        with mock.patch.object(api, '_load_bundled_fonts', return_value={}), \
             mock.patch('server.matrix_gpu_runtime.GpuRuntime', return_value=runtime):
            return api.MatrixTemplateService(
                data_root=root / 'data', skill_root=root,
                library_url='http://127.0.0.1:8111',
                library_token='fixture-token', legacy_templates_enabled=False,
                gpu_mode='required' if gpu else 'disabled',
                gpu_runtime_root=root, hyperframes_browser=root / 'browser',
                concurrency=5, hyperframes_concurrency=slots, start_worker=False,
            )

    def test_verified_gpu_admits_five_real_slots_but_not_six(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), 5, True)
            try:
                self.assertEqual(5, service.hyperframes_concurrency)
                self.assertEqual(5, service.concurrency)
                for _ in range(5):
                    self.assertTrue(service.hyperframes_slots.acquire(blocking=False))
                self.assertFalse(service.hyperframes_slots.acquire(blocking=False))
                for _ in range(5):
                    service.hyperframes_slots.release()
            finally:
                service.shutdown()

    def test_five_gpu_slot_users_can_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), 5, True)
            barrier = threading.Barrier(6)
            errors = []
            def run():
                acquired = False
                try:
                    service._acquire_hyperframes_slot(time.time() + 5)
                    acquired = True
                    barrier.wait(timeout=3)
                except Exception as error:
                    errors.append(type(error).__name__)
                finally:
                    if acquired:
                        service.hyperframes_slots.release()
            threads = [threading.Thread(target=run) for _ in range(5)]
            try:
                for thread in threads:
                    thread.start()
                barrier.wait(timeout=3)
                for thread in threads:
                    thread.join(timeout=4)
                self.assertFalse(errors)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
            finally:
                barrier.abort()
                for thread in threads:
                    thread.join(timeout=4)
                service.shutdown()

    def test_legacy_limit_and_invalid_gpu_limits_stay_closed(self):
        for gpu, slots in [(False, 3), (False, 5), (True, 0), (True, 6)]:
            with self.subTest(gpu=gpu, slots=slots), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(api.MatrixTemplateError, 'HyperFrames concurrency'):
                    self.service(Path(tmp), slots, gpu)
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), 2, False)
            service.shutdown()


if __name__ == '__main__':
    unittest.main()
