"""Launch a Windows worker from private environment configuration, without a shell."""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import sys
import traceback


def read_config(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or set(value) != {'version', 'mode', 'program', 'env', 'log_dir'}:
        raise ValueError('Invalid Windows worker configuration')
    if value['version'] != 1 or value['mode'] not in {'renderer', 'poller'}:
        raise ValueError('Invalid Windows worker mode/version')
    program = Path(value['program']).resolve(strict=True)
    expected = {'renderer': {'api.py', 'matrix_template_api.py'}, 'poller': {'node_poller.py'}}
    if not program.is_file() or program.name not in expected[value['mode']]:
        raise ValueError('Invalid Windows worker entry point')
    env = value['env']
    if not isinstance(env, dict):
        raise ValueError('Invalid Windows worker environment')
    platform = {'PATH', 'TEMP', 'TMP', 'HOME', 'XDG_CACHE_HOME', 'PYTHONUTF8', 'PYTHONIOENCODING', 'PYTHONUNBUFFERED', 'NO_PROXY'}
    for key, item in env.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key)
                or not (key in platform or key.startswith(('MATRIX_TEMPLATE_', 'NODE_', 'PIXELLE_MATERIAL_LIBRARY_')))
                or not isinstance(item, str) or any(c in item for c in '\0\r\n')):
            raise ValueError('Invalid Windows worker environment entry')
    return value, program


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config, program = read_config(args.config)
    logs = Path(config['log_dir']).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / (config['mode'] + '.log')
    if log.exists() and log.stat().st_size > 20 * 1024 * 1024:
        os.replace(log, log.with_suffix('.previous.log'))
    with log.open('a', encoding='utf-8', buffering=1) as stream:
        original_stdout, original_stderr = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = stream
            os.environ.update(config['env'])
            sys.path.insert(0, str(program.parent))
            sys.argv = [str(program)] + (['--host', '127.0.0.1', '--port', '8212'] if config['mode'] == 'renderer' else [])
            runpy.run_path(str(program), run_name='__main__')
        except Exception:
            traceback.print_exc(file=stream)
            raise
        finally:
            sys.stdout, sys.stderr = original_stdout, original_stderr


if __name__ == '__main__':
    main()
