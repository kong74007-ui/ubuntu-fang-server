"""Fixed public-template color palette guard.

These tests cover the ``apply-public-template-palettes.py`` contract: the 20
public templates in production order, the exact palette mapping, and the hard
rule that the injected overlay may only ever touch color properties.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPLIER = ROOT / "deploy/matrix-template-video/apply-public-template-palettes.py"

# 20 public templates, production order. (id, kind, variant, c1, c2, c3)
EXPECTED_ORDER = (
    ("ref-01-chengdu-green-brush", "reference", "v01", "#fffbea", "#470125", "#fffbea"),
    ("ref-02-shenzhen-ai-orange", "reference", "v02", "#ff043a", "#002169", "#fffbea"),
    ("ref-03-zhengzhou-blue-banner", "reference", "v03", "#12f2fd", "#690dad", "#fffbea"),
    ("ref-04-foshan-yellow-strip", "reference", "v04", "#fffbea", "#47176d", "#fffbea"),
    ("ref-05-changsha-white-red", "reference", "v05", "#f0ff0c", "#ff0086", "#fffbea"),
    ("ref-06-guangzhou-yellow-button", "reference", "v06", "#fffeee", "#4e8966", "#fffeee"),
    ("ref-07-shenzhen-red-growth", "reference", "v07", "#fe019a", "#101820", "#fffbea"),
    ("ref-08-puyang-yellow-white", "reference", "v08", "#fdeb55", "#185a56", "#fffbea"),
    ("ref-09-urumqi-soft-brush", "reference", "v09", "#fade1f", "#690dad", "#fffbea"),
    ("ref-10-shenzhen-sisters", "reference", "v10", "#ff6047", "#000035", "#fffbea"),
    ("ref-11-nansha-clean", "reference", "v11", "#12f2fd", "#e4058e", "#fffbea"),
    ("ref-12-guangzhou-brush", "reference", "v12", "#ffff00", "#002fa7", "#fffbea"),
    ("ref-13-shenzhen-green-location", "reference", "v13", "#00e592", "#da2357", "#fffbea"),
    ("ref-14-karamay-green", "reference", "v14", "#fffbea", "#0f64b5", "#fffbea"),
    ("ref-15-tianjin-monochrome", "reference", "v15", "#08fc2e", "#101820", "#fffbea"),
    ("ref-16-shenzhen-opc", "reference", "v16", "#fd742d", "#01008a", "#fffbea"),
    ("ref-17-shenzhen-yellow-red", "reference", "v17", "#fbfff2", "#008e6b", "#fbfff2"),
    ("nine-grid-reveal", "nine-grid", "", "#cca4e3", "#6583e0", "#fffbea"),
    ("triple-strip-shutter", "triple-strip", "", "#f2e1ff", "#61ac4c", "#fffbea"),
    ("yellow-banner-zoom", "yellow-banner", "", "#90e0d6", "#e97a46", "#fffbea"),
)

ALLOWED_PROPERTIES = {
    "color",
    "background-color",
    "background",
    "border-color",
    "outline-color",
    "text-decoration-color",
    "-webkit-text-stroke-color",
    "text-shadow",
    "box-shadow",
    "fill",
    "stroke",
}
HEX_RE = re.compile(r"^#[0-9a-f]{6}$")


def load_applier():
    spec = importlib.util.spec_from_file_location(
        "apply_public_template_palettes", APPLIER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declarations(css_block: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"([a-zA-Z-]+)\s*:\s*([^;{}]+);", css_block):
        found.setdefault(match.group(1).lower(), []).append(match.group(2).strip())
    return found


def fixture(kind: str) -> str:
    if kind == "reference":
        layers = "".join(
            f'<div id="{layer}" class="{layer}"></div>'
            for layer in ("top1", "top2", "top3", "bottom1", "bottom2")
        )
        return f'<html><head></head><body><div id="root">{layers}</div></body></html>'
    if kind == "nine-grid":
        return '<html><head></head><body><div id="headline">a</div><div id="tagline">b</div></body></html>'
    if kind == "triple-strip":
        return (
            '<html><head></head><body>'
            '<p class="title"></p><p class="subtitle"></p>'
            '<p class="footer-text"></p>'
            '<div class="underline"><span class="line"></span>'
            '<span class="slash"></span></div>'
            '<div class="chevron"><svg><path fill="none"></path>'
            '<path fill="#23d5ff"></path></svg></div>'
            '</body></html>'
        )
    return '<html><head></head><body><div class="banner"><h1 id="title"></h1></div><p class="subtitle"></p><p id="body"></p></body></html>'


class PublicTemplatePaletteDataTests(unittest.TestCase):
    def setUp(self):
        self.module = load_applier()

    def test_twenty_templates_in_production_order(self):
        palettes = self.module.PALETTES
        self.assertEqual(20, len(palettes))
        self.assertEqual(
            [item[0] for item in EXPECTED_ORDER],
            [item[0] for item in palettes],
        )
        self.assertEqual("reference", palettes[0][2])
        self.assertEqual("reference", palettes[16][2])
        self.assertEqual("nine-grid", palettes[17][2])
        self.assertEqual("triple-strip", palettes[18][2])
        self.assertEqual("yellow-banner", palettes[19][2])

    def test_ids_unique(self):
        ids = [item[0] for item in self.module.PALETTES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_role_is_valid_hex(self):
        for _tid, _name, _kind, _variant, c1, c2, c3 in self.module.PALETTES:
            for color in (c1, c2, c3):
                self.assertRegex(color, HEX_RE, color)

    def test_legacy_recovery_templates_absent(self):
        ids = {item[0] for item in self.module.PALETTES}
        self.assertNotIn("full-overlay-bold", ids)
        self.assertNotIn("poster-split", ids)

    def test_palette_values_match_spec(self):
        self.assertEqual(len(EXPECTED_ORDER), len(self.module.PALETTES))
        for expected, actual in zip(EXPECTED_ORDER, self.module.PALETTES):
            _tid, name, kind, variant, c1, c2, c3 = actual
            self.assertEqual(expected[0], actual[0])
            self.assertEqual(expected[1], kind)
            self.assertEqual(expected[2], variant)
            self.assertEqual(expected[3], c1)
            self.assertEqual(expected[4], c2)
            self.assertEqual(expected[5], c3)

    def test_outline_is_fixed_dark(self):
        self.assertEqual("#080808", self.module.OUTLINE)


class PublicTemplatePaletteApplyTests(unittest.TestCase):
    def setUp(self):
        self.module = load_applier()

    def test_inject_adds_single_style_block(self):
        for kind in ("reference", "nine-grid", "triple-strip", "yellow-banner"):
            with self.subTest(kind=kind):
                updated = self.module.inject(fixture(kind), kind)
                self.assertEqual(
                    1, updated.count(f'id="{self.module.STYLE_ID}"')
                )
                self.assertTrue(updated.rstrip().endswith("</html>"))
                self.module.assert_written(updated, kind)

    def test_double_injection_fails(self):
        once = self.module.inject(fixture("nine-grid"), "nine-grid")
        with self.assertRaises(ValueError):
            self.module.inject(once, "nine-grid")

    def test_missing_head_fails(self):
        with self.assertRaises(ValueError):
            self.module.inject(
                '<html><body><div id="headline"></div><div id="tagline"></div></body></html>',
                "nine-grid",
            )

    def test_multiple_heads_fail(self):
        html = '<html><head></head><head></head><body><div id="headline"></div><div id="tagline"></div></body></html>'
        with self.assertRaises(ValueError):
            self.module.inject(html, "nine-grid")

    def test_missing_selector_fails(self):
        html = '<html><head></head><body><div id="headline"></div></body></html>'
        with self.assertRaises(ValueError):
            self.module.inject(html, "nine-grid")

    def test_injected_block_uses_only_allowed_properties(self):
        for kind in ("reference", "nine-grid", "triple-strip", "yellow-banner"):
            with self.subTest(kind=kind):
                block = self.module.style_block(kind)
                for name in declarations(block):
                    self.assertIn(name, ALLOWED_PROPERTIES, name)

    def test_injected_block_has_no_banned_properties(self):
        for kind in ("reference", "nine-grid", "triple-strip", "yellow-banner"):
            with self.subTest(kind=kind):
                block = self.module.style_block(kind)
                self.assertIsNone(self.module.BANNED_RE.search(block))

    def test_text_shadow_offsets_preserved(self):
        nine = self.module.style_block("nine-grid")
        self.assertIn("text-shadow: 5px 6px 3px #6583e0;", nine)
        self.assertIn("text-shadow: 3px 4px 2px #6583e0;", nine)
        triple = self.module.style_block("triple-strip")
        self.assertIn(
            "text-shadow: -4px -3px 0 #61ac4c, 5px 5px 0 #f2e1ff, 11px 9px 0 #101820;",
            triple,
        )
        self.assertIn(
            "text-shadow: -3px -3px 0 #61ac4c, 4px 4px 0 #f2e1ff, 7px 6px 0 #101820;",
            triple,
        )

    def test_body_panel_alpha_preserved(self):
        banner = self.module.style_block("yellow-banner")
        self.assertIn(
            "background-color: rgba(16, 24, 32, 0.42);", banner
        )

    def test_reference_v05_box_shadow_only_neutralised(self):
        reference = self.module.style_block("reference")
        self.assertIn(
            '#root[class~="v05"] .bottom2 { box-shadow: 0 10px 0 rgba(8, 8, 8, 0.85), '
            "0 15px 24px rgba(0, 0, 0, 0.35); }",
            reference,
        )
        self.assertNotIn("17, 36, 29", reference)

    def test_reference_overlay_avoids_variant_layer_detection(self):
        # The service detects a present layer with r"\.vNN\s+\.layer\s*(?:,|\{)".
        # The overlay must never introduce such a fragment; otherwise a two-layer
        # variant is mis-detected as having top3 and startup fails with
        # "HyperFrames reference template font size is missing".
        reference = self.module.style_block("reference")
        self.assertNotIn("#root.v", reference)
        for variant in (f"v{i:02d}" for i in range(1, 18)):
            for layer in ("top1", "top2", "top3", "bottom1", "bottom2"):
                pattern = rf"\.{variant}\s+\.{layer}\s*(?:,|\{{)"
                self.assertIsNone(
                    re.search(pattern, reference),
                    f"overlay shadows {variant} {layer} detection",
                )
        self.assertIn('#root[class~="v02"] .top3', reference)

    def test_triple_strip_decorations_have_no_old_colors_left(self):
        triple = self.module.style_block("triple-strip")
        # The base template's :last-child slash and the filled chevron keep
        # their old blue unless these higher-specificity rules exist.
        self.assertIn(
            ".underline .slash:last-child {\n  background-color: #61ac4c;\n}",
            triple,
        )
        self.assertIn(
            '.chevron path:not([fill="none"]) {\n  fill: #61ac4c;\n}',
            triple,
        )
        self.assertNotIn("#4d63ec", triple)
        self.assertNotIn("#23d5ff", triple)
        self.assertNotIn("#42d8ff", triple)


GATE = ROOT / "scripts/verify-public-template-palettes.py"


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "verify_public_template_palettes", GATE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def element(selector, index, style=None, shadows=None):
    return {
        "selector": selector, "index": index,
        "style": dict(style or {}), "colors": {}, "shadows": dict(shadows or {}),
    }


class ShadowParserTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()

    def equal(self, before, after):
        return self.gate.shadow_geometry_equal(before, after)[0]

    def test_rgb_only_change_passes(self):
        self.assertTrue(self.equal(
            "rgba(17, 36, 29, 0.85) 0px 10px 0px 0px, "
            "rgba(0, 0, 0, 0.35) 0px 15px 24px 0px",
            "rgba(8, 8, 8, 0.85) 0px 10px 0px 0px, "
            "rgba(0, 0, 0, 0.35) 0px 15px 24px 0px",
        ))

    def test_offset_x_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 0px 10px 2px", "rgb(0, 0, 0) 5px 10px 2px"))

    def test_offset_y_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 0px 10px 2px", "rgb(0, 0, 0) 0px 11px 2px"))

    def test_blur_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 0px 10px 2px", "rgb(0, 0, 0) 0px 10px 3px"))

    def test_spread_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 0px 10px 2px 1px", "rgb(0, 0, 0) 0px 10px 2px 4px"))

    def test_layer_count_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 0px 10px 2px",
            "rgb(0, 0, 0) 0px 10px 2px, rgb(0, 0, 0) 1px 1px 1px"))

    def test_layer_order_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 1px 1px 1px, rgb(0, 0, 0) 2px 2px 2px",
            "rgb(0, 0, 0) 2px 2px 2px, rgb(0, 0, 0) 1px 1px 1px"))

    def test_none_to_shadow_fails(self):
        self.assertFalse(self.equal("none", "rgb(0, 0, 0) 1px 1px 1px"))
        self.assertFalse(self.equal("rgb(0, 0, 0) 1px 1px 1px", "none"))

    def test_alpha_change_fails(self):
        self.assertFalse(self.equal(
            "rgba(0, 0, 0, 0.85) 0px 10px 0px 0px",
            "rgba(0, 0, 0, 0.50) 0px 10px 0px 0px"))

    def test_inset_change_fails(self):
        self.assertFalse(self.equal(
            "rgb(0, 0, 0) 0px 10px 2px", "rgb(0, 0, 0) 0px 10px 2px inset"))


class PaletteGateCompareTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()

    def test_colour_only_change_passes(self):
        before = [element("#top1", 0, {"fontSize": "70px"},
                          {"textShadow": "rgb(9, 9, 9) 5px 6px 3px"})]
        after = [element("#top1", 0, {"fontSize": "70px"},
                         {"textShadow": "rgb(101, 131, 224) 5px 6px 3px"})]
        self.assertEqual([], self.gate.compare(before, after, "t"))

    def test_wrong_element_count_fails(self):
        before = [element("#top1", 0)]
        after = [element("#top1", 0), element("#top2", 0)]
        self.assertTrue(self.gate.compare(before, after, "t"))

    def test_wrong_identity_fails(self):
        before = [element("#top1", 0)]
        after = [element("#top2", 0)]
        self.assertTrue(self.gate.compare(before, after, "t"))

    def test_layout_change_fails(self):
        before = [element("#top1", 0, {"fontSize": "70px"})]
        after = [element("#top1", 0, {"fontSize": "68px"})]
        self.assertTrue(self.gate.compare(before, after, "t"))

    def test_shadow_geometry_change_fails(self):
        before = [element("#top1", 0, {}, {"boxShadow": "rgb(0, 0, 0) 0px 10px 0px 0px"})]
        after = [element("#top1", 0, {}, {"boxShadow": "rgb(0, 0, 0) 0px 11px 0px 0px"})]
        self.assertTrue(self.gate.compare(before, after, "t"))


GATE = ROOT / "scripts/verify-public-template-palettes.py"
COMPAT = ROOT / "deploy/matrix-template-video/verify-reference-palette-compat.py"


def load_compat():
    spec = importlib.util.spec_from_file_location(
        "verify_reference_palette_compat", COMPAT,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOP3_VARIANTS = {1, 4, 5, 6, 7, 8, 10, 11, 12, 16, 17}
REFERENCE_LAYERS = ("top1", "top2", "top3", "bottom1", "bottom2")


def reference_fixture_html() -> str:
    """Minimal reference-typography-17 index.html with production CSS structure."""
    styles = [
        "* { box-sizing: border-box; }",
        ".top, .bottom { width: 100%; padding-left: 42px; padding-right: 42px; }",
        ".top1, .top2, .top3, .bottom1, .bottom2 { max-width: 996px; letter-spacing: .01em; }",
    ]
    for index in range(1, 18):
        variant = f"v{index:02d}"
        if variant == "v07":
            styles.extend((
                ".v07 .top1 { font-size: 118px; }",
                ".v07 .top2 { font-size: 82px; }",
                ".v07 .top3 { font-size: 51px; }",
                ".v07 .bottom1 { font-size: 57px; }",
                ".v07 .bottom2 { font-size: 86px; }",
            ))
            continue
        styles.extend((
            f".{variant} .top1 {{ font-size: 80px; }}",
            f".{variant} .top2 {{ font-size: 60px; }}",
            f".{variant} .bottom2 {{ font-size: 70px; }}",
        ))
        if index in TOP3_VARIANTS:
            styles.append(f".{variant} .top3 {{ font-size: 50px; }}")
    layers = "".join(
        f'<div id="{layer}" class="{layer}"></div>'
        for layer in REFERENCE_LAYERS
    )
    return (
        '<html><head><style>\n' + "\n".join(styles) + "\n</style></head>"
        '<body><div id="root"><section id="typography" class="clip" '
        'data-start="0">' + layers + "</section></div></body></html>"
    )


def old_selector_overlay(css: str) -> str:
    """Turn the current `#root[class~="vNN"] .layer` overlay back into the
    pre-#186 `#root.vNN .layer` form that shadowed layer detection."""
    return css.replace('[class~="', ".").replace('"] ', " ")


