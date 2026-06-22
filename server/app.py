#!/usr/bin/env python3
"""
抖音评论区获客系统 —— 服务器后端（全栈在服务器）
任务入队 → server_worker 领取爬取(青果住宅代理+cookie注入) → 回传结果展示。
支持：关键词搜索 / 账号深扒 / 提取文案(ASR) / 提取视频(下载)。

跑法（服务器）：
  cd server && ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8090
环境变量：
  LEADGEN_PASSWORD   伙伴提交任务的共享口令
  LEADGEN_WORKER_TOKEN  worker 领任务的令牌
"""
import os
import re
import json
import glob
import time
import hashlib
import sqlite3
import datetime
import subprocess
from contextlib import closing
from urllib.parse import urlparse, unquote
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

# 只允许抖音系域名（防 SSRF：禁止内网/localhost/任意URL）
_ALLOW_DOMAINS = ("douyin.com", "douyinvod.com", "amemv.com", "iesdouyin.com",
                  "bytecdn.cn", "pstatp.com", "ixigua.com", "snssdk.com", "zjcdn.com")


def is_safe_video_url(u):
    try:
        p = urlparse(u or "")
        if p.scheme not in ("http", "https"):
            return False
        host = (p.hostname or "").lower()
        return bool(host) and any(host == d or host.endswith("." + d) for d in _ALLOW_DOMAINS)
    except Exception:
        return False

DB = os.path.join(os.path.dirname(__file__), "jobs.db")
PASSWORD = os.environ.get("LEADGEN_PASSWORD", "meiye2026")
WORKER_TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "worker-secret-2026")
BASE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE, "index.html")
DISCOVERED = os.path.join(BASE, "discovered.json")
FILES_DIR = os.path.join(BASE, "files")   # 提取出来的视频存这（供下载）
os.makedirs(FILES_DIR, exist_ok=True)

EXTRACT_MODES = ("transcribe", "download_video")


def merge_discovered(words):
    pool = {}
    if os.path.exists(DISCOVERED):
        try:
            pool = json.loads(open(DISCOVERED, encoding="utf-8").read())
        except Exception:
            pool = {}
    for w in words or []:
        w = (w or "").strip()
        if 1 < len(w) <= 12:
            pool[w] = pool.get(w, 0) + 1
    open(DISCOVERED, "w", encoding="utf-8").write(json.dumps(pool, ensure_ascii=False))


app = FastAPI(title="抖音评论区获客")


@app.get("/api/download/{filename:path}")
def download(filename: str):
    # 防路径穿越：解析后必须仍在 BASE 内
    full = os.path.realpath(os.path.join(BASE, filename))
    if not (full == os.path.realpath(BASE) or full.startswith(os.path.realpath(BASE) + os.sep)):
        raise HTTPException(403, "非法路径")
    if not os.path.isfile(full):
        raise HTTPException(404, "文件不存在")
    return FileResponse(full, filename=os.path.basename(full))


