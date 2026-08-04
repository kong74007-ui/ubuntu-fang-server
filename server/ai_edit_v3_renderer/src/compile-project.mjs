import {copyFile, mkdir, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {applyAnimation, compileAnimationScript} from "./registry/animations.mjs";
import {assertSafeId, assertSafeText, escapeAttribute, seconds} from "./registry/layout-primitives.mjs";
import {compileOverlay, compileOverlayV2, getOverlayContract} from "./registry/overlays.mjs";
import {getRegistrySha256, resolveLayout, resolveLayoutV2, resolveOverlay, resolveTheme} from "./registry/index.mjs";
import {applyTransition, compileTransitionScript} from "./registry/transitions.mjs";
import {overlayTrackIndex} from "./registry/track-allocation.mjs";

const MODULE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const TRUSTED_TRANSITION_COMPOSITIONS = new WeakSet();

export async function compileProject({manifest, outputRoot, sceneOptions = {}}) {
  assertManifestShape(manifest);
  if (manifest.version === "2.0") manifest = freezeTransitionInputs(manifest);
  const registrySha256 = getRegistrySha256();
  const suppliedRegistry = manifest.registry_sha256;
  if (suppliedRegistry !== registrySha256 && suppliedRegistry !== registrySha256.slice(7)) {
    throw new Error("registry_sha256_mismatch");
  }
  const theme = resolveTheme(manifest.theme);
  const projectRoot = path.resolve(outputRoot);
  await mkdir(projectRoot, {recursive: false});
  await mkdir(path.join(projectRoot, "compositions"));
  await mkdir(path.join(projectRoot, "vendor"));
  await mkdir(path.join(projectRoot, "assets", "fonts"), {recursive: true});
  await copyRuntime(projectRoot);

  const compositionIds = ["main"];
  const snapshotTimes = new Set([0, manifest.duration_ms]);
  for (const composition of manifest.compositions) {
    const compositionId = assertSafeId(composition.id, "composition_id");
    if (compositionIds.includes(compositionId)) throw new Error("composition_id_duplicate");
    compositionIds.push(compositionId);
    snapshotTimes.add(composition.start_ms);
    snapshotTimes.add(Math.floor((composition.start_ms + composition.end_ms) / 2));
    const sceneHtml = compileScene({manifest, composition, theme, ...sceneOptions});
    await writeFile(path.join(projectRoot, "compositions", `${compositionId}.html`), sceneHtml, {encoding: "utf8", flag: "wx"});
  }
  const entry = compileIndex({manifest, compositionIds});
  await writeFile(path.join(projectRoot, "index.html"), entry, {encoding: "utf8", flag: "wx"});
  const expectedFrames = Math.ceil(manifest.duration_ms * manifest.output_spec.fps_num / manifest.output_spec.fps_den / 1000);
  return Object.freeze({
    projectRoot,
    entryRelativePath: "index.html",
    compositionIds: Object.freeze(compositionIds),
    registrySha256,
    expectedFrames,
    snapshotTimesMs: Object.freeze([...snapshotTimes].sort((a, b) => a - b)),
  });
}

function compileIndex({manifest, compositionIds}) {
  const {width, height} = manifest.output_spec;
  const duration = seconds(manifest.duration_ms);
  const timeline = timelineRecorder();
  const transitions = manifest.version === "2.0" ? manifest.compositions.slice(1).map((composition, offset) => {
    const previous = manifest.compositions[offset];
    const identity = composition.transition === "card_match_cut" ? deriveCardIdentity(previous, composition, manifest.output_spec.ratio, manifest.assets ?? []) : null;
    const outgoing = `#${previous.id}_host`; const incoming = `#${composition.id}_host`;
    if (identity) {
      const matched = compileMatchedCardTransition({identity, outgoing, incoming, boundaryMs: composition.start_ms, sceneDurationMs: manifest.duration_ms});
      return Object.freeze({previous, composition, audit: matched.audit, script: matched.script});
    }
    const flashTarget = composition.transition === "light_flash" ? "#transition_flash_global" : undefined;
    const audit = applyTransition({
      timeline, transition: composition.transition, outgoing, incoming,
      ...(flashTarget ? {flashTarget} : {}),
      operationVersion: "2.0", boundaryMs: composition.start_ms, sceneDurationMs: manifest.duration_ms,
      fps: manifest.output_spec.fps_num / manifest.output_spec.fps_den,
    });
    return Object.freeze({previous, composition, audit, script: compileTransitionScript({...audit, outgoing, incoming})});
  }) : [];
  const hosts = manifest.compositions.map((composition, index) => {
    const start = seconds(composition.start_ms);
    const sceneDuration = seconds(composition.end_ms - composition.start_ms);
    const incoming = transitions[index - 1];
    const outgoing = transitions[index];
    const identity = incoming?.audit.identity ?? outgoing?.audit.identity;
    const identityAttribute = identity ? ` data-card-identity="${identity.slot_id}:${identity.asset_id}"` : "";
    const transitionAttribute = incoming ? ` data-transition-audit="${transitionAuditLabel(incoming.audit)}"` : "";
    return `<div id="${composition.id}_host" data-composition-id="${composition.id}" data-composition-src="compositions/${composition.id}.html" data-start="${start}" data-duration="${sceneDuration}" data-track-index="0"${identityAttribute}${transitionAttribute}></div>`;
  }).join("");
  const flashLayers = transitions.some((item) => item.audit.flashTarget)
    ? '<div id="transition_flash_global" class="hf-transition-flash" aria-hidden="true"></div>' : "";
  const transitionScript = transitions.map((item) => item.script).join("");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>@font-face{font-family:"Noto Sans SC";src:url("assets/fonts/NotoSansSC-Regular.woff2") format("woff2");font-weight:400}@font-face{font-family:"Noto Sans SC";src:url("assets/fonts/NotoSansSC-Bold.woff2") format("woff2");font-weight:700}html,body{margin:0;background:transparent;overflow:hidden}#main{position:relative;overflow:hidden}.hf-transition-flash{position:absolute;inset:0;z-index:2147483000;background:#fff;opacity:0;pointer-events:none}</style></head><body><div id="main" data-composition-id="main" data-width="${width}" data-height="${height}" data-start="0" data-duration="${duration}">${hosts}${flashLayers}</div><script src="vendor/gsap.min.js"></script><script>window.__timelines=window.__timelines||{};const tl=gsap.timeline({paused:true});${transitionScript}window.__timelines["main"]=tl;Object.freeze(${JSON.stringify(compositionIds)});</script></body></html>`;
}

function deriveCardIdentity(previous, current, ratio, assets) {
  if (!TRUSTED_TRANSITION_COMPOSITIONS.has(previous) || !TRUSTED_TRANSITION_COMPOSITIONS.has(current)
    || !Object.isFrozen(previous.layout_slot_bindings) || !Object.isFrozen(current.layout_slot_bindings)) return null;
  let previousLayout; let currentLayout;
  try {
    previousLayout = resolveLayoutV2(previous.layout_id, previous.layout_variant, ratio);
    currentLayout = resolveLayoutV2(current.layout_id, current.layout_variant, ratio);
  } catch (error) {
    if (/^(?:layout_unknown|layout_variant_unknown)$/u.test(error?.message ?? "")) return null;
    throw error;
  }
  const currentSlots = new Set(currentLayout.contract.identitySlots);
  const identitySlots = previousLayout.contract.identitySlots.filter((slotId) => currentSlots.has(slotId));
  const previousBindings = transitionBindingMap(previous.layout_slot_bindings);
  const currentBindings = transitionBindingMap(current.layout_slot_bindings);
  const shared = identitySlots.filter((slotId) => previousBindings.has(slotId) && previousBindings.get(slotId) === currentBindings.get(slotId));
  if (shared.length !== 1) return null;
  const slotId = shared[0];
  const assetId = previousBindings.get(slotId);
  const declaredAssets = new Set(assets.map((asset) => asset?.id).filter((id) => typeof id === "string"));
  if (!declaredAssets.has(assetId) || !previous.asset_ids?.includes(assetId) || !current.asset_ids?.includes(assetId)) return null;
  return Object.freeze({
    slot_id: slotId, asset_id: assetId,
    outgoing: `#${previous.id}_host`, incoming: `#${current.id}_host`,
    outgoing_slot: `#${previous.id}_${slotId}`, incoming_slot: `#${current.id}_${slotId}`,
  });
}

function freezeTransitionInputs(manifest) {
  const compositions = Object.freeze(manifest.compositions.map((composition) => {
    const bindings = composition.layout_slot_bindings ?? [];
    if (!Array.isArray(bindings)) throw new Error("transition_identity_source_invalid");
    const trusted = Object.freeze({
      ...composition,
      layout_slot_bindings: Object.freeze(bindings.map((binding) => Object.freeze({...binding}))),
    });
    TRUSTED_TRANSITION_COMPOSITIONS.add(trusted);
    return trusted;
  }));
  return {...manifest, compositions};
}

function transitionBindingMap(bindings) {
  const map = new Map();
  for (const binding of bindings) {
    if (!binding || Object.getPrototypeOf(binding) !== Object.prototype || !Object.isFrozen(binding)
      || typeof binding.slot_id !== "string" || typeof binding.asset_id !== "string" || map.has(binding.slot_id)) throw new Error("transition_identity_source_invalid");
    map.set(binding.slot_id, binding.asset_id);
  }
  return map;
}

function compileMatchedCardTransition({identity, outgoing, incoming, boundaryMs, sceneDurationMs}) {
  if (!identity || !Object.isFrozen(identity) || identity.outgoing !== outgoing || identity.incoming !== incoming) throw new Error("transition_identity_unproven");
  const durationMs = Math.min(420, Math.max(180, Math.floor(sceneDurationMs / 5)));
  const startMs = Math.max(0, Math.min(sceneDurationMs - durationMs, boundaryMs - Math.floor(durationMs / 2)));
  const operations = Object.freeze([
    Object.freeze({target: outgoing, from: Object.freeze({opacity: 1, scale: 1}), to: Object.freeze({opacity: 0, scale: 1.06})}),
    Object.freeze({target: incoming, from: Object.freeze({opacity: 0, scale: .94}), to: Object.freeze({opacity: 1, scale: 1})}),
  ]);
  const script = operations.map((operation) => `tl.fromTo(${JSON.stringify(operation.target)},${JSON.stringify(operation.from)},${JSON.stringify({...operation.to, duration: durationMs / 1000, ease: "power2.out"})},${startMs / 1000});`).join("");
  const audit = Object.freeze({
    operationVersion: "2.0", transition: "card_match_cut", effectiveTransition: "card_match_cut", fallbackReason: null,
    boundaryMs, startMs, endMs: startMs + durationMs, durationMs, identityRequired: true, identity,
  });
  return Object.freeze({audit, script});
}

function compileScene({manifest, composition, theme, layoutResolver = resolveLayout, buildLayoutInput = legacyLayoutInput, compileSource = compileSourceVideo}) {
  const {width, height, ratio} = manifest.output_spec;
  const durationMs = composition.end_ms - composition.start_ms;
  const duration = seconds(durationMs);
  const prefix = assertSafeId(composition.id, "composition_id");
  const layout = layoutResolver(composition.layout_id, composition.layout_variant, ratio);
  const captions = manifest.captions
    .filter((caption) => caption.start_ms < composition.end_ms && caption.end_ms > composition.start_ms)
    .map((caption) => ({
      text: assertSafeText(caption.text, {maxChars: 240, maxLines: 3}),
      startMs: Math.max(caption.start_ms, composition.start_ms) - composition.start_ms,
      endMs: Math.min(caption.end_ms, composition.end_ms) - composition.start_ms,
    }));
  const overlayBindings = manifest.version === "2.0"
    ? composition.overlay_instances.map((instance) => ({instanceId: instance.instance_id, componentId: instance.component_id, instance}))
    : composition.overlay_ids.map((componentId) => ({instanceId: componentId, componentId}));
  const overlayByTarget = new Map(overlayBindings.map((binding) => [binding.instanceId, binding]));
  const overlayEntries = overlayBindings.map(({instanceId, componentId, instance}, index) => {
    resolveOverlay(componentId);
    const overlayContract = getOverlayContract(componentId);
    const idPrefix = manifest.version === "2.0" ? `${prefix}_${instanceId}` : prefix;
    if (manifest.version === "2.0" && instance?.content_ref !== undefined) {
      assertVisualOverlayInstance(instance);
      const content = resolveAuthoritativeContent(composition.authoritative_content, instance.content_ref);
      const output = compileOverlayV2({
        componentId, instanceId: idPrefix, content, placement: instance.placement, ratio,
        durationMs, trackIndex: overlayTrackIndex(index),
      });
      return Object.freeze({instanceId, componentId, html: output.html, animationTarget: output.animationTarget, publicTargets: output.publicTargets, textAudit: output.textAudit});
    }
    if (componentId === "standard_caption") {
      const html = captions.map((caption, captionIndex) => compileOverlay({
        overlayId: componentId,
        idPrefix: `${idPrefix}_caption_${captionIndex + 1}`,
        text: caption.text,
        startMs: caption.startMs,
        durationMs: caption.endMs - caption.startMs,
        trackIndex: index + 21 + captionIndex,
      })).join("");
      return Object.freeze({instanceId, componentId, html});
    }
    const overlayText = [...captions.map((caption) => caption.text).join(" ")].slice(0, overlayContract.maxChars).join("");
    const html = compileOverlay({
      overlayId: componentId,
      idPrefix,
      text: overlayText,
      durationMs,
      trackIndex: index + 21,
    });
    return Object.freeze({instanceId, componentId, html});
  });
  const overlayEntryByTarget = new Map(overlayEntries.map((entry) => [entry.instanceId, entry]));
  const overlays = overlayEntries.map(({html}) => html).join("");
  const assetById = new Map((manifest.assets ?? []).map((asset) => [asset.id, asset]));
  const assets = (composition.asset_ids ?? []).map((assetId) => {
    const asset = assetById.get(assetId);
    if (!asset) throw new Error("composition_asset_unknown");
    return {id: asset.id, kind: asset.kind, relativePath: asset.path};
  });
  const layoutOutput = layout.compile(buildLayoutInput({manifest, composition, prefix, durationMs, overlays, overlayEntries, scene: composition, assets, captions, theme, layout}));
  const body = typeof layoutOutput === "string" ? layoutOutput : layoutOutput?.html;
  if (typeof body !== "string") throw new Error("layout_compile_invalid");
  const sourceVideo = compileSource({manifest, composition, prefix, layout});
  const variables = Object.entries(theme).sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}:${escapeAttribute(value)}`).join(";");
  const rootId = `${prefix}_root`;
  const timeline = timelineRecorder();
  const fps = manifest.output_spec.fps_num / manifest.output_spec.fps_den;
  const minimumAnimationMs = Math.ceil(1000 / fps);
  const operationVersion = manifest.version === "2.0" ? "2.0" : "1.0";
  const animationScript = (composition.animations ?? []).flatMap((animation) => {
    const binding = overlayByTarget.get(animation.target);
    if (!binding) throw new Error("animation_target_unknown");
    const overlayEntry = overlayEntryByTarget.get(animation.target);
    const animationPrefix = manifest.version === "2.0" ? `${prefix}_${binding.instanceId}` : prefix;
    const targets = binding.instance?.content_ref !== undefined
      ? [{target: overlayEntry?.animationTarget, windowStartMs: 0, windowDurationMs: durationMs, delayMs: animation.delay_ms}]
      : binding.componentId === "standard_caption"
      ? captions.map((caption, captionIndex) => ({
        target: `#${animationPrefix}_caption_${captionIndex + 1}_standard_caption`,
        windowStartMs: caption.startMs,
        windowDurationMs: caption.endMs - caption.startMs,
        delayMs: animation.delay_ms,
      })).filter((item) => item.windowDurationMs >= minimumAnimationMs)
      : [{
        target: `#${animationPrefix}_${binding.componentId}`,
        windowStartMs: 0,
        windowDurationMs: durationMs,
        delayMs: animation.delay_ms,
      }];
    return targets.map(({target, windowStartMs, windowDurationMs, delayMs}) => {
      const semanticTargets = manifest.version === "2.0" ? animationSemanticTargets(overlayEntry, animation.preset) : {};
      target = semanticTargets.target ?? target;
      assertUniqueAnimationTarget(body, target);
      for (const childTarget of semanticTargets.childTargets ?? []) assertUniqueAnimationTarget(body, childTarget);
      const audit = applyAnimation({
        timeline, preset: animation.preset, target, direction: animation.direction, operationVersion,
        childTargets: semanticTargets.childTargets,
        params: {durationMs: animation.duration_ms, delayMs, ...semanticTargets.params},
        sceneDurationMs: windowDurationMs, fps,
      });
      return compileAnimationScript({
        ...audit, target, startMs: audit.startMs + windowStartMs,
        ...(manifest.version === "2.0" ? {windowStartMs, compositionDurationMs: durationMs} : {}),
      });
    });
  }).join("");
  const transitionAudit = applyTransition({
    timeline, transition: manifest.version === "2.0" ? "hard_cut" : composition.transition, outgoing: `#${prefix}_background`, incoming: `#${rootId}`,
    operationVersion, boundaryMs: 0, sceneDurationMs: durationMs, fps: manifest.output_spec.fps_num / manifest.output_spec.fps_den,
  });
  const transitionScript = compileTransitionScript({...transitionAudit, outgoing: `#${prefix}_background`, incoming: `#${rootId}`});
  return `<template id="${prefix}_template"><div id="${rootId}" data-composition-id="${prefix}" data-width="${width}" data-height="${height}" data-start="0" data-duration="${duration}" style="${variables}">${body}${sourceVideo}</div><style>#${rootId}{position:relative;overflow:hidden;color:var(--hf-text);font-family:var(--hf-font)}#${rootId} .hf-background{position:absolute;inset:0;z-index:0;background:linear-gradient(145deg,var(--hf-bg),var(--hf-surface))}#${rootId} .hf-source-video{position:absolute;inset:0;width:100%;height:100%;object-fit:var(--hf-image-fit);z-index:1}#${rootId} .hf-layout-frame{position:absolute;inset:5%;z-index:2;display:grid;gap:var(--hf-gap)}#${rootId} .hf-speaker-zone,#${rootId} .hf-materials{display:grid;place-items:center;position:relative;overflow:hidden;border:1px solid var(--hf-border);border-radius:var(--hf-radius);background:rgba(23,42,66,.14);box-shadow:var(--hf-shadow)}#${rootId} .hf-materials{background:var(--hf-surface-strong)}#${rootId} .hf-speaker-zone span{display:${manifest.source_video ? "none" : "block"}}#${rootId} .hf-speaker-zone span,#${rootId} .hf-fallback span{color:var(--hf-muted);font-size:34px}#${rootId} .hf-materials{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:12px}#${rootId} .hf-material-count-1{grid-template-columns:1fr}#${rootId} .hf-asset{width:100%;height:100%;min-height:0;object-fit:var(--hf-image-fit);border-radius:18px}#${rootId} .hf-fallback{display:grid;place-items:center;width:100%;height:100%}#${rootId} .hf-layout-speaker_fullscreen{grid-template-columns:1fr}#${rootId} .hf-layout-speaker_fullscreen .hf-materials{display:none}#${rootId} .hf-layout-speaker_left_info_right{grid-template-columns:1.15fr .85fr}#${rootId} .hf-layout-speaker_right_evidence_left{grid-template-columns:.85fr 1.15fr}#${rootId} .hf-layout-speaker_right_evidence_left .hf-speaker-zone{order:2}#${rootId} .hf-layout-material_fullscreen_speaker_pip .hf-materials,#${rootId} .hf-layout-product_hero .hf-materials{position:absolute;inset:0}#${rootId} .hf-layout-material_fullscreen_speaker_pip .hf-speaker-zone{position:absolute;right:3%;bottom:4%;width:28%;height:34%;z-index:2}#${rootId} .hf-layout-product_hero .hf-speaker-zone{display:none}#${rootId} .hf-layout-editorial_collage{grid-template-columns:.75fr 1.25fr}#${rootId} .hf-layout-editorial_collage .hf-materials{grid-template-columns:repeat(2,1fr)}#${rootId} .hf-layout-comparison_split{grid-template-columns:1fr 1fr}#${rootId} .hf-layout-steps_stack,#${rootId} .hf-layout-method_timeline{grid-template-rows:.55fr 1.45fr}#${rootId} .hf-layout-number_proof .hf-speaker-zone{display:none}#${rootId} .hf-layout-number_proof .hf-materials{font-size:96px}#${rootId} .hf-layout-quote_reversal{transform:rotate(-1deg);inset:9% 7%}#${rootId} .hf-layout-cta_offer{inset:12%;transform:scale(.94)}#${rootId} .hf-variant-emphasis_b .hf-speaker-zone{border-width:3px}#${rootId} .hf-safe-area{position:absolute;inset:8% 7%;z-index:20;display:flex;flex-direction:column;justify-content:flex-end;gap:var(--hf-gap)}#${rootId} .hf-overlay{max-width:88%;padding:18px 28px;border:1px solid var(--hf-border);border-radius:20px;background:rgba(7,17,31,.82);font-size:40px;font-weight:700;line-height:1.28;box-shadow:var(--hf-shadow)}#${rootId} .hf-overlay-standard_caption{align-self:center;text-align:center;font-size:34px}</style><script>(()=>{const root=document.querySelector('#${rootId}');for(const node of root.querySelectorAll('[data-safe-text]'))node.querySelector('span').textContent=node.dataset.safeText;const tl=gsap.timeline({paused:true});tl.set(root,{autoAlpha:1},0);${transitionScript}${animationScript}window.__timelines=window.__timelines||{};window.__timelines["${prefix}"] = tl;})();</script></template>`;
}

