import hashlib
import json
import threading
import unittest
import urllib.request
from unittest import mock

from server import matrix_template_api as matrix
from tests import test_matrix_template_api as fixtures
from tests import test_matrix_public_material_scope as scope_fixtures


class OwnedReferenceAdmissionTests(unittest.TestCase):
    setUp = fixtures.ReferenceOverridesTests.setUp
    tearDown = fixtures.ReferenceOverridesTests.tearDown
    TOP = fixtures.ReferenceOverridesTests.TOP
    BOTTOM = fixtures.ReferenceOverridesTests.BOTTOM
    _width = staticmethod(fixtures.ReferenceOverridesTests._width)
    _semantic = fixtures.ReferenceOverridesTests._semantic
    _raw = fixtures.ReferenceOverridesTests._raw

    def materials(self, count):
        root = self.service.data_root / matrix.USER_ASSET_DIRNAME
        root.mkdir(exist_ok=True)
        result = []
        for i in range(count):
            value = ('owned-video-%d' % i).encode()
            sha = hashlib.sha256(value).hexdigest()
            (root / (sha + '.mp4')).write_bytes(value)
            result.append({'sha256': sha, 'media_type': 'video'})
        return result

    def test_http_preflight_submission_and_replay_agree_on_three_to_five(self):
        self.service.enforce_user_materials = True
        server = matrix.build_server('127.0.0.1', 0, self.service, 'test-token')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(self.service, '_inspect_user_asset', return_value=5.), mock.patch.object(
                self.service, '_library_request', side_effect=AssertionError('no shared visuals'),
            ):
                for count in (3, 4, 5):
                    with self.subTest(count=count):
                        raw = self._raw(materials=self.materials(count), policy='owned_public')
                        req = urllib.request.Request(
                            'http://127.0.0.1:%d/v1/preflight' % server.server_port,
                            data=json.dumps(raw).encode(), headers={'Authorization': 'Bearer test-token'},
                        )
                        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3) as reply:
                            preflight = json.load(reply)
                        self.assertEqual(count, preflight['required_visuals'])
                        job = self.service.submit(raw, 'count-%d' % count)
                        frozen = json.loads(self.service.store.get(job['job_id'])['payload'])
                        self.assertEqual(count, self.service.required_visuals(frozen))
                        self.assertEqual(count, len(self.service._user_materials(frozen)))
                        self.assertEqual(job['job_id'], self.service.submit(raw, 'count-%d' % count)['job_id'])
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_out_of_range_count_and_short_sources_fail_before_admission(self):
        self.service.enforce_user_materials = True
        with mock.patch.object(self.service, '_inspect_user_asset', return_value=5.):
            for count in (2, 6):
                with self.subTest(count=count), self.assertRaises(matrix.MatrixTemplateError):
                    self.service.submit(self._raw(materials=self.materials(count)), 'invalid-%d' % count)
        with mock.patch.object(self.service, '_inspect_user_asset', return_value=1.):
            with self.assertRaises(matrix.MatrixTemplateError):
                self.service.submit(self._raw(materials=self.materials(5)), 'short-inputs')
        self.assertEqual([], self.service.store.pending_ids())


class SharedBgmContractTests(unittest.TestCase):
    setUp = fixtures.UserMaterialsTests.setUp
    tearDown = fixtures.UserMaterialsTests.tearDown
    _store_user_asset = fixtures.UserMaterialsTests._store_user_asset
    sha = staticmethod(scope_fixtures.PublicMaterialScopeTests.sha)
    response = scope_fixtures.PublicMaterialScopeTests.response
    user_materials = scope_fixtures.PublicMaterialScopeTests.user_materials

    def test_real_library_bgm_without_provider_is_frozen_and_replayed(self):
        raw = {'top_text': 'BGM contract', 'bottom_text': 'Owned visuals', 'bgm': True,
               'material_policy': 'owned_public', 'duration': 10, 'user_materials': self.user_materials(4)}
        job = self.service.submit(raw, 'bgm-no-provider')
        payload = json.loads(self.service.store.get(job['job_id'])['payload'])
        def response(method, path, body):
            self.assertEqual(['bgm'], [x['media_type'] for x in body['scenes']])
            value = self.response(body)
            # MaterialCandidate.public_dict omits provider in the actual library API.
            for item in value['materials']: item.pop('provider')
            return value
        with mock.patch.object(self.service, '_library_request', side_effect=response) as call:
            selected = self.service._select_materials(payload, job['job_id'])
            self.assertEqual(['user'] * 4 + ['huangque'], [x['provider'] for x in selected])
            self.assertEqual(selected, self.service._select_materials(payload, job['job_id']))
            self.assertEqual(1, call.call_count)

    def test_bgm_normalization_cannot_admit_video_or_foreign_provider(self):
        raw = {'template_id': 'full-overlay-bold', 'top_text': 'Test', 'bottom_text': 'Test',
               'bgm': True, 'duration': 10, 'user_materials': self.user_materials(4)}
        own = self.service._user_materials(raw)
        for extra in ({'media_type': 'video'}, {'provider': 'external'}, {'provider': 'user'}):
            item = dict(scene_id='bgm', sha256='f' * 64, media_type='bgm', **({'provider': extra['provider']} if 'provider' in extra else {}))
            item.update(extra)
            with self.subTest(extra=extra), self.assertRaises(matrix.MatrixTemplateError):
                self.service._validate_material_selection(raw, own + [item], 2)


if __name__ == '__main__': unittest.main()
