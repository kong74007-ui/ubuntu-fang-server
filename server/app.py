#!/usr/bin/env python3
"""
抖音评论区获客系统 —— 服务器后端（混合架构）
部署在服务器，伙伴通过公网访问。任务入队 → 等 Mac worker 领取爬取 → 回传结果展示。

跑法（服务器）：
  cd server && ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8090
环境变量：
  LEADGEN_PASSWORD   伙伴提交任务的共享口令（默认 see below）
  LEADGEN_WORKER_TOKEN  Mac worker 领任务的令牌
"""
import os
import json
import sqlite3
import time
from contextlib import closing
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pathlib import Path

DB = os.path.join(os.path.dirname(__file__), "jobs.db")
PASSWORD = os.environ.get("LEADGEN_PASSWORD", "meiye2026")          # 伙伴口令
WORKER_TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "worker-secret-2026")  # worker 令牌
INDEX_HTML = Path(os.path.join(os.path.dirname(__file__), "index.html"))
DISCOVERED = Path(os.path.join(os.path.dirname(__file__), "discovered.json"))


def merge_discovered(words):
    """把爬取中发现的话题词汇入发现词库（按出现频次累计）。"""
    pool = {}
    if DISCOVERED.exists():
        try:
            pool = json.loads(DISCOVERED.read_text(encoding="utf-8"))
        except Exception:
            pool = {}
    for w in words or []:
        w = (w or "").strip()
        if 1 < len(w) <= 12:
            pool[w] = pool.get(w, 0) + 1
    DISCOVERED.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")

app = FastAPI(title="抖音评论区获客")


@app.get("/api/download/{filename:path}")
def download(filename: str):
    base = Path(os.path.dirname(__file__))
    filepath = base / filename
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(filepath), filename=filepath.name)


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT, count INTEGER,
            mode TEXT DEFAULT 'search',      -- search(关键词) / account(抖音号)
            status TEXT DEFAULT 'pending',   -- pending/running/done/error
            result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER)""")
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN mode TEXT DEFAULT 'search'")
        except Exception:
            pass
        conn.commit()


init_db()


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/api/keywords")
def keywords():
    kwf = Path(os.path.join(os.path.dirname(__file__), "keywords.json"))
    kw = json.loads(kwf.read_text(encoding="utf-8")) if kwf.exists() else {}
    # 追加"发现的词"分类（爬取自动挖到的，去掉已在精选库里的）
    if DISCOVERED.exists():
        try:
            pool = json.loads(DISCOVERED.read_text(encoding="utf-8"))
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
    count = max(1, min(int(count), 100))  # 限幅，保护服务器
    now = int(time.time())
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO jobs(keyword,count,mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            (keyword, count, mode, now, now))
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
        d["result"] = json.loads(d["result"])
    return d


# ---- Mac worker 专用 ----
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
        return {"job": {"id": r["id"], "keyword": r["keyword"], "count": r["count"],
                        "mode": r["mode"] if "mode" in r.keys() else "search"}}


@app.post("/api/complete")
def complete(token: str = Form(...), job_id: int = Form(...), result: str = Form(...)):
    if token != WORKER_TOKEN:
        raise HTTPException(403, "token 错误")
    data = json.loads(result)  # 校验是合法 JSON
    try:
        merge_discovered(data.get("related_keywords"))  # 发现的词汇入词库
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
