from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from server import ai_edit_v2_worker
from server.ai_edit_v3_worker import run_worker, worker_config
from server.content_domains import ai_edit_v2_store
from server.content_domains.ai_edit_v3.contracts import request_fingerprint
from server.content_domains.ai_edit_v3.delivery import (
    build_object_key,
    register_current_generation,
)
from server.content_domains.ai_edit_v3.feature import FeatureConfig
from server.content_domains.ai_edit_v3.runtime import Runtime, RuntimeDependencies
from server.content_domains.ai_edit_v3.store import (
    StoreConflictError,
    V3Store,
    init_db,
)
from server.content_domains.video_asset_publish import PublicationDecision


def tables(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not row[0].startswith("sqlite_")
        }


def repository_v3_paths(repository: Path) -> list[str]:
    tracked = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    untracked = subprocess.run(
        ("git", "ls-files", "--others", "--exclude-standard", "-z"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    ignored = subprocess.run(
        ("git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    paths = {
        value.decode("utf-8")
        for value in (tracked + untracked + ignored).split(b"\0")
        if value
    }
    return sorted(
        path
        for path in paths
        if re.search(r"(?i)ai[_-]edit[_-]v3", path)
        and not re.search(r"(?:^|/)node_modules(?:/|$)", path)
    )


PRIVATE_ARTIFACT_PATTERNS = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE" + r" KEY-----"),
    ),
    (
        "authorization",
        re.compile(
            r"(?i)\bAuthorization[\"']?\s*:\s*[\"']?\s*"
            r"Bearer\s+(?P<credential>[A-Za-z0-9._~+/=-]{12,})"
        ),
    ),
    (
        "cookie",
        re.compile(
            r"(?i)\bCookie[\"']?\s*:\s*[\"']?"
            r"(?P<cookie_values>[^\r\n]+)"
        ),
    ),
    (
        "cloud-secret",
        re.compile(
            r"(?i)(?:\b(?P<access_id>(?:AKIA|ASIA)[A-Z0-9]{16})\b|"
            r"\b(?:secret_?id|secret_?key)\s*[:=]\s*['\"]?"
            r"(?P<credential>[A-Za-z0-9._~+/=-]{12,}))"
        ),
    ),
    (
        "signed-query",
        re.compile(r"(?i)(?:x-amz-signature|q-signature|signature)=[0-9a-f]{16,}"),
    ),
)

EXPLICIT_TEST_PLACEHOLDER = re.compile(
    r"(?i)(?:test-only-secret(?:-(?:token|cookie|1234))?|"
    r"example-(?:token|cookie)-value)"
)
COOKIE_CREDENTIAL = re.compile(
    r"\b[A-Za-z0-9_-]+\s*=\s*[\"']?"
    r"(?P<credential>[A-Za-z0-9._~+/%=-]{12,})"
)


def private_artifact_labels(
    text: str, *, allow_test_placeholders: bool = False
) -> set[str]:
    labels: set[str] = set()
    for label, pattern in PRIVATE_ARTIFACT_PATTERNS:
        for match in pattern.finditer(text):
            if label == "cookie":
                credentials = [
                    item.group("credential")
                    for item in COOKIE_CREDENTIAL.finditer(
                        match.group("cookie_values")
                    )
                ]
            else:
                credentials = [
                    value
                    for name, value in match.groupdict().items()
                    if name in {"credential", "access_id"} and value is not None
                ]
            if not credentials:
                if label != "cookie":
                    labels.add(label)
                continue
            if any(
                not (
                    allow_test_placeholders
                    and EXPLICIT_TEST_PLACEHOLDER.fullmatch(credential) is not None
                )
                for credential in credentials
            ):
                labels.add(label)
    return labels


class StopAfterOneLoop:
    def __init__(self) -> None:
        self.waits = 0

    def is_set(self) -> bool:
        return self.waits > 0

    def wait(self, _timeout: float) -> bool:
        self.waits += 1
        return True


class FixedClock:
    def now(self) -> float:
        return 0.1


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def register_generation(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision:
        self.calls.append(
            (mode, source_job_id, generation, idempotency_key)
        )
        return PublicationDecision("accepted", generation, None)


class V3IsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.v2_db = self.root / "ai_edit_v2.db"
        self.v3_db = self.root / "ai_edit_v3.db"
        ai_edit_v2_store.init_db(str(self.v2_db))
        init_db(self.v3_db, v2_db_path=self.v2_db)
        self.store = V3Store(
            self.v3_db,
            v2_db_path=self.v2_db,
            environment="test",
        )

    def _seed_v3_job(self, *, state: str = "queued") -> None:
        request = {"input_type": "uploaded_video"}
        self.store.insert_pricing_version(
            "price-v1",
            {"base": 1},
            status="published",
            created_at=1,
            published_at=1,
        )
        self.store.insert_quote(
            "alice",
            "quote-v3",
            request,
            pricing_version="price-v1",
            min_points=1,
            max_points=1,
            breakdown={"base": 1},
            expires_at=10_000,
            created_at=1,
        )
        self.store._write(
            lambda connection: connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,
                       request_sha256,quote_id,idempotency_key,queued_at,
                       processing_deadline_at,confirmed_preheld_total,
                       confirmed_refunded_total,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "job-only-v3",
                    "test",
                    "alice",
                    state,
                    '{"input_type":"uploaded_video"}',
                    request_fingerprint(request),
                    "quote-v3",
                    "job-only-v3-key",
                    2,
                    20_000,
                    1,
                    0,
                    2,
                    2,
                ),
            )
        )

    def _runtime(self, store: object) -> Runtime:
        dependencies = RuntimeDependencies(
            store=store,
            clock=FixedClock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )
        return Runtime(
            config=FeatureConfig(False, self.v3_db, self.v2_db, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )

    def test_each_database_contains_only_its_version_tables(self):
        self.assertFalse(
            any(name.startswith("edit_v3_") for name in tables(self.v2_db))
        )
        self.assertFalse(
            any(name.startswith("edit_v2_") for name in tables(self.v3_db))
        )

    def test_v3_worker_reads_only_the_configured_v3_database(self):
        v2_before = hashlib.sha256(self.v2_db.read_bytes()).digest()

        run_worker(
            StopAfterOneLoop(),
            config=worker_config(),
            runtime=self._runtime(self.store),
        )

        self.assertEqual(hashlib.sha256(self.v2_db.read_bytes()).digest(), v2_before)
        self.assertTrue(tables(self.v2_db))
        self.assertTrue(tables(self.v3_db))

    def test_v2_worker_configuration_cannot_claim_a_v3_only_row(self):
        self._seed_v3_job()
        config = ai_edit_v2_worker.worker_config()
        config["db_path"] = str(self.v2_db)

        v2_claim = ai_edit_v2_store.claim_next_job(
            "v2-worker", 30, 100, db_path=config["db_path"]
        )
        v3_claim = self.store.claim_next_job("v3-worker", 30, 100)

        self.assertIsNone(v2_claim)
        self.assertIsNotNone(v3_claim)
        self.assertEqual(v3_claim.job_id, "job-only-v3")

    def test_v3_billing_object_and_asset_namespaces_are_versioned(self):
        self._seed_v3_job(state="publishing")
        claim = self.store.claim_job(
            "job-only-v3",
            "v3-publisher",
            30,
            100,
            expected_states={"publishing"},
        )
        self.assertIsNotNone(claim)
        key = build_object_key(
            "test",
            "alice",
            "job-only-v3",
            "source",
            "source.mp4",
            b"test-only-secret-1234",
        )
        self.store.freeze_delivery_object_key(claim, key, 101)
        publisher = RecordingPublisher()

        register_current_generation(
            claim,
            metadata_sha256="a" * 64,
            now=102,
            store=self.store,
            publisher=publisher,
        )
        intent_keys = self.store._read(
            lambda connection: tuple(
                row[0]
                for row in connection.execute(
                    """SELECT external_idempotency_key
                       FROM edit_v3_publish_intents WHERE job_id=?""",
                    (claim.job_id,),
                )
            )
        )

        self.assertTrue(key.startswith("test/ai-edit-v3/"))
        self.assertTrue(intent_keys)
        self.assertTrue(
            all(value.startswith("ai-edit-v3:") for value in intent_keys)
        )
        self.assertEqual([call[0] for call in publisher.calls], ["ai_edit_v3"])

    def test_typed_worker_errors_emit_a_v3_prefixed_log_record(self):
        class FailingStore:
            def list_due_billing_intents(self, _now: int, *, limit: int):
                raise StoreConflictError(
                    "isolation_probe_failed", "injected typed store conflict"
                )

        with self.assertLogs(level="ERROR") as captured:
            run_worker(
                StopAfterOneLoop(),
                config=worker_config(),
                runtime=self._runtime(FailingStore()),
            )

        self.assertTrue(captured.records)
        self.assertTrue(
            all(record.getMessage().startswith("[ai-edit-v3]") for record in captured.records)
        )

    def test_tracked_and_untracked_v3_paths_contain_no_private_artifacts(self):
        repository = Path(__file__).resolve().parents[1]
        candidates = repository_v3_paths(repository)
        findings: list[str] = []
        for relative in candidates:
            suffix = Path(relative).suffix.lower()
            normalized = relative.replace("\\", "/")
            if suffix in {".mp4", ".mov", ".mkv", ".webm", ".mp3", ".wav", ".flac"}:
                findings.append(f"media:{normalized}")
                continue
            if suffix in {".db", ".sqlite", ".sqlite3"} and not normalized.startswith(
                "tests/"
            ):
                findings.append(f"database:{normalized}")
                continue
            raw = (repository / relative).read_bytes()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            labels = private_artifact_labels(
                text, allow_test_placeholders=normalized.startswith("tests/")
            )
            findings.extend(f"{label}:{normalized}" for label in sorted(labels))
        self.assertTrue(candidates)
        self.assertEqual(findings, [])

    def test_v3_path_discovery_includes_standard_ignored_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            subprocess.run(
                ("git", "init", "--quiet"), cwd=repository, check=True
            )
            (repository / ".gitignore").write_text(
                "*.db\n*.env\n", encoding="utf-8"
            )
            (repository / "ai_edit_v3_private.db").write_text(
                "not-a-real-database", encoding="utf-8"
            )
            (repository / "ai-edit-v3.env").write_text(
                "PLACEHOLDER=1", encoding="utf-8"
            )
            dependency = (
                repository
                / "server"
                / "ai_edit_v3_renderer"
                / "node_modules"
                / "fixture"
                / "secret.js"
            )
            dependency.parent.mkdir(parents=True)
            dependency.write_text("vendored fixture", encoding="utf-8")

            discovered = repository_v3_paths(repository)

        self.assertIn("ai_edit_v3_private.db", discovered)
        self.assertIn("ai-edit-v3.env", discovered)
        self.assertNotIn(
            "server/ai_edit_v3_renderer/node_modules/fixture/secret.js",
            discovered,
        )

    def test_private_header_detection_handles_quoted_curl_and_cookie_lists(self):
        examples = {
            "authorization": (
                'headers = {"' + "Authorization" + '": "Bearer '
                + 'test-only-secret-token"}'
            ),
            "curl-authorization": (
                'curl -H "' + "Authorization" + ": Bearer test-only-secret-token\""
            ),
            "cookie": (
                'curl -H "' + "Cookie" + ": session=test-only-secret-cookie; theme=dark\""
            ),
        }
        expected = {
            "authorization": {"authorization"},
            "curl-authorization": {"authorization"},
            "cookie": {"cookie"},
        }

        self.assertEqual(
            {name: private_artifact_labels(value) for name, value in examples.items()},
            expected,
        )

    def test_test_placeholder_exemption_is_anchored_to_the_credential_value(self):
        allowed = (
            "Authorization: Bearer test-only-secret-token",
            "Cookie: session=test-only-secret-cookie; Path=/; HttpOnly",
        )
        rejected = {
            "authorization": (
                "Author" + "ization: Bearer stolen-token-" + "example-123456789"
            ),
            "cookie": (
                "Cook" + "ie: session=stolen-token-" + "test-only-secret-cookie"
            ),
        }

        self.assertEqual(
            [
                private_artifact_labels(value, allow_test_placeholders=True)
                for value in allowed
            ],
            [set(), set()],
        )
        self.assertEqual(
            {
                name: private_artifact_labels(
                    value, allow_test_placeholders=True
                )
                for name, value in rejected.items()
            },
            {"authorization": {"authorization"}, "cookie": {"cookie"}},
        )

    def test_cookie_placeholder_cannot_hide_a_later_real_credential(self):
        header = (
            "Cook" + "ie: fixture=test-only-secret-cookie; "
            "session=actual-secret-token-123456; Path=/"
        )

        self.assertEqual(
            private_artifact_labels(header, allow_test_placeholders=True),
            {"cookie"},
        )

    def test_unquoted_cloud_secrets_are_scanned_per_credential(self):
        allowed = "SECRET" + "_KEY=test-only-secret-token"
        rejected = (
            "Secret" + "Id=actual-secret-token-123456",
            "SECRET" + "_KEY=stolen-token-test-only-secret-token",
        )

        self.assertEqual(
            private_artifact_labels(allowed, allow_test_placeholders=True),
            set(),
        )
        self.assertEqual(
            [
                private_artifact_labels(value, allow_test_placeholders=True)
                for value in rejected
            ],
            [{"cloud-secret"}, {"cloud-secret"}],
        )

    def test_cookie_scanner_handles_quoted_and_percent_encoded_values(self):
        risky = (
            "Cook" + 'ie: session="' + "abcdefghijklmnop" + '"',
            "Cook" + "ie: session=" + "abc%2Fdef%3Dghijklmnop",
        )
        ordinary = "Cook" + "ie: theme=dark; Path=/"

        self.assertEqual(
            [private_artifact_labels(value) for value in risky],
            [{"cookie"}, {"cookie"}],
        )
        self.assertEqual(private_artifact_labels(ordinary), set())


if __name__ == "__main__":
    unittest.main()