function transitionAuditLabel(audit) {
  if (audit.transition !== "card_match_cut") return `${audit.transition}:direct`;
  if (audit.fallbackReason) return `${audit.transition}:fallback:${audit.fallbackReason}`;
  return `${audit.transition}:matched:${audit.identity.slot_id}:${audit.identity.asset_id}`;
}

function animationSemanticTargets(entry, preset) {
  if (!entry || typeof entry.html !== "string") return {};
  const targets = extractPublicTargets(entry.html);
  if (preset === "count_up") {
    const metric = targets.filter((item) => item.name === "metric_value");
    if (metric.length !== 1) throw new Error("animation_numeric_target_invalid");
    const match = metric[0].safeText?.match(/-?\d+(?:\.\d+)?/u);
    if (!match) throw new Error("animation_numeric_target_invalid");
    const precision = match[0].split(".")[1]?.length ?? 0;
    const scaledDigits = match[0].replace("-", "").replace(".", "");
    const scaled = Number(`${match[0].startsWith("-") ? "-" : ""}${scaledDigits}`);
    if (precision > 6 || !Number.isSafeInteger(scaled)) throw new Error("animation_numeric_target_invalid");
    return {target: `#${metric[0].id}`, params: {numericStart: 0, numericStartToken: "0", numericEnd: Number(match[0]), numericEndToken: match[0], numericPrecision: precision, numericPrefix: "", numericSuffix: ""}};
  }
  if (preset === "stagger") {
    const publicTarget = Object.freeze({bullet_list: "bullets", step_indicator: "steps"})[entry.componentId];
    if (!publicTarget) throw new Error("animation_child_targets_invalid");
    const children = targets.filter((item) => item.name === publicTarget).map((item) => `#${item.id}`);
    if (children.length === 0) throw new Error("animation_child_targets_invalid");
    return {childTargets: children};
  }
  if (preset === "light_sweep") return {target: uniquePublicTarget(targets, ["underline", "accent", "highlight"], "animation_light_target_invalid")};
  if (preset === "highlight_draw") return {target: uniquePublicTarget(targets, ["highlight", "underline", "rule"], "animation_highlight_target_invalid")};
  if (preset === "subtitle_pop") return {target: uniquePublicTarget(targets, ["caption", "body", "headline"], "animation_subtitle_target_invalid")};
  return {};
}

