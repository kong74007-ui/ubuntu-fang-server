"""Website adapters for the pinned September Skill templates."""
import copy
import html
import json
import math
import re

UPSTREAM_COMMIT = "981ecf0584d963c6e26a2f9d5cfa7fd6985758d2"
VERSION = "0.8.38"
INSET = "inset-flip-whip"
OPENING = "fixed-opening-whip"
BILINGUAL = "bilingual-stagger-salon"
IDS = (INSET, OPENING, BILINGUAL)


def configs(base):
    result = {}
    specs = [
        (INSET, "小窗推拉·翻片甩切", "inset-flip", 443, 7, 150, "bgm",
         "6f011a2a509560d47d495bd5123bbd50ef0d7a62a2fd61f5d44d955e13444a1a", 14.745011),
        (OPENING, "固定双镜开场·横向甩切", "fixed-opening", 519, 4, 180, "reference-bgm",
         "02421fad56a79ff3116a1a45b09787f3f118ccfc48421b41e6e9edbffeabeb2f", 17.3),
        (BILINGUAL, "双语错位字幕·配音成片", "bilingual-stagger", 240, 3, 86, "",
         "", 0),
    ]
    for identifier, name, variant, frames, count, clip_frames, audio_id, sha, audio_duration in specs:
        c = copy.deepcopy(base)
        c.update(name=name, description=name, variant=variant, manifest_version=1,
                 hyperframes_version=VERSION, composition_id="main" if identifier == BILINGUAL else identifier, frames=frames,
                 duration=frames / 30, required_visuals=count, slot_frames=(clip_frames,) * count,
                 slot_heights=(1920 if identifier == BILINGUAL else 608,) * count,
                 media_paths=tuple(f"assets/media/{i:02d}.mp4" for i in range(count)),
                 bgm_path="" if identifier == BILINGUAL else "assets/media/reference-bgm.m4a",
                 bgm_sha256=sha, bgm_duration=audio_duration, audio_id=audio_id,
                 extra_variable_ids=(), required_files=("assets/vendor/gsap.min.js", "upstream-template.json"))
        c.pop("still_frames", None)
        if identifier == INSET:
            c["intro_backplate"] = True
        if identifier == OPENING:
            c["slot_frames"] = (153, 85, 153, 78)
            c["required_files"] += ("assets/media/opening-a-subtitles.mp4", "assets/media/opening-b-subtitles.mp4")
            c["fixed_assets"] = {
                "assets/media/opening-a-subtitles.mp4":"bbccb7b90d456a45598f6e3c3a066ef011d12d7caf2151aead345a7506d6c54b",
                "assets/media/opening-b-subtitles.mp4":"bbe870631ae8aa5db798d41a5bc601bea0a43106a4f2f05471dba6b7576e7b2a",
            }
        if identifier == BILINGUAL:
            c["required_files"] += ("index.html.in",)
            c["font_files"] = {"Ma Shan Zheng": "MaShanZheng-Regular.ttf", "Noto Serif SC": "NotoSerifSC-Variable.ttf"}
            c["duration_mode"] = "narration"
            for layer, metrics in c["semantic"].items():
                main = layer != "bottom2"
                metrics.update(family="Ma Shan Zheng" if main else "Noto Serif SC", font_weight=400 if main else 800,
                               font_size_px=128 if main else 80, max_width_px=950, max_lines=12, stroke_px=2, letter_spacing_em=5/128 if main else 0)
            for field, metrics in c["field_specs"].items():
                main = field != "cta"
                size = 128 if main else 80
                metrics.update(family="Ma Shan Zheng" if main else "Noto Serif SC", weight=400 if main else 800,
                               minimum=size, maximum=size, width=950, height=2000, max_lines=12, line_height=1.2,
                               stroke_px=2, letter_spacing_em=5/128 if main else 0)
        result[identifier] = c
    return result