def db():
    # isolation_level=None → autocommit，手动管事务（claim 用 BEGIN IMMEDIATE 拿写锁）
    conn = sqlite3.connect(DB, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT, count INTEGER,
            mode TEXT DEFAULT 'search',
            status TEXT DEFAULT 'pending',
            result TEXT, error TEXT, payload TEXT,
            created_at INTEGER, updated_at INTEGER)""")
        for col, ddl in (("mode", "ALTER TABLE jobs ADD COLUMN mode TEXT DEFAULT 'search'"),
                         ("payload", "ALTER TABLE jobs ADD COLUMN payload TEXT")):
            try:
                conn.execute(ddl)
            except Exception:
                pass
        conn.commit()


init_db()


@app.get("/", response_class=HTMLResponse)
def index():
    return open(INDEX_HTML, encoding="utf-8").read()


@app.get("/api/keywords")
def keywords():
    kwf = os.path.join(BASE, "keywords.json")
    kw = json.loads(open(kwf, encoding="utf-8").read()) if os.path.exists(kwf) else {}
    if os.path.exists(DISCOVERED):
        try:
            pool = json.loads(open(DISCOVERED, encoding="utf-8").read())
            curated = set(w for v in kw.values() for w in v)
            disc = [w for w, _ in sorted(pool.items(), key=lambda x: -x[1]) if w not in curated][:40]
            if disc:
                kw["🔥发现的词"] = disc
        except Exception:
            pass
    return kw


@app.post("/api/submit")
def submit(password: str = Form(...), keyword: str = Form(...),
           count: int = Form(10), mode: str = Form("search"),
           deep_dig: str = Form("0")):
    if password != PASSWORD:
        raise HTTPException(403, "口令错误")
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(400, "关键词/抖音号不能为空")
    mode = mode if mode in ("search", "account") else "search"
    count = max(1, min(int(count), 100))
    # 爆款深挖开关（仅 search 模式有意义）：搜完自动对爆款 detail 补抓
    dig = 1 if str(deep_dig) in ("1", "true", "True", "on") and mode == "search" else 0
    payload = json.dumps({"deep_dig": dig}, ensure_ascii=False) if dig else None
    now = int(time.time())
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO jobs(keyword,count,mode,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (keyword, count, mode, payload, now, now))
        conn.commit()
        return {"job_id": cur.lastrowid}


@app.post("/api/extract")
def extract(password: str = Form(...), aweme_id: str = Form(...), url: str = Form(...),
            type: str = Form("transcribe"), title: str = Form("")):
    """提交「提取文案(transcribe) / 提取视频(download_video)」单条视频任务。"""
    if password != PASSWORD:
        raise HTTPException(403, "口令错误")
    if type not in EXTRACT_MODES:
        raise HTTPException(400, "type 只能是 transcribe / download_video")
    aweme_id = (aweme_id or "").strip()
    url = (url or "").strip()
    if not aweme_id or not url:
        raise HTTPException(400, "aweme_id / url 不能为空")
    if not is_safe_video_url(url):
        raise HTTPException(400, "url 必须是抖音视频链接")
    payload = json.dumps({"aweme_id": aweme_id, "url": url, "title": title[:60]}, ensure_ascii=False)
    now = int(time.time())
    with closing(db()) as conn:
        # 同一视频同一类型已成功且有结果，直接复用（避免重复下载/转写）
        prev = conn.execute(
            "SELECT id,result FROM jobs WHERE mode=? AND keyword=? AND status='done' "
            "AND result IS NOT NULL AND result != '' ORDER BY id DESC LIMIT 1",
            (type, aweme_id)).fetchone()
        if prev:
            return {"job_id": prev["id"], "cached": True}
        cur = conn.execute(
            "INSERT INTO jobs(keyword,mode,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
            (aweme_id, type, payload, now, now))
        conn.commit()
        return {"job_id": cur.lastrowid}


@app.get("/api/job/{job_id}")
def job(job_id: int):
    with closing(db()) as conn:
        r = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        raise HTTPException(404, "任务不存在")
    d = dict(r)
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except Exception:
            pass
    return d


# ---- worker 专用 ----
STALE_RUNNING = 2400  # running 超 40 分钟无更新视为僵尸(worker崩)，claim 时回收成 pending


@app.get("/api/claim")
def claim(token: str = Query(...), modes: str = Query("")):
    """多 worker 安全领单。modes 非空时只领这些 mode 的任务(快慢分道)。"""
    if token != WORKER_TOKEN:
        raise HTTPException(403, "token 错误")
    now = int(time.time())
    allow = tuple(m.strip() for m in modes.split(",") if m.strip())
    with closing(db()) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")   # 拿写锁，保证 SELECT+UPDATE 原子，多 worker 不抢同一任务
        try:
            conn.execute("UPDATE jobs SET status='pending' WHERE status='running' AND updated_at < ?",
                         (now - STALE_RUNNING,))  # 回收僵尸
            if allow:
                ph = ",".join("?" * len(allow))
                r = conn.execute(
                    f"SELECT * FROM jobs WHERE status='pending' AND mode IN ({ph}) "
                    f"ORDER BY id ASC LIMIT 1", allow).fetchone()
            else:
                r = conn.execute(
                    "SELECT * FROM jobs WHERE status='pending' ORDER BY id ASC LIMIT 1").fetchone()
            if not r:
                conn.execute("COMMIT")
                return {"job": None}
            conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now, r["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        keys = r.keys()
        return {"job": {"id": r["id"], "keyword": r["keyword"], "count": r["count"],
                        "mode": r["mode"] if "mode" in keys else "search",
                        "payload": r["payload"] if "payload" in keys else None}}


@app.post("/api/complete")
def complete(token: str = Form(...), job_id: int = Form(...), result: str = Form(...)):
    if token != WORKER_TOKEN:
        raise HTTPException(403, "token 错误")
    try:
        data = json.loads(result)
        merge_discovered(data.get("related_keywords"))
    except Exception:
        pass
    with closing(db()) as conn:
        conn.execute("UPDATE jobs SET status='done',result=?,updated_at=? WHERE id=?",
                     (result, int(time.time()), job_id))
        conn.commit()
    return {"ok": True}


@app.post("/api/fail")
def fail(token: str = Form(...), job_id: int = Form(...), error: str = Form("")):
    if token != WORKER_TOKEN:
        raise HTTPException(403, "token 错误")
    with closing(db()) as conn:
        conn.execute("UPDATE jobs SET status='error',error=?,updated_at=? WHERE id=?",
                     (error[:500], int(time.time()), job_id))
        conn.commit()
    return {"ok": True}


# ============================ 号池管理后台 (admin) ============================
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
NUMBER_POOL = os.environ.get("NUMBER_POOL", "/home/ubuntu/number_pool")
USER_HOME = os.environ.get("LEADGEN_USER_HOME", "/home/ubuntu")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "6"))
MC_PY = os.environ.get("MC_VENV_PY", "/home/ubuntu/MediaCrawler/.venv/bin/python")
POOL_HEALTH = os.environ.get("POOL_HEALTH_PY", os.path.join(BASE, "pool_health.py"))
ADMIN_HTML = os.path.join(BASE, "admin.html")
HEALTH_OUT = "/tmp/pool_health_result.json"
_SYS_ENV = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/%d" % os.getuid()}


def _admin_token():
    return hashlib.sha256(("leadgen-admin|" + ADMIN_PASSWORD).encode()).hexdigest()


def _need_admin(token):
    if not token or token != _admin_token():
        raise HTTPException(403, "未登录或登录已失效，请重新登录")


def _convert_cookies(raw):
    """浏览器扩展导出格式 → Playwright add_cookies 格式。"""
    out = []
    for c in raw:
        name = c.get("name", "")
        if not name:
            continue
        pc = {"name": name, "value": c.get("value", ""), "domain": c.get("domain", ".douyin.com"),
              "path": c.get("path", "/"), "httpOnly": bool(c.get("httpOnly", False)),
              "secure": bool(c.get("secure", False))}
        pc["expires"] = -1 if (c.get("session") or "expirationDate" not in c) else float(c["expirationDate"])
        ss = c.get("sameSite")
        if ss in ("no_restriction", "none", "None"):
            pc["sameSite"] = "None"; pc["secure"] = True
        elif ss in ("lax", "Lax"):
            pc["sameSite"] = "Lax"
        elif ss in ("strict", "Strict"):
            pc["sameSite"] = "Strict"
        out.append(pc)
    return out


def _cookie_uid(cookies):
    for c in cookies:
        if c.get("name") == "uid_tt":
            return hashlib.md5(c["value"].encode()).hexdigest()[:10]
    return None


def _pool_files():
    fs = glob.glob(os.path.join(NUMBER_POOL, "cookie_*.json"))
    return sorted(fs, key=lambda p: int(re.search(r"cookie_(\d+)", p).group(1)))


def _worker_active(n):
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "leadgen-worker@%d.service" % n],
                           capture_output=True, text=True, env=_SYS_ENV, timeout=8)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _create_and_start_worker(n):
    """复制 worker_1 结构 → 清空账号数据 → 起 systemd 服务。返回 (ok, msg)。"""
    script = (
        "set -e\n"
        "systemctl --user stop leadgen-worker@%d.service 2>/dev/null || true\n"
        "rm -rf %s/worker_%d\n"
        "cp -a %s/worker_1 %s/worker_%d\n"
        "rm -rf %s/worker_%d/browser_data/* %s/worker_%d/data/* %s/worker_%d/worker.log 2>/dev/null || true\n"
        "mkdir -p %s/worker_%d/browser_data %s/worker_%d/data\n"
        "systemctl --user enable --now leadgen-worker@%d.service\n"
    ) % (n, USER_HOME, n, USER_HOME, USER_HOME, n, USER_HOME, n, USER_HOME, n, USER_HOME, n,
         USER_HOME, n, USER_HOME, n, n)
    try:
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=_SYS_ENV, timeout=90)
        return (r.returncode == 0, (r.stderr or r.stdout)[-200:])
    except Exception as e:
        return (False, str(e)[:200])


def _check_cookie_live(path, timeout=70):
    """对单个 cookie 文件做实时体检，返回 pool_health 的那行 dict。"""
    try:
        r = subprocess.run([MC_PY, POOL_HEALTH, "--json", path],
                           capture_output=True, text=True, cwd=os.path.dirname(MC_PY).rsplit("/.venv", 1)[0],
                           env={**_SYS_ENV}, timeout=timeout)
        data = json.loads(r.stdout.strip().splitlines()[-1])
        return data["rows"][0] if data.get("rows") else None
    except Exception as e:
        return {"valid": False, "err": "体检执行失败:%s" % str(e)[:80]}


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return open(ADMIN_HTML, encoding="utf-8").read()


@app.post("/api/admin/login")
def admin_login(password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        raise HTTPException(403, "管理员密码错误")
    return {"token": _admin_token()}


@app.get("/api/admin/pool")
def admin_pool(token: str = Query(...)):
    _need_admin(token)
    live = {}
    if os.path.exists(HEALTH_OUT):
        try:
            for r in json.load(open(HEALTH_OUT)).get("rows", []):
                if r.get("n") is not None:
                    live[r["n"]] = r
        except Exception:
            pass
    rows = []
    for p in _pool_files():
        n = int(re.search(r"cookie_(\d+)", p).group(1))
        try:
            cs = json.load(open(p))
            d = {c["name"]: c["value"] for c in cs}
        except Exception:
            rows.append({"n": n, "err": "cookie 文件解析失败"}); continue
        uid = _cookie_uid(cs) or "?"
        lt = d.get("login_time")
        lt_s = datetime.datetime.fromtimestamp(int(lt) / 1000).strftime("%Y-%m-%d %H:%M") if lt and lt.isdigit() else "-"
        sg = unquote(d.get("sid_guard", ""))
        exp = sg.split("|")[-1].replace("+", " ") if "|" in sg else "-"
        row = {"n": n, "uid": uid, "login": lt_s, "expire": exp, "active": _worker_active(n)}
        lr = live.get(n)
        if lr:
            row.update({"dyid": lr.get("dyid"), "nick": lr.get("nick"),
                        "fans": lr.get("fans"), "valid": lr.get("valid")})
        rows.append(row)
    ht = os.path.getmtime(HEALTH_OUT) if os.path.exists(HEALTH_OUT) else None
    return {"pool": rows, "max_workers": MAX_WORKERS, "health_time": ht}


@app.post("/api/admin/pool/health")
def admin_pool_health(token: str = Form(...)):
    _need_admin(token)
    # 后台异步体检整池，写 HEALTH_OUT（前端轮询 /api/admin/pool 取最新身份）
    subprocess.Popen([MC_PY, POOL_HEALTH, "--json", "--out", HEALTH_OUT],
                     env=_SYS_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"started": True, "note": "体检已在后台开始，约每号 15 秒，稍后刷新"}


@app.post("/api/admin/pool/import")
def admin_import(token: str = Form(...), cookies: str = Form(...), start_worker: str = Form("1")):
    _need_admin(token)
    try:
        raw = json.loads(cookies)
        assert isinstance(raw, list)
    except Exception:
        raise HTTPException(400, "cookie 必须是浏览器导出的 JSON 数组（整段粘贴）")
    pw = _convert_cookies(raw)
    if not any(c["name"] == "sessionid" for c in pw):
        raise HTTPException(400, "这份 cookie 里没有 sessionid，不是有效的抖音登录态")
    uid = _cookie_uid(pw)
    for p in _pool_files():
        try:
            if _cookie_uid(json.load(open(p))) == uid:
                raise HTTPException(409, "这个号已经在池里了（与 %s 重复）" % os.path.basename(p))
        except HTTPException:
            raise
        except Exception:
            pass
    used = [int(re.search(r"cookie_(\d+)", p).group(1)) for p in _pool_files()]
    n = (max(used) + 1) if used else 1
    json.dump(pw, open(os.path.join(NUMBER_POOL, "cookie_%d.json" % n), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    started, warn = False, None
    want_worker = start_worker not in ("0", "false", "False", "")
    if want_worker and n > MAX_WORKERS:
        warn = "已入池 cookie_%d，但 worker 已达上限 %d，未自动起 worker（机器并发到顶，先别加更多）。" % (n, MAX_WORKERS)
    elif want_worker:
        ok, msg = _create_and_start_worker(n)
        started = ok
        if not ok:
            warn = "入池成功，但 worker 启动失败：%s" % msg
    # 立即体检这个新号 → 无效即时反馈
    health = _check_cookie_live(os.path.join(NUMBER_POOL, "cookie_%d.json" % n))
    return {"ok": True, "slot": n, "uid": uid, "worker": "worker_%d" % n,
            "worker_started": started, "warn": warn, "health": health}


@app.post("/api/admin/pool/remove")
def admin_remove(token: str = Form(...), n: int = Form(...)):
    _need_admin(token)
    cookie = os.path.join(NUMBER_POOL, "cookie_%d.json" % n)
    if not os.path.exists(cookie):
        raise HTTPException(404, "cookie_%d 不存在" % n)
    script = (
        "systemctl --user disable --now leadgen-worker@%d.service 2>/dev/null || true\n"
        "mkdir -p %s/_removed\n"
        "mv %s %s/_removed/cookie_%d.$(date +%%s).json\n"
    ) % (n, NUMBER_POOL, cookie, NUMBER_POOL, n)
    subprocess.run(["bash", "-c", script], env=_SYS_ENV, capture_output=True, timeout=40)
    return {"ok": True, "removed": "cookie_%d / worker_%d" % (n, n)}
