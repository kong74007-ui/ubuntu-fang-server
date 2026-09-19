#!/usr/bin/env python3
"""Prepare private verification hints before enabling a synchronized library."""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from material_library import MaterialLibrary


def prepare(root, output, workers=4):
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("workers must be 1 through 4")
    library = MaterialLibrary(root)
    library.refresh()
    before = hashlib.sha256(library.index_path.read_bytes()).hexdigest()
    materials = tuple(library._materials)
    started = time.monotonic()

    def check(material):
        if not library._is_available(material):
            return "unavailable"
        if material.media_type == "video" and not library._video_color_compatible(
            material, [1, time.monotonic() + 15],
        ):
            return "incompatible"
        return "compatible"

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        outcomes = list(pool.map(check, materials))
    if hashlib.sha256(library.index_path.read_bytes()).hexdigest() != before:
        raise RuntimeError("index changed during preparation")
    library.write_prepared_cache(output)
    return {"records": len(materials), "compatible": outcomes.count("compatible"),
            "incompatible": outcomes.count("incompatible"),
            "unavailable": outcomes.count("unavailable"),
            "seconds": round(time.monotonic() - started, 3),
            "index_sha256": before,
            "cache_sha256": hashlib.sha256(Path(output).read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root, args.output, args.workers)), flush=True)


if __name__ == "__main__":
    main()