def validate_plan(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
        raise ValueError("双语模板缺少有效字幕时间轴")
    def number(x):
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError("双语字幕时间无效")
        return float(x)
    voice = number(value.get("audio_duration"))
    duration = number(value.get("duration"))
    expected = math.ceil((voice + .6) * 30 - 1e-7) / 30
    if not .1 <= voice <= 60 or abs(duration - expected) > .00001:
        raise ValueError("双语模板时长必须跟随配音并保留尾帧")
    fingerprint = value.get("audio_fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("双语模板配音身份无效")
    raw_cues = value.get("cues")
    if not isinstance(raw_cues, list) or not 1 <= len(raw_cues) <= 60:
        raise ValueError("双语字幕数量无效")
    cues = []
    previous = [0., 0.]
    for raw in raw_cues:
        if not isinstance(raw, dict):
            raise ValueError("双语字幕格式无效")
        text, en = raw.get("text"), raw.get("en")
        times = raw.get("times")
        row = raw.get("row", 0)
        if (not isinstance(text, str) or not 1 <= len(text) <= 10
                or not isinstance(en, str) or not 1 <= len(en) <= 48
                or not isinstance(times, list) or len(times) != len(text)
                or type(row) is not int or row not in (0, 1)):
            raise ValueError("双语字幕文案或逐字时间无效")
        times = [number(t) for t in times]
        end = number(raw.get("end"))
        en = ' '.join(en.split())
        if (times != sorted(times) or times[0] < 0 or times[-1] > voice
                or times[-1] + .2 > end + .001 or end > duration
                or times[0] + .23 + .035 * max(0, len(en.split())-1) > end + .001
                or previous[row] > times[0] + .055):
            raise ValueError("双语字幕时间重叠或超出配音范围")
        yellow = raw.get("yellow", [])
        if not isinstance(yellow, list) or any(type(i) is not int or not 0 <= i < len(text) for i in yellow):
            raise ValueError("双语字幕强调位置无效")
        previous[row] = end
        cues.append(dict(text=text, en=en, times=times, end=end, row=row, yellow=yellow))
    if sum(len(c["text"]) for c in cues) > 180:
        raise ValueError("双语字幕总长度无效")
    count = value.get("visual_count", min(20, max(3, math.ceil(duration / 2.8))))
    if type(count) is not int or not 3 <= count <= 20:
        raise ValueError("双语模板需要 3-20 段本人视频")
    return dict(version=1, audio_duration=voice, duration=duration, audio_fingerprint=fingerprint, cues=cues, visual_count=count)


def runtime_config(base, payload):
    if payload.get("template_id") != BILINGUAL or not payload.get("narration_plan"):
        return base
    plan = validate_plan(payload["narration_plan"])
    duration = plan["duration"]
    count = plan["visual_count"]
    starts = [round(i * duration / count, 6) for i in range(count)]
    ends = [round(min(duration, (i + 1) * duration / count + (.18 if i < count - 1 else 0)), 6) for i in range(count)]
    return dict(base, duration=duration, frames=round(duration * 30), required_visuals=count,
                slot_frames=tuple(max(60,math.ceil((b-a)*30 - 1e-6)) for a,b in zip(starts,ends)),
                slot_heights=(1920,) * count,
                media_paths=tuple(f"assets/media/{i:02d}.mp4" for i in range(count)),
                starts=starts, ends=ends)


def build_bilingual(markup, fields, sizes, plan, config):
    plan = validate_plan(plan)
    duration = plan["duration"]
    blocks, moves = [], []
    for i, (start, end, path) in enumerate(zip(config["starts"], config["ends"], config["media_paths"])):
        length = end - start
        blocks.append(f'<div class="shot"><div class="camera" id="cam-{i}"><video class="clip" src="{path}" data-start="{start}" data-duration="{length}" data-track-index="{i+1}" muted playsinline></video></div></div>')
        moves.append(f"tl.fromTo('#cam-{i}',{{scale:1}},{{scale:1.03,duration:{length},ease:'none',immediateRender:false}},{start});")
        if i:
            moves.append(f"gsap.set('#cam-{i}',{{opacity:0}});tl.to('#cam-{i}',{{opacity:1,duration:.18,ease:'none'}},{start});")
    titles, motions = [], []
    main_lines = [line for key in ('title','subtitle','body') for line in fields.get(key,'').splitlines() if line]
    subtitle_lines = [line for line in fields.get('cta','').splitlines() if line]
    for style, lines in (('', main_lines), (' subtitle', subtitle_lines)):
        if not lines or duration / len(lines) < .8:
            raise ValueError('双语模板标题阶段过密，请缩短标题或副标题')
        for i, line in enumerate(lines):
            key = ('sub' if style else 'main') + str(i)
            start, end = i * duration / len(lines), (i+1) * duration / len(lines)
            titles.append(f'<div class="headline{style}"><div id="heading-{key}" class="title-visual" data-var-text="phase-{key}">{html.escape(line)}</div></div>')
            motions.append(f"title('#heading-{key}',{start},{duration+.3 if i==len(lines)-1 else end},42);")
    replacements = {"__MEDIA_HTML__":"\n".join(blocks), "__MEDIA_MOTION__":"\n".join(moves),
                    "__TITLE_HTML__":"\n".join(titles), "__TITLE_MOTION__":"\n".join(motions),
                    "__DURATION__":str(duration), "__VOICE_DURATION__":str(plan["audio_duration"]), "__VOICE_PATH__":""}
    for key, value in replacements.items():
        markup = markup.replace(key, value)
    markup = re.sub(r'<audio\b[^>]*>\s*</audio>', '', markup)
    # Mark the timed-caption container as a dynamic GPU text layer without replacing its spans.
    markup = markup.replace('id="captions"', 'id="captions" data-var-text="timed_captions"')
    return markup
