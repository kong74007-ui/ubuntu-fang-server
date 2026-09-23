"""Per-job text styles. Template files and legacy v05 overrides stay immutable."""
from __future__ import annotations

import copy
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

from PIL import ImageFont

VERSION = 1
ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
BILINGUAL = "bilingual-stagger-salon"
FIELDS = {"font_family", "font_size_px", "color", "offset_x_px", "offset_y_px", "stroke_width_px", "stroke_color"}
NUMBERS = {"font_size_px": (16, 240), "offset_x_px": (-60, 60), "offset_y_px": (-60, 60), "stroke_width_px": (0, 24)}
COLORS = {"color", "stroke_color"}
FIXED_ROLES = {"title":"top1", "subtitle":"top2", "body":"top3", "cta":"bottom2", "subtitle1":"top2", "subtitle2":"top3", "ctaLine1":"bottom2", "ctaLine2":"bottom2", "metric":"top3", "platform":"top3", "lead":"bottom2"}

LAYOUT_GUARD = r"""
var records=[],peers=[],peerNames=[];
selectors.forEach(function(entry){var selector=typeof entry==='string'?entry:entry.selector,name=typeof entry==='string'?entry:entry.layer;document.querySelectorAll(selector).forEach(function(node){if(peers.indexOf(node)<0){peers.push(node);peerNames.push(name)}})});
rows.forEach(function(row){var nodes=document.querySelectorAll(row.selector);if(!nodes.length)throw new Error('Missing text style layer');nodes.forEach(function(node){
 var font=getComputedStyle(node).font;if(font)document.fonts.load(font).catch(function(){});
 records.push({node:node,before:null,properties:row.properties,baseline:row.baseline||{}});
})});
window.__matrixApplyTextStyles=function(){records.forEach(function(record){
 if(!record.before){record.before={};Object.keys(record.properties).forEach(function(key){var value=record.node.style.getPropertyValue(key),priority=record.node.style.getPropertyPriority(key);if(!value&&record.baseline[key]){value=record.baseline[key];priority='important'}record.before[key]=[value,priority]})}
 Object.keys(record.properties).forEach(function(key){record.node.style.setProperty(key,record.properties[key],'important')})
});if(!window.__matrixTextFontsReady)window.__matrixTextFontsReady=Promise.all(records.map(function(record){var s=getComputedStyle(record.node);return document.fonts.load(s.fontStyle+' '+s.fontWeight+' '+s.fontSize+' '+s.fontFamily,record.node.textContent||'Aa')}))};
function box(node){
 if(!node.textContent.trim())return null;
 for(var p=node;p&&p!==document.body;p=p.parentElement){var s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)<.98)return null}
 var range=document.createRange();range.selectNodeContents(node);var r=range.getBoundingClientRect();if(!r.width||!r.height)return null;
 var stroke=parseFloat(getComputedStyle(node).webkitTextStrokeWidth)||0;
 return {left:r.left-stroke,right:r.right+stroke,top:r.top-stroke,bottom:r.bottom+stroke};
}
function inside(r){return r&&r.left>=-2&&r.top>=-2&&r.right<=1082&&r.bottom<=1922}
function overlap(a,b){if(!a||!b)return 0;return Math.max(0,Math.min(a.right,b.right)-Math.max(a.left,b.left))*Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top))}
var previous='';
window.__matrixValidateTextStyles=function(){
 var actual=peers.map(box),signature=JSON.stringify(actual);if(signature===previous)return [];
 var saved=records.map(function(record){var current={};Object.keys(record.before).forEach(function(key){current[key]=[record.node.style.getPropertyValue(key),record.node.style.getPropertyPriority(key)];var old=record.before[key];if(old[0])record.node.style.setProperty(key,old[0],old[1]);else record.node.style.removeProperty(key)});return current});
 var baseline;
 try{baseline=peers.map(box)}finally{records.forEach(function(record,i){Object.keys(saved[i]).forEach(function(key){var v=saved[i][key];record.node.style.setProperty(key,v[0],v[1])})})}
 var errors=[];
 actual.forEach(function(rect,i){if(inside(baseline[i])&&rect&&!inside(rect))errors.push('Text layer exceeds canvas: '+peerNames[i]);for(var j=0;j<i;j++)if(inside(baseline[i])&&inside(baseline[j])&&overlap(rect,actual[j])>overlap(baseline[i],baseline[j])+8)errors.push('Text layers overlap: '+peerNames[j]+','+peerNames[i])});
 if(!errors.length)previous=signature;return errors;
};
"""


