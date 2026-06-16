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
import json
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlparse
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
    conn = sqlite3.connect(DB)
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
           count: int = Form(10), mode: str = Form("search")):
    if password != PASSWORD:
        raise HTTPException(403, "口令错误")
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(400, "关键词/抖音号不能为空")
    mode = mode if mode in ("search", "account") else "search"
    count = max(1, min(int(count), 100))
    now = int(time.time())
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO jobs(keyword,count,mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            (keyword, count, mode, now, now))
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
@app.get("/api/claim")
def claim(token: str = Query(...)):
    if token != WORKER_TOKEN:
        raise HTTPException(403, "token 错误")
    now = int(time.time())
    with closing(db()) as conn:
        r = conn.execute(
            "SELECT * FROM jobs WHERE status='pending' ORDER BY id ASC LIMIT 1").fetchone()
        if not r:
            return {"job": None}
        conn.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now, r["id"]))
        conn.commit()
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
