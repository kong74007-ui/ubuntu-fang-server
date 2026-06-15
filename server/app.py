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
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path

DB = os.path.join(os.path.dirname(__file__), "jobs.db")
PASSWORD = os.environ.get("LEADGEN_PASSWORD", "meiye2026")          # 伙伴口令
WORKER_TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "worker-secret-2026")  # worker 令牌
INDEX_HTML = Path(os.path.join(os.path.dirname(__file__), "index.html"))

app = FastAPI(title="抖音评论区获客")


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT, count INTEGER,
            status TEXT DEFAULT 'pending',   -- pending/running/done/error
            result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER)""")
        conn.commit()


init_db()


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML.read_text(encoding="utf-8")


@app.post("/api/submit")
def submit(password: str = Form(...), keyword: str = Form(...), count: int = Form(10)):
    if password != PASSWORD:
        raise HTTPException(403, "口令错误")
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(400, "关键词不能为空")
    count = max(1, min(int(count), 30))  # 限幅，保护服务器
    now = int(time.time())
    with closing(db()) as conn:
        cur = conn.execute(
            "INSERT INTO jobs(keyword,count,created_at,updated_at) VALUES(?,?,?,?)",
            (keyword, count, now, now))
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
        return {"job": {"id": r["id"], "keyword": r["keyword"], "count": r["count"]}}


@app.post("/api/complete")
def complete(token: str = Form(...), job_id: int = Form(...), result: str = Form(...)):
    if token != WORKER_TOKEN:
        raise HTTPException(403, "token 错误")
    json.loads(result)  # 校验是合法 JSON
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
