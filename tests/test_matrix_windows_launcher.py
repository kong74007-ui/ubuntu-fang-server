import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_node_launcher', ROOT / 'deploy/matrix-gpu/windows-node-launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class WindowsLauncherTests(unittest.TestCase):
    def test_real_entry_binds_loopback_and_keeps_failures_in_log(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); program=root/'api.py'
            program.write_text('import json,sys; print(json.dumps(sys.argv[1:]))',encoding='utf-8')
            config={'version':1,'mode':'renderer','program':str(program),'log_dir':str(root/'logs'),'env':{}}
            path=root/'private.json';path.write_text(json.dumps(config),encoding='utf-8')
            command=[sys.executable,str(ROOT/'deploy/matrix-gpu/windows-node-launcher.py'),'--config',str(path)]
            run=subprocess.run(command,capture_output=True,text=True,timeout=10)
            self.assertEqual(0,run.returncode,run.stderr)
            self.assertEqual(['--host','127.0.0.1','--port','8212'],json.loads((root/'logs/renderer.log').read_text()))
            program.write_text('raise RuntimeError("intentional test failure")',encoding='utf-8')
            failed=subprocess.run(command,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(0,failed.returncode)
            self.assertIn('intentional test failure',(root/'logs/renderer.log').read_text())

    def test_accepts_private_config_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            program = root/'api.py'; program.touch()
            config = {'version': 1, 'mode': 'renderer', 'program': str(program),
                      'log_dir': str(root/'logs'), 'env': {'MATRIX_TEMPLATE_GPU_MODE': 'required', 'NODE_LOCAL_TOKEN': 'test-fixture'}}
            path = root/'private.json';path.write_text(json.dumps(config),encoding='utf-8-sig')
            parsed, actual = launcher.read_config(path)
            self.assertEqual(config, parsed)
            self.assertEqual(program.resolve(), actual)

    def test_rejects_unknown_modes_entrypoints_and_environment(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);program=root/'api.py';program.touch()
            original={'version':1,'mode':'renderer','program':str(program),'log_dir':str(root/'logs'),'env':{}}
            invalid=[{'mode':'shell'},{'version':2},{'mode':'poller'},{'extra':'value'},
                     {'env':{'UNAPPROVED':'test'}},{'env':{'NODE_LOCAL_TOKEN':'line1\nline2'}},
                     {'env':{'PATH':None}},{'env':[]}, {'program':str(root)}]
            for patch in invalid:
                with self.subTest(patch=patch):
                    path=root/'private.json';path.write_text(json.dumps({**original,**patch}),encoding='utf-8')
                    with self.assertRaises(ValueError):launcher.read_config(path)


if __name__=='__main__':unittest.main()
