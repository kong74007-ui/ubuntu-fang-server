#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黄雀 AI · 内容生成后端 API（能力中心）
=====================================================
架构：能力集中在后端，网页 + 飞书 bot 都来调；点数/额度统一在这里扣。
- 鉴权：复用现有认证服务(:8095)，前端带 Bearer <hq_token>；本服务调 /api/auth/me 校验 + 取 username/points/role。
- 异步任务模型：/api/gen/<能力> 提交 → job_id → 轮询 /api/gen/job/{id}（与 leadgen 同套路）。
- 点数：提交即预扣（够才受理），失败自动退点。点数落在 auth 的 users.db。

端口 127.0.0.1:8096，nginx 把 /api/gen/ 路由过来。零第三方依赖外只用 requests(已在 venv)。

P1：图片(gpt-image-2)。P2 文案 / P3 视频按同样的 register_capability 往里加。
"""
import os, sqlite3, json, time, threading, base64, pathlib, urllib.request, urllib.error
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT       = int(os.environ.get("CONTENT_API_PORT", "8096"))
AUTH_BASE  = os.environ.get("AUTH_BASE", "http://127.0.0.1:8095")
AUTH_DB    = os.environ.get("AUTH_DB", "/home/ubuntu/auth-service/users.db")  # 点数扣减直接落这
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE       = pathlib.Path(__file__).resolve().parent
JOB_DB     = str(BASE / "content_jobs.db")
OUT_DIR    = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- 能力定义：成本(点数) + 处理函数 ----
COST = {"image": 12, "copy": 3, "video": 13}
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com")

# ============ 任务库 ============
def jdb():
    c = sqlite3.connect(JOB_DB, timeout=10); c.row_factory = sqlite3.Row; return c

def init_db():
    with closing(jdb()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT, username TEXT, cost INTEGER,
            status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
            created_at INTEGER, updated_at INTEGER)""")
        c.commit()

# ============ 点数(落 auth users.db) ============
def get_points(username):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            r = c.execute("SELECT points FROM users WHERE username=?", (username,)).fetchone()
            return r[0] if r else 0
    except Exception:
        return 0

def add_points(username, delta):
    try:
        with closing(sqlite3.connect(AUTH_DB, timeout=10)) as c:
            c.execute("UPDATE users SET points = MAX(0, points + ?) WHERE username=?", (delta, username))
            c.commit()
    except Exception:
        pass

# ============ 鉴权（向 auth 服务核验 token） ============
def verify(token):
    if not token: return None
    try:
        req = urllib.request.Request(AUTH_BASE + "/api/auth/me",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read()).get("user")
    except Exception:
        return None

# ============ 图片能力：gpt-image-2 ============
# 三种模式同一入口：无图=文生图(generations)；有图无蒙版=图生图(edits)；有图有蒙版=局部修改(edits+mask)
SIZES = {"1:1": "1024x1024", "9:16": "1024x1536", "16:9": "1536x1024", "3:4": "1024x1536"}

def _multipart(fields, files):
    """手搓 multipart/form-data；files=[(name, filename, bytes)]"""
    b = "----hqcontent7e3f"
    out = []
    for k, v in fields.items():
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode())
    for name, fn, data in files:
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: image/png\r\n\r\n' % (b, name, fn)).encode())
        out.append(data); out.append(b"\r\n")
    out.append(("--%s--\r\n" % b).encode())
    return b"".join(out), "multipart/form-data; boundary=" + b

