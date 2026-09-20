from contextlib import closing
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


sync = load('hy_material_sync')
export = load('hy_material_export')


class SyncContracts(unittest.TestCase):
    def test_relative_paths_reject_escapes(self):
        for path in ['../a', '/tmp/a', 'C:/a', 'a\\b', 'a\nb', '-option', 'a/../b', './a']:
            with self.subTest(path=path), self.assertRaises(ValueError): sync.relative(path)
        self.assertEqual(sync.relative('files/example.mp4'), 'files/example.mp4')

    def test_index_conflicting_hash_and_path_fail(self):
        row={'SHA256':'a'*64,'server_relative_path':'files/a.mp4'}
        self.assertEqual(len(sync.rows_from_bytes(json.dumps(row).encode())),1)
        for rows in [[dict(row,sha256='b'*64)], [row,dict(row,SHA256='b'*64)]]:
            with self.assertRaises(ValueError):sync.rows_from_bytes('\n'.join(json.dumps(r) for r in rows).encode())

    def test_private_schema_never_accepts_url_or_unscoped_owner(self):
        row={'owner_hash':'b'*64,'sha256':'a'*64,'suffix':'.mp4','bytes':4,'source_mtime_ns':1,'expires_at':100}
        valid={'version':1,'records':[row]}
        self.assertEqual(sync.validate_private(valid),[row])
        for bad in [dict(row,url='https://invalid.example'),dict(row,owner_hash='../owner'),dict(row,bytes=True),dict(row,suffix='.env')]:
            with self.assertRaises(ValueError):sync.validate_private({'version':1,'records':[bad]})
        with self.assertRaises(ValueError):sync.validate_private({'version':1,'records':[row,row]})

    def test_windows_to_wsl_path(self):
        self.assertEqual(sync.wsl_path('D:/folder/name.json'),'/mnt/d/folder/name.json')
        with self.assertRaises(ValueError):sync.wsl_path('//server/share/a')

    def test_atomic_write_and_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json';sync.atomic_json(path,{'ok':True})
            self.assertEqual(json.loads(path.read_text()),{'ok':True})
            lock=Path(folder)/'lock'
            with sync.exclusive(lock):
                with self.assertRaises(RuntimeError):
                    with sync.exclusive(lock):pass

    def test_private_copy_verifies_bytes_and_does_not_touch_public_index(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'shared').mkdir();(base/'private').mkdir()
            data=b'media';sha=hashlib.sha256(data).hexdigest()
            row={'owner_hash':'b'*64,'sha256':sha,'suffix':'.mp4','bytes':len(data),'source_mtime_ns':1,'expires_at':2000000000}
            manifest={'version':1,'records':[row]}
            runner=sync.Sync({'base':str(base),'shared_root':str(base/'shared'),'private_root':str(base/'private')})
            def fake(operation,dest=None):
                if operation=='manifest':return copy.deepcopy(manifest)
                self.assertEqual(operation,'blob '+sha);Path(dest).write_bytes(data)
            runner.export=fake
            self.assertEqual(runner.users()['downloaded'],1)
            self.assertEqual(runner.users()['state'],'unchanged')
            self.assertFalse((base/'shared/index.jsonl').exists())
            self.assertEqual((base/'private/owners'/('b'*64)/(sha+'.mp4')).read_bytes(),data)

    def test_private_source_change_never_publishes_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);(base/'shared').mkdir();(base/'private').mkdir()
            runner=sync.Sync({'base':str(base),'shared_root':str(base/'shared'),'private_root':str(base/'private')})
            runner.export=mock.Mock(side_effect=[{'version':1,'records':[]},{'version':1,'records':[{}]}])
            with self.assertRaises(ValueError):runner.users()
            self.assertFalse((base/'private/current.json').exists())

    def test_shared_preserves_repair_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            b=Path(folder);root=b/'shared';root.mkdir();(b/'private').mkdir()
            media=b'fixed';sha=hashlib.sha256(media).hexdigest();(root/'fixed.mp4').write_bytes(media)
            original={'SHA256':'a'*64,'server_relative_path':'original.mp4'}
            fixed={'SHA256':sha,'server_relative_path':'fixed.mp4'}
            (root/'index.jsonl').write_text(json.dumps(fixed)+'\n')
            (b/'repairs.json').write_text(json.dumps({'a'*64:fixed}))
            runner=sync.Sync({'base':str(b),'shared_root':str(root),'private_root':str(b/'private'),'prepared_cache':str(b/'cache'),'repair_map':str(b/'repairs.json')})
            runner.rsync=lambda source,dest,file_list=None:Path(dest).write_text(json.dumps(original)+'\n')
            runner.export=mock.Mock(side_effect=AssertionError('no queue call for unchanged'))
            self.assertEqual(runner.shared()['state'],'unchanged')
            (root/'fixed.mp4').write_bytes(b'corrupt')
            with self.assertRaises(ValueError):runner.shared()

    def shared_fixture(self, base, idle=True):
        root=base/'shared';root.mkdir();(base/'private').mkdir()
        old=b'old-image';new=b'new-image'
        old_sha=hashlib.sha256(old).hexdigest();new_sha=hashlib.sha256(new).hexdigest()
        (root/'old.jpg').write_bytes(old)
        rows=[{'SHA256':old_sha,'server_relative_path':'old.jpg','状态':'可使用'},
              {'SHA256':new_sha,'server_relative_path':'files/new.jpg','状态':'可使用'}]
        (root/'index.jsonl').write_text(json.dumps(rows[0])+'\n')
        (base/'repairs.json').write_text('{}')
        cache={'version':1,'policy':'matrix-hdr-v1','root_sha256':hashlib.sha256(str(root.resolve()).encode()).hexdigest(),'entries':{}}
        (base/'cache.json').write_text(json.dumps(cache))
        runner=sync.Sync({'base':str(base),'shared_root':str(root),'private_root':str(base/'private'),'prepared_cache':str(base/'cache.json'),'repair_map':str(base/'repairs.json'),'library_code':str(ROOT/'server')})
        def rsync(source,dest,file_list=None):
            if file_list:
                p=Path(dest)/'files/new.jpg';p.parent.mkdir();p.write_bytes(new)
            else:Path(dest).write_text('\n'.join(json.dumps(r) for r in rows)+'\n')
        runner.rsync=rsync;runner.export=lambda op:{'idle':idle}
        return runner

    def test_shared_busy_does_not_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            b=Path(folder);runner=self.shared_fixture(b,idle=False)
            before=(b/'shared/index.jsonl').read_bytes()
            self.assertEqual(runner.shared()['state'],'deferred_busy')
            self.assertEqual((b/'shared/index.jsonl').read_bytes(),before)

    def test_shared_publish_and_health_rollback(self):
        for good in (True,False):
            with self.subTest(good=good),tempfile.TemporaryDirectory() as folder:
                b=Path(folder);runner=self.shared_fixture(b)
                before=(b/'shared/index.jsonl').read_bytes();cache=(b/'cache.json').read_bytes()
                response=io.BytesIO(json.dumps({'ok':good,'records':2}).encode())
                with mock.patch.object(sync.urllib.request,'urlopen',return_value=response):
                    if good:self.assertEqual(runner.shared()['state'],'updated')
                    else:
                        with self.assertRaises(RuntimeError):runner.shared()
                if good:self.assertEqual(len(sync.rows_from_bytes((b/'shared/index.jsonl').read_bytes())),2)
                else:
                    self.assertEqual((b/'shared/index.jsonl').read_bytes(),before)
                    self.assertEqual((b/'cache.json').read_bytes(),cache)


