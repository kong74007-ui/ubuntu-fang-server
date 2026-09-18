import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from server.matrix_template_api import MatrixTemplateService, MatrixTemplateError


class HdrColorTests(unittest.TestCase):
    def test_persistent_text_remains_above_hdr_media_without_style_changes(self):
        original = '<h1 id="title" data-var-text="title" style="color:red">Title</h1>'
        patched = MatrixTemplateService._hdr_text_layers(original, 12.5)
        self.assertIn('style="color:red"', patched)
        self.assertIn('data-start="0" data-duration="12.500000000"', patched)
        self.assertIn('>Title</h1>', patched)
        self.assertEqual(patched, MatrixTemplateService._hdr_text_layers(patched, 12.5))

    def test_hlg_and_pq_are_not_relabelled_bt709_or_sent_to_h264_nvenc(self):
        for transfer in ('arib-std-b67', 'smpte2084'):
            color = dict(dynamic_range='hdr', transfer=transfer,
                         primaries='bt2020', matrix='bt2020nc', range='tv')
            pixel, args = MatrixTemplateService._clip_color_encoding(color, 'h264_nvenc')
            self.assertEqual(pixel, 'yuv420p10le')
            self.assertEqual(args[args.index('-c:v') + 1], 'libx265')
            self.assertEqual(args[args.index('-color_trc') + 1], transfer)
            self.assertNotIn('bt709', args)

    def test_output_rejects_hdr_tag_on_eight_bit_h264(self):
        service = object.__new__(MatrixTemplateService)
        video = dict(codec_type='video', codec_name='h264', pix_fmt='yuv420p',
                     width=1080, height=1920, color_transfer='arib-std-b67',
                     color_primaries='bt2020', color_space='bt2020nc')
        data = {'streams': [video, {'codec_type': 'audio', 'codec_name': 'aac'}],
                'format': {'duration': '12'}}
        with mock.patch('server.matrix_template_api.subprocess.run') as run:
            run.return_value.stdout = json.dumps(data)
            with self.assertRaises(MatrixTemplateError):
                service._probe(Path('test.mp4'))
            video.update(codec_name='hevc', pix_fmt='yuv420p10le')
            run.return_value.stdout = json.dumps(data)
            self.assertEqual(service._probe(Path('test.mp4'))['color_profile']['dynamic_range'], 'hdr')

    def test_invalid_probe_does_not_silently_treat_hdr_as_sdr(self):
        with mock.patch('server.matrix_template_api.subprocess.run') as run:
            run.return_value.stdout = '{"streams":[]}'
            with self.assertRaises(MatrixTemplateError):
                MatrixTemplateService._source_color(Path('test.mov'))

    def test_hdr_fan_stills_keep_high_precision_and_native_image_layers(self):
        service = object.__new__(MatrixTemplateService)
        color = dict(dynamic_range='hdr', transfer='arib-std-b67',
                     primaries='bt2020', matrix='bt2020nc', range='tv')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source.mp4'
            source.write_bytes(b'video')
            styles = ''.join("background-image:url('still-%d.jpg');" % i for i in range(3))
            body = '<div class="fan-band"></div>' * 18
            index = root / 'index.html'
            index.write_text('<style>' + styles + '</style>' + body, encoding='utf-8')
            config = {'duration': 12.5, 'still_frames': [
                ('source.mp4', 'still-%d.jpg' % i, 0.5) for i in range(3)
            ]}
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                Path(command[-1]).write_bytes(b'png' * 500)
                return 0, b'', b''

            with mock.patch.object(service, '_source_color', return_value=color), \
                 mock.patch.object(service, '_run_tracked_process', side_effect=run):
                service._prepare_fixed_skill_stills(root, config, deadline_at=time.time() + 30)
            rendered = index.read_text(encoding='utf-8')
            self.assertEqual(rendered.count('<img '), 18)
            self.assertEqual(rendered.count('background-image:none'), 3)
            self.assertEqual(rendered.count('object-position:center 45%'), 18)
            self.assertNotIn(".jpg'", rendered)
            for command in calls:
                self.assertIn('rgb48be', command)
                self.assertIn('arib-std-b67', command)
                self.assertTrue(command[-1].endswith('.png'))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'requires FFmpeg')
    def test_real_hdr_fixed_and_nine_grid_clips_preserve_ten_bits_and_transfer(self):
        encoders = subprocess.check_output(['ffmpeg', '-hide_banner', '-encoders'], text=True)
        if 'libx265' not in encoders:
            self.skipTest('requires libx265')
        service = object.__new__(MatrixTemplateService)
        service.stop_event = threading.Event()
        service.process_lock = threading.Lock()
        service.active_processes = set()
        service.active_process = None
        service.nine_grid_prep_encoder = 'h264_nvenc'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for transfer in ('arib-std-b67', 'smpte2084'):
                source = root / (transfer + '.mp4')
                subprocess.run([
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30:d=5',
                    '-c:v', 'libx265', '-preset', 'ultrafast',
                    '-x265-params', 'pools=1:frame-threads=1:log-level=error:colorprim=9:colormatrix=9:transfer='
                    + ('18' if transfer == 'arib-std-b67' else '16'),
                    '-pix_fmt', 'yuv420p10le', '-color_primaries', 'bt2020',
                    '-color_trc', transfer, '-colorspace', 'bt2020nc',
                    '-color_range', 'tv', str(source),
                ], check=True, capture_output=True, timeout=30)
                for method in ('fixed', 'nine'):
                    dest = root / (transfer + '-' + method + '.mp4')
                    if method == 'fixed':
                        service._prepare_fixed_skill_clip(source, dest, 0.5, 90, 640,
                                                         deadline_at=time.time() + 120)
                    else:
                        service._prepare_nine_grid_clip(source, dest, 0.5,
                                                       deadline_at=time.time() + 120)
                    actual = json.loads(subprocess.check_output([
                        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_streams', '-of', 'json', str(dest)
                    ], text=True))['streams'][0]
                    self.assertEqual(actual['codec_name'], 'hevc')
                    self.assertEqual(actual['pix_fmt'], 'yuv420p10le')
                    self.assertEqual(actual['color_transfer'], transfer)
                    self.assertEqual(actual['color_primaries'], 'bt2020')
                    self.assertFalse(list(root.glob('*.part.mp4')))


if __name__ == '__main__':
    unittest.main()
