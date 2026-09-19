import hashlib
import json
import tempfile
import shutil
import subprocess
import threading
import urllib.request
import urllib.error
import time
import unittest
from pathlib import Path
from unittest import mock

from server import material_library as ml
from server import material_library_api
from server.matrix_template_api import MatrixTemplateService


class MaterialColorGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = []

    def add(self, name):
        data = name.encode()
        sha = hashlib.sha256(data).hexdigest()
        (self.root / (name + '.mp4')).write_bytes(data)
        self.rows.append({'record_id': name, 'sha256': sha, '状态': '可使用',
                          '素材名称': name, 'server_relative_path': name + '.mp4'})
        return sha

    def library(self):
        (self.root / 'index.jsonl').write_text(
            '\n'.join(json.dumps(r) for r in self.rows), encoding='utf-8')
        return ml.MaterialLibrary(self.root, usage_path=self.root / 'usage.json')

    @staticmethod
    def probe(command, **kwargs):
        bad = Path(command[-1]).name.startswith('bad')
        return mock.Mock(stdout=json.dumps({'streams': [{
            'pix_fmt': 'yuv420p' if bad else 'yuv420p10le',
            'color_transfer': 'arib-std-b67', 'color_primaries': 'bt2020',
            'color_space': 'bt2020nc'}]}))

    def scene(self):
        return {'scene_id': 's1', 'media_type': 'video',
                'video_color_contract': 1, 'query': 'bad'}

    def test_real_selection_skips_eight_bit_hdr_and_caches_probe(self):
        bad = self.add('bad'); good = self.add('good'); lib = self.library()
        with mock.patch('subprocess.run', side_effect=self.probe) as probe:
            for _ in range(2):
                result = lib.select([self.scene()])
                self.assertEqual(result['materials'][0]['sha256'], good)
                self.assertNotEqual(result['materials'][0]['sha256'], bad)
            self.assertEqual(probe.call_count, 2)

    def test_shortage_does_not_return_incompatible_material(self):
        self.add('bad'); lib = self.library()
        with mock.patch('subprocess.run', side_effect=self.probe):
            with self.assertRaises(ml.MaterialShortageError):
                lib.select([self.scene()])

    def test_probe_failure_is_not_silently_accepted(self):
        self.add('good'); lib = self.library()
        with mock.patch('subprocess.run', side_effect=OSError('missing ffprobe')):
            with self.assertRaises(ml.MaterialLibraryError):
                lib.select([self.scene()])

    def test_matrix_visual_scenes_request_guard_but_bgm_does_not(self):
        service = object.__new__(MatrixTemplateService)
        service.reference_templates = {}
        scenes, count, _ = service._material_scenes({
            'template_id': 'native-bold', 'top_text': 'title',
            'bottom_text': 'cta', 'bgm': True, 'duration': 10,
            '_video_color_contract': 1})
        self.assertTrue(count)
        self.assertTrue(all(s.get('video_color_contract') == 1 for s in scenes[:count]))
        self.assertNotIn('video_color_contract', scenes[-1])

    def test_old_frozen_payload_keeps_identical_selection_request(self):
        service = object.__new__(MatrixTemplateService)
        service.reference_templates = {}
        scenes, _, _ = service._material_scenes({
            'template_id': 'native-bold', 'top_text': 'title',
            'bottom_text': 'cta', 'bgm': False, 'duration': 10})
        self.assertTrue(all('video_color_contract' not in s for s in scenes))

    def test_sdr_and_native_high_depth_hdr_pass_but_eight_bit_formats_do_not(self):
        self.add('good'); lib = self.library(); scene = self.scene()
        for transfer in ['arib-std-b67', 'smpte2084']:
            for pix, expected in [('yuv420p', False), ('yuv410p', False),
                                  ('nv12', False), ('yuv420p10le', True),
                                  ('p010le', True), ('yuv444p12le', True)]:
                with self.subTest(transfer=transfer, pix=pix):
                    lib._video_color_cache.clear()
                    stream = {'pix_fmt': pix, 'color_transfer': transfer,
                              'color_primaries': 'bt2020', 'color_space': 'bt2020nc'}
                    with mock.patch('subprocess.run', return_value=mock.Mock(
                            stdout=json.dumps({'streams': [stream]}))):
                        if expected:
                            self.assertEqual(len(lib.select([scene])['materials']), 1)
                        else:
                            with self.assertRaises(ml.MaterialShortageError):lib.select([scene])
        lib._video_color_cache.clear()
        with mock.patch('subprocess.run', return_value=mock.Mock(stdout=json.dumps(
                {'streams': [{'pix_fmt': 'yuv420p', 'color_transfer': 'bt709'}]}))):
            self.assertEqual(len(lib.select([scene])['materials']), 1)

    def test_legacy_selection_never_probes_or_changes_receipt(self):
        bad = self.add('bad'); lib = self.library()
        scene = self.scene(); scene.pop('video_color_contract')
        with mock.patch('subprocess.run', side_effect=AssertionError('legacy probe')):
            result = lib.select([scene], selection_mode='round_robin', selection_id='old-job')
            self.assertEqual(result['materials'][0]['sha256'], bad)
            self.assertEqual(result, lib.select([scene], selection_mode='round_robin', selection_id='old-job'))
        with self.assertRaises(ml.MaterialSelectionConflictError):
            lib.select([self.scene()], selection_mode='round_robin', selection_id='old-job')

    def test_guarded_receipt_replays_without_reselecting_or_probing(self):
        self.add('bad'); good = self.add('good'); lib = self.library()
        with mock.patch('subprocess.run', side_effect=self.probe):
            result = lib.select([self.scene()], selection_mode='round_robin', selection_id='new-job')
        self.assertEqual(result['materials'][0]['sha256'], good)
        with mock.patch('subprocess.run', side_effect=AssertionError('replay probe')):
            self.assertEqual(result, lib.select([self.scene()], selection_mode='round_robin', selection_id='new-job'))

    def test_unknown_color_contract_is_rejected(self):
        self.add('good'); lib = self.library()
        for version in [True, '1', 2, None]:
            with self.subTest(version=version), self.assertRaises(ValueError):
                lib.select([{**self.scene(), 'video_color_contract': version}])

    def test_changed_source_does_not_reuse_color_cache(self):
        self.add('good'); lib = self.library()
        with mock.patch('subprocess.run', side_effect=self.probe) as probe:
            lib.select([self.scene()])
            (self.root / 'good.mp4').write_bytes(b'changed')
            with self.assertRaises(ml.MaterialShortageError):lib.select([self.scene()])
            self.assertEqual(probe.call_count, 1)

    def test_http_guard_preserves_exclusions_and_returns_shortage(self):
        self.add('bad'); good = self.add('good'); self.library()
        server = material_library_api.build_server('127.0.0.1', 0, self.root, 'test-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            body = {'scenes': [self.scene()], 'selection_mode': 'semantic'}
            def call():
                request = urllib.request.Request(
                    f'http://127.0.0.1:{server.server_port}/v1/select',
                    data=json.dumps(body).encode(),
                    headers={'Authorization': 'Bearer test-token', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=3) as response:return json.load(response)
            with mock.patch('subprocess.run', side_effect=self.probe):
                self.assertEqual(call()['materials'][0]['sha256'], good)
                body['used_sha256'] = [good]
                with self.assertRaises(urllib.error.HTTPError) as cm:call()
                self.assertEqual(cm.exception.code, 409)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_probe_budget_and_file_change_fail_closed(self):
        sha = self.add('good'); lib = self.library(); lib.refresh()
        material, path = lib.resolve(sha); self.assertTrue(lib._is_available(material))
        with mock.patch('subprocess.run') as probe:
            with self.assertRaisesRegex(ml.MaterialLibraryError, 'budget'):
                lib._video_color_compatible(material, [0, time.monotonic() + 5])
            probe.assert_not_called()
        def change(*args, **kwargs):
            path.write_bytes(b'changed-during-probe')
            return self.probe(*args, **kwargs)
        with mock.patch('subprocess.run', side_effect=change):
            with self.assertRaisesRegex(ml.MaterialLibraryError, 'changed'):
                lib._video_color_compatible(material, [1, time.monotonic() + 5])
        self.assertNotIn(sha, lib._video_color_cache)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_real_eight_bit_hlg_file_is_skipped_without_changing_its_bytes(self):
        for name, transfer in [('bad', 'arib-std-b67'), ('good', 'bt709')]:
            path = self.root / (name + '.mp4')
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=blue:s=64x64:r=1:d=1', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', '-color_trc', transfer,
                            '-color_primaries', 'bt2020' if name == 'bad' else 'bt709',
                            '-colorspace', 'bt2020nc' if name == 'bad' else 'bt709',
                            '-bsf:v', ('h264_metadata=colour_primaries=9:transfer_characteristics=18:matrix_coefficients=9'
                                       if name == 'bad' else 'h264_metadata=colour_primaries=1:transfer_characteristics=1:matrix_coefficients=1'),
                            str(path)], check=True, capture_output=True, timeout=20)
            self.rows.append({'record_id': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                              '状态': '可使用', '素材名称': name, 'server_relative_path': path.name})
        before = (self.root / 'bad.mp4').read_bytes()
        result = self.library().select([self.scene()])
        self.assertEqual(result['materials'][0]['sha256'], self.rows[1]['sha256'])
        self.assertEqual((self.root / 'bad.mp4').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