class _Document(HTMLParser):
    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("document")
        self.stack = [self.root]
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        node = ET.SubElement(self.stack[-1], tag, {k: v or "" for k, v in attrs})
        if tag not in {"meta", "link", "img", "input", "br", "hr", "source", "area", "base", "embed", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].text = (self.stack[-1].text or "") + data


def _css_styles(markup, variant="", bilingual=False):
    import cssselect2
    import tinycss2

    document = _Document(markup)
    root = next((n for n in document.root.iter() if n.get("data-composition-id")), document.root)
    if variant:
        classes = [c for c in root.get("class", "").split() if not re.fullmatch(r"v\d\d", c)]
        root.set("class", " ".join(classes + [variant]))
    if bilingual:
        for classes in ("headline", "headline subtitle"):
            parent = ET.SubElement(root, "div", {"class": classes})
            ET.SubElement(parent, "div", {"class": "title-visual"})
        parent = ET.SubElement(root, "div", {"class": "cue row-0"})
        for classes in ("zh", "en"):
            ET.SubElement(parent, "div", {"class": classes})
    matcher = cssselect2.Matcher()
    for style in document.root.iter("style"):
        for rule in tinycss2.parse_stylesheet(style.text or "", skip_comments=True, skip_whitespace=True):
            if rule.type != "qualified-rule":
                continue
            try:
                selectors = cssselect2.compile_selector_list(tinycss2.serialize(rule.prelude))
            except cssselect2.SelectorError:
                continue
            declarations = tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
            for selector in selectors:
                matcher.add_selector(selector, declarations)
    styles = {}
    wrappers = list(cssselect2.ElementWrapper.from_html_root(document.root).iter_subtree())
    inherited = {"color", "-webkit-text-stroke", "-webkit-text-stroke-width", "-webkit-text-stroke-color"}
    for wrapper in wrappers:
        values = {k: v for k, v in styles.get(wrapper.parent, {}).items() if k in inherited or k.startswith("--")}
        winners = {}
        matches = matcher.match(wrapper)
        inline = wrapper.etree_element.get("style")
        if inline:
            matches.append(((1000000, 0, 0), 1000000, None, tinycss2.parse_declaration_list(inline)))
        for specificity, order, pseudo, declarations in matches:
            if pseudo:
                continue
            for declaration in declarations:
                if declaration.type != "declaration":
                    continue
                priority = (declaration.important, specificity, order)
                previous = winners.get(declaration.lower_name)
                if previous is None or priority >= previous[0]:
                    winners[declaration.lower_name] = (priority, tinycss2.serialize(declaration.value).strip())
        values.update({k: v[1] for k, v in winners.items()})
        for name, value in list(values.items()):
            for _ in range(8):
                expanded = re.sub(r"var\((--[\w-]+)(?:,\s*([^()]+))?\)", lambda m: values.get(m[1], m[2] or ""), value)
                if expanded == value:
                    break
                value = expanded
            values[name] = value
        shorthand = winners.get("-webkit-text-stroke")
        if shorthand:
            tokens = [t for t in tinycss2.parse_component_value_list(values["-webkit-text-stroke"]) if t.type != "whitespace"]
            width = next((t for t in tokens if t.type in {"number", "dimension"}), None)
            if width is not None:
                pieces = {"-webkit-text-stroke-width": tinycss2.serialize([width]),
                          "-webkit-text-stroke-color": tinycss2.serialize([t for t in tokens if t is not width]) or "currentColor"}
                for name, value in pieces.items():
                    if name not in winners or shorthand[0] >= winners[name][0]:
                        values[name] = value
        if values.get("color") in {"inherit", "unset", "currentColor", "currentcolor"}:
            values["color"] = styles.get(wrapper.parent, {}).get("color", "#000000")
        styles[wrapper] = values
    return wrappers, styles


