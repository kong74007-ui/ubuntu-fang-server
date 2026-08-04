"""Deterministic decision-to-edit-plan compiler."""
from __future__ import annotations
import copy
from collections.abc import Mapping, Sequence
from typing import Any
from . import contracts

_ARC = {"hook":"hook", "context":"problem", "problem":"problem", "method":"method", "proof":"evidence", "transition":"offer", "cta":"cta"}

def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping): return copy.deepcopy(dict(value))
    slots = getattr(value, "__slots__", ())
    if slots: return {name: copy.deepcopy(getattr(value, name)) for name in slots}
    raise ValueError("director_candidate_invalid")

def _visible(value: Mapping[str, Any] | None, captions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if not value: return {"text_kind":"ui_label", "ui_label_id":"chapter"}
    if value.get("text_kind") == "ui_label":
        label = value.get("ui_label_id")
        if label not in {"chapter","step","category","evidence_marker","cta_prompt"}: raise ValueError("director_label_unknown")
        return {"text_kind":"ui_label","ui_label_id":label}
    refs = value.get("source_caption_ids")
    if value.get("text_kind") not in {"verbatim","compressed"} or not isinstance(refs,list) or not refs: raise ValueError("director_text_reference_invalid")
    try: text = "".join(str(captions[ref]["text"]) for ref in refs)
    except (KeyError, TypeError): raise ValueError("director_text_reference_invalid") from None
    return {"text":text,"text_kind":value["text_kind"],"source_caption_ids":list(refs)}

def compile_edit_plan(decision: Mapping[str, Any], *, candidates: Sequence[Any], timeline: Mapping[str, Any], materials: Sequence[Any], capabilities: Mapping[str, Any], variation_seed: int) -> dict[str, Any]:
    if isinstance(variation_seed, bool) or not isinstance(variation_seed, int): raise ValueError("director_variation_seed_invalid")
    rows = [_record(item) for item in candidates]; directives = decision.get("scene_directives")
    if not isinstance(directives,list) or [item.get("scene_id") for item in directives if isinstance(item,Mapping)] != [row.get("id") for row in rows]: raise ValueError("director_scene_coverage_invalid")
    captions = timeline.get("captions"); duration = timeline.get("duration_ms")
    if not isinstance(captions,list) or not captions or isinstance(duration,bool) or not isinstance(duration,int): raise ValueError("director_timeline_invalid")
    caption_by_id = {item.get("id"):item for item in captions if isinstance(item,Mapping)}
    if len(caption_by_id) != len(captions): raise ValueError("director_timeline_invalid")
    def allowed(name):
        values = capabilities.get(name)
        if not isinstance(values,(list,tuple)): raise ValueError("director_capabilities_invalid")
        return set(values)
    layouts, overlays, presets, transitions = (allowed(name) for name in ("layout_capabilities","overlay_capabilities","animation_capabilities","transition_capabilities"))
    variants = capabilities.get("layout_variants", {})
    if not isinstance(variants,Mapping): raise ValueError("director_capabilities_invalid")
    scenes=[]; arcs=[]; requests=[]; cues=[{"id":"bgm_01","type":"bgm","priority":"required","start_ms":0,"end_ms":duration,"description":str(decision.get("audio_intent",{}).get("bgm_description","bounded instrumental"))[:240]}]
    material_by_id = {str(_record(item).get("material_id")):_record(item) for item in materials}
    for index,(candidate,directive) in enumerate(zip(rows,directives,strict=True),1):
        if not isinstance(directive,Mapping): raise ValueError("director_scene_coverage_invalid")
        layout, variant, transition = directive.get("layout_id"), directive.get("layout_variant"), directive.get("transition")
        if layout not in layouts or variant not in set(variants.get(layout,())): raise ValueError("director_layout_variant_unknown")
        if transition not in transitions: raise ValueError("director_transition_unknown")
        instances = [copy.deepcopy(dict(item)) for item in directive.get("overlay_instances",()) if isinstance(item,Mapping)]
        if len(instances) != len(directive.get("overlay_instances",())) or any(item.get("component_id") not in overlays for item in instances): raise ValueError("director_component_unknown")
        ids={item.get("instance_id") for item in instances}
        if len(ids)!=len(instances) or not all(isinstance(item,str) for item in ids): raise ValueError("director_overlay_duplicate")
        anim=[]
        for item in directive.get("animations",()):
            if not isinstance(item,Mapping) or item.get("preset") not in presets or item.get("target_id") not in ids: raise ValueError("director_animation_unknown")
            anim.append({"target":item["target_id"],"preset":item["preset"],"direction":item["direction"],"duration_ms":item["duration_ms"],"delay_ms":item["delay_ms"]})
        start,end=candidate.get("start_ms"),candidate.get("end_ms")
        if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int) or end<=start: raise ValueError("director_candidate_timing_invalid")
        headline=_visible(directive.get("headline"),caption_by_id); highlight=_visible(directive.get("highlight"),caption_by_id)
        if headline["text_kind"]=="ui_label" and highlight["text_kind"]=="ui_label": headline=_visible({"text_kind":"verbatim","source_caption_ids":list(candidate.get("caption_ids",()))},caption_by_id)
        slots=[]
        for source in [*directive.get("material_bindings",()),*directive.get("material_slot_directives",())]:
            if not isinstance(source,Mapping) or not isinstance(source.get("slot_id"),str): raise ValueError("director_material_slot_invalid")
            mid=source["slot_id"]; bound=material_by_id.get(str(source.get("material_id")),{})
            semantic=str(source.get("semantic") or bound.get("semantic") or "source-bound visual")[:240]; purpose=source.get("purpose") or "context"; priority=source.get("priority") or ("required" if source.get("required") else "optional"); ratio=source.get("ratio") or "auto"
            slot={"id":mid,"semantic":semantic,"purpose":purpose,"priority":priority,"ratio":ratio,"start_ms":start,"end_ms":end}; slots.append(slot); requests.append({"request_id":mid,"semantic":semantic,"purpose":purpose,"priority":priority,"ratio":ratio,"time_range":{"start_ms":start,"end_ms":end}})
        scenes.append({"id":f"scene_{index:02d}","start_ms":start,"end_ms":end,"intent":str(candidate.get("authoritative_text") or "authoritative scene")[:240],"layout_id":layout,"layout_variant":variant,"visual_type":"director_program","headline":headline,"highlight":highlight,"overlay_ids":[item["component_id"] for item in instances],"overlay_instances":instances,"material_slots":slots,"animations":anim,"transition":transition})
        arcs.append({"id":f"arc_{index:02d}","role":_ARC.get(directive.get("narrative_role"),"problem"),"start_ms":start,"end_ms":end,"summary":str(candidate.get("authoritative_text") or "authoritative scene")[:240]})
        for event_index,event in enumerate(directive.get("sound_events",()),1):
            if not isinstance(event,Mapping) or event.get("role") not in {"reversal","number","method","transition","cta"}: raise ValueError("director_sound_event_invalid")
            offset=int(event.get("offset_ms",0)); cue_start=start+offset; cues.append({"id":f"scene_{index:02d}_sfx_{event_index:02d}","type":"sfx","priority":event.get("priority","optional"),"role":event["role"],"start_ms":cue_start,"end_ms":min(end,cue_start+500),"description":f"{event['role']} cue"})
    intent=decision.get("design_intent",{})
    plan={"version":"2.0","visual_program_version":"1.0","duration_ms":duration,"ratio":timeline.get("ratio","9:16"),"creative_concept":str(decision.get("creative_concept","director program"))[:240],"theme":{"palette_id":"midnight_gold","typography_id":"editorial_sans","density":intent.get("density","balanced"),"motion_energy":intent.get("motion_energy","medium"),"image_fit":intent.get("image_fit","cover")},"narrative_arc":arcs,"captions":[{**dict(item),"emphasis":"primary" if index==0 else "none"} for index,item in enumerate(captions)],"source_segments":[{"id":"segment_01","source_start_ms":0,"source_end_ms":duration,"output_start_ms":0,"output_end_ms":duration,"caption_ids":[item["id"] for item in captions],"keep_reason":"authoritative full timeline"}],"scenes":scenes,"materials":requests,"audio_cues":cues}
    return contracts.validate_edit_plan(plan,timeline={"duration_ms":duration,"accurate_captions":captions,**copy.deepcopy(dict(capabilities))})

__all__=("compile_edit_plan",)
