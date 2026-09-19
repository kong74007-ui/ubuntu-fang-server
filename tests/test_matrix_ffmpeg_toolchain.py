import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

RECIPE = Path(__file__).resolve().parents[1] / 'deploy/matrix-gpu/ffmpeg-nvenc12'
spec = importlib.util.spec_from_file_location('verify_sources', RECIPE / 'verify_sources.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ToolchainSourcesTests(unittest.TestCase):
    def test_exact_source_hash_and_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.tar.gz'
            path.write_bytes(b'fixture')
            manifest = {'sources': [{'archive': path.name, 'sha256': hashlib.sha256(b'fixture').hexdigest()}]}
            self.assertEqual(module.verify(tmp, manifest), 1)
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'source_digest_mismatch'):
                module.verify(tmp, manifest)

    def test_path_missing_duplicate_and_digest_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = {'archive': 'fixture', 'sha256': hashlib.sha256(b'fixture').hexdigest()}
            (Path(tmp) / 'fixture').write_bytes(b'fixture')
            for entries in [[], [dict(entry, archive='../outside')], [dict(entry, archive='missing')], [dict(entry, sha256='bad')], [entry, entry]]:
                with self.subTest(entries=entries), self.assertRaises(ValueError):
                    module.verify(tmp, {'sources': entries})

    def test_recipe_pins_supported_headers_and_current_ffmpeg(self):
        manifest = json.loads((RECIPE / 'sources.json').read_text())
        by_name = {x['name']: x for x in manifest['sources']}
        self.assertEqual(by_name['ffmpeg']['version'], '8.1.2')
        self.assertEqual(by_name['nv-codec-headers']['version'], '12.2.72.0')
        self.assertEqual(len(by_name), 5)
        for source in by_name.values():
            self.assertRegex(source['sha256'], r'^[a-f0-9]{64}$')
            self.assertRegex(source['commit'], r'^[a-f0-9]{40}$')
            self.assertTrue(source['url'].startswith('https://'))
        script = (RECIPE / 'build.sh').read_text()
        self.assertIn('--enable-libzimg', script)
        self.assertIn('--enable-nvenc', script)
        self.assertIn('--disable-network', script)
        self.assertNotIn('apt-get', script)
        self.assertNotIn('systemctl', script)


if __name__ == '__main__':
    unittest.main()
