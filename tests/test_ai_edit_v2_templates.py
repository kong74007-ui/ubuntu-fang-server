import unittest

from server.content_domains.ai_edit_v2_templates import (
    TemplateError,
    get_published_template,
    list_published_templates,
)


def nested_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from nested_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from nested_keys(child)


class TemplateTests(unittest.TestCase):
    def test_template_fixes_visual_language_not_scene_content_or_coordinates(self):
        template = get_published_template("business_diagnostic")

        self.assertEqual(template["status"], "published")
        for field in (
            "component_family",
            "typography",
            "palette_relationships",
            "motion_intensity",
            "sound_policy",
        ):
            self.assertIn(field, template)
        keys = set(nested_keys(template))
        for forbidden in (
            "fixed_scenes",
            "scenes",
            "headline",
            "headlines",
            "material_coordinates",
            "x",
            "y",
            "width",
            "height",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, keys)

    def test_catalog_returns_defensive_copies_and_published_versions_only(self):
        templates = list_published_templates()
        self.assertGreaterEqual(len(templates), 2)
        self.assertTrue(all(item["status"] == "published" for item in templates))
        self.assertEqual(
            [(item["id"], item["version"]) for item in templates],
            sorted((item["id"], item["version"]) for item in templates),
        )

        templates[0]["typography"]["heading"] = "mutated"
        fresh = get_published_template(templates[0]["id"], templates[0]["version"])
        self.assertNotEqual(fresh["typography"]["heading"], "mutated")

    def test_unknown_or_unpublished_version_fails_closed(self):
        for template_id, version in (("missing", None), ("business_diagnostic", "99.0")):
            with self.subTest(template_id=template_id, version=version), self.assertRaises(
                TemplateError
            ):
                get_published_template(template_id, version)


if __name__ == "__main__":
    unittest.main()
