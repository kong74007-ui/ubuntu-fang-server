#!/usr/bin/env python3
"""用生产代码链路把原始 payload 转成模板变量，并输出可核查 manifest。

链路（与服务端 /v1/jobs 完全一致）：
    validate_payload(..., require_reference_semantic_layout=True)
      -> _freeze_font_provenance(job_id, payload)
        -> _reference_template.source_text / display_text

用法（在 fang 上，已 source 生产 env）:
    python3 pipeline_manifest.py /tmp/accept_raw.json /tmp/pipeline_manifest.json
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

SOURCE = "/opt/huangque/matrix-template-video/source/api.py"


def load_api():
    spec = importlib.util.spec_from_file_location("matrix_api_prod", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["matrix_api_prod"] = module
    spec.loader.exec_module(module)
    return module


def env_path(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def build_service(api):
    return api.MatrixTemplateService(
        data_root=Path(env_path("MATRIX_TEMPLATE_DATA_ROOT", "/var/lib/huangque-matrix-template")),
        skill_root=Path(env_path("MATRIX_TEMPLATE_SKILL_ROOT", "/opt/huangque/matrix-template-video/source/skill/script-to-matrix-video")),
        library_url=env_path("PIXELLE_MATERIAL_LIBRARY_URL", "http://127.0.0.1:8111"),
        library_token=os.environ.get("PIXELLE_MATERIAL_LIBRARY_TOKEN", ""),
        pexels_api_key=os.environ.get("PEXELS_API_KEY", ""),
        legacy_templates_enabled=False,
        python=env_path("MATRIX_TEMPLATE_PYTHON", sys.executable),
        private_font_root=Path(env_path("MATRIX_TEMPLATE_PRIVATE_FONT_ROOT", "/var/lib/huangque-matrix-template/private-fonts")),
        reference_skill_root=Path(env_path("MATRIX_TEMPLATE_REFERENCE_SKILL_ROOT", "")) or None,
        nine_grid_root=Path(env_path("MATRIX_TEMPLATE_NINE_GRID_ROOT", "")) or None,
        triple_strip_root=Path(env_path("MATRIX_TEMPLATE_TRIPLE_STRIP_ROOT", "")) or None,
        yellow_banner_root=Path(env_path("MATRIX_TEMPLATE_YELLOW_BANNER_ROOT", "")) or None,
        fan_whip_root=Path(env_path("MATRIX_TEMPLATE_FAN_WHIP_ROOT", "")) or None,
        brush_panel_root=Path(env_path("MATRIX_TEMPLATE_BRUSH_PANEL_ROOT", "")) or None,
        hyperframes_cli=Path(env_path("MATRIX_TEMPLATE_HYPERFRAMES_CLI", "/usr/local/bin/hyperframes")),
        nine_grid_hyperframes_cli=Path(env_path("MATRIX_TEMPLATE_NINE_GRID_HYPERFRAMES_CLI", "/opt/huangque/matrix-template-video/source/nine-grid-runtime/node_modules/.bin/hyperframes")),
        motion_v2_hyperframes_cli=Path(env_path("MATRIX_TEMPLATE_MOTION_V2_HYPERFRAMES_CLI", "/opt/huangque/matrix-template-video/source/motion-v2-runtime/node_modules/.bin/hyperframes")),
        hyperframes_browser=Path(env_path("MATRIX_TEMPLATE_HYPERFRAMES_BROWSER", "/usr/bin/google-chrome-stable")),
        hyperframes_gsap=(Path(v) if (v := env_path("MATRIX_TEMPLATE_HYPERFRAMES_GSAP", "")) else None),
        start_worker=False,
    )


def main() -> int:
    raw_path, out_path = sys.argv[1], sys.argv[2]
    templates = json.load(open(raw_path, encoding="utf-8"))
    api = load_api()
    # 注意：__init__ 已完整构建目录（reference + nine-grid + fixed-skill），
    # 重复调用 _load_reference_catalog() 会让字体映射检查看到九宫格的
    # Noto Serif SC 而误报，故不再手动调用。
    service = build_service(api)

    manifest = {"source": SOURCE, "templates": {}}
    for tid, raw in templates.items():
        payload = service.validate_payload(
            raw, require_available_font=False,
            require_reference_semantic_layout=True,
        )
        frozen = service._freeze_font_provenance("0" * 32, payload)
        ref = frozen["_reference_template"]
        display, source = ref["display_text"], ref["text"]
        layers = service.reference_templates[tid]["text_layers"]
        entry = {
            "template_id": tid,
            "variant": ref["variant"],
            "duration": ref["duration"],
            "text_layers": layers,
            "input": {
                "top_text": payload["top_text"],
                "bottom_text": payload["bottom_text"],
                "semantic_layout": payload["semantic_layout"],
            },
            "source_text": source,
            "display_text": display,
            "top_layer_count": ref.get("top_layer_count"),
        }
        manifest["templates"][tid] = entry
        print("== %s (%s) dur=%s layers=%s" % (tid, ref["variant"], ref["duration"], layers))
        for key in ("top1", "top2", "top3", "bottom1", "bottom2"):
            print("   %-8s source=%-30r display=%r" % (key, source[key], display[key]))
    json.dump(manifest, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\nmanifest ->", out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
