#!/usr/bin/env python3
"""Render Pixelle config from existing root-readable env files without eval."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum() or not key[0].isalpha():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render(llm: dict[str, str], runninghub: dict[str, str]) -> str:
    api_key = llm.get("OPENAI_API_KEY", "").strip()
    runninghub_key = runninghub.get("RUNNINGHUB_API_KEY", "").strip()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing")
    if not runninghub_key:
        raise ValueError("RUNNINGHUB_API_KEY is missing")

    base_url = llm.get("OPENAI_BASE", "").strip() or "https://api.openai.com/v1"
    model = llm.get("PIXELLE_LLM_MODEL", "").strip() or "gpt-4o-mini"
    image_prefix = (
        "Professional Chinese social media editorial illustration, clean composition, "
        "cinematic lighting, no text, no logo, no watermark"
    )
    video_prefix = "Professional cinematic visual, clean composition, no text, no logo, no watermark"
    return f"""project_name: Huangque-Text-Video

llm:
  api_key: {quoted(api_key)}
  base_url: {quoted(base_url)}
  model: {quoted(model)}

api_providers:
  common:
    print_model_input: false
    local_proxy: ""
  openai:
    api_key: {quoted(api_key)}
    base_url: {quoted(base_url)}
    use_proxy: false
  dashscope:
    api_key: ""
    base_url: "https://dashscope.aliyuncs.com/api/v1"
    use_proxy: false
  ark:
    api_key: ""
    base_url: "https://ark.cn-beijing.volces.com/api/v3"
    use_proxy: false
  kling:
    base_url: "https://api-beijing.klingai.com"
    access_key: ""
    secret_key: ""
    use_proxy: false

comfyui:
  comfyui_url: "http://127.0.0.1:8188"
  comfyui_api_key: ""
  runninghub_api_key: {quoted(runninghub_key)}
  runninghub_concurrent_limit: 5
  tts:
    default_workflow: "selfhost/tts_edge.json"
  image:
    default_workflow: "runninghub/image_flux.json"
    prompt_prefix: {quoted(image_prefix)}
  video:
    default_workflow: "runninghub/video_wan2.1_fusionx.json"
    prompt_prefix: {quoted(video_prefix)}

template:
  default_template: "1080x1920/image_default.html"
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-env", type=Path, required=True)
    parser.add_argument("--runninghub-env", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    text = render(parse_env(args.llm_env), parse_env(args.runninghub_env))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    old_umask = os.umask(0o077)
    try:
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(args.output)
    finally:
        os.umask(old_umask)
    print(f"rendered {args.output} with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