function uniquePublicTarget(targets, names, code) {
  for (const name of names) {
    const candidates = targets.filter((item) => item.name === name);
    if (candidates.length === 1) return `#${candidates[0].id}`;
    if (candidates.length > 1) throw new Error(code);
  }
  throw new Error(code);
}

function extractPublicTargets(html) {
  const targets = [];
  for (const match of html.matchAll(/<[^>]+>/gu)) {
    const tag = match[0];
    const id = attribute(tag, "id");
    const name = attribute(tag, "data-public-target");
    if (!id || !name) continue;
    targets.push({id, name, safeText: attribute(tag, "data-safe-text")});
  }
  return targets;
}

function attribute(tag, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  return tag.match(new RegExp(`\\s${escaped}="([^"]*)"`, "u"))?.[1];
}

function assertUniqueAnimationTarget(html, selector) {
  if (typeof selector !== "string" || !selector.startsWith("#")) throw new Error("animation_target_unknown");
  const escaped = selector.slice(1).replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  if ((html.match(new RegExp(`\\bid="${escaped}"`, "gu")) ?? []).length !== 1) throw new Error("animation_target_unknown");
}

function assertVisualOverlayInstance(instance) {
  if (!instance || typeof instance !== "object" || Array.isArray(instance)) throw new Error("manifest_overlay_instance_invalid");
  const allowed = new Set(["instance_id", "component_id", "content_ref", "placement", "variant"]);
  if (Object.keys(instance).some((key) => !allowed.has(key))) throw new Error("manifest_overlay_instance_invalid");
  if (!['headline', 'highlight'].includes(instance.content_ref)) throw new Error("manifest_overlay_content_ref_invalid");
  if (!['title_safe', 'subtitle_safe', 'left_panel', 'right_panel', 'center', 'lower_third'].includes(instance.placement)) throw new Error("manifest_overlay_placement_invalid");
}

