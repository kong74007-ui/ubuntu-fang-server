from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.renderers.release import (
    RendererReleaseError,
    resolve_renderer_release,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELEASE = ROOT / "server" / "ai_edit_v3_renderer"


def _canonical_build_id(lock: dict[str, object]) -> str:
    payload = dict(lock)
    payload.pop("renderer_build_id", None)
    payload.pop("release_archive_sha256", None)
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class RendererReleaseResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.releases = Path(self.temp.name) / "releases"
        self.releases.mkdir()

    def _copy_v1(self, commit: str, *, historical: bool = False) -> tuple[str, Path]:
        staging = Path(self.temp.name) / f"staging-{commit[0]}"
        shutil.copytree(SOURCE_RELEASE, staging, ignore=shutil.ignore_patterns("node_modules"))
        lock_path = staging / "renderer-release.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["schema_version"] = 1
        lock.pop("release_tree_files", None)
        lock.pop("release_tree_sha256", None)
        lock["git_commit"] = commit
        lock["renderer_build_id"] = _canonical_build_id(lock)
        lock_path.write_text(json.dumps(lock, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        destination = (
            self.releases / "historical" / f"legacy-{commit[0]}"
            if historical
            else self.releases / lock["renderer_build_id"].removeprefix("sha256:")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(destination)
        return lock["renderer_build_id"], destination

    def _write_historical_index(self, entries: dict[str, str]) -> None:
        payload = {"schema_version": 1, "releases": entries}
        (self.releases / "historical-release-index.json").write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def test_manifest_build_id_resolves_historical_release_not_current(self) -> None:
        old_id, old_root = self._copy_v1("a" * 40, historical=True)
        self._write_historical_index({old_id: "historical/legacy-a"})
        _new_id, new_root = self._copy_v1("b" * 40)
        if os.name != "nt":
            (Path(self.temp.name) / "current").symlink_to(new_root, target_is_directory=True)

        release = resolve_renderer_release(old_id, self.releases)

        self.assertEqual(release.root, old_root.resolve())
        self.assertEqual(release.report.renderer_build_id, old_id)
        self.assertEqual(json.loads((old_root / "renderer-release.lock.json").read_text())["schema_version"], 1)

    def test_historical_index_is_canonical_and_cannot_escape_release_root(self) -> None:
        build_id, _root = self._copy_v1("f" * 40, historical=True)
        self._write_historical_index({build_id: "../outside"})
        with self.assertRaisesRegex(RendererReleaseError, "renderer_release_index_invalid"):
            resolve_renderer_release(build_id, self.releases)

        self._write_historical_index({build_id: "historical/legacy-f"})
        index = self.releases / "historical-release-index.json"
        index.write_text(index.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RendererReleaseError, "renderer_release_index_invalid"):
            resolve_renderer_release(build_id, self.releases)

    def test_unknown_escape_mismatch_and_incomplete_release_fail_closed(self) -> None:
        build_id, root = self._copy_v1("c" * 40)
        with self.assertRaisesRegex(RendererReleaseError, "renderer_release_unknown"):
            resolve_renderer_release("sha256:" + "0" * 64, self.releases)

        if os.name != "nt":
            escaped = self.releases / ("d" * 64)
            escaped.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(RendererReleaseError, "renderer_release_path_invalid"):
                resolve_renderer_release("sha256:" + "d" * 64, self.releases)

        wrong = self.releases / ("e" * 64)
        shutil.copytree(root, wrong)
        with self.assertRaisesRegex(RendererReleaseError, "renderer_build_id_mismatch"):
            resolve_renderer_release("sha256:" + "e" * 64, self.releases)

        font = root / "assets" / "fonts" / "NotoSansSC-Regular.woff2"
        font.unlink()
        with self.assertRaisesRegex(RendererReleaseError, "renderer_release_file_missing"):
            resolve_renderer_release(build_id, self.releases)


if __name__ == "__main__":
    unittest.main()