class ExportContracts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.assets=self.root/'assets';self.assets.mkdir();self.db=self.root/'jobs.db'
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('CREATE TABLE jobs(username TEXT,payload TEXT,kind TEXT,deleted INTEGER,status TEXT)')
            c.commit()
        self.config={'asset_root':str(self.assets),'jobs_db':str(self.db),'relay_db':str(self.db),'retention_seconds':259200}

    def tearDown(self):self.tmp.cleanup()

    def add(self,data,username='example-owner',deleted=0,mtime=None):
        sha=hashlib.sha256(data).hexdigest();p=self.assets/(sha+'.mp4');p.write_bytes(data)
        if mtime:os.utime(p,(mtime,mtime))
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',(username,json.dumps({'user_materials':[{'sha256':sha}]}),'matrix_template_video',deleted,'done'));c.commit()
        return sha

    def test_expired_deleted_and_unowned_files_not_exported(self):
        good=self.add(b'good');self.add(b'deleted',deleted=1);self.add(b'expired',mtime=100)
        (self.assets/('c'*64+'.mp4')).write_bytes(b'unowned')
        rows=export.inventory(self.config)['records']
        self.assertEqual([r['sha256'] for r in rows],[good]);self.assertNotIn('username',rows[0])

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'Linux source uses nofollow')
    def test_blob_rechecks_ownership_and_hash(self):
        sha=self.add(b'good');out=io.BytesIO();export.blob(self.config,sha,out);self.assertEqual(out.getvalue(),b'good')
        (self.assets/(sha+'.mp4')).write_bytes(b'bad!')
        with self.assertRaises(ValueError):export.blob(self.config,sha,io.BytesIO())

    def test_forced_command_refuses_shell_and_path(self):
        for cmd in ['','sh','blob ../../etc/passwd','manifest; id','idle extra']:
            with self.assertRaises(ValueError):export.dispatch(self.config,cmd,io.BytesIO())

    def test_idle_refuses_running_jobs(self):
        self.add(b'a')
        self.assertTrue(export.idle(self.config)['idle'])
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("UPDATE jobs SET status='running'");c.commit()
        self.assertFalse(export.idle(self.config)['idle'])


if __name__=='__main__':unittest.main()
