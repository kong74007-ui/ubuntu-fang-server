import importlib.metadata
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


REQUIREMENTS = Path(__file__).resolve().parents[1] / "deploy" / "requirements-ai-edit-v3.txt"
EXPECTED = (
    "attrs==25.4.0",
    "jsonschema==4.26.0",
    "jsonschema-specifications==2025.9.1",
    "referencing==0.37.0",
    "rpds-py==0.30.0",
    'typing-extensions==4.15.0; python_version < "3.13"',
)


class V3DependencyManifestTests(unittest.TestCase):
    def test_v3_dependency_file_is_a_complete_exact_pin_set(self):
        lines = tuple(
            line.strip()
            for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertEqual(lines, EXPECTED)
        self.assertFalse(any(">=" in line or "~=" in line for line in lines))

    def test_jsonschema_runtime_is_exact_and_supports_draft_2020_12(self):
        self.assertEqual(importlib.metadata.version("jsonschema"), "4.26.0")
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
        Draft202012Validator.check_schema(schema)
