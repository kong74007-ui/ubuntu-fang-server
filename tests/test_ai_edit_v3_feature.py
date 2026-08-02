from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest import mock

import server.content_domains.ai_edit_v3.store as store_module
from server.content_domains.ai_edit_v3.contracts import LeaseClaim
from server.content_domains.ai_edit_v3.feature import (
    CapabilityItem,
    CapabilityReport,
    CapabilityUnavailable,
    FeatureConfigurationError,
    load_config,
)
from server.content_domains.ai_edit_v3.providers.base import (
    DefinitiveNotAccepted,
    ProviderResult,
    SubmissionUnknown,
)
from server.content_domains.ai_edit_v3.renderers import (
    RenderRequest,
    Renderer,
    RenderResult,
)
from server.content_domains.ai_edit_v3.runtime import (
    Clock,
    ProcessSupervisor,
    RuntimeDependencies,
    StageContext,
    StageHandler,
    StageOutcome,
    assert_ready_for_request,
    build_runtime,
    preflight,
)
from server.content_domains.ai_edit_v3.store import V3Store


class V3EnvironmentManifestTests(unittest.TestCase):
    EXPECTED_V3_ENV = {
        "AI_EDIT_V3_ENABLED": "0",
        "AI_EDIT_V3_ENVIRONMENT": "test",
        "AI_EDIT_V3_DB_PATH": "/home/ubuntu/content-api/ai_edit_v3.db",
        "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE": (
            "/etc/huangque/ai-edit-v3-owner-hmac.secret"
        ),
        "AI_EDIT_V3_WORKER_CONCURRENCY": "5",
        "AI_EDIT_V3_QUEUE_CAPACITY": "50",
        "AI_EDIT_V3_TEMP_BYTES_LIMIT": "10737418240",
    }
    FORBIDDEN_V3_NAMES = {
        "AI_EDIT_V3_OWNER_HMAC_SECRET",
        "AI_EDIT_V3_COS_SECRET_ID",
        "AI_EDIT_V3_COS_SECRET_KEY",
        "AI_EDIT_V3_COS_REGION",
        "AI_EDIT_V3_COS_BUCKET",
        "AI_EDIT_V3_COS_PREFIX",
        "AI_EDIT_V3_PROVIDER_API_KEY",
        "AI_EDIT_V3_PIPELINE_CONCURRENCY",
        "AI_EDIT_V3_RENDER_SLOTS",
        "AI_EDIT_V3_QUEUE_LIMIT",
    }

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        example = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "huangque-secrets.env.example"
        )
        self.entries = []
        for line in example.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            self.entries.append(tuple(stripped.split("=", 1)))

    def manifest_env(self) -> dict[str, str]:
        return {
            name: value
            for name, value in self.entries
            if name.startswith("AI_EDIT_V3_")
        }

    def test_manifest_declares_the_exact_default_off_v3_contract(self):
        v3_entries = [
            (name, value)
            for name, value in self.entries
            if name.startswith("AI_EDIT_V3_")
        ]
        self.assertEqual(dict(v3_entries), self.EXPECTED_V3_ENV)
        self.assertEqual(len(v3_entries), len(self.EXPECTED_V3_ENV))

    def test_manifest_excludes_secrets_providers_cos_and_stale_aliases(self):
        names = {name for name, _value in self.entries}
        self.assertTrue(self.FORBIDDEN_V3_NAMES.isdisjoint(names))

    def test_manifest_preserves_one_existing_v2_database_definition(self):
        self.assertEqual(
            [value for name, value in self.entries if name == "AI_EDIT_V2_DB"],
            ["/home/ubuntu/content-api/ai_edit_v2.db"],
        )

    def test_manifest_is_accepted_without_creating_configured_files(self):
        env = self.manifest_env()
        env["AI_EDIT_V3_DB_PATH"] = os.fspath(self.root / "v3.db")
        env["AI_EDIT_V2_DB"] = os.fspath(self.root / "v2.db")
        env["AI_EDIT_V3_OWNER_HMAC_SECRET_FILE"] = os.fspath(
            self.root / "owner-hmac.secret"
        )

        before = tuple(self.root.iterdir())
        config = load_config(env)

        self.assertFalse(config.enabled)
        self.assertEqual(config.environment, "test")
        self.assertEqual(config.worker_concurrency, 5)
        self.assertEqual(config.queue_capacity, 50)
        self.assertEqual(config.temp_bytes_limit, 10737418240)
        self.assertEqual(tuple(self.root.iterdir()), before)

    def test_file_reference_alone_does_not_make_the_runtime_ready(self):
        env = self.manifest_env()
        env["AI_EDIT_V3_DB_PATH"] = os.fspath(self.root / "v3.db")
        env["AI_EDIT_V2_DB"] = os.fspath(self.root / "v2.db")
        env["AI_EDIT_V3_OWNER_HMAC_SECRET_FILE"] = os.fspath(
            self.root / "owner-hmac.secret"
        )

        report = preflight(build_runtime(env=env))

        self.assertFalse(report.accepts_uploads)
        self.assertFalse(report.accepts_new_jobs)
        self.assertEqual(
            report.items["isolated_v3_store"].reason_code,
            "capability_not_injected",
        )


class FeatureConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def enabled_env(self) -> dict[str, str]:
        return {
            "AI_EDIT_V3_ENABLED": "1",
            "AI_EDIT_V3_DB_PATH": os.fspath(self.root / "v3.db"),
            "AI_EDIT_V2_DB": os.fspath(self.root / "v2.db"),
            "AI_EDIT_V3_ENVIRONMENT": "test",
            "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE": os.fspath(
                self.root / "owner-hmac.secret"
            ),
            "AI_EDIT_V3_WORKER_CONCURRENCY": "2",
            "AI_EDIT_V3_QUEUE_CAPACITY": "8",
            "AI_EDIT_V3_TEMP_BYTES_LIMIT": "1048576",
        }

    def assert_reason(self, reason_code: str, env: dict[str, str]) -> None:
        with self.assertRaises(FeatureConfigurationError) as caught:
            load_config(env)
        self.assertEqual(caught.exception.reason_code, reason_code)

    def test_disabled_defaults_without_write_dependencies_and_is_immutable(self):
        before = dict(os.environ)
        config = load_config({})
        self.assertFalse(config.enabled)
        self.assertIsNone(config.db_path)
        self.assertEqual(dict(os.environ), before)
        with self.assertRaises(FrozenInstanceError):
            config.enabled = True

    def test_enabled_requires_every_documented_write_dependency(self):
        full = self.enabled_env()
        for name in tuple(full):
            if name == "AI_EDIT_V3_ENABLED":
                continue
            with self.subTest(name=name):
                env = dict(full)
                del env[name]
                self.assert_reason("config_required", env)

    def test_enabled_config_normalizes_only_non_secret_references(self):
        config = load_config(self.enabled_env())
        self.assertTrue(config.enabled)
        self.assertEqual(config.environment, "test")
        self.assertEqual(config.worker_concurrency, 2)
        self.assertEqual(config.queue_capacity, 8)
        self.assertEqual(config.temp_bytes_limit, 1048576)
        self.assertEqual(config.owner_hmac_secret_file, self.root / "owner-hmac.secret")
        self.assertNotIn("secret=b", repr(config).lower())

    def test_enabled_flag_is_strict_and_rejects_bool_like_or_whitespace(self):
        for value in ("true", "false", "yes", "01", " 1", "1 ", ""):
            with self.subTest(value=value):
                self.assert_reason(
                    "config_enabled_invalid", {"AI_EDIT_V3_ENABLED": value}
                )

    def test_paths_must_be_absolute_local_and_distinct(self):
        for name in (
            "AI_EDIT_V3_DB_PATH",
            "AI_EDIT_V2_DB",
            "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE",
        ):
            with self.subTest(name=name):
                env = self.enabled_env()
                env[name] = "relative/path"
                self.assert_reason("config_path_not_absolute", env)

        for value in (r"\\server\share\v3.db", r"\\?\C:\v3.db", "//host/v3.db"):
            with self.subTest(value=value):
                env = self.enabled_env()
                env["AI_EDIT_V3_DB_PATH"] = value
                self.assert_reason("config_path_network", env)

        env = self.enabled_env()
        env["AI_EDIT_V2_DB"] = env["AI_EDIT_V3_DB_PATH"]
        self.assert_reason("config_db_paths_same", env)

        env = self.enabled_env()
        env["AI_EDIT_V2_DB"] = os.fspath(self.root / "child" / ".." / "v3.db")
        self.assert_reason("config_db_paths_same", env)

    def test_existing_database_hardlink_aliases_are_rejected(self):
        v3_path = self.root / "v3.db"
        v2_alias = self.root / "v2-alias.db"
        v3_path.touch()
        try:
            os.link(v3_path, v2_alias)
        except OSError as exc:
            self.skipTest(f"hardlinks unavailable: {exc}")
        env = self.enabled_env()
        env["AI_EDIT_V3_DB_PATH"] = os.fspath(v3_path)
        env["AI_EDIT_V2_DB"] = os.fspath(v2_alias)
        self.assert_reason("config_db_paths_same", env)

    def test_environment_is_exact(self):
        for value in ("prod", "TEST", "production ", "", "staging"):
            with self.subTest(value=value):
                env = self.enabled_env()
                env["AI_EDIT_V3_ENVIRONMENT"] = value
                self.assert_reason("config_environment_invalid", env)

    def test_integer_boundaries_are_strict_and_bounded(self):
        cases = {
            "AI_EDIT_V3_WORKER_CONCURRENCY": ("0", "11", "true", " 1", "1.0"),
            "AI_EDIT_V3_QUEUE_CAPACITY": ("0", "51", "false", "+1", "01"),
            "AI_EDIT_V3_TEMP_BYTES_LIMIT": (
                "0",
                "-1",
                "true",
                str(1 << 63),
            ),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    env = self.enabled_env()
                    env[name] = value
                    self.assert_reason("config_integer_invalid", env)

    def test_raw_or_unknown_security_sensitive_variables_are_rejected_safely(self):
        for name in (
            "AI_EDIT_V3_OWNER_HMAC_SECRET",
            "AI_EDIT_V3_OWNER_HMAC_SECRET_VALUE",
            "AI_EDIT_V3_PROVIDER_API_KEY",
            "AI_EDIT_V3_PRIVATE_KEY",
            "AI_EDIT_V3_ACCESS_KEY",
            "AI_EDIT_V3_SIGNING_KEY",
            "AI_EDIT_V3_KEY",
            "AI_EDIT_V3_TOKEN",
            "AI_EDIT_V3_PASSWORD",
            "AI_EDIT_V3_CREDENTIAL",
            "AI_EDIT_V3_COOKIE",
            "AI_EDIT_V3_AUTH_HEADER",
        ):
            with self.subTest(name=name):
                secret = "do-not-leak-this-value"
                env = self.enabled_env()
                env[name] = secret
                with self.assertRaises(FeatureConfigurationError) as caught:
                    load_config(env)
                self.assertEqual(caught.exception.reason_code, "config_secret_forbidden")
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(caught.exception))

    def test_raw_security_variables_are_forbidden_even_when_blank(self):
        for name in (
            "AI_EDIT_V3_OWNER_HMAC_SECRET",
            "AI_EDIT_V3_COS_SECRET_ID",
            "AI_EDIT_V3_COS_SECRET_KEY",
            "AI_EDIT_V3_PROVIDER_API_KEY",
        ):
            with self.subTest(name=name):
                env = self.enabled_env()
                env[name] = ""
                self.assert_reason("config_secret_forbidden", env)

    def test_security_sensitive_unknown_names_are_classified_case_insensitively(self):
        names = (
            "AI_EDIT_V3_owner_hmac_secret",
            "AI_EDIT_V3_SIGNING_KEY",
            "AI_EDIT_V3_COOKIE",
            "AI_EDIT_V3_AUTH_HEADER",
            "ai_edit_v3_mixed_token",
        )
        for name in names:
            with self.subTest(name=name):
                secret = "mixed-case-secret-value"
                env = self.enabled_env()
                env[name] = secret
                with self.assertRaises(FeatureConfigurationError) as caught:
                    load_config(env)
                self.assertEqual(caught.exception.reason_code, "config_secret_forbidden")
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(caught.exception))

        env = self.enabled_env()
        env["AI_EDIT_V3_TELEMETRY_LABEL"] = "safe-unknown-value"
        config = load_config(env)
        self.assertTrue(config.enabled)

    def test_common_compound_security_names_are_rejected_without_value_leaks(self):
        names = (
            "AI_EDIT_V3_APIKEY",
            "AI_EDIT_V3_DASHSCOPE_APIKEY",
            "AI_EDIT_V3_SECRETKEY",
            "AI_EDIT_V3_PROVIDER_SECRETKEY",
            "AI_EDIT_V3_PRIVATEKEY",
            "AI_EDIT_V3_AWS_PRIVATEKEY",
            "AI_EDIT_V3_ACCESSKEY",
            "AI_EDIT_V3_AWS_ACCESSKEY",
            "AI_EDIT_V3_SIGNINGKEY",
            "AI_EDIT_V3_PROVIDER_SIGNINGKEY",
            "AI_EDIT_V3_HMACKEY",
            "AI_EDIT_V3_OWNER_HMACKEY",
            "AI_EDIT_V3_AUTHHEADER",
            "AI_EDIT_V3_PROVIDER_AUTHHEADER",
            "AI_EDIT_V3_AUTHTOKEN",
            "AI_EDIT_V3_PROVIDER_AUTHTOKEN",
            "AI_EDIT_V3_BEARERTOKEN",
            "AI_EDIT_V3_PROVIDER_BEARERTOKEN",
            "AI_EDIT_V3_SESSIONCOOKIE",
            "AI_EDIT_V3_PROVIDER_SESSIONCOOKIE",
        )
        for name in names:
            with self.subTest(name=name):
                secret = "compound-name-value-must-not-leak"
                env = self.enabled_env()
                env[name] = secret
                with self.assertRaises(FeatureConfigurationError) as caught:
                    load_config(env)
                self.assertEqual(caught.exception.reason_code, "config_secret_forbidden")
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(caught.exception))

    def test_concatenated_provider_compounds_are_rejected_without_value_leaks(self):
        compound_tokens = (
            "PROVIDERAPIKEY",
            "DASHSCOPEAPIKEY",
            "DASHSCOPEAPIKEYPRIMARY",
            "PROVIDERSECRETKEY",
            "KMSSECRETKEY",
            "AWSPRIVATEKEY",
            "PROVIDERPRIVATEKEY",
            "ALIYUNACCESSKEY",
            "PROVIDERACCESSKEY",
            "WEBHOOKSIGNINGKEY",
            "PROVIDERSIGNINGKEY",
            "OWNERHMACKEY",
            "PROVIDERHMACKEY",
            "XAUTHHEADER",
            "PROVIDERAUTHHEADER",
            "UPSTREAMAUTHTOKEN",
            "PROVIDERAUTHTOKEN",
            "OAUTHBEARERTOKEN",
            "PROVIDERBEARERTOKEN",
            "BROWSERSESSIONCOOKIE",
            "PROVIDERSESSIONCOOKIE",
        )
        for token in compound_tokens:
            name = f"AI_EDIT_V3_{token}"
            with self.subTest(name=name):
                secret = "concatenated-marker-value-must-not-leak"
                env = self.enabled_env()
                env[name] = secret
                with self.assertRaises(FeatureConfigurationError) as caught:
                    load_config(env)
                self.assertEqual(caught.exception.reason_code, "config_secret_forbidden")
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(caught.exception))

    def test_security_name_classifier_preserves_safe_compound_lookalikes(self):
        env = self.enabled_env()
        for name in (
            "AI_EDIT_V3_MONKEY",
            "AI_EDIT_V3_KEYFRAME",
            "AI_EDIT_V3_SECRETARY",
            "AI_EDIT_V3_COOKIECUTTER",
            "AI_EDIT_V3_PROVIDER_MONKEY",
            "AI_EDIT_V3_PROVIDER_KEYFRAME",
            "AI_EDIT_V3_PROVIDER_SECRETARY",
            "AI_EDIT_V3_PROVIDER_COOKIECUTTER",
            "AI_EDIT_V3_KEYBOARD",
            "AI_EDIT_V3_KEYSTONE",
            "AI_EDIT_V3_KEYNOTE",
            "AI_EDIT_V3_HOCKEY",
            "AI_EDIT_V3_AUTHENTICATION",
            "AI_EDIT_V3_COOKIEJAR",
            "AI_EDIT_V3_TOKENIZER",
            "AI_EDIT_V3_SECRETARIAT",
        ):
            env[name] = "safe-lookalike"
        self.assertTrue(load_config(env).enabled)

    def test_windows_reserved_device_aliases_are_rejected_in_every_path_role(self):
        aliases = (
            "NUL",
            "nul.txt",
            "CON ",
            "PRN.",
            "AUX.db",
            "CLOCK$.log",
            "CONIN$.txt",
            "CONOUT$ ",
            "COM1",
            "com9.db",
            "LPT1",
            "lpt9.txt",
        )
        for field_name in (
            "AI_EDIT_V3_DB_PATH",
            "AI_EDIT_V2_DB",
            "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE",
        ):
            for alias in aliases:
                with self.subTest(field_name=field_name, alias=alias):
                    env = self.enabled_env()
                    env[field_name] = os.fspath(self.root / alias / "value")
                    self.assert_reason("config_path_reserved", env)

    def test_db_filesystem_classification_rejects_remote_and_unknown(self):
        for classification, reason_code in (
            ("remote", "config_db_filesystem_remote"),
            ("unknown", "config_db_filesystem_unknown"),
        ):
            with self.subTest(classification=classification):
                with mock.patch(
                    "server.content_domains.ai_edit_v3.feature.classify_filesystem",
                    return_value=mock.Mock(policy=classification),
                    create=True,
                ):
                    self.assert_reason(reason_code, self.enabled_env())

    def test_db_filesystem_classification_accepts_local_and_skips_secret_reference(self):
        with mock.patch(
            "server.content_domains.ai_edit_v3.feature.classify_filesystem",
            return_value=mock.Mock(policy="local"),
            create=True,
        ) as classify:
            config = load_config(self.enabled_env())
        self.assertTrue(config.enabled)
        classified_paths = {Path(call.args[0]) for call in classify.call_args_list}
        self.assertEqual(
            classified_paths,
            {self.root},
        )
        self.assertEqual(classify.call_count, 2)
        self.assertNotIn(self.root / "owner-hmac.secret", classified_paths)

    def test_public_filesystem_classifier_and_feature_share_store_policy(self):
        classify = getattr(store_module, "classify_filesystem", None)
        self.assertTrue(callable(classify), "public filesystem policy is required")
        for fs_type, expected in (
            ("windows_fixed", "local"),
            ("nfs", "remote"),
            (None, "unknown"),
            ("mysteryfs", "unknown"),
        ):
            with self.subTest(fs_type=fs_type):
                with mock.patch.object(
                    store_module, "_filesystem_type_for_path", return_value=fs_type
                ):
                    result = classify(self.root)
                    self.assertEqual(result.policy, expected)
                    self.assertEqual(result.filesystem_type, fs_type)

        with mock.patch(
            "server.content_domains.ai_edit_v3.feature.classify_filesystem",
            wraps=classify,
            create=True,
        ) as feature_policy, mock.patch.object(
            store_module, "_filesystem_type_for_path", return_value="windows_fixed"
        ):
            self.assertTrue(load_config(self.enabled_env()).enabled)
        self.assertEqual(feature_policy.call_count, 2)

    def test_public_filesystem_classifier_fails_closed_for_non_path_input(self):
        classify = getattr(store_module, "classify_filesystem")
        with mock.patch.object(
            store_module,
            "_filesystem_type_for_path",
            return_value="windows_fixed",
        ) as probe:
            result = classify("not-a-path")
        self.assertEqual(result.policy, "unknown")
        self.assertIsNone(result.filesystem_type)
        probe.assert_not_called()


class ProviderAndRendererContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.input_root = self.root / "input"
        self.output_root = self.root / "output"
        self.manifest_path = self.input_root / "render-manifest.json"

    def render_request(self, **overrides: object) -> RenderRequest:
        values: dict[str, object] = {
            "instance_id": "instance-1",
            "job_id": "job-1",
            "attempt": 1,
            "manifest_path": self.manifest_path,
            "input_root": self.input_root,
            "output_root": self.output_root,
            "manifest_sha256": "a" * 64,
            "renderer_build_id": "renderer-build-1",
            "deadline_at": 1234.5,
        }
        values.update(overrides)
        return RenderRequest(**values)

    @staticmethod
    def render_result(**overrides: object) -> RenderResult:
        values: dict[str, object] = {
            "silent_video_relpath": "video/silent.mp4",
            "sha256": "b" * 64,
            "report_relpath": "reports/render.json",
            "snapshots": ("snapshots/0001.png",),
            "environment": {
                "node_version": "22.14.0",
                "renderer": "hyperframes",
            },
            "performance": {"elapsed_ms": 10, "peak_memory_mb": 2.5},
        }
        values.update(overrides)
        return RenderResult(**values)

    def test_provider_result_has_exact_frozen_fields_and_immutable_mappings(self):
        self.assertEqual(
            tuple(field.name for field in fields(ProviderResult)),
            ("provider", "capability", "request_id", "payload", "usage", "elapsed_ms"),
        )
        payload = {"nested": {"items": ["one"]}}
        usage = {"seconds": 1.5}
        result = ProviderResult("fake", "director", None, payload, usage, 12)
        payload["nested"]["items"].append("two")
        usage["seconds"] = 9
        self.assertEqual(result.payload["nested"]["items"], ("one",))
        self.assertEqual(result.usage["seconds"], 1.5)
        with self.assertRaises(FrozenInstanceError):
            result.elapsed_ms = 13
        with self.assertRaises(TypeError):
            result.payload["new"] = "value"

    def test_provider_result_rejects_boolean_usage_and_elapsed_values(self):
        with self.assertRaises(ValueError):
            ProviderResult("fake", "tts", None, {}, {"calls": True}, 1)
        with self.assertRaises(ValueError):
            ProviderResult("fake", "tts", None, {}, {}, True)

    def test_provider_payload_rejects_non_json_mutable_leaves_and_non_string_keys(self):
        class MutableLeaf:
            pass

        for payload in (
            {"raw": bytearray(b"mutable")},
            {"custom": MutableLeaf()},
            {1: "non-string-key"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ProviderResult("fake", "director", None, payload, {}, 1)

    def test_submission_unknown_is_distinct_from_definitive_absence(self):
        definitive = DefinitiveNotAccepted("provider_rejected")
        unknown = SubmissionUnknown("provider_response_unknown")
        self.assertNotIsInstance(unknown, DefinitiveNotAccepted)
        self.assertNotIsInstance(definitive, SubmissionUnknown)
        self.assertEqual(definitive.reason_code, "provider_rejected")
        self.assertEqual(unknown.reason_code, "provider_response_unknown")
        self.assertEqual(str(unknown), "provider_response_unknown")

    def test_provider_identifiers_reject_all_c0_and_c1_controls(self):
        for field_name in ("provider", "capability", "request_id"):
            for codepoint in (0x00, 0x1F, 0x7F, 0x85, 0x9F):
                with self.subTest(field_name=field_name, codepoint=codepoint):
                    values: dict[str, object] = {
                        "provider": "fake",
                        "capability": "director",
                        "request_id": "request-1",
                    }
                    values[field_name] = f"bad{chr(codepoint)}identifier"
                    with self.assertRaises(ValueError):
                        ProviderResult(
                            values["provider"],
                            values["capability"],
                            values["request_id"],
                            {},
                            {},
                            1,
                        )

    def test_renderer_dtos_have_exact_frozen_fields_and_no_authority_inputs(self):
        self.assertEqual(
            tuple(field.name for field in fields(RenderRequest)),
            (
                "instance_id",
                "job_id",
                "attempt",
                "manifest_path",
                "input_root",
                "output_root",
                "manifest_sha256",
                "renderer_build_id",
                "deadline_at",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(RenderResult)),
            (
                "silent_video_relpath",
                "sha256",
                "report_relpath",
                "snapshots",
                "environment",
                "performance",
            ),
        )
        forbidden = {"url", "key", "credential", "command", "environment_override"}
        self.assertTrue(forbidden.isdisjoint(field.name for field in fields(RenderRequest)))
        request = self.render_request()
        with self.assertRaises(FrozenInstanceError):
            request.attempt = 2

    def test_render_request_rejects_attempt_sha_and_sandbox_path_ambiguity(self):
        for attempt in (True, 1.0, "1", 0):
            with self.subTest(attempt=attempt):
                with self.assertRaises(ValueError):
                    self.render_request(attempt=attempt)
        for digest in ("a" * 63, "A" * 64, "g" * 64):
            with self.subTest(digest=digest):
                with self.assertRaises(ValueError):
                    self.render_request(manifest_sha256=digest)
        with self.assertRaises(ValueError):
            self.render_request(input_root=Path("relative-input"))
        with self.assertRaises(ValueError):
            self.render_request(output_root=Path("relative-output"))
        with self.assertRaises(ValueError):
            self.render_request(manifest_path=self.root / "outside.json")
        with self.assertRaises(ValueError):
            self.render_request(output_root=self.input_root / "nested-output")
        with self.assertRaises(ValueError):
            self.render_request(output_root=self.input_root)

    def test_render_result_rejects_unsafe_paths_empty_snapshots_and_nonfinite_metrics(self):
        for name, value in (
            ("silent_video_relpath", "/absolute.mp4"),
            ("silent_video_relpath", "../escape.mp4"),
            ("report_relpath", r"reports\render.json"),
            ("snapshots", ("snapshots/../escape.png",)),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ValueError):
                    self.render_result(**{name: value})
        with self.assertRaises(ValueError):
            self.render_result(snapshots=())
        for value in (True, float("nan"), float("inf"), "10"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.render_result(performance={"elapsed_ms": value})

    def test_render_result_rejects_uri_colon_and_control_relpaths(self):
        unsafe = (
            "https://host/path.mp4",
            "https:/host/path.mp4",
            "data:text/plain,value",
            "file:/tmp/value",
            "bad\x00path",
            "bad\x1fpath",
            "bad\x7fpath",
            "bad\x85path",
            "bad\x9fpath",
        )
        for field_name in ("silent_video_relpath", "report_relpath", "snapshots"):
            for value in unsafe:
                with self.subTest(field_name=field_name, value=repr(value)):
                    override: object = (value,) if field_name == "snapshots" else value
                    with self.assertRaises(ValueError):
                        self.render_result(**{field_name: override})

        result = self.render_result(
            silent_video_relpath="video/silent.mp4",
            report_relpath="reports/render.json",
            snapshots=("snapshots/0001.png",),
        )
        self.assertEqual(result.silent_video_relpath, "video/silent.mp4")

    def test_render_request_identifiers_reject_controls(self):
        for field_name in ("instance_id", "job_id", "renderer_build_id"):
            for codepoint in (0x00, 0x1F, 0x7F, 0x85, 0x9F):
                with self.subTest(field_name=field_name, codepoint=codepoint):
                    with self.assertRaises(ValueError):
                        self.render_request(
                            **{field_name: f"bad{chr(codepoint)}identifier"}
                        )

    def test_render_environment_uses_strict_non_secret_evidence_allowlist(self):
        allowed = {
            "renderer": "hyperframes",
            "renderer_build_id": "renderer-20260801-0123456789ab",
            "code_commit_sha": "1" * 40,
            "package_lock_sha256": "2" * 64,
            "render_bundle_sha256": "3" * 64,
            "component_registry_sha256": "4" * 64,
            "font_bundle_sha256": "5" * 64,
            "node_version": "22.14.0",
            "chromium_version": "128.0.6613.84",
            "chromium_build_id": "chromium-128.0.6613.84",
            "ffmpeg_version": "7.1.0",
            "ffprobe_version": "7.1.0",
            "hyperframes_version": "0.7.84",
            "gsap_version": "3.15.0",
            "os_name": "windows",
            "os_version": "11.0.26100",
            "architecture": "x86_64",
            "locale": "en_US.UTF-8",
            "timezone": "UTC",
        }
        self.assertEqual(dict(self.render_result(environment=allowed).environment), allowed)

        for name in (
            "COOKIE",
            "AUTH_HEADER",
            "HMAC_KEY",
            "API_KEY",
            "PASSWORD",
            "PATH",
        ):
            with self.subTest(name=name):
                secret = "do-not-leak-environment-value"
                with self.assertRaises(ValueError) as caught:
                    self.render_result(environment={name: secret})
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, repr(caught.exception))

        secret_values = (
            ("aws_access", "AK" + "IA" + "IOSFODNN7EXAMPLE"),
            ("aws_session", "AS" + "IA" + "IOSFODNN7EXAMPLE"),
            ("aws_legacy", "A3" + "T" + "IOSFODNN7EXAMPLE"),
            ("github_classic", "gh" + "p_" + "exampletokenvalue"),
            ("github_fine_grained", "github_" + "pat_" + "exampletokenvalue"),
            ("slack_bot", "xox" + "b-" + "example-token-value"),
            ("slack_user", "xox" + "p-" + "example-token-value"),
            (
                "jwt",
                "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
            ),
            ("bearer", "Bearer " + "example-token-value"),
            ("basic", "Basic " + "ZXhhbXBsZTpleGFtcGxl"),
            ("cookie_header", "Cookie: session=" + "example-cookie-value"),
            ("cookie_fragment", "session=" + "example-cookie-value; csrf=example"),
            ("control", "bad\x00value"),
        )

        for name in allowed:
            for secret_kind, value in secret_values:
                with self.subTest(name=name, secret_kind=secret_kind):
                    with self.assertRaises(ValueError) as caught:
                        self.render_result(environment={name: value})
                    self.assertNotIn(value, str(caught.exception))
                    self.assertNotIn(value, repr(caught.exception))

    def test_render_result_freezes_environment_performance_and_snapshots(self):
        environment = {"node_version": "22.14.0"}
        performance = {"elapsed_ms": 10}
        snapshots = ["snapshots/0001.png"]
        result = self.render_result(
            environment=environment,
            performance=performance,
            snapshots=snapshots,
        )
        environment["node_version"] = "changed"
        performance["elapsed_ms"] = 99
        snapshots.append("snapshots/0002.png")
        self.assertEqual(dict(result.environment), {"node_version": "22.14.0"})
        self.assertEqual(dict(result.performance), {"elapsed_ms": 10})
        self.assertEqual(result.snapshots, ("snapshots/0001.png",))
        with self.assertRaises(TypeError):
            result.environment["node_version"] = "changed"

    def test_render_result_repr_omits_legitimate_environment_evidence(self):
        environment = {
            "code_commit_sha": "6" * 40,
            "renderer_build_id": "renderer-20260801-abcdef012345",
        }
        result_repr = repr(self.render_result(environment=environment))
        self.assertNotIn("environment=", result_repr)
        for value in environment.values():
            self.assertNotIn(value, result_repr)

    def test_renderer_protocol_is_runtime_checkable(self):
        class FakeRenderer:
            def render(self, request: RenderRequest) -> RenderResult:
                return ProviderAndRendererContractTests.render_result()

        self.assertIsInstance(FakeRenderer(), Renderer)


class _ProbeFake:
    def __init__(
        self,
        *,
        available: bool = True,
        reason_code: str = "capability_ready",
        claims: dict[str, object] | None = None,
        probe_error: BaseException | None = None,
    ):
        self.available = available
        self.reason_code = reason_code
        self.claims = dict(claims or {})
        self.probe_error = probe_error
        self.probes: list[tuple[str, str | None]] = []
        self.mutations: list[str] = []

    def probe_capability(
        self, capability: str, *, environment: str | None
    ) -> dict[str, object]:
        self.probes.append((capability, environment))
        if self.probe_error is not None:
            raise self.probe_error
        return {
            "available": self.available,
            "reason_code": self.reason_code,
            **self.claims,
        }

    def _mutating(self, name: str) -> None:
        self.mutations.append(name)
        raise AssertionError(f"preflight invoked mutating method: {name}")

    def now(self) -> float:
        self._mutating("now")
        return 0.0

    def terminate_job(self, job_id: str) -> None:
        self._mutating("terminate_job")

    def deduct(self, *args: object, **kwargs: object) -> object:
        self._mutating("deduct")

    def refund(self, *args: object, **kwargs: object) -> object:
        self._mutating("refund")

    def query_transaction(self, *args: object, **kwargs: object) -> object:
        self._mutating("query_transaction")

    def register_generation(self, *args: object, **kwargs: object) -> object:
        self._mutating("register_generation")

    def prepare_hidden(self, *args: object, **kwargs: object) -> object:
        self._mutating("prepare_hidden")

    def commit_publish(self, *args: object, **kwargs: object) -> object:
        self._mutating("commit_publish")

    def cancel_publish(self, *args: object, **kwargs: object) -> object:
        self._mutating("cancel_publish")

    def query_decision(self, *args: object, **kwargs: object) -> object:
        self._mutating("query_decision")

    def submit(self, *args: object, **kwargs: object) -> object:
        self._mutating("submit")

    def render(self, request: RenderRequest) -> RenderResult:
        self._mutating("render")
        raise AssertionError("unreachable")


class _StageProbe(_ProbeFake):
    def __call__(self, job: object, context: StageContext) -> StageOutcome:
        self._mutating("stage_handler")
        raise AssertionError("unreachable")


class RuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def enabled_env(self, environment: str = "test") -> dict[str, str]:
        return {
            "AI_EDIT_V3_ENABLED": "1",
            "AI_EDIT_V3_DB_PATH": os.fspath(self.root / "v3.db"),
            "AI_EDIT_V2_DB": os.fspath(self.root / "v2.db"),
            "AI_EDIT_V3_ENVIRONMENT": environment,
            "AI_EDIT_V3_OWNER_HMAC_SECRET_FILE": os.fspath(
                self.root / "owner-hmac.secret"
            ),
            "AI_EDIT_V3_WORKER_CONCURRENCY": "2",
            "AI_EDIT_V3_QUEUE_CAPACITY": "8",
            "AI_EDIT_V3_TEMP_BYTES_LIMIT": "1048576",
        }

    def dependencies(
        self,
        *,
        director: object = ...,
        cos: object = ...,
        stage_handlers: dict[str, object] | None = None,
    ) -> tuple[RuntimeDependencies, list[_ProbeFake]]:
        probes = [_ProbeFake() for _ in range(11)]
        (
            store,
            clock,
            points,
            assets,
            default_cos,
            tts,
            asr,
            default_director,
            image_generator,
            audio_generator,
            renderer,
        ) = probes
        supervisor = _ProbeFake()
        handlers = stage_handlers or {"planning": _StageProbe()}
        actual_probes = [
            store,
            clock,
            points,
            assets,
            default_cos if cos is ... else cos,
            tts,
            asr,
            default_director if director is ... else director,
            image_generator,
            audio_generator,
            renderer,
            supervisor,
            *handlers.values(),
        ]
        dependencies = RuntimeDependencies(
            store=store,
            clock=clock,
            points=points,
            assets=assets,
            cos=default_cos if cos is ... else cos,
            tts=tts,
            asr=asr,
            director=default_director if director is ... else director,
            image_generator=image_generator,
            audio_generator=audio_generator,
            renderer=renderer,
            process_supervisor=supervisor,
            stage_handlers=handlers,
        )
        return dependencies, [item for item in actual_probes if isinstance(item, _ProbeFake)]

    def test_stage_and_runtime_dependencies_have_exact_frozen_fields(self):
        self.assertEqual(
            tuple(field.name for field in fields(StageContext)),
            (
                "claim",
                "attempt_id",
                "stage_attempt_id",
                "deadline_at",
                "assert_active",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(StageOutcome)),
            (
                "next_state",
                "checkpoint",
                "checkpoint_input_sha256",
                "provider_result",
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(RuntimeDependencies)),
            (
                "store",
                "clock",
                "points",
                "assets",
                "cos",
                "tts",
                "asr",
                "director",
                "image_generator",
                "audio_generator",
                "renderer",
                "process_supervisor",
                "stage_handlers",
            ),
        )
        dependencies, _probes = self.dependencies()
        with self.assertRaises(FrozenInstanceError):
            dependencies.director = None
        with self.assertRaises(TypeError):
            dependencies.stage_handlers["rendering"] = _StageProbe()

    def test_stage_context_and_outcome_are_validated_and_deeply_immutable(self):
        claim = LeaseClaim("job", "worker", 1, 1000)
        context = StageContext(claim, "attempt", "stage-attempt", 123.5, lambda: None)
        checkpoint = {"nested": {"items": ["one"]}}
        outcome = StageOutcome("planning", checkpoint, "c" * 64)
        checkpoint["nested"]["items"].append("two")
        self.assertEqual(outcome.checkpoint["nested"]["items"], ("one",))
        with self.assertRaises(TypeError):
            outcome.checkpoint["new"] = "value"
        with self.assertRaises(ValueError):
            StageContext(claim, "attempt", "stage-attempt", float("nan"), lambda: None)
        with self.assertRaises(ValueError):
            StageOutcome("planning", {}, "not-a-sha")

    def test_runtime_identifiers_reject_all_c0_and_c1_controls(self):
        claim = LeaseClaim("job", "worker", 1, 1000)
        for field_name in ("attempt_id", "stage_attempt_id"):
            for codepoint in (0x00, 0x1F, 0x7F, 0x85, 0x9F):
                with self.subTest(field_name=field_name, codepoint=codepoint):
                    values = {
                        "attempt_id": "attempt",
                        "stage_attempt_id": "stage-attempt",
                    }
                    values[field_name] = f"bad{chr(codepoint)}identifier"
                    with self.assertRaises(ValueError):
                        StageContext(
                            claim,
                            values["attempt_id"],
                            values["stage_attempt_id"],
                            10.0,
                            lambda: None,
                        )
        for codepoint in (0x00, 0x1F, 0x7F, 0x85, 0x9F):
            with self.subTest(next_state_codepoint=codepoint):
                with self.assertRaises(ValueError):
                    StageOutcome(f"bad{chr(codepoint)}state", {}, "f" * 64)

    def test_stage_checkpoint_rejects_non_json_mutable_leaves_and_non_string_keys(self):
        class MutableLeaf:
            pass

        for checkpoint in (
            {"raw": bytearray(b"mutable")},
            {"custom": MutableLeaf()},
            {1: "non-string-key"},
        ):
            with self.subTest(checkpoint=checkpoint):
                with self.assertRaises(ValueError):
                    StageOutcome("planning", checkpoint, "e" * 64)

    def test_runtime_protocols_are_runtime_checkable(self):
        class FakeClock:
            def now(self) -> float:
                return 1.0

        class FakeSupervisor:
            def terminate_job(self, job_id: str) -> None:
                return None

        def handler(job: object, context: StageContext) -> StageOutcome:
            return StageOutcome("planning", {}, "d" * 64)

        self.assertIsInstance(FakeClock(), Clock)
        self.assertIsInstance(FakeSupervisor(), ProcessSupervisor)
        self.assertIsInstance(handler, StageHandler)

    def test_disabled_runtime_allows_reads_but_rejects_writes(self):
        report = preflight(build_runtime(env={"AI_EDIT_V3_ENABLED": "0"}))
        self.assertTrue(report.allows_existing_reads)
        self.assertFalse(report.accepts_uploads)
        self.assertFalse(report.accepts_new_jobs)
        self.assertEqual(report.items["feature_enabled"].reason_code, "feature_disabled")
        self.assertEqual(
            report.items["content_safety"].reason_code,
            "content_safety_not_implemented",
        )

    def test_environment_variable_without_wiring_is_not_ready(self):
        dependencies, _probes = self.dependencies(director=None)
        env = self.enabled_env()
        env["DASHSCOPE_API_KEY"] = "configured-but-not-wired"
        report = preflight(build_runtime(dependencies, env=env))
        self.assertEqual(report.items["director"].status, "missing_or_unavailable")
        self.assertEqual(report.items["director"].reason_code, "capability_not_injected")
        self.assertFalse(report.accepts_new_jobs)

    def test_non_none_object_without_probe_or_explicit_contract_is_not_wired(self):
        dependencies, _probes = self.dependencies(director=object())
        report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertEqual(
            report.items["director"].reason_code,
            "capability_contract_missing",
        )
        self.assertFalse(report.accepts_new_jobs)

    def test_fully_injected_runtime_is_ready_without_mutating_dependencies(self):
        dependencies, probes = self.dependencies()
        report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertTrue(report.allows_existing_reads)
        self.assertTrue(report.accepts_uploads)
        self.assertTrue(report.accepts_new_jobs)
        self.assertEqual(report.items["director"].status, "configured_and_wired")
        self.assertEqual(report.items["isolated_v3_store"].status, "configured_and_wired")
        self.assertTrue(all(not probe.mutations for probe in probes))
        self.assertTrue(all(probe.probes for probe in probes))

    def test_probe_failure_is_fail_closed_and_never_leaks_exception_text(self):
        secret = "provider-secret-value"
        dependencies, _probes = self.dependencies(
            director=_ProbeFake(probe_error=RuntimeError(secret))
        )
        report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        item = report.items["director"]
        self.assertEqual(item.reason_code, "capability_probe_failed")
        self.assertNotIn(secret, item.detail)
        self.assertNotIn(secret, repr(report))
        self.assertFalse(report.accepts_new_jobs)

    def test_runtime_errors_are_sanitized_but_process_control_exceptions_propagate(self):
        dependencies, _probes = self.dependencies(
            director=_ProbeFake(probe_error=RuntimeError("secret-runtime-error"))
        )
        report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertEqual(report.items["director"].reason_code, "capability_probe_failed")

        dependencies, _probes = self.dependencies()
        schema_secret = "secret-schema-runtime-error"
        with mock.patch(
            "server.content_domains.ai_edit_v3.runtime.schema_sha256",
            side_effect=RuntimeError(schema_secret),
        ):
            report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        schema_item = report.items["schema:edit-plan-2.0.schema.json"]
        self.assertEqual(schema_item.reason_code, "schema_unavailable")
        self.assertNotIn(schema_secret, repr(report))

        for exception in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(probe_exception=type(exception).__name__):
                dependencies, _probes = self.dependencies(
                    director=_ProbeFake(probe_error=exception)
                )
                with self.assertRaises(type(exception)):
                    preflight(build_runtime(dependencies, env=self.enabled_env()))

        for exception in (KeyboardInterrupt(), SystemExit(8)):
            with self.subTest(schema_exception=type(exception).__name__):
                dependencies, _probes = self.dependencies()
                with mock.patch(
                    "server.content_domains.ai_edit_v3.runtime.schema_sha256",
                    side_effect=exception,
                ):
                    with self.assertRaises(type(exception)):
                        preflight(build_runtime(dependencies, env=self.enabled_env()))

    def test_interface_and_native_store_checks_do_not_swallow_process_control(self):
        class ExplodingInterface:
            def __init__(self, exception: BaseException):
                self.exception = exception

            def __getattribute__(self, name: str) -> object:
                if name == "deduct":
                    raise object.__getattribute__(self, "exception")
                return object.__getattribute__(self, name)

        base_dependencies, _probes = self.dependencies()
        interface_secret = "secret-interface-runtime-error"
        dependencies = replace(
            base_dependencies,
            points=ExplodingInterface(RuntimeError(interface_secret)),
        )
        report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertEqual(
            report.items["points_transaction_query"].reason_code,
            "capability_interface_invalid",
        )
        self.assertNotIn(interface_secret, repr(report))
        for exception in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(interface_exception=type(exception).__name__):
                dependencies = replace(
                    base_dependencies,
                    points=ExplodingInterface(exception),
                )
                with self.assertRaises(type(exception)):
                    preflight(build_runtime(dependencies, env=self.enabled_env()))

        native_store = object.__new__(V3Store)
        native_store.db_path = self.root / "v3.db"
        native_store.v2_db_path = self.root / "v2.db"
        dependencies = replace(base_dependencies, store=native_store)
        store_secret = "secret-native-store-runtime-error"
        with mock.patch(
            "server.content_domains.ai_edit_v3.runtime.assert_isolated_db",
            side_effect=RuntimeError(store_secret),
        ):
            report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertEqual(
            report.items["isolated_v3_store"].reason_code,
            "v3_store_isolation_unavailable",
        )
        self.assertNotIn(store_secret, repr(report))
        for exception in (KeyboardInterrupt(), SystemExit(10)):
            with self.subTest(store_exception=type(exception).__name__):
                with mock.patch(
                    "server.content_domains.ai_edit_v3.runtime.assert_isolated_db",
                    side_effect=exception,
                ):
                    with self.assertRaises(type(exception)):
                        preflight(build_runtime(dependencies, env=self.enabled_env()))

    def test_capability_items_and_reports_are_strictly_immutable(self):
        with self.assertRaises(ValueError):
            CapabilityItem("ready", "bad_status", "bad")
        items = {"feature": CapabilityItem("implemented", "capability_ready", "ready")}
        versions = {"python": "3.12"}
        report = CapabilityReport(items, versions, True, False, False)
        items["feature"] = CapabilityItem(
            "missing_or_unavailable", "changed", "changed"
        )
        versions["python"] = "changed"
        self.assertEqual(report.items["feature"].status, "implemented")
        self.assertEqual(report.runtime_versions["python"], "3.12")
        with self.assertRaises(TypeError):
            report.items["new"] = report.items["feature"]
        with self.assertRaises(TypeError):
            report.runtime_versions["python"] = "changed"

    def test_runtime_versions_include_exact_jsonschema_and_all_schema_hashes(self):
        dependencies, _probes = self.dependencies()
        report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertEqual(report.runtime_versions["jsonschema"], "4.26.0")
        self.assertIn("python", report.runtime_versions)
        self.assertIn("sqlite", report.runtime_versions)
        self.assertEqual(
            report.runtime_versions["edit-plan-2.0.schema.json"],
            "b96c059fa2e4ef7d91cd48278b474d61a34606f1cbce6963c3b65fa66f7d046c",
        )
        self.assertEqual(
            report.runtime_versions["render-manifest-v1.schema.json"],
            "a61ab87058918ee2cdaa778b690a09f3f5796c01ee343a699bf5fbe83435c54d",
        )
        self.assertEqual(
            report.runtime_versions["quality-verdict-v1.schema.json"],
            "33d35a1c858c03a9a96309b334ec9c3fb2076a4fbff179221930dd78c83f066e",
        )

    def test_schema_hash_mismatch_is_fail_closed(self):
        dependencies, _probes = self.dependencies()
        with mock.patch(
            "server.content_domains.ai_edit_v3.runtime.schema_sha256",
            return_value="0" * 64,
        ):
            report = preflight(build_runtime(dependencies, env=self.enabled_env()))
        self.assertFalse(report.accepts_uploads)
        self.assertFalse(report.accepts_new_jobs)
        self.assertEqual(
            report.items["schema:edit-plan-2.0.schema.json"].reason_code,
            "schema_hash_mismatch",
        )

    def test_cos_environment_separation_is_fail_closed(self):
        test_cos = _ProbeFake(claims={"production_prefix_writable": True})
        dependencies, _probes = self.dependencies(cos=test_cos)
        report = preflight(build_runtime(dependencies, env=self.enabled_env("test")))
        self.assertEqual(report.items["cos"].reason_code, "cos_environment_scope_invalid")
        self.assertFalse(report.accepts_uploads)

        production_cos = _ProbeFake(claims={"test_only": True})
        dependencies, _probes = self.dependencies(cos=production_cos)
        report = preflight(
            build_runtime(dependencies, env=self.enabled_env("production"))
        )
        self.assertEqual(report.items["cos"].reason_code, "cos_environment_scope_invalid")
        self.assertFalse(report.accepts_new_jobs)

    def test_cos_authority_claims_must_use_strict_boolean_values(self):
        for environment, claims in (
            ("test", {"production_prefix_writable": "false"}),
            ("production", {"test_only": "false"}),
        ):
            with self.subTest(environment=environment):
                dependencies, _probes = self.dependencies(
                    cos=_ProbeFake(claims=claims)
                )
                report = preflight(
                    build_runtime(
                        dependencies,
                        env=self.enabled_env(environment),
                    )
                )
                self.assertEqual(
                    report.items["cos"].reason_code,
                    "capability_probe_invalid",
                )
                self.assertFalse(report.accepts_uploads)

    def test_request_specific_stage_handler_requirements_are_enforced(self):
        dependencies, _probes = self.dependencies(stage_handlers={"planning": _StageProbe()})
        runtime = build_runtime(dependencies, env=self.enabled_env())
        report = preflight(
            runtime,
            required_capabilities=("director", "renderer", "stage_handler:rendering"),
        )
        self.assertEqual(
            report.items["stage_handler:rendering"].reason_code,
            "capability_not_injected",
        )
        self.assertFalse(report.accepts_new_jobs)

        dependencies, _probes = self.dependencies(
            stage_handlers={"rendering": _StageProbe()}
        )
        report = preflight(
            build_runtime(dependencies, env=self.enabled_env()),
            required_capabilities=("director", "renderer", "stage_handler:rendering"),
        )
        self.assertTrue(report.accepts_new_jobs)

    def test_assert_ready_raises_reason_codes_only_and_has_no_side_effects(self):
        secret = "must-not-leak"
        dependencies, probes = self.dependencies(
            director=_ProbeFake(probe_error=RuntimeError(secret))
        )
        runtime = build_runtime(dependencies, env=self.enabled_env())
        with self.assertRaises(CapabilityUnavailable) as caught:
            assert_ready_for_request(runtime)
        self.assertEqual(caught.exception.error_code, "capability_unavailable")
        self.assertIn("capability_probe_failed", caught.exception.reason_codes)
        self.assertNotIn(secret, str(caught.exception))
        self.assertTrue(all(not probe.mutations for probe in probes))


if __name__ == "__main__":
    unittest.main()