class ReferencePaletteIntegrationTests(unittest.TestCase):
    """Drive the REAL production parser against the palette-injected pack."""

    def setUp(self):
        self.applier = load_applier()
        sys.path.insert(0, str(ROOT))
        from server import matrix_template_api as matrix
        self.matrix = matrix

    def test_paletted_reference_pack_parses_in_production_parser(self):
        html = self.applier.inject(reference_fixture_html(), "reference")
        audit = self.matrix.reference_pack_layer_audit(html)
        self.assertEqual(17, audit["templates"])
        self.assertEqual({"2": 6, "3": 10, "4": 1}, audit["top_layer_counts"])
        self.assertEqual(63, len(audit["font_sizes"]))
        for key, size in audit["font_sizes"].items():
            self.assertIsInstance(size, int)
            self.assertTrue(8 <= size <= 240, key)

    def test_two_layer_variants_do_not_gain_top3_from_overlay(self):
        html = self.applier.inject(reference_fixture_html(), "reference")
        for variant in ("v02", "v03", "v09", "v13", "v14", "v15"):
            self.assertFalse(
                self.matrix._reference_variant_has_layer(html, variant, "top3"),
                variant,
            )

    def test_old_selector_overlay_reproduces_startup_failure(self):
        html = reference_fixture_html()
        block = old_selector_overlay(self.applier.style_block("reference"))
        html = html.replace("</head>", block + "</head>", 1)
        with self.assertRaisesRegex(
            self.matrix.MatrixTemplateError, "font size is missing",
        ):
            self.matrix.reference_pack_layer_audit(html)


