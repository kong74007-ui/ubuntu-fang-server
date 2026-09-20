import http.client
import json
import threading
import unittest
import urllib.error
from unittest import mock

from server import matrix_template_api as matrix


class ReadinessAdmissionTests(unittest.TestCase):
    def service(self):
        s=object.__new__(matrix.MatrixTemplateService)
        s.library_readiness_lock=threading.Lock();s._library_readiness_cache=None
        return s

    def healthy(self):
        return {'ok':True,'records':10,'selection_contract_version':matrix.MATERIAL_SELECTION_CONTRACT_VERSION,'clip_contract_version':matrix.MATERIAL_CLIP_CONTRACT_VERSION}

    def test_failed_probe_does_not_poison_following_admissions(self):
        s=self.service();s._library_request=mock.Mock(side_effect=[matrix.MatrixTemplateError('probe unavailable'),self.healthy()])
        with self.assertRaises(matrix.MatrixTemplateError):s.require_library_ready()
        self.assertTrue(s.require_library_ready()['ready'])
        self.assertEqual(s._library_request.call_count,2)

    def test_success_ttl_starts_after_probe_finishes(self):
        s=self.service();clock=[100.0]
        def probe(*a,**k):clock[0]+=3;return self.healthy()
        s._library_request=mock.Mock(side_effect=probe)
        with mock.patch.object(matrix.time,'monotonic',side_effect=lambda:clock[0]):
            self.assertTrue(s.library_readiness()['ready']);clock[0]=107
            self.assertTrue(s.library_readiness()['ready'])
        self.assertEqual(s._library_request.call_count,1)

    def test_contract_mismatch_is_not_retryable(self):
        s=self.service();s._library_request=mock.Mock(return_value={**self.healthy(),'clip_contract_version':0})
        with self.assertRaises(matrix.MatrixTemplateError) as raised:s.require_library_ready()
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.reason_code,'contract_invalid')

    def test_auth_failure_is_not_retryable(self):
        s=self.service()
        error=matrix.MatrixTemplateError('unavailable')
        error.__cause__=urllib.error.HTTPError('http://local.invalid',401,'auth',{},None)
        s._library_request=mock.Mock(side_effect=error)
        with self.assertRaises(matrix.MaterialLibraryUnavailableError) as raised:s.require_library_ready()
        self.assertEqual(raised.exception.reason_code,'auth_failed');self.assertFalse(raised.exception.retryable)

    def test_concurrent_recovery_does_not_fan_out_rejections(self):
        s=self.service();s._library_request=mock.Mock(side_effect=[matrix.MatrixTemplateError('unavailable'),self.healthy()])
        barrier=threading.Barrier(5);out=[]
        def check():
            barrier.wait()
            try:out.append(s.require_library_ready()['ready'])
            except matrix.MatrixTemplateError:out.append(False)
        threads=[threading.Thread(target=check) for _ in range(5)]
        for t in threads:t.start()
        for t in threads:t.join(2)
        self.assertEqual(out.count(True),4);self.assertEqual(out.count(False),1)
        self.assertEqual(s._library_request.call_count,2)

    def test_http_admission_exposes_safe_transient_contract(self):
        s=self.service();s._library_request=mock.Mock(side_effect=matrix.MatrixTemplateError('probe unavailable'))
        s.submit=lambda body,key:s.require_library_ready()
        server=matrix.build_server('127.0.0.1',0,s,'test-token')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            connection.request('POST','/v1/jobs',body=b'{}',headers={'Authorization':'Bearer test-token','Content-Type':'application/json','X-Request-Id':'probe-test'})
            response=connection.getresponse();payload=json.loads(response.read());connection.close()
            self.assertEqual(response.status,503)
            self.assertEqual(payload['error'],'material_library_unavailable')
            self.assertTrue(payload['retryable'])
            self.assertEqual(payload['reason_code'],'probe_failed')
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