function resolveAuthoritativeContent(content, reference) {
  if (!content || typeof content !== "object" || Array.isArray(content) || Object.keys(content).some((key) => !["headline", "highlight"].includes(key))) {
    throw new Error("manifest_overlay_content_ref_invalid");
  }
  const value = content[reference];
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some((key) => !["text", "source_caption_ids"].includes(key)) || typeof value.text !== "string" || !value.text.trim() || !Array.isArray(value.source_caption_ids) || !value.source_caption_ids.every((item) => typeof item === "string" && /^[a-z0-9_]{1,64}$/u.test(item))) {
    throw new Error("manifest_overlay_content_ref_invalid");
  }
  return {text: value.text};
}

function legacyLayoutInput({prefix, durationMs, overlays, scene, assets, manifest}) {
  return {idPrefix: prefix, durationMs, hasVideo: Boolean(manifest.source_video), overlays, scene, assets};
}

export function compileSourceVideo({manifest, composition, prefix}) {
  if (!manifest.source_video) return "";
  if (manifest.source_video.silent !== true) throw new Error("source_video_audio_forbidden");
  const sourcePath = manifest.source_video.path;
  if (typeof sourcePath !== "string" || !/^(?!\/)(?![A-Za-z]:)(?!.*\\)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._/-]+$/u.test(sourcePath)) {
    throw new Error("source_video_path_invalid");
  }
  const speakerPip = composition.layout_id === "material_fullscreen_speaker_pip";
  const sourceClass = speakerPip ? "hf-source-video hf-source-video-pip clip" : "hf-source-video clip";
  const pipStyle = speakerPip
    ? ` style="inset:auto 7.85% 8.8% auto;width:26.6%;height:30.6%;z-index:3;border-radius:var(--hf-radius)"`
    : "";
  return sourceSegmentClips({manifest, composition}).map((clip) => {
    return `<video id="${prefix}_source_${clip.index}" class="${sourceClass}" muted playsinline preload="auto" src="${escapeAttribute(sourcePath)}" data-start="${seconds(clip.localStartMs)}" data-duration="${seconds(clip.durationMs)}" data-playback-start="${seconds(clip.playbackStartMs)}" data-volume="0" data-track-index="10"${pipStyle}></video>`;
  }).join("");
}