class CompatibilityScriptTests(unittest.TestCase):
    def setUp(self):
        self.applier = load_applier()
        self._tmp = tempfile.TemporaryDirectory()
        self.pack = Path(self._tmp.name) / "reference-typography-17"
        self.pack.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_pack(self, css_block: str) -> None:
        html = reference_fixture_html().replace(
            "</head>", css_block + "</head>", 1,
        )
        (self.pack / "index.html").write_text(html, encoding="utf-8")

    def run_compat(self) -> int:
        done = subprocess.run(
            [sys.executable, str(COMPAT), "--pack-root", str(self.pack)],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        return done.returncode

    def test_compat_script_passes_on_current_overlay(self):
        self.write_pack(self.applier.style_block("reference"))
        self.assertEqual(0, self.run_compat())

    def test_compat_script_fails_on_old_selector_overlay(self):
        self.write_pack(old_selector_overlay(self.applier.style_block("reference")))
        self.assertEqual(1, self.run_compat())

    def test_compat_script_fails_when_overlay_missing(self):
        self.write_pack("")
        self.assertEqual(1, self.run_compat())


class InstallerPaletteGateTests(unittest.TestCase):
    def setUp(self):
        self.installer = (
            ROOT / "deploy/matrix-template-video/install.sh"
        ).read_text(encoding="utf-8")

    def test_compat_checker_is_hash_locked_and_shipped(self):
        self.assertIn(
            'REFERENCE_PALETTE_COMPAT_SOURCE="${DEPLOY_ROOT}/deploy/'
            'matrix-template-video/verify-reference-palette-compat.py"',
            self.installer,
        )
        self.assertIn('"${REFERENCE_PALETTE_COMPAT_SOURCE}"', self.installer)
        self.assertIn('REFERENCE_PALETTE_COMPAT_SHA256="', self.installer)
        self.assertIn(
            'sha256sum "${REFERENCE_PALETTE_COMPAT_SOURCE}"', self.installer,
        )
        self.assertIn('"${REFERENCE_PALETTE_COMPAT_SHA256}" \\', self.installer)

    def test_compat_check_runs_after_injection_before_switch(self):
        inject = self.installer.index(
            'python3 "${PUBLIC_PALETTE_APPLIER_SOURCE}" --reference-root'
        )
        check = self.installer.index(
            'python3 "${REFERENCE_PALETTE_COMPAT_SOURCE}" \\'
        )
        switch = self.installer.index('mv -Tf "${NEXT_LINK}" "${SOURCE_LINK}"')
        self.assertLess(inject, check)
        self.assertLess(check, switch)


if __name__ == "__main__":
    unittest.main()
