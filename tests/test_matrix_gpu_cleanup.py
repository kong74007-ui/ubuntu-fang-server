import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from server.matrix_gpu_runtime import GpuProcessGuard
from server.matrix_template_api import MatrixTemplateService, MatrixTemplateError


@unittest.skipUnless(shutil.which("node"), "Node is required for real process-tree regressions")
class GpuScratchCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = object.__new__(MatrixTemplateService)
        self.service.data_root = self.root / "jobs"
        self.service.process_lock = threading.Lock()
        self.service.active_processes = set()
        self.service.active_process = None
        self.job = "a" * 32
        self.cache = self.service.data_root / self.job / "output/raw-cache"
        self.cache.mkdir(parents=True)
        self.external = self.root / "external-cache"
        self.external.mkdir()
        (self.external / "keep").write_text("original")
        self.process = None
        self.guard = None

    def tearDown(self):
        if self.guard is not None:
            self.guard.stop()
        if self.process is not None:
            self.process.communicate(timeout=5)
        self.temp.cleanup()

    def start_renderer(self, crash=False):
        child = "const fs=require('fs');fs.writeFileSync(process.argv[1],'child');setInterval(()=>fs.writeFileSync(process.argv[1],'alive'),30);"
        script = self.root / "renderer.cjs"
        script.write_text("""
const fs=require('fs');const {spawn}=require('child_process');
const cache=process.argv[2],ready=process.argv[3];
(async()=>{try{
 const child=spawn(process.execPath,['-e',CHILD,cache+'/writer'],{stdio:'ignore'});
 while(!fs.existsSync(cache+'/writer'))await new Promise(r=>setTimeout(r,10));
 fs.writeFileSync(ready,String(child.pid));
 process.stdin.once('data',()=>process.exit(17));
 await new Promise(()=>{});
}finally{fs.rmSync(cache,{recursive:true,force:true});}})();
""".replace("CHILD", json.dumps(child)), encoding="utf-8")
        options = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
        options.update({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})
        ready = self.root / "ready"
        self.process = subprocess.Popen([shutil.which("node"), str(script), str(self.cache), str(ready)], **options)
        self.guard = GpuProcessGuard(self.process)
        self.process._matrix_gpu = True
        self.process._matrix_gpu_job = self.job
        self.process._matrix_gpu_guard = self.guard
        self.service.active_processes.add(self.process)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(self.process.stderr.read().decode())
            time.sleep(.02)
        self.assertTrue(ready.exists())
        if crash:
            self.process.stdin.write(b"crash\n")
            self.process.stdin.flush()
            self.process.wait(timeout=5)
            self.assertEqual(17, self.process.returncode)

    def assert_cleanup(self):
        self.assertTrue(self.cache.exists(), "Node finally must not be the cleanup being tested")
        self.service._finish_render_process(self.process, self.job)
        self.service._discard_output(self.job)
        self.assertFalse(self.cache.exists())
        self.assertTrue(self.guard.stopped)
        self.assertFalse(self.service.active_processes)
        self.assertEqual("original", (self.external / "keep").read_text())

    def test_forced_tree_termination_removes_owned_cache(self):
        self.start_renderer()
        self.service._terminate(self.process)
        self.assertIsNotNone(self.process.poll())
        self.assert_cleanup()

    def test_nonzero_renderer_exit_also_stops_surviving_writer(self):
        self.start_renderer(crash=True)
        self.assert_cleanup()

    def test_active_render_cache_is_not_removed_by_discard(self):
        self.start_renderer()
        self.service._discard_output(self.job)
        self.assertTrue(self.cache.exists())
        self.service._finish_render_process(self.process, self.job)
        self.assertFalse(self.cache.exists())

    def test_linked_and_outside_scratch_are_rejected(self):
        with self.assertRaises(MatrixTemplateError):
            self.service._cleanup_gpu_scratch("../outside")
        self.cache.rmdir()
        try:
            self.cache.symlink_to(self.external, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("Directory symlinks unavailable")
            junction = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(self.cache), str(self.external)],
                capture_output=True, timeout=5,
            )
            self.assertEqual(0, junction.returncode, junction.stderr)
        with self.assertRaises(MatrixTemplateError):
            self.service._cleanup_gpu_scratch(self.job)
        self.assertEqual("original", (self.external / "keep").read_text())
        if self.cache.is_symlink():
            self.cache.unlink()
        else:
            self.cache.rmdir()


if __name__ == "__main__":
    unittest.main()
