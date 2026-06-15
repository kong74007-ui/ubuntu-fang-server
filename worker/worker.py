#!/usr/bin/env python3
"""
Mac 端 worker（混合架构）—— 在 Tang 的 Mac 上跑（住宅 IP 能爬通抖音搜索）。
轮询服务器领任务 → 跑 MediaCrawler 关键词搜索 → leads_filter 过滤 → 回传名单。

跑法（Mac）：
  python worker/worker.py
环境变量（可选）：
  LEADGEN_SERVER   服务器地址，默认 http://129.204.166.13:8090
  LEADGEN_WORKER_TOKEN  与服务器一致的 worker 令牌
  MEDIACRAWLER_DIR  MediaCrawler 路径，默认 ~/code/MediaCrawler
"""
import os
import re
import sys
import json
import time
import glob
import shutil
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.leads_filter import is_spam, is_high, load_jsonl  # 复用过滤核心
import re as _re

SERVER = os.environ.get("LEADGEN_SERVER", "http://129.204.166.13:8090")
TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "worker-secret-2026")
MC_DIR = os.path.expanduser(os.environ.get("MEDIACRAWLER_DIR", "~/code/MediaCrawler"))
HEADLESS = True   # Mac 住宅 IP；若搜索返回空改 False(有头)
POLL = 10         # 轮询间隔(秒)

import urllib.request
import urllib.parse


def http_get(path):
    with urllib.request.urlopen(SERVER + path, timeout=20) as r:
        return json.loads(r.read())


def http_post(path, data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(SERVER + path, data=body), timeout=20) as r:
        return json.loads(r.read())


def patch_config(keyword, count):
    """改 MediaCrawler 配置：平台/关键词/数量/搜索/无头/评论。"""
    cfg = os.path.join(MC_DIR, "config/base_config.py")
    src = open(cfg, encoding="utf-8").read()
    rules = {
        r'^PLATFORM\s*=.*$': 'PLATFORM = "dy"',
        r'^KEYWORDS\s*=.*$': f'KEYWORDS = "{keyword}"',
        r'^CRAWLER_MAX_NOTES_COUNT\s*=.*$': f'CRAWLER_MAX_NOTES_COUNT = {count}',
        r'^HEADLESS\s*=.*$': f'HEADLESS = {HEADLESS}',
        r'^ENABLE_CDP_MODE\s*=.*$': 'ENABLE_CDP_MODE = False',
        r'^ENABLE_GET_COMMENTS\s*=.*$': 'ENABLE_GET_COMMENTS = True',
        r'^SAVE_DATA_OPTION\s*=.*$': 'SAVE_DATA_OPTION = "jsonl"',
    }
    for pat, rep in rules.items():
        src = re.sub(pat, rep, src, count=1, flags=re.M)
    open(cfg, "w", encoding="utf-8").write(src)


def run_crawl():
    """清旧数据→跑 MediaCrawler→返回输出 jsonl 路径。"""
    jsonl_dir = os.path.join(MC_DIR, "data/douyin/jsonl")
    if os.path.isdir(jsonl_dir):
        for f in glob.glob(os.path.join(jsonl_dir, "*.jsonl")):
            os.remove(f)
    subprocess.run(
        ["uv", "run", "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "search"],
        cwd=MC_DIR, timeout=900, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonl_dir


def build_result(jsonl_dir):
    comments = load_jsonl(os.path.join(jsonl_dir, "search_comments_*.jsonl"))
    titles = {}
    hashtags = {}
    for d in load_jsonl(os.path.join(jsonl_dir, "search_contents_*.jsonl")):
        title = d.get("title") or ""
        titles[d.get("aweme_id")] = title[:24]
        # 从标题话题标签里挖相关关键词（#美容院获客 #美业老板…）
        for tag in _re.findall(r"#([^#\s\n]+)", title):
            tag = tag.strip()
            if 1 < len(tag) <= 12:
                hashtags[tag] = hashtags.get(tag, 0) + 1
    related = [t for t, _ in sorted(hashtags.items(), key=lambda x: -x[1])][:24]
    leads, spam, chat, seen = [], 0, 0, set()
    for c in comments:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if is_spam(t):
            spam += 1; continue
        if len(_re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if is_high(t):
            key = (c.get("user_id"), t)
            if key in seen:
                continue
            seen.add(key)
            leads.append({
                "nickname": c.get("nickname"),
                "douyin_id": c.get("user_unique_id") or "",
                "ip": c.get("ip_location"),
                "content": c.get("content"),
                "source": titles.get(c.get("aweme_id"), ""),
                "like": c.get("like_count", 0),
            })
        else:
            chat += 1
    leads.sort(key=lambda x: (len(x["content"]), x["like"]), reverse=True)
    return {"total": len(comments), "leads_count": len(leads),
            "spam": spam, "chat": chat, "leads": leads,
            "related_keywords": related}


def handle(job):
    jid, kw, cnt = job["id"], job["keyword"], job["count"]
    print(f"[job {jid}] 关键词「{kw}」x{cnt} 开始爬取…")
    try:
        patch_config(kw, cnt)
        jsonl_dir = run_crawl()
        result = build_result(jsonl_dir)
        http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                    "result": json.dumps(result, ensure_ascii=False)})
        print(f"[job {jid}] ✅ 完成：{result['leads_count']} 精准客户")
    except subprocess.TimeoutExpired:
        http_post("/api/fail", {"token": TOKEN, "job_id": jid, "error": "爬取超时(15分钟)"})
        print(f"[job {jid}] ❌ 超时")
    except Exception as e:
        http_post("/api/fail", {"token": TOKEN, "job_id": jid, "error": str(e)[:300]})
        print(f"[job {jid}] ❌ {e}")


def main():
    print(f"worker 启动，连接 {SERVER}，MediaCrawler={MC_DIR}")
    while True:
        try:
            res = http_get(f"/api/claim?token={TOKEN}")
            job = res.get("job")
            if job:
                handle(job)
            else:
                time.sleep(POLL)
        except KeyboardInterrupt:
            print("退出"); break
        except Exception as e:
            print("轮询出错(稍后重试):", e); time.sleep(POLL)


if __name__ == "__main__":
    main()
