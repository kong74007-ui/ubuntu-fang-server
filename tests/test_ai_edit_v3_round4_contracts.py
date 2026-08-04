from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.contracts import (
    ContractError,
    canonical_json,
    freeze_render_manifest,
    schema_sha256,
)
from server.content_domains.ai_edit_v3.director_compiler import compile_edit_plan
from server.content_domains.ai_edit_v3.director_decision import validate_director_decision
from server.content_domains.ai_edit_v3.production import (
    _layout_slot_bindings,
    _render_captions,
    _resolve_design_tokens,
    _scene_asset_ids,
)


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "server" / "ai_edit_v3_renderer"
DESIGN_INTENT = {
    "density": "balanced",
    "motion_energy": "medium",
    "image_fit": "cover",
    "decoration_intensity": "medium",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_plan(
    *,
    layout_id: str,
    layout_variant: str,
    bindings: list[dict[str, object]] | None = None,
    materials: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    bindings = bindings or []
    materials = materials or []
    captions = [{"id": "caption_001", "start_ms": 0, "end_ms": 4000, "text": "Authoritative product method"}]
    candidate = {
        "id": "candidate_01",
        "start_ms": 0,
        "end_ms": 4000,
        "caption_ids": ["caption_001"],
        "authoritative_text": captions[0]["text"],
        "protected_fact_ids": [],
        "available_material_ids": [str(item["material_id"]) for item in materials],
        "speaker_available": True,
    }
    capabilities = {
        "layout_capabilities": [layout_id],
        "layout_variants": {layout_id: [layout_variant]},
        "overlay_capabilities": ["headline_block", "standard_caption"],
        "overlay_variants": {},
        "overlay_animation_targets": {"headline_block": [], "standard_caption": []},
        "layout_animation_targets": {layout_id: []},
        "animation_capabilities": ["fade", "scale"],
        "transition_capabilities": ["hard_cut"],
        "theme_capabilities": {
            "palette_id": ["midnight_gold"],
            "typography_id": ["editorial_sans"],
            "density": ["balanced"],
            "motion_energy": ["medium"],
            "image_fit": ["cover"],
        },
        "theme_profile_ids": ["editorial_clean"],
        "identity_match_capability": False,
    }
    decision = {
        "version": "1.0",
        "creative_concept": "One authoritative scene",
        "narrative_pattern": "question_proof",
        "theme_profile_id": "editorial_clean",
        "design_intent": DESIGN_INTENT,
        "scene_directives": [{
            "scene_id": "candidate_01",
            "narrative_role": "hook",
            "layout_id": layout_id,
            "layout_variant": layout_variant,
            "headline": {"text_kind": "verbatim", "source_caption_ids": ["caption_001"]},
            "overlay_instances": [
                {"instance_id": "headline", "component_id": "headline_block", "content_ref": "headline", "placement": "title_safe"},
                {"instance_id": "subtitle", "component_id": "standard_caption", "content_ref": "headline", "placement": "subtitle_safe"},
            ],
            "material_bindings": bindings,
            "material_slot_directives": [],
            "animations": [
                {"target_id": "headline", "preset": "fade", "direction": "none", "duration_ms": 400, "delay_ms": 0},
                {"target_id": "subtitle", "preset": "scale", "direction": "none", "duration_ms": 400, "delay_ms": 50},
            ],
            "transition": "hard_cut",
            "sound_events": [],
        }],
        "audio_intent": {"bgm_description": "bounded instrumental", "energy": "medium", "dialogue_priority": True},
    }
    validated = validate_director_decision(decision, candidates=[candidate], capabilities=capabilities)
    return compile_edit_plan(
        validated,
        candidates=[candidate],
        timeline={"duration_ms": 4000, "captions": captions, "ratio": "16:9"},
        materials=materials,
        capabilities=capabilities,
        variation_seed=7,
    )


def _freeze_plan_manifest(
    plan: dict[str, object],
    root: Path,
    *,
    asset_order: list[str] | None = None,
):
    input_root = root / "input"
    media = input_root / "media"
    media.mkdir(parents=True)
    source = media / "source.mp4"
    master = media / "master.wav"
    source.write_bytes(b"source-video")
    master.write_bytes(b"master-audio")
    scene = plan["scenes"][0]
    material_ids = [str(slot["id"]) for slot in scene["material_slots"]]
    ordered = asset_order or material_ids
    assets = []
    for material_id in ordered:
        target = media / f"{material_id}.png"
        target.write_bytes(f"image:{material_id}".encode())
        assets.append({
            "id": material_id,
            "kind": "image",
            "path": target.relative_to(input_root).as_posix(),
            "sha256": _sha(target),
            "size_bytes": target.stat().st_size,
        })
    lock = json.loads((RENDERER / "renderer-release.lock.json").read_text(encoding="utf-8"))
    registry = (RENDERER / "registry-sha256.txt").read_text(encoding="utf-8").strip().removeprefix("sha256:")
    source_sha = _sha(source)
    manifest = {
        "version": "2.0",
        "schema_sha256": schema_sha256("render-manifest-v2.schema.json"),
        "renderer_environment": {
            "renderer_build_id": lock["renderer_build_id"],
            "code_sha256": "1" * 64,
            "package_lock_sha256": lock["package_lock_sha256"],
            "release_sha256": "2" * 64,
            "node_version": "22.22.0",
            "chromium_version": "149.0.7827.115",
            "ffmpeg_version": "8.1.1",
            "ffprobe_version": "8.1.1",
            "locale": "C.UTF-8",
            "timezone": "UTC",
        },
        "output_spec": {"ratio": "16:9", "width": 1920, "height": 1080, "fps_num": 30, "fps_den": 1, "video_codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac", "sample_rate": 48000, "channels": 2},
        "duration_ms": 4000,
        "edit_plan_sha256": hashlib.sha256(canonical_json(plan)).hexdigest(),
        "registry_sha256": registry,
        "theme": plan["theme"],
        "seed": 7,
        "theme_profile_id": "editorial_clean",
        "design_intent": DESIGN_INTENT,
        "variation_seed": "0123456789abcdef",
        "design_tokens": _resolve_design_tokens("editorial_clean", DESIGN_INTENT, "0123456789abcdef"),
        "source_video": {"path": "media/source.mp4", "sha256": source_sha, "size_bytes": source.stat().st_size, "silent": True, "duration_ms": 4000, "width": 1920, "height": 1080},
        "source_segments": [{"id": "segment_01", "source_path": "media/source.mp4", "sha256": source_sha, "source_start_ms": 0, "source_end_ms": 4000, "output_start_ms": 0, "output_end_ms": 4000}],
        "master_audio": {"path": "media/master.wav", "sha256": _sha(master), "size_bytes": master.stat().st_size, "duration_ms": 4000, "sample_rate": 48000, "channels": 2},
        "assets": assets,
        "compositions": [{
            "id": "composition_001",
            "scene_id": scene["id"],
            "start_ms": scene["start_ms"],
            "end_ms": scene["end_ms"],
            "layout_id": scene["layout_id"],
            "layout_variant": scene["layout_variant"],
            "overlay_ids": scene["overlay_ids"],
            "overlay_instances": scene["overlay_instances"],
            "animations": scene["animations"],
            "transition": scene["transition"],
            "asset_ids": _scene_asset_ids(scene, ordered),
            "layout_slot_bindings": _layout_slot_bindings(scene, ordered),
        }],
        "captions": _render_captions(plan["captions"]),
    }
    return freeze_render_manifest(manifest, input_root / "render-manifest.json", sandbox_root=input_root)


def _node_compile(manifest_path: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
    program = "import fs from 'node:fs'; import {compileProjectV2} from './src/compile-project-v2.mjs'; const manifest=JSON.parse(fs.readFileSync(process.argv.at(-2),'utf8')); await compileProjectV2({manifest,outputRoot:process.argv.at(-1)});"
    return subprocess.run(
        ["node", "--input-type=module", "-e", program, str(manifest_path), str(output_root)],
        cwd=RENDERER,
        capture_output=True,
        text=True,
        check=False,
    )


class Round4CrossLanguageContractsTests(unittest.TestCase):
    def test_director_protocol_placements_survive_freeze_and_compile_to_safe_hosts(self):
        plan = _compile_plan(layout_id="speaker_fullscreen", layout_variant="headline_top")
        self.assertEqual(["title_safe", "subtitle_safe"], [item["placement"] for item in plan["scenes"][0]["overlay_instances"]])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = _freeze_plan_manifest(plan, root)
            compiled = _node_compile(frozen.path, root / "project")
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            scene = (root / "project" / "compositions" / "composition_001.html").read_text(encoding="utf-8")
        title = re.search(r'<aside\b[^>]*data-safe-host="title"[^>]*>([\s\S]*?)</aside>', scene).group(1)
        captions = re.search(r'<aside\b[^>]*data-safe-host="captions"[^>]*>([\s\S]*?)</aside>', scene).group(1)
        self.assertIn('id="composition_001_headline_headline_block"', title)
        self.assertNotIn('id="composition_001_subtitle_caption_1_standard_caption"', title)
        self.assertIn('id="composition_001_subtitle_caption_1_standard_caption"', captions)
        self.assertIn('tl.fromTo("#composition_001_headline_headline_block"', scene)
        self.assertIn('tl.fromTo("#composition_001_subtitle_caption_1_standard_caption"', scene)

    def test_explicit_layout_slots_survive_reordered_assets_and_only_consumed_bindings_emit(self):
        bindings = [
            {"slot_id": "primary", "material_id": "material_primary", "required": True},
            {"slot_id": "detail", "material_id": "material_detail", "required": False},
            {"slot_id": "evidence", "material_id": "material_evidence", "required": False},
        ]
        materials = [
            {"material_id": "material_primary", "semantic": "hero package"},
            {"material_id": "material_detail", "semantic": "detail view"},
            {"material_id": "material_evidence", "semantic": "proof image"},
        ]
        plan = _compile_plan(layout_id="product_hero", layout_variant="split_copy", bindings=bindings, materials=materials)
        slots = plan["scenes"][0]["material_slots"]
        self.assertTrue(all("layout_slot_id" in slot for slot in slots), "explicit director layout slots must survive in the edit plan")
        self.assertEqual(
            [("material_primary", "primary"), ("material_detail", "detail"), ("material_evidence", "evidence")],
            [(slot["id"], slot["layout_slot_id"]) for slot in slots],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = _freeze_plan_manifest(plan, root, asset_order=["material_evidence", "material_detail", "material_primary"])
            composition = frozen.document["compositions"][0]
            self.assertEqual(
                [{"slot_id": "primary", "asset_id": "material_primary"}, {"slot_id": "detail", "asset_id": "material_detail"}],
                composition["layout_slot_bindings"],
            )
            compiled = _node_compile(frozen.path, root / "project")
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            scene = (root / "project" / "compositions" / "composition_001.html").read_text(encoding="utf-8")
        for binding in composition["layout_slot_bindings"]:
            self.assertIn(f'data-slot="{binding["slot_id"]}"', scene)
            self.assertIn(f'src="media/{binding["asset_id"]}.png"', scene)
        self.assertNotIn('data-slot="evidence"', scene)

    def test_steps_stack_freezes_only_material_bindings_consumed_by_the_node_layout(self):
        plan = _compile_plan(
            layout_id="steps_stack",
            layout_variant="numbered_cards",
            bindings=[
                {"slot_id": "steps", "material_id": "material_steps", "required": False},
                {"slot_id": "accent", "material_id": "material_accent", "required": False},
            ],
            materials=[
                {"material_id": "material_steps", "semantic": "visual steps reference"},
                {"material_id": "material_accent", "semantic": "supporting accent image"},
            ],
        )
        self.assertEqual(
            [("material_steps", "steps"), ("material_accent", "accent")],
            [(slot["id"], slot["layout_slot_id"]) for slot in plan["scenes"][0]["material_slots"]],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = _freeze_plan_manifest(plan, root)
            composition = frozen.document["compositions"][0]
            compiled = _node_compile(frozen.path, root / "project")
            self.assertEqual(0, compiled.returncode, compiled.stderr)
            scene = (root / "project" / "compositions" / "composition_001.html").read_text(encoding="utf-8")

        for binding in composition["layout_slot_bindings"]:
            self.assertIn(
                f'src="media/{binding["asset_id"]}.png"',
                scene,
                f'manifest material binding {binding["slot_id"]} must be consumed by the selected layout',
            )
        self.assertEqual(
            [{"slot_id": "accent", "asset_id": "material_accent"}],
            composition["layout_slot_bindings"],
        )
        self.assertIn('data-slot="steps"', scene)
        self.assertIn('data-safe-text="Authoritative product method"', scene)
        self.assertNotIn("material_steps.png", scene)

    def test_product_primary_is_required_before_manifest_can_freeze(self):
        evidence_only = _compile_plan(
            layout_id="product_hero",
            layout_variant="split_copy",
            bindings=[{"slot_id": "evidence", "material_id": "material_evidence", "required": False}],
            materials=[{"material_id": "material_evidence", "semantic": "proof image"}],
        )
        with self.assertRaisesRegex(ValueError, "scene_layout_required_slot_missing"):
            _layout_slot_bindings(evidence_only["scenes"][0], ["material_evidence"])

        valid = _compile_plan(
            layout_id="product_hero",
            layout_variant="split_copy",
            bindings=[
                {"slot_id": "primary", "material_id": "material_primary", "required": True},
                {"slot_id": "evidence", "material_id": "material_evidence", "required": False},
            ],
            materials=[
                {"material_id": "material_primary", "semantic": "hero package"},
                {"material_id": "material_evidence", "semantic": "proof image"},
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen = _freeze_plan_manifest(valid, root)
            invalid = json.loads(frozen.path.read_text(encoding="utf-8"))
            invalid["compositions"][0]["layout_slot_bindings"] = [{"slot_id": "evidence", "asset_id": "material_evidence"}]
            with self.assertRaisesRegex(ContractError, "render_layout_required_slot_missing"):
                freeze_render_manifest(invalid, frozen.path.with_name("invalid-render-manifest.json"), sandbox_root=frozen.path.parent)


if __name__ == "__main__":
    unittest.main()
