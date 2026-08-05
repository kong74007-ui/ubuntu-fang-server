from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class VisualQualityEvidenceTests(unittest.TestCase):
    def test_snapshot_real_size_is_bounded_before_any_unbounded_read(self):
        from server.content_domains.ai_edit_v3.production import (
            _verified_snapshot_inputs,
        )

        with TemporaryDirectory() as folder:
            output_root = Path(folder)
            snapshot = output_root / "snapshots" / "frame-001.png"
            snapshot.parent.mkdir()
            with snapshot.open("wb") as output:
                output.seek(33 * 1024 * 1024)
                output.write(b"x")
            render = {"snapshots": ["snapshots/frame-001.png"]}
            report = {
                "snapshots": [
                    {
                        "path": "frame-001.png",
                        "size_bytes": 1,
                        "sha256": "0" * 64,
                    }
                ]
            }

            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("unbounded read forbidden"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "quality_snapshot_evidence_invalid",
                ):
                    _verified_snapshot_inputs(
                        output_root,
                        render,
                        report,
                        duration_ms=4_000,
                    )

    def test_snapshot_inputs_are_real_bounded_files_with_verified_hashes(self):
        from server.content_domains.ai_edit_v3.production import (
            _verified_snapshot_inputs,
        )

        with TemporaryDirectory() as folder:
            output_root = Path(folder)
            snapshot = output_root / "snapshots" / "frame-001.png"
            snapshot.parent.mkdir()
            snapshot.write_bytes(b"png-fixture")
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            render = {"snapshots": ["snapshots/frame-001.png"]}
            report = {
                "snapshots": [
                    {
                        "path": "frame-001.png",
                        "size_bytes": snapshot.stat().st_size,
                        "sha256": digest,
                    }
                ]
            }

            evidence = _verified_snapshot_inputs(
                output_root,
                render,
                report,
                duration_ms=4_000,
            )

        self.assertEqual(1, len(evidence))
        self.assertEqual(digest, evidence[0]["frame_sha256"])
        self.assertEqual(0, evidence[0]["timestamp_ms"])
        self.assertEqual(snapshot.resolve(), evidence[0]["local_path"])

    def test_snapshot_inputs_fail_closed_on_missing_escape_or_hash_mismatch(self):
        from server.content_domains.ai_edit_v3.production import (
            _verified_snapshot_inputs,
        )

        with TemporaryDirectory() as folder:
            output_root = Path(folder)
            snapshot = output_root / "snapshots" / "frame-001.png"
            snapshot.parent.mkdir()
            snapshot.write_bytes(b"png-fixture")
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
            valid_report = {
                "snapshots": [
                    {
                        "path": "frame-001.png",
                        "size_bytes": snapshot.stat().st_size,
                        "sha256": digest,
                    }
                ]
            }

            cases = (
                ({"snapshots": []}, valid_report),
                ({"snapshots": ["../frame-001.png"]}, valid_report),
                (
                    {"snapshots": ["snapshots/frame-001.png"]},
                    {
                        "snapshots": [
                            {
                                **valid_report["snapshots"][0],
                                "sha256": "0" * 64,
                            }
                        ]
                    },
                ),
            )
            for render, report in cases:
                with self.subTest(render=render, report=report):
                    with self.assertRaisesRegex(
                        ValueError,
                        "quality_snapshot_evidence_invalid",
                    ):
                        _verified_snapshot_inputs(
                            output_root,
                            render,
                            report,
                            duration_ms=4_000,
                        )


if __name__ == "__main__":
    unittest.main()
