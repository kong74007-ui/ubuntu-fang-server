import concurrent.futures
import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from server import material_library as ml
from server.matrix_template_api import MatrixTemplateService, MatrixTemplateError


class SelectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.rows = []
        for i in range(24):
            name = f'{i}.mp4'; data = name.encode(); (self.root/name).write_bytes(data)
            self.rows.append({'record_id': name, 'sha256': hashlib.sha256(data).hexdigest(),
                              '状态': '可使用', '素材名称': 'material '+str(i),
                              'server_relative_path': name, '时长秒': 30})
        (self.root/'index.jsonl').write_text('\n'.join(json.dumps(r) for r in self.rows),encoding='utf-8')
        self.lib = ml.MaterialLibrary(self.root,usage_path=self.root/'usage.json')
        self.scenes = [{'scene_id': 's', 'media_type': 'video', 'video_color_contract': 1}]

    def test_slow_probe_does_not_hold_usage_commit_lock(self):
        entered=threading.Event();release=threading.Event()
        def probe(*args,**kwargs):
            entered.set();release.wait(3)
            return mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'bt709'}]}))
        with mock.patch.object(ml.subprocess,'run',side_effect=probe), concurrent.futures.ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.lib.select,self.scenes,selection_mode='round_robin',selection_id='slow')
            try:
                self.assertTrue(entered.wait(2))
                acquired=self.lib._usage_lock.acquire(timeout=.2)
                if acquired:self.lib._usage_lock.release()
                self.assertTrue(acquired,'FFprobe currently holds the global commit lock')
            finally:release.set()
            future.result(timeout=5)

    def test_round_robin_does_not_score_semantics_or_expand_same_slots_per_scene(self):
        scenes=[{'scene_id':str(i),'media_type':'video','clip_duration_seconds':5,'query':'material'} for i in range(7)]
        with mock.patch.object(ml,'_score',wraps=ml._score) as score, mock.patch.object(ml,'_material_candidates',wraps=ml._material_candidates) as candidates:
            result=self.lib.select(scenes,selection_mode='round_robin',selection_id='seven')
        self.assertEqual(score.call_count,0)
        self.assertTrue(all(x['match_score']==0 for x in result['materials']))
        self.assertLessEqual(candidates.call_count,len(self.rows)*3)

    def test_timeout_replays_same_selection_request_once(self):
        service=object.__new__(MatrixTemplateService);service.library_url='http://127.0.0.1:1';service.library_token='test'
        response=mock.MagicMock();response.__enter__.return_value.read.return_value=b'{"materials":[]}'
        body={'selection_mode':'round_robin','selection_id':'matrix-template:stable','scenes':[]}
        with mock.patch('urllib.request.urlopen',side_effect=[TimeoutError('slow'),response]) as call:
            self.assertEqual(service._library_request('POST','/v1/select',body),{'materials':[]})
        self.assertEqual(call.call_count,2)
        self.assertEqual(call.call_args_list[0].args[0].data,call.call_args_list[1].args[0].data)

    def test_twenty_concurrent_requests_keep_atomic_distinct_usage_and_bounded_probes(self):
        active=0;peak=0;mutex=threading.Lock()
        def probe(*args,**kwargs):
            nonlocal active,peak
            with mutex:active+=1;peak=max(peak,active)
            time.sleep(.015)
            with mutex:active-=1
            return mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'bt709'}]}))
        with mock.patch.object(ml.subprocess,'run',side_effect=probe), concurrent.futures.ThreadPoolExecutor(20) as pool:
            outputs=list(pool.map(lambda i:self.lib.select(self.scenes,selection_mode='round_robin',selection_id=f'concurrent-{i}',seed=str(i)),range(20)))
        self.assertGreater(peak,1);self.assertLessEqual(peak,4)
        self.assertEqual(len({x['materials'][0]['sha256'] for x in outputs}),20)
        usage=json.loads((self.root/'usage.json').read_text())
        self.assertEqual(sum(v['count'] for v in usage.values()),20)
        self.assertEqual(len(self.lib._selection_receipts),20)

    def test_concurrent_same_key_has_one_receipt_and_one_charge_to_usage(self):
        def probe(*args,**kwargs):
            time.sleep(.02)
            return mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'bt709'}]}))
        with mock.patch.object(ml.subprocess,'run',side_effect=probe), concurrent.futures.ThreadPoolExecutor(12) as pool:
            outputs=list(pool.map(lambda _:self.lib.select(self.scenes,selection_mode='round_robin',selection_id='same',seed='same'),range(12)))
        self.assertTrue(all(x==outputs[0] for x in outputs))
        self.assertEqual(len(self.lib._selection_receipts),1)
        self.assertEqual(sum(x['count'] for x in self.lib._usage.values()),1)

    def test_deferred_failure_does_not_persist_tentative_reservations(self):
        with mock.patch.object(ml.subprocess,'run',side_effect=OSError('probe absent')):
            with self.assertRaises(ml.MaterialLibraryError):
                self.lib.select(self.scenes,selection_mode='round_robin',selection_id='failed')
        self.assertEqual(self.lib._usage,{})
        self.assertEqual(self.lib._selection_receipts,{})
        self.assertFalse((self.root/'usage.json').exists())

    def test_replay_and_conflict_do_not_wait_for_unrelated_slow_probe(self):
        scene=[{'scene_id':'s','media_type':'video'}]
        previous=self.lib.select(scene,selection_mode='round_robin',selection_id='existing')
        entered=threading.Event();release=threading.Event()
        def probe(*args,**kwargs):
            entered.set();release.wait(3)
            return mock.Mock(stdout=json.dumps({'streams':[{'pix_fmt':'yuv420p','color_transfer':'bt709'}]}))
        with mock.patch.object(ml.subprocess,'run',side_effect=probe),concurrent.futures.ThreadPoolExecutor(2) as pool:
            slow=pool.submit(self.lib.select,self.scenes,selection_mode='round_robin',selection_id='slow')
            try:
                self.assertTrue(entered.wait(2))
                replay=pool.submit(self.lib.select,scene,selection_mode='round_robin',selection_id='existing')
                self.assertEqual(replay.result(timeout=.5),previous)
                with self.assertRaises(ml.MaterialSelectionConflictError):
                    self.lib.select(self.scenes,selection_mode='round_robin',selection_id='existing')
            finally:release.set()
            slow.result(timeout=5)

    def test_retry_is_bounded_and_not_used_for_unkeyed_or_definitive_errors(self):
        service=object.__new__(MatrixTemplateService);service.library_url='http://127.0.0.1:1';service.library_token='test'
        keyed={'selection_mode':'round_robin','selection_id':'matrix-template:stable'}
        cases=[('GET','/health',None,TimeoutError('slow'),1),
               ('POST','/v1/select',{},TimeoutError('slow'),1),
               ('POST','/v1/select',keyed,TimeoutError('slow'),2),
               ('POST','/v1/select',keyed,urllib.error.URLError(ConnectionRefusedError()),1),
               ('POST','/v1/select',keyed,urllib.error.HTTPError('http://local',409,'conflict',{},None),1)]
        for method,path,body,error,count in cases:
            with self.subTest(error=type(error).__name__,count=count),mock.patch('urllib.request.urlopen',side_effect=error) as call:
                with self.assertRaises(MatrixTemplateError):service._library_request(method,path,body)
                self.assertEqual(call.call_count,count)

    def test_real_http_lost_first_response_replays_durable_receipt_without_double_usage(self):
        library=self.lib;received=[];release=threading.Event();mutex=threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']))
                with mutex:
                    received.append(raw);first=len(received)==1
                body=json.loads(raw)
                result=library.select(body['scenes'],selection_mode='round_robin',selection_id=body['selection_id'])
                if first:release.wait(2)
                else:release.set()
                data=json.dumps(result).encode()
                try:
                    self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        service=object.__new__(MatrixTemplateService);service.library_url=f'http://127.0.0.1:{server.server_port}';service.library_token='test'
        try:
            result=service._library_request('POST','/v1/select',{
                'selection_mode':'round_robin','selection_id':'lost-http-response',
                'scenes':[{'scene_id':'s','media_type':'video'}]},timeout=.2)
            self.assertEqual(len(result['materials']),1)
            self.assertEqual(len(received),2);self.assertEqual(received[0],received[1])
            self.assertEqual(sum(x['count'] for x in json.loads((self.root/'usage.json').read_text()).values()),1)
            self.assertEqual(len(list(self.lib._receipt_dir.glob('*.json'))),1)
        finally:
            release.set();server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
