"""Verify offline upstream source archives before extracting or compiling them."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def verify(root, manifest):
    root = Path(root).resolve(strict=True)
    seen = set()
    for entry in manifest['sources']:
        name = entry['archive']
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', name) or name in seen:
            raise ValueError('source_archive_name_invalid')
        seen.add(name)
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ValueError('source_archive_missing_or_linked')
        expected = entry['sha256']
        if not re.fullmatch(r'[a-f0-9]{64}', expected):
            raise ValueError('source_digest_invalid')
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(data)
        if digest.hexdigest() != expected:
            raise ValueError('source_digest_mismatch:' + name)
    if not seen:
        raise ValueError('source_manifest_empty')
    return len(seen)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('sources', type=Path)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))
    print(json.dumps({'verified_archives': verify(args.sources, manifest)}))
