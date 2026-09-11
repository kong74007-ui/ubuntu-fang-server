#!/usr/bin/env python3
"""Installer gate: audit the reference pack AFTER the palette overlay is injected.

Runs the same production parsing the service uses (``reference_pack_layer_audit``)
so an overlay that shadows layer detection fails HERE, before the release
symlink is switched or the service restarted, instead of crashing at startup.

The expected palette version / count are read from the production module and the
applier itself (never from the shell), so no undefined shell variable can creep in.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


EXPECTED_TEMPLATES = 17
EXPECTED_TOP_LAYER_COUNTS = {"2": 6, "3": 10, "4": 1}
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack-root", type=Path, required=True)
    args = parser.parse_args()
    index_path = args.pack_root / "index.html"
    if not index_path.is_file():
        print("reference pack index.html is missing", file=sys.stderr)
        return 1
    index_html = index_path.read_text(encoding="utf-8")

    applier = _load(HERE / "apply-public-template-palettes.py", "palette_applier")
    matrix = _load(
        REPO_ROOT / "server" / "matrix_template_api.py", "matrix_template_api_compat",
    )
    if f'id="{applier.STYLE_ID}"' not in index_html:
        print("palette overlay is missing from reference pack", file=sys.stderr)
        return 1
    palette_version = getattr(matrix, "PUBLIC_TEMPLATE_PALETTE_VERSION", "")
    palette_count = getattr(matrix, "PUBLIC_TEMPLATE_PALETTE_COUNT", None)
    if (
        not isinstance(palette_version, str) or not palette_version
        or not isinstance(palette_count, int) or isinstance(palette_count, bool)
        or palette_count <= 0
    ):
        print("public template palette constants are invalid", file=sys.stderr)
        return 1
    if len(applier.PALETTES) != palette_count:
        print(
            f"applier defines {len(applier.PALETTES)} palettes but the service "
            f"advertises {palette_count}",
            file=sys.stderr,
        )
        return 1

    try:
        audit = matrix.reference_pack_layer_audit(index_html)
    except Exception as exc:  # noqa: BLE001 - report and fail the install
        print(f"reference palette compatibility failed: {exc}", file=sys.stderr)
        return 1
    if audit["templates"] != EXPECTED_TEMPLATES:
        print(f"expected {EXPECTED_TEMPLATES} templates, got {audit['templates']}",
              file=sys.stderr)
        return 1
    if audit["top_layer_counts"] != EXPECTED_TOP_LAYER_COUNTS:
        print(f"unexpected top layer counts {audit['top_layer_counts']}",
              file=sys.stderr)
        return 1
    for key, size in audit["font_sizes"].items():
        if not isinstance(size, int) or not 8 <= size <= 240:
            print(f"invalid font size for {key}: {size}", file=sys.stderr)
            return 1
    print(json.dumps({
        "ok": True,
        "palette_version": palette_version,
        "palette_count": palette_count,
        "templates": audit["templates"],
        "top_layer_counts": audit["top_layer_counts"],
        "font_sizes": len(audit["font_sizes"]),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
