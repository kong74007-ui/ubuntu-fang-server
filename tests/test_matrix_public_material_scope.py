"""Regressions for the public-material restriction recovered from the live worker."""
import hashlib
import json
import unittest
from unittest import mock

from server import matrix_template_api as matrix
from tests import test_matrix_template_api as fixtures


class PublicMaterialScopeTests(unittest.TestCase):
    setUp = fixtures.UserMaterialsTests.setUp
    tearDown = fixtures.UserMaterialsTests.tearDown
    _store_user_asset = fixtures.UserMaterialsTests._store_user_asset

    def body(self, **extra):
        return dict(top_text="Public material test", bottom_text="View details",
                    bgm=False, material_scope="public_only", **extra)

    @staticmethod
    def sha(label):
        return hashlib.sha256(label.encode()).hexdigest()

    def whitelist(self, shas):
        self.service.public_materials_path = self.root / "public.json"
        self.service.public_materials_path.write_text(
            json.dumps({"version": 1, "materials": [{"sha256": s} for s in shas]}),
            encoding="utf-8",
        )

    def response(self, body, *, private=False):
        items = []
        for index, scene in enumerate(body["scenes"]):
            is_bgm = scene["scene_id"] == "bgm"
            sha = self.sha("private" if private and index == 0 else scene["scene_id"])
            items.append(dict(
                scene_id=scene["scene_id"], sha256=sha, record_id=scene["scene_id"],
                provider="huangque", media_type="bgm" if is_bgm else "video",
                clip_id=None if is_bgm else self.sha(sha + "clip"),
                clip_start_seconds=0.0,
                clip_duration_seconds=0.0 if is_bgm else scene["clip_duration_seconds"],
                clip_slot_index=1, clip_slot_count=1,
            ))
        return dict(materials=items, selection_contract_version=2, clip_contract_version=3)

    def test_scope_validation_and_persistence(self):
        job = self.service.submit(self.body(), "scope-persist")
        payload = json.loads(self.service.store.get(job["job_id"])["payload"])
        self.assertEqual("public_only", payload["material_scope"])
        for value in (True, [], "private", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "material_scope"):
                self.service.validate_payload(dict(self.body(), material_scope=value))

    def test_legacy_request_replay_preserves_original_payload(self):
        legacy = self.body()
        legacy.pop("material_scope")
        job = self.service.submit(legacy, "legacy-scope")
        replay = self.service.submit(self.body(), "legacy-scope")
        self.assertEqual(job["job_id"], replay["job_id"])
        stored = json.loads(self.service.store.get(job["job_id"])["payload"])
        self.assertNotIn("material_scope", stored)

    def test_missing_malformed_and_empty_whitelist_reject_before_library(self):
        self.service.public_materials_path = self.root / "public.json"
        for content in (None, "{", '{"materials":[]}', '{"materials":["invalid"]}'):
            with self.subTest(content=content):
                self.service.public_materials_path.unlink(missing_ok=True)
                if content is not None:
                    self.service.public_materials_path.write_text(content, encoding="utf-8")
                with mock.patch.object(self.service, "_library_request") as request:
                    with self.assertRaises(matrix.MatrixTemplateError):
                        self.service._select_used_sha256(self.body(), [])
                request.assert_not_called()

    def test_whitelist_reload_and_removal_invalidate_cache(self):
        self.whitelist([self.sha("first")])
        self.assertEqual({self.sha("first")}, self.service._load_public_materials())
        self.whitelist([self.sha("second"), self.sha("third")])
        self.assertEqual({self.sha("second"), self.sha("third")}, self.service._load_public_materials())
        self.service.public_materials_path.unlink()
        self.assertEqual(set(), self.service._load_public_materials())

    def test_private_exclusion_preserves_existing_dedup(self):
        public, private, used = [self.sha(s) for s in ("public", "private", "used")]
        self.whitelist([public])
        with mock.patch.object(self.service, "_library_snapshot", return_value={"sha256": [public, private]}):
            self.assertEqual(sorted([private, used]), self.service._select_used_sha256(self.body(), [used]))

    def test_unrestricted_requests_do_not_read_whitelist_or_snapshot(self):
        with mock.patch.object(self.service, "_load_public_materials", side_effect=AssertionError), \
             mock.patch.object(self.service, "_library_snapshot", side_effect=AssertionError):
            self.assertEqual([self.sha("used")], self.service._select_used_sha256({}, [self.sha("used")]))
            self.service._assert_restricted_selection({}, [{"sha256": self.sha("private")}])

    def test_snapshot_enumeration_excludes_seen_items_and_does_not_reserve_round_robin(self):
        calls = []
        def request(method, path, body, **kwargs):
            calls.append(body)
            if len(calls) == 1:
                return {"materials": [{"sha256": self.sha("one")}, {"sha256": self.sha("two")} ]}
            raise matrix.MatrixTemplateError("no unique approved material")
        with mock.patch.object(self.service, "_library_request", side_effect=request):
            self.assertEqual({self.sha("one"), self.sha("two")}, self.service._enumerate_library_sha())
        self.assertTrue(all(c["selection_mode"] == "semantic" and "selection_id" not in c for c in calls))
        self.assertTrue(all(set(c["used_sha256"]) == {self.sha("one"), self.sha("two")} for c in calls[1:]))
        self.assertEqual(1, len(calls[-1]["scenes"]))

    def test_snapshot_reuses_disk_cache_and_refreshes_changed_library(self):
        self.service._save_library_snapshot([self.sha("old")], 1)
        with mock.patch.object(self.service, "_library_health_records", return_value=1), \
             mock.patch.object(self.service, "_enumerate_library_sha", side_effect=AssertionError):
            self.assertEqual([self.sha("old")], self.service._library_snapshot()["sha256"])
        with mock.patch.object(self.service, "_library_health_records", return_value=2), \
             mock.patch.object(self.service, "_enumerate_library_sha", return_value={self.sha("new")}) as refresh:
            self.assertEqual([self.sha("new")], self.service._library_snapshot()["sha256"])
            refresh.assert_called_once()

    def test_actual_selection_and_frozen_replay_reject_private_material(self):
        body = self.body()
        payload = self.service._freeze_font_provenance("a" * 32, self.service.validate_payload(body))
        scenes, _, _ = self.service._material_scenes(payload)
        allowed = [self.sha(s["scene_id"]) for s in scenes]
        self.whitelist(allowed)
        snapshot = {"sha256": allowed + [self.sha("private")]}
        def request(method, path, body):
            self.assertIn(self.sha("private"), body["used_sha256"])
            return self.response(body)
        with mock.patch.object(self.service, "_library_snapshot", return_value=snapshot), \
             mock.patch.object(self.service, "_library_request", side_effect=request) as select:
            selected = self.service._select_materials(payload, "a" * 32)
            self.assertEqual(selected, self.service._select_materials(payload, "a" * 32))
            select.assert_called_once()
        self.whitelist([self.sha("unrelated")])
        with mock.patch.object(self.service, "_library_request") as request, self.assertRaises(matrix.MatrixTemplateError):
            self.service._select_materials(payload, "a" * 32)
        request.assert_not_called()
        self.whitelist(allowed)
        with mock.patch.object(self.service, "_library_snapshot", return_value=snapshot), \
             mock.patch.object(self.service, "_library_request", side_effect=lambda m, p, b: self.response(b, private=True)), \
             self.assertRaises(matrix.MatrixTemplateError):
            self.service._select_materials(payload, "b" * 32)
        self.assertIsNone(self.service.store.material_selection("b" * 32))

    def test_user_upload_plus_visual_and_bgm_fill_apply_same_restriction(self):
        user_sha = self._store_user_asset(b"own-upload")
        body = dict(self.body(material_policy="owned_public", duration=10,
                              user_materials=[dict(sha256=user_sha, media_type="video")]), bgm=True)
        job = self.service.submit(body, "user-fill")
        payload = json.loads(self.service.store.get(job["job_id"])["payload"])
        scenes, _, _ = self.service._material_scenes(payload)
        allowed = [self.sha(s["scene_id"]) for s in scenes]
        self.whitelist(allowed)
        calls = []
        def request(method, path, body):
            calls.append(body)
            self.assertIn(self.sha("private"), body["used_sha256"])
            self.assertIn(user_sha, body["used_sha256"])
            return self.response(body)
        with mock.patch.object(self.service, "_library_snapshot", return_value={"sha256": allowed + [self.sha("private")]}), \
             mock.patch.object(self.service, "_library_request", side_effect=request):
            selected = self.service._select_materials(payload, job["job_id"])
        self.assertEqual("user", selected[0]["provider"])
        self.assertEqual(user_sha, selected[0]["sha256"])
        self.assertEqual("bgm", selected[-1]["media_type"])
        self.assertEqual(2, len(calls))
        self.assertNotEqual(calls[0]["selection_id"], calls[1]["selection_id"])
        self.assertTrue(all(i["sha256"] in allowed for i in selected[1:]))

    def test_restricted_library_shortage_remains_failure(self):
        exc = matrix.MatrixTemplateError("no unique approved material for bgm")
        translated = self.service._translate_restricted_shortage(self.body(), exc)
        self.assertIsInstance(translated, matrix.MatrixTemplateError)
        self.assertIn("背景音乐", str(translated))
        self.assertIs(exc, self.service._translate_restricted_shortage({}, exc))


if __name__ == "__main__":
    unittest.main()