def _color(value, fallback):
    from tinycss2.color3 import parse_color
    parsed = parse_color(value or "")
    if parsed is None or parsed == "currentColor":
        return fallback
    if parsed.alpha != 1:
        return "rgba(%d,%d,%d,%g)" % (round(parsed.red*255), round(parsed.green*255), round(parsed.blue*255), parsed.alpha)
    return "#%02X%02X%02X" % (round(parsed.red*255), round(parsed.green*255), round(parsed.blue*255))


def render_error(stdout, stderr):
    detail = b"\n".join((stdout or b"", stderr or b"")).decode("utf-8", "replace")
    match = re.search(r"Text (layer exceeds canvas|layers overlap): ([A-Za-z0-9_, -]+)", detail)
    if match:
        names = match[2].strip()[:120]
        return (f"文字层 {names} 超出画布，请减小字号或调整位置" if match[1] == "layer exceeds canvas"
                else f"文字层 {names} 发生重叠，请减小字号或调整位置")
    if "Font load failed" in detail:
        return "微调字体加载失败，请检查节点字体包"
    return None


class TextControls:
    def __init__(self, service, fixed, nine, labels):
        self.service, self.fixed, self.nine, self.labels = service, fixed, nine, labels

    def fonts(self):
        result = {}
        for attr in ("bundled_fonts", "reference_fonts", "private_fonts", "nine_grid_fonts"):
            for key, record in getattr(self.service, attr, {}).items():
                family = record.get("family") or key
                if record.get("path") and record.get("sha256"):
                    result[family] = record
        for mapping in getattr(self.service, "fixed_skill_fonts", {}).values():
            for family, record in mapping.items():
                if record.get("path") and record.get("sha256"):
                    result.setdefault(family, record)
        return result

    def semantic_roles(self, template_id, layer):
        if template_id in getattr(self.service, "reference_templates", {}):
            return [layer]
        if template_id == "nine-grid-reveal":
            return ["top1", "top2"] if layer == "top_text" else ["bottom2"]
        if template_id == BILINGUAL:
            return {"main_title":["top1", "top2", "top3"], "subtitle":["bottom2"]}.get(layer, [])
        return [FIXED_ROLES[layer]] if layer in FIXED_ROLES else []

    def _definition(self, template_id):
        service = self.service
        record = service.templates.get(template_id)
        if record is None:
            raise ValueError("请选择有效模板")
        selectors, specs = {}, {}
        if template_id in getattr(service, "reference_templates", {}):
            source = Path(service.reference_pack_root) / "index.html"
            for layer, metrics in service.reference_semantic_layouts[record["variant"]].items():
                specs[layer] = dict(metrics)
                selectors[layer] = '[data-var-text="%s"]' % layer
        elif template_id == "nine-grid-reveal" and getattr(service, "nine_grid_template", None):
            source = Path(service.nine_grid_root) / "index.html"
            for layer, spec in self.nine.items():
                family = "Noto Serif SC" if layer == "top_text" else "Noto Sans SC"
                specs[layer] = dict(family=family, font_size_px=spec["maximum"], font_weight=spec["weight"],
                                    max_width_px=spec["width"], max_lines=spec["max_lines"], stroke_px=2,
                                    minimum=spec["minimum"], maximum=spec["maximum"], line_height=spec["line_height"])
                selectors[layer] = '[data-var-text="%s"]' % layer
        elif template_id in getattr(service, "fixed_skill_templates", {}):
            source = Path(service.fixed_skill_roots[template_id]) / ("index.html.in" if template_id == BILINGUAL else "index.html")
            for layer, spec in self.fixed[template_id]["field_specs"].items():
                specs[layer] = dict(family=spec["family"], font_size_px=spec["maximum"], font_weight=spec["weight"],
                                    max_width_px=spec["width"], max_lines=spec["max_lines"], stroke_px=spec.get("stroke_px", 0),
                                    minimum=spec["minimum"], maximum=spec["maximum"], line_height=spec["line_height"])
                selectors[layer] = '[data-var-text="%s"]' % layer
            if template_id == BILINGUAL:
                specs = {"main_title": specs["title"], "subtitle": specs["cta"],
                         "caption_zh": dict(family="Noto Serif SC", font_size_px=82, font_weight=700, max_width_px=950, max_lines=1, stroke_px=1, line_height=1.24),
                         "caption_en": dict(family="Noto Serif SC", font_size_px=38, font_weight=400, max_width_px=950, max_lines=1, stroke_px=2, line_height=1.4)}
                selectors = {"main_title": ".headline:not(.subtitle) .title-visual", "subtitle": ".headline.subtitle .title-visual",
                             "caption_zh": ".cue .zh", "caption_en": ".cue .en"}
        else:
            return None
        if source.is_symlink() or not source.is_file():
            raise ValueError("模板文字样式源不可用")
        font_identity = {family: record["sha256"] for family, record in self.fonts().items()}
        stamp = source.stat()
        cache_key = (str(source), stamp.st_mtime_ns, stamp.st_size, ADAPTER_SHA256, json.dumps([specs, font_identity], sort_keys=True))
        cache = getattr(service, "_text_definition_cache", None)
        if cache is None:
            cache = service._text_definition_cache = {}
        if template_id in cache and cache[template_id][0] == cache_key:
            return copy.deepcopy(cache[template_id][1])
        markup = source.read_text(encoding="utf-8")
        wrappers, styles = _css_styles(markup, record.get("variant", ""), template_id == BILINGUAL)
        layers = {}
        for layer, metrics in specs.items():
            node = next((n for n in wrappers if n.matches(selectors[layer])), None)
            if node is None:
                raise ValueError("模板文字图层不存在：" + layer)
            css = styles[node]
            stroke = css.get("-webkit-text-stroke", "")
            stroke_color = re.sub(r"^[\d.]+(?:px|em)\s*", "", stroke)
            color = _color(css.get("color"), "#000000")
            stroke_width = re.fullmatch(r"([\d.]+)(px|em)?", css.get("-webkit-text-stroke-width", "0"))
            actual_stroke = (float(stroke_width[1]) * (metrics["font_size_px"] if stroke_width[2] == "em" else 1)) if stroke_width else 0
            defaults = dict(font_family=metrics["family"], font_size_px=metrics["font_size_px"], color=color,
                            offset_x_px=0, offset_y_px=0, stroke_width_px=round(actual_stroke, 3),
                            stroke_color=_color(css.get("-webkit-text-stroke-color") or stroke_color, color))
            layers[layer] = {"defaults": defaults, "font_weight": metrics["font_weight"], "max_width_px": metrics["max_width_px"],
                             "semantic_layers": self.semantic_roles(template_id, layer),
                             "max_lines": metrics["max_lines"], "font_size_mode": "auto" if metrics.get("minimum") != metrics.get("maximum") and "minimum" in metrics else "fixed",
                             "default_size_range_px": [metrics.get("minimum", metrics["font_size_px"]), metrics.get("maximum", metrics["font_size_px"])]}
        digest = hashlib.sha256(markup.encode("utf-8") + ADAPTER_SHA256.encode("ascii")
                                + json.dumps([VERSION, template_id, layers, font_identity], sort_keys=True).encode()).hexdigest()
        definition = ({"text_revision": digest, "layers": layers}, selectors, specs)
        cache[template_id] = (cache_key, copy.deepcopy(definition))
        return definition

    def node_contract(self):
        revisions = {}
        for template_id in self.service.templates:
            definition = self._definition(template_id)
            if definition is not None:
                revisions[template_id] = definition[0]["text_revision"]
        return {"version": VERSION, "templates": revisions}

    def describe(self, template_id):
        definition = self._definition(template_id)
        if definition is None:
            return None
        controls = definition[0]
        return dict(controls, contract_version=VERSION, text_tunable=True, preview_supported=False,
                    coordinate_system="1080x1920; offsets relative to template defaults; positive x right, positive y down",
                    fields={**{key: {"type": "integer" if key == "font_size_px" else "number", "minimum": bounds[0], "maximum": bounds[1]} for key, bounds in NUMBERS.items()},
                            **{key: {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"} for key in sorted(COLORS)},
                            "font_family": {"type": "string", "enum": sorted(self.fonts())}},
                    fonts=[{"family": family, "label": self.labels.get(family, family), "sha256": self.fonts()[family]["sha256"]} for family in sorted(self.fonts())])

    def normalize(self, raw, controls=None):
        value = raw.get("text_overrides")
        if value is None or value == {}:
            return {}
        if not isinstance(value, dict) or not 1 <= len(value) <= 8:
            raise ValueError("text_overrides 必须是文字层参数对象")
        if raw.get("overrides") or raw.get("preview_id"):
            raise ValueError("逐层文字微调不能与旧版 overrides 或 preview_id 混用")
        controls = controls or self.describe(str(raw.get("template_id") or ""))
        if controls is None:
            raise ValueError("当前模板不支持逐层文字微调")
        if raw.get("text_revision") != controls["text_revision"]:
            raise ValueError("文字样式版本不匹配，请重新读取模板可调参数 text_revision")
        result = {}
        for layer, values in sorted(value.items()):
            if layer not in controls["layers"]:
                raise ValueError("模板不支持文字层：" + str(layer))
            if not isinstance(values, dict) or set(values) - FIELDS:
                raise ValueError("文字层参数格式或字段无效：" + layer)
            cleaned = {}
            for key, item in sorted(values.items()):
                if key in NUMBERS:
                    low, high = NUMBERS[key]
                    if type(item) not in (int, float) or not low <= item <= high or not math.isfinite(item):
                        raise ValueError(f"{layer}.{key} 必须在 {low} 到 {high} 范围内")
                    cleaned[key] = int(item) if key == "font_size_px" else round(float(item), 3)
                    if key == "font_size_px" and item != int(item):
                        raise ValueError(f"{layer}.{key} 必须是整数像素")
                elif key in COLORS:
                    if not isinstance(item, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", item):
                        raise ValueError(f"{layer}.{key} 必须是 #RRGGBB 颜色")
                    cleaned[key] = item.upper()
                elif not isinstance(item, str) or item not in controls["fields"]["font_family"]["enum"]:
                    raise ValueError("字体不可用，请从模板参数返回的字体列表选择")
                else:
                    cleaned[key] = item
            if cleaned:
                result[layer] = cleaned
        return result

    def _weight(self, family, requested):
        record = self.fonts()[family]
        try:
            font = ImageFont.truetype(str(record["path"]), 16)
        except (OSError, ValueError) as exc:
            raise ValueError("微调字体不可读取，请重新选择字体") from exc
        try:
            axes = font.get_variation_axes()
        except OSError:
            return 400
        axis = next((a for a in axes if a["name"].lower() in (b"weight", "weight")), None)
        return max(int(axis["minimum"]), min(int(axis["maximum"]), int(requested))) if axis else 400

    def view(self, raw, values):
        service = copy.copy(self.service)
        service._text_style_view = True
        service._text_style_values = values
        service._text_font_registry = self.fonts()
        service.reference_measure_fonts = {}
        service.nine_grid_measure_fonts = {}
        service.reference_semantic_layouts = copy.deepcopy(self.service.reference_semantic_layouts)
        template_id = raw["template_id"]
        variant = self.service.templates[template_id]["variant"]
        service._fixed_text_configs = copy.deepcopy(self.fixed)
        service._nine_grid_text_specs = copy.deepcopy(self.nine)
        definition = self._definition(template_id)[0]
        weights = {layer: self._weight(v["font_family"], definition["layers"][layer]["font_weight"]) for layer, v in values.items() if "font_family" in v}

        def metrics(target, layer, spec=False):
            settings = values.get(layer, {})
            if "font_family" in settings:
                target["family"] = settings["font_family"]
                target["weight" if spec else "font_weight"] = weights[layer]
            if "font_size_px" in settings:
                size = int(settings["font_size_px"])
                if spec:
                    target.update(minimum=size, maximum=size)
                else:
                    target["font_size_px"] = size
            if "stroke_width_px" in settings:
                target["stroke_px"] = math.ceil(settings["stroke_width_px"])
            width_key = "width" if spec else "max_width_px"
            target[width_key] -= 2 * abs(settings.get("offset_x_px", 0))

        if template_id in getattr(service, "reference_templates", {}):
            for layer, target in service.reference_semantic_layouts[variant].items():
                metrics(target, layer)
        elif template_id == "nine-grid-reveal":
            for layer, target in service._nine_grid_text_specs.items():
                metrics(target, layer, True)
                target.setdefault("family", "Noto Serif SC" if layer == "top_text" else "Noto Sans SC")
                for role in self.semantic_roles(template_id, layer):
                    if role in service.reference_semantic_layouts.get(variant, {}):
                        metrics(service.reference_semantic_layouts[variant][role], layer)
        else:
            config = service._fixed_text_configs[template_id]
            role_layers = {}
            for field, target in config["field_specs"].items():
                layer = ("subtitle" if field == "cta" else "main_title") if template_id == BILINGUAL else field
                metrics(target, layer, True)
                for role in self.semantic_roles(template_id, layer):
                    role_layers.setdefault(role, set()).add(layer)
            for role, layers in role_layers.items():
                if role not in service.reference_semantic_layouts[variant] or not any(layer in values for layer in layers):
                    continue
                base = service.reference_semantic_layouts[variant][role]
                candidates = []
                for layer in sorted(layers):
                    candidate = dict(base)
                    metrics(candidate, layer)
                    candidates.append(candidate)
                # Shared CTA/body roles must satisfy every output field, not
                # whichever field happened to be visited last.
                strictest = max(candidates, key=lambda c: service._reference_text_width("文字测试AB", c)/c["max_width_px"])
                strictest["max_width_px"] = min(c["max_width_px"] for c in candidates)
                service.reference_semantic_layouts[variant][role] = strictest
        service._text_style_weights = weights
        return service

    def freeze(self, payload, values, view):
        template_id = payload["template_id"]
        controls = self.describe(template_id)
        frozen_fonts = {}
        for settings in values.values():
            family = settings.get("font_family")
            if family:
                record = self.fonts()[family]
                path = Path(record["path"])
                if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                    raise ValueError("微调字体文件已变化，请重新选择")
                frozen_fonts[family] = {"sha256": record["sha256"], "file": path.name}
        baseline_sizes, effective_sizes = {}, {}
        if template_id != BILINGUAL and payload.get("semantic_layout"):
            if "_fixed_skill_template" in payload:
                effective_sizes = payload["_fixed_skill_template"]["text"]["font_size_px"]
                try:
                    baseline_sizes = self.service._fixed_skill_text_layout(template_id, payload["top_text"], payload["bottom_text"], payload["semantic_layout"])["font_size_px"]
                except ValueError:
                    baseline_sizes = {k: v["defaults"]["font_size_px"] for k, v in controls["layers"].items()}
            elif "_nine_grid_template" in payload:
                effective_sizes = payload["_nine_grid_template"]["text"]["font_size_px"]
                try:
                    baseline_sizes = self.service._nine_grid_text_layout(payload["top_text"], payload["bottom_text"], payload["semantic_layout"])["font_size_px"]
                except ValueError:
                    baseline_sizes = {k: v["defaults"]["font_size_px"] for k, v in controls["layers"].items()}
        payload["_text_style"] = {"version": VERSION, "revision": controls["text_revision"], "overrides": values,
                                  "fonts": frozen_fonts, "weights": view._text_style_weights, "controls": controls,
                                  "baseline_sizes": baseline_sizes, "effective_sizes": effective_sizes}
        # Captions do not pass through the title packer.
        if template_id == BILINGUAL and payload.get("narration_plan"):
            for layer, key in (("caption_zh", "text"), ("caption_en", "en")):
                style = dict(controls["layers"][layer]["defaults"], **values.get(layer, {}))
                metric = {"family": style["font_family"], "font_weight": view._text_style_weights.get(layer, controls["layers"][layer]["font_weight"]),
                          "font_size_px": style["font_size_px"], "stroke_px": math.ceil(style["stroke_width_px"]), "letter_spacing_em": 0}
                for cue in payload["narration_plan"]["cues"]:
                    if view._reference_text_width(cue[key], metric) > 950-2*abs(style["offset_x_px"]):
                        raise ValueError("微调后字幕超出文字区域：" + layer)
        return payload

    def inject(self, payload, markup, workdir):
        frozen = payload.get("_text_style")
        if not payload.get("text_overrides"):
            return markup
        if not isinstance(frozen, dict) or frozen.get("overrides") != payload["text_overrides"]:
            raise ValueError("任务缺少冻结的文字样式，禁止静默恢复默认")
        definition = self._definition(payload["template_id"])
        if definition[0]["text_revision"] != frozen.get("revision"):
            raise ValueError("模板文字样式版本已变化")
        font_faces, aliases = [], {}
        for family, saved in frozen["fonts"].items():
            record = self.fonts().get(family)
            if not record or record["sha256"] != saved["sha256"]:
                raise ValueError("冻结的微调字体不可用或已变化")
            source = Path(record["path"])
            if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != saved["sha256"]:
                raise ValueError("冻结的微调字体校验失败")
            suffix = source.suffix.lower()
            if suffix not in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
                raise ValueError("微调字体格式不支持")
            relative = "assets/matrix-text-fonts/" + saved["sha256"] + suffix
            target = Path(workdir) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            alias = "MatrixText_" + saved["sha256"]
            aliases[family] = alias
            font_faces.append('@font-face{font-family:"%s";src:url("%s");font-weight:100 900;font-display:block}' % (alias, relative))
        operations = []
        mapping = {"font_size_px":"font-size", "color":"color", "stroke_width_px":"-webkit-text-stroke-width", "stroke_color":"-webkit-text-stroke-color"}
        for layer, settings in frozen["overrides"].items():
            properties = {}
            for field, prop in mapping.items():
                if field in settings:
                    properties[prop] = str(settings[field]) + ("px" if field.endswith("_px") else "")
            if "font_family" in settings:
                properties["font-family"] = '"' + aliases[settings["font_family"]] + '"'
                properties["font-weight"] = str(frozen["weights"][layer])
            if (set(settings) & {"font_family", "font_size_px", "stroke_width_px", "offset_x_px"}
                    and layer in frozen.get("effective_sizes", {})):
                properties["font-size"] = str(frozen["effective_sizes"][layer]) + "px"
            if "offset_x_px" in settings or "offset_y_px" in settings:
                properties["translate"] = "%spx %spx" % (settings.get("offset_x_px", 0), settings.get("offset_y_px", 0))
            if "color" in settings:
                properties["-webkit-text-fill-color"] = settings["color"]
            baseline = {"font-size": str(frozen["baseline_sizes"][layer])+"px"} if layer in frozen.get("baseline_sizes", {}) else {}
            operations.append({"selector": definition[1][layer], "properties": properties, "baseline": baseline})
        serialized = json.dumps(operations, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        selectors = json.dumps([{"layer": key, "selector": value} for key, value in definition[1].items()], ensure_ascii=True).replace("<", "\\u003c")
        script = '<style id="matrix-text-fonts">%s</style><script id="matrix-text-overrides">(function(){var rows=%s,selectors=%s;%s})();</script>' % ("".join(font_faces), serialized, selectors, LAYOUT_GUARD)
        if "</body>" not in markup:
            raise ValueError("模板缺少文字样式注入位置")
        return markup.replace("</body>", script + "</body>", 1)