def _post(path, data, ctype):
    req = urllib.request.Request(OPENAI_BASE + path, data=data,
                                 headers={"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())

def gen_image(payload):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    ratio = payload.get("ratio") or "1:1"
    size  = SIZES.get(ratio, "1024x1024")
    img   = payload.get("image")   # base64(无 data: 前缀) — 上传参考图 → 图生图 / 局部修改
    mask  = payload.get("mask")    # base64 — 蒙版(透明处=要重绘的区域) → 局部修改
    if img:
        files = [("image", "in.png", base64.b64decode(img))]
        if mask:
            files.append(("mask", "mask.png", base64.b64decode(mask)))
        body, ct = _multipart({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": "high", "n": "1"}, files)
        d = _post("/v1/images/edits", body, ct)
        mode = "inpaint" if mask else "img2img"
    else:
        body = json.dumps({"model": "gpt-image-2", "prompt": prompt, "size": size, "quality": "high", "n": 1}).encode()
        d = _post("/v1/images/generations", body, "application/json")
        mode = "text2img"
    fn = "img_%d.png" % int(time.time() * 1000)
    (OUT_DIR / fn).write_bytes(base64.b64decode(d["data"][0]["b64_json"]))
    return {"type": "image", "mode": mode, "file": fn, "url": "/api/gen/file/" + fn, "ratio": ratio, "prompt": prompt}

HANDLERS = {"image": gen_image}  # P2/P3: 在此注册 copy / video

# ============ 后台 worker（串行跑任务，失败退点） ============
def run_job(job_id):
    with closing(jdb()) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r: return
    kind = r["kind"]; payload = json.loads(r["payload"] or "{}")
    try:
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (int(time.time()), job_id)); c.commit()
        result = HANDLERS[kind](payload)
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='done', result=?, updated_at=? WHERE id=?",
                      (json.dumps(result, ensure_ascii=False), int(time.time()), job_id)); c.commit()
    except Exception as e:
        add_points(r["username"], r["cost"])  # 失败退点
        with closing(jdb()) as c:
            c.execute("UPDATE jobs SET status='error', error=?, updated_at=? WHERE id=?",
                      (str(e)[:300], int(time.time()), job_id)); c.commit()

# ============ HTTP ============
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _token(self):
        a = self.headers.get("Authorization") or ""
        return a[7:].strip() if a.startswith("Bearer ") else ""
    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}

    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/gen/") and p[9:] in COST:
            kind = p[9:]
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录或登录已过期"})
            cost = COST[kind]
            if get_points(user["username"]) < cost:
                return self._send(402, {"detail": "点数不足", "need": cost})
            add_points(user["username"], -cost)  # 预扣
            body = self._json_body(); now = int(time.time())
            with closing(jdb()) as c:
                cur = c.execute("INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                (kind, user["username"], cost, json.dumps(body, ensure_ascii=False), now, now))
                c.commit(); jid = cur.lastrowid
            threading.Thread(target=run_job, args=(jid,), daemon=True).start()
            return self._send(200, {"job_id": jid, "cost": cost, "points_left": get_points(user["username"])})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/gen/job/"):
            try: jid = int(p.rsplit("/", 1)[1])
            except Exception: return self._send(400, {"detail": "bad id"})
            with closing(jdb()) as c:
                r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not r: return self._send(404, {"detail": "任务不存在"})
            d = dict(r)
            if d.get("result"):
                try: d["result"] = json.loads(d["result"])
                except Exception: pass
            return self._send(200, d)
        if p.startswith("/api/gen/file/"):
            fn = os.path.basename(p.rsplit("/", 1)[1]); fp = OUT_DIR / fn
            if not fp.exists(): return self._send(404, {"detail": "no file"})
            data = fp.read_bytes()
            self.send_response(200); self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers(); self.wfile.write(data); return
        if p == "/api/gen/history":   # 本人生成历史（资产/最近作品都读这）
            user = verify(self._token())
            if not user: return self._send(401, {"detail": "未登录"})
            try: lim = min(120, int(self.path.split("limit=")[1].split("&")[0])) if "limit=" in self.path else 60
            except Exception: lim = 60
            with closing(jdb()) as c:
                rows = c.execute("SELECT id,result,created_at FROM jobs WHERE username=? AND status='done' AND kind='image' ORDER BY id DESC LIMIT ?",
                                 (user["username"], lim)).fetchall()
            items = []
            for r in rows:
                try: res = json.loads(r["result"])
                except Exception: continue
                items.append({"job_id": r["id"], "url": res.get("url"), "mode": res.get("mode"),
                              "prompt": res.get("prompt"), "created_at": r["created_at"]})
            return self._send(200, {"items": items})
        if p == "/api/gen/health":
            return self._send(200, {"ok": True, "service": "huangque-content", "caps": list(HANDLERS), "has_openai": bool(OPENAI_KEY)})
        self._send(404, {"detail": "not found"})

if __name__ == "__main__":
    init_db()
    print("huangque-content-api on 127.0.0.1:%d  caps=%s" % (PORT, list(HANDLERS)))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
