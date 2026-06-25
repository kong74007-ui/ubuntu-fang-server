#!/usr/bin/env python3
# 黄雀 AI · 独立认证服务（零依赖，标准库）
# 端口 127.0.0.1:8095，nginx 把 /api/auth/ 路由过来。与 leadgen(8090) 完全隔离。
import sqlite3, hashlib, secrets, json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
PORT = 8095
ITER = 200000

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        pw_salt TEXT NOT NULL,
        display_name TEXT,
        points INTEGER DEFAULT 0,
        role TEXT DEFAULT 'member',
        must_change INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tokens(
        token TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    c.commit(); c.close()

def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITER).hex()

def create_user(username, password, points=0, role='member'):
    init_db()
    salt = secrets.token_hex(16)
    c = db()
    c.execute("""INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change)
                 VALUES(?,?,?,?,?,?,1)
                 ON CONFLICT(username) DO UPDATE SET
                   pw_hash=excluded.pw_hash, pw_salt=excluded.pw_salt,
                   points=excluded.points, role=excluded.role, must_change=1""",
              (username, hash_pw(password, salt), salt, username, points, role))
    c.commit(); c.close()
    print("OK user:", username)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}
    def _user(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        tok = auth[7:].strip()
        c = db()
        r = c.execute("""SELECT u.* FROM tokens t JOIN users u ON u.username=t.username
                         WHERE t.token=?""", (tok,)).fetchone()
        c.close()
        return r

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/auth/login":
            d = self._body()
            u = (d.get("username") or "").strip()
            pw = d.get("password") or ""
            c = db(); row = c.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone(); c.close()
            if not row or hash_pw(pw, row["pw_salt"]) != row["pw_hash"]:
                return self._send(401, {"detail": "账号或密码错误"})
            tok = secrets.token_urlsafe(32)
            c = db(); c.execute("INSERT INTO tokens(token,username) VALUES(?,?)", (tok, u)); c.commit(); c.close()
            return self._send(200, {"token": tok, "user": {
                "username": u, "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"])}})
        if p == "/api/auth/logout":
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Bearer "):
                c = db(); c.execute("DELETE FROM tokens WHERE token=?", (auth[7:].strip(),)); c.commit(); c.close()
            return self._send(200, {"ok": True})
        if p == "/api/auth/change_password":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            d = self._body(); newp = d.get("new_password") or ""
            if len(newp) < 6: return self._send(400, {"detail": "新密码至少 6 位"})
            salt = secrets.token_hex(16)
            c = db(); c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change=0 WHERE username=?",
                                (hash_pw(newp, salt), salt, row["username"])); c.commit(); c.close()
            return self._send(200, {"ok": True})
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/auth/me":
            row = self._user()
            if not row: return self._send(401, {"detail": "未登录"})
            return self._send(200, {"user": {
                "username": row["username"], "name": row["display_name"], "points": row["points"],
                "role": row["role"], "must_change": bool(row["must_change"])}})
        if p == "/api/auth/health":
            return self._send(200, {"ok": True, "service": "huangque-auth"})
        self._send(404, {"detail": "not found"})

if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "create-user":
        pts = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        role = sys.argv[5] if len(sys.argv) > 5 else 'member'
        create_user(sys.argv[2], sys.argv[3], pts, role)
        sys.exit(0)
    init_db()
    print("huangque-auth on 127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
