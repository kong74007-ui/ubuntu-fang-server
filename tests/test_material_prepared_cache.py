import hashlib
import json
import os
import tempfile
import unittest
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path
from unittest import mock

from server import material_library as ml
from server import material_library_api
from scripts.prepare_material_library_cache import prepare


class PreparedCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'media';self.root.mkdir();self.asset=self.root/'clip.mp4';self.asset.write_bytes(b'approved-video')
        self.sha=hashlib.sha256(self.asset.read_bytes()).hexdigest()
        (self.root/'index.jsonl').write_text(json.dumps({'record_id':'v','sha256':self.sha,'状态':'可使用','server_relative_path':'clip.mp4'}),encoding='utf-8')
        self.cache=Path(self.tmp.name)/'prepared.json'

    def prepare(self):
        library=ml.MaterialLibrary(self.root);library.refresh();m,_=library.resolve(self.sha)
        self.assertTrue(library._is_available(m))
        with mock.patch.object(ml.subprocess,'run',return_value=mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'bt709'}]}))):
            library._video_color_compatible(m,[1,__import__('time').monotonic()+5])
        library.write_prepared_cache(self.cache)

    def test_restart_uses_prepared_checks_without_hash_or_ffprobe(self):
        self.prepare()
        library=ml.MaterialLibrary(self.root,prepared_cache_path=self.cache)
        with mock.patch.object(ml,'sha256_file',side_effect=AssertionError('rehashed unchanged bytes')),mock.patch.object(ml.subprocess,'run',side_effect=AssertionError('reprobed unchanged bytes')):
            result=library.select([{'scene_id':'s','media_type':'video','video_color_contract':1}])
        self.assertEqual(result['materials'][0]['sha256'],self.sha)

    def test_changed_file_cannot_reuse_prepared_validation(self):
        self.prepare();self.asset.write_bytes(b'changed')
        library=ml.MaterialLibrary(self.root,prepared_cache_path=self.cache)
        with self.assertRaises(ml.MaterialShortageError):
            library.select([{'scene_id':'s','media_type':'video','video_color_contract':1}])

    def test_wrong_root_or_malformed_cache_is_rejected(self):
        self.prepare();original=json.loads(self.cache.read_text())
        for mutation in [lambda d:d.update(root_sha256='0'*64),lambda d:d.update(extra=True),lambda d:d['entries'][self.sha].update(available='true')]:
            data=json.loads(json.dumps(original));mutation(data);self.cache.write_text(json.dumps(data))
            with self.assertRaises(ml.MaterialLibraryError):ml.MaterialLibrary(self.root,prepared_cache_path=self.cache).stats()

    def test_preparation_does_not_touch_usage_or_assets_and_warms_restart(self):
        before=self.asset.read_bytes()
        with mock.patch('subprocess.run',return_value=mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'bt709'}]}))):
            report=prepare(self.root,self.cache)
        self.assertEqual(report['compatible'],1)
        self.assertFalse((self.root/'usage.json').exists())
        self.assertEqual(self.asset.read_bytes(),before)
        self.assertEqual(ml.MaterialLibrary(self.root,prepared_cache_path=self.cache).stats()['prepared_cache_entries'],1)

    def test_cached_bad_hdr_stays_excluded(self):
        with mock.patch('subprocess.run',return_value=mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'arib-std-b67','color_primaries':'bt2020','color_space':'bt2020nc'}]}))):
            report=prepare(self.root,self.cache)
        self.assertEqual(report['incompatible'],1)
        with mock.patch('subprocess.run',side_effect=AssertionError('unneeded reprobe')):
            with self.assertRaises(ml.MaterialShortageError):
                ml.MaterialLibrary(self.root,prepared_cache_path=self.cache).select([{'scene_id':'s','media_type':'video','video_color_contract':1}])

    def test_forged_hint_cannot_bypass_download_checksum(self):
        self.prepare();self.asset.write_bytes(b'corrupted-file')
        data=json.loads(self.cache.read_text());s=self.asset.stat()
        data['entries'][self.sha]['identity']=[s.st_dev,s.st_ino,s.st_mtime_ns,s.st_size]
        self.cache.write_text(json.dumps(data))
        server=material_library_api.build_server('127.0.0.1',0,self.root,'test',prepared_cache_path=self.cache)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/assets/'+self.sha,headers={'Authorization':'Bearer test'})
            with self.assertRaises(urllib.error.HTTPError) as cm:urllib.request.urlopen(request,timeout=2)
            self.assertEqual(cm.exception.code,409)
        finally:server.shutdown();server.server_close();thread.join(timeout=2)

    def test_cache_cannot_overwrite_materials_or_index(self):
        library=ml.MaterialLibrary(self.root)
        with self.assertRaises(ml.MaterialLibraryError):library.write_prepared_cache(self.root/'index.jsonl')

    def test_api_main_passes_configured_prepared_cache(self):
        self.prepare()
        with mock.patch.dict(os.environ,{'MATERIAL_LIBRARY_API_TOKEN':'test','MATERIAL_LIBRARY_ROOT':str(self.root),'MATERIAL_LIBRARY_USAGE_PATH':str(Path(self.tmp.name)/'usage.json'),'MATERIAL_LIBRARY_PREPARED_CACHE':str(self.cache)}),mock.patch('sys.argv',['material_library_api.py']),mock.patch.object(material_library_api,'build_server') as build:
            material_library_api.main()
        self.assertEqual(build.call_args.kwargs['prepared_cache_path'],self.cache)


if __name__=='__main__':unittest.main()
