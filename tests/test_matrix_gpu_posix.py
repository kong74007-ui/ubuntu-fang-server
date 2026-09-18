import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from server.matrix_gpu_runtime import GpuProcessGuard, supervised_command
from server.matrix_template_api import MatrixTemplateService

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper regression")
class GpuPosixTreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="matrix-posix-")
        self.root = Path(self.temp.name)
        self.service = object.__new__(MatrixTemplateService)
        self.service.data_root = self.root / "jobs"
        self.service.process_lock = threading.Lock()
        self.service.active_processes = set()
        self.service.active_process = None
        self.job = "d" * 32
        self.cache = self.service.data_root / self.job / "output/raw-cache"
        self.cache.mkdir(parents=True)
        (self.cache / "owned").write_text("scratch")
        self.process = self.guard = None
        self.ready = self.root / "ready.json"
        self.unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], start_new_session=True)

    def tearDown(self):
        try:
            if self.guard is not None:
                self.guard.stop()
            if self.process is not None:
                self.process.communicate(timeout=5)
        finally:
            self.unrelated.kill()
            self.unrelated.wait(timeout=5)
            self.temp.cleanup()

    def start(self, command):
        self.process = subprocess.Popen(supervised_command(command, self.cache.parent),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.guard = GpuProcessGuard(self.process)
        self.process._matrix_gpu = True
        self.process._matrix_gpu_guard = self.guard
        self.process._matrix_gpu_job = self.job
        self.service.active_processes.add(self.process)
        deadline = time.monotonic() + 30
        while not self.ready.exists() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail(self.process.communicate()[1].decode())
            time.sleep(.02)
        self.assertTrue(self.ready.exists())
        return json.loads(self.ready.read_text())

    def synthetic(self, crash):
        leaf = "import time;time.sleep(120)"
        middle = self.root / "detached.py"
        middle.write_text(
            "import subprocess,sys,os,json,time\n"
            f"child=subprocess.Popen([sys.executable,'-c',{leaf!r}],start_new_session=True)\n"
            "with open(sys.argv[1]+'.tmp','w') as f: json.dump({'pids':[os.getpid(),child.pid],'pgid':os.getpgrp()},f)\n"
            "os.replace(sys.argv[1]+'.tmp',sys.argv[1])\n"
            "time.sleep(120)\n"
        )
        renderer = self.root / "renderer.py"
        renderer.write_text(
            "import subprocess,sys,os,time,pathlib\n"
            f"subprocess.Popen([sys.executable,{str(middle)!r},sys.argv[1]],start_new_session=True)\n"
            "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)\n"
            + ("os._exit(17)\n" if crash else "time.sleep(120)\n")
        )
        return self.start([sys.executable, str(renderer), str(self.ready)])

    def finish(self, pids):
        self.service._finish_render_process(self.process, self.job)
        self.assertTrue(self.guard.stopped)
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.service.active_processes)
        self.assertIsNone(self.unrelated.poll(), "Unrelated process must not be signalled")
        for pid in pids:
            self.assertFalse(Path(f"/proc/{pid}").exists(), f"Owned descendant {pid} survived")

    def test_renderer_crash_reaps_detached_child_and_grandchild(self):
        observed = self.synthetic(crash=True)
        self.process.wait(timeout=15)
        self.assertEqual(17, self.process.returncode)
        self.finish(observed["pids"])

    def test_cancel_keeps_supervisor_alive_until_detached_children_are_reaped(self):
        observed = self.synthetic(crash=False)
        self.service._terminate(self.process)
        self.assertEqual(143, self.process.returncode)
        self.finish(observed["pids"])

    def test_missing_or_wrong_supervision_receipt_never_reports_stopped(self):
        process = mock.Mock(pid=123456789)
        process.args = supervised_command(["unused"], self.cache.parent)
        process.poll.return_value = -signal.SIGKILL
        guard = GpuProcessGuard(process)
        for value in (None, {"version":1,"supervisor_pid":0,"all_children_reaped":True}):
            if value is not None:
                guard.receipt.write_text(json.dumps(value))
            with self.assertRaises(RuntimeError):
                guard.stop()
            self.assertFalse(guard.stopped)
        process.args = ["node", "uncontained-renderer.mjs"]
        with self.assertRaises(ValueError):
            GpuProcessGuard(process)

    def test_immediate_cancel_waits_for_supervisor_initialization(self):
        command = supervised_command([sys.executable, "-c", "import time;time.sleep(120)"], self.cache.parent)
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.guard = GpuProcessGuard(self.process)
        self.guard.stop()
        self.assertEqual(143, self.process.returncode)
        self.assertTrue(self.guard.stopped)

    def test_real_puppeteer_default_detached_chrome_is_reaped_after_renderer_sigkill(self):
        browser = os.environ.get("MATRIX_GPU_TEST_BROWSER") or shutil.which("google-chrome") or shutil.which("chromium")
        module = ROOT / "deploy/matrix-gpu/node_modules/puppeteer-core/lib/esm/puppeteer/puppeteer-core.js"
        if not browser or not module.is_file():
            if os.environ.get("MATRIX_GPU_REQUIRE_BROWSER_TEST") == "1":
                self.fail("Real Chrome regression dependencies are required")
            self.skipTest("Real Chrome regression runs in its dedicated CI step")
        script = self.root / "browser.mjs"
        script.write_text(r"""
import puppeteer from MODULE;
import fs from 'node:fs';
const browser=await puppeteer.launch({executablePath:process.argv[2],headless:true,args:['--no-sandbox','--disable-gpu']});
await browser.newPage();
const records=new Map();
for(const name of fs.readdirSync('/proc'))if(/^\d+$/.test(name)){
 try{const f=fs.readFileSync('/proc/'+name+'/stat','utf8').split(')').at(-1).trim().split(/\s+/);records.set(Number(name),{ppid:Number(f[1]),pgid:Number(f[2])});}catch{}
}
const pids=new Set([browser.process().pid]);let size=-1;
while(size!==pids.size){size=pids.size;for(const[pid,r]of records)if(pids.has(r.ppid))pids.add(pid);}
fs.writeFileSync(process.argv[3],JSON.stringify({renderer:process.pid,browser:browser.process().pid,renderer_pgid:records.get(process.pid).pgid,browser_pgid:records.get(browser.process().pid).pgid,pids:[...pids]}));
process.kill(process.pid,'SIGKILL');
""".replace("MODULE", json.dumps(module.as_uri())), encoding="utf-8")
        observed = self.start([shutil.which("node"), str(script), browser, str(self.ready)])
        self.assertNotEqual(observed["renderer_pgid"], observed["browser_pgid"])
        self.assertEqual(observed["browser"], observed["browser_pgid"])
        self.process.wait(timeout=15)
        self.assertEqual(137, self.process.returncode)
        self.finish(observed["pids"])


if __name__ == "__main__":
    unittest.main()