/** Returns source intervals that actually intersect this composition. */
export function sourceSegmentClips({manifest, composition}) {
  if (!manifest.source_video) return [];
  const sourcePath = manifest.source_video.path;
  const segments = Array.isArray(manifest.source_segments) ? manifest.source_segments : [];
  return segments.flatMap((segment, index) => {
    const start = Math.max(segment.output_start_ms, composition.start_ms);
    const end = Math.min(segment.output_end_ms, composition.end_ms);
    if (end <= start) return [];
    if (segment.source_path !== sourcePath) throw new Error("source_segment_path_mismatch");
    return [{index, localStartMs: start - composition.start_ms, playbackStartMs: segment.source_start_ms + (start - segment.output_start_ms), durationMs: end - start}];
  });
}

function timelineRecorder() {
  const timeline = {};
  for (const method of ["fromTo", "to", "set"]) timeline[method] = () => timeline;
  return timeline;
}

async function copyRuntime(projectRoot) {
  await copyFile(path.join(MODULE_ROOT, "node_modules", "gsap", "dist", "gsap.min.js"), path.join(projectRoot, "vendor", "gsap.min.js"));
  for (const file of ["NotoSansSC-Regular.woff2", "NotoSansSC-Bold.woff2"]) {
    await copyFile(path.join(MODULE_ROOT, "assets", "fonts", file), path.join(projectRoot, "assets", "fonts", file));
  }
}

function assertManifestShape(manifest) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) throw new Error("manifest_invalid");
  if (!Number.isInteger(manifest.duration_ms) || manifest.duration_ms <= 0) throw new Error("manifest_duration_invalid");
  if (!manifest.output_spec || !Array.isArray(manifest.compositions) || !Array.isArray(manifest.captions)) throw new Error("manifest_shape_invalid");
  if (!Number.isInteger(manifest.output_spec.fps_num) || !Number.isInteger(manifest.output_spec.fps_den)) throw new Error("manifest_fps_invalid");
  const ids = new Set();
  let expectedStart = 0;
  for (const composition of manifest.compositions) {
    if (ids.has(composition.id)) throw new Error("composition_id_duplicate");
    ids.add(composition.id);
    if (composition.start_ms !== expectedStart || composition.end_ms <= composition.start_ms) throw new Error("composition_timeline_invalid");
    expectedStart = composition.end_ms;
    if (!Array.isArray(composition.overlay_ids)) throw new Error("composition_overlays_invalid");
  }
  if (expectedStart !== manifest.duration_ms) throw new Error("composition_timeline_invalid");
}
