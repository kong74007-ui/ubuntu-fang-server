#!/usr/bin/env python3
"""服务器端 worker —— 跑在腾讯云服务器上，用青果住宅代理 + 注入 cookie 爬抖音。
轮询本机队列(127.0.0.1:8090) → 提青果IP + 注cookie/localStorage → MediaCrawler 爬 → 过滤 → 回传。
取代 Mac worker，彻底脱离 Mac。
"""
import os
import re
import sys
import json
import time
import glob
import shutil
import subprocess
import urllib.request
import urllib.parse

MC = "/home/ubuntu/MediaCrawler"
VENV = MC + "/.venv/bin/python"
UD = MC + "/browser_data/dy_user_data_dir"
COOKIES_JSON = "/home/ubuntu/dy_cookies.json"
QG_KEY = os.environ.get("QG_KEY", "55DC9F7E")
SERVER = os.environ.get("LEADGEN_SERVER", "http://127.0.0.1:8090")
TOKEN = os.environ.get("LEADGEN_WORKER_TOKEN", "worker-secret-2026")
POLL = 8

sys.path.insert(0, "/home/ubuntu/douyin-leadgen")
from scripts.leads_filter import is_spam, is_high  # noqa


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


def http_get(path):
    with urllib.request.urlopen(SERVER + path, timeout=20) as r:
        return json.loads(r.read())


def http_post(path, data):
    body = urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(urllib.request.Request(SERVER + path, data=body), timeout=30) as r:
        return json.loads(r.read())


def load_jsonl(pattern):
    rows = []
    for p in glob.glob(pattern):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def extract_qg():
    """提青果住宅代理，返回 (gateway_host:port, 出口IP, 地区)。"""
    url = f"https://share.proxy.qg.net/get?key={QG_KEY}&num=1&format=json"
    d = json.loads(urllib.request.urlopen(url, timeout=15).read())
    it = d["data"][0]
    return it["server"], it["proxy_ip"], it["area"], it["isp"]


def prep_login(proxy_server=None):
    """全新档案：注入 cookie + localStorage HasUserLogin=1，使 pong 跳过登录。
    proxy_server=None 时直连（机房IP，深采接口放行）。"""
    import asyncio
    from playwright.async_api import async_playwright
    cookies = json.load(open(COOKIES_JSON, encoding="utf-8"))
    for c in cookies:
        if c.get("sameSite") not in ("Strict", "Lax", "None"):
            c["sameSite"] = "Lax"
    if os.path.isdir(UD):
        shutil.rmtree(UD)
    proxy_arg = {"server": f"http://{proxy_server}"} if proxy_server else None

    async def _run():
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                UD, headless=True, proxy=proxy_arg,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45000)
            await page.evaluate("() => window.localStorage.setItem('HasUserLogin','1')")
            await page.wait_for_timeout(2500)
            await page.goto("https://www.douyin.com/user/self", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            body = await page.evaluate("() => document.body.innerText")
            ok = bool(re.search(r"抖音号[:：]", body))
            await ctx.close()
            return ok
    return asyncio.get_event_loop().run_until_complete(_run())


def patch_config(keyword, count, proxy_server):
    cfg = os.path.join(MC, "config/base_config.py")
    src = open(cfg, encoding="utf-8").read()
    rules = {
        r'^PLATFORM\s*=.*$': 'PLATFORM = "dy"',
        r'CRAWLER_TYPE = \(\s*"[^"]+"': 'CRAWLER_TYPE = (\n    "search"',
        r'^KEYWORDS\s*=.*$': f'KEYWORDS = "{keyword}"',
        r'^CRAWLER_MAX_NOTES_COUNT\s*=.*$': f'CRAWLER_MAX_NOTES_COUNT = {count}',
        r'^HEADLESS\s*=.*$': 'HEADLESS = True',
        r'^ENABLE_CDP_MODE\s*=.*$': 'ENABLE_CDP_MODE = False',
        r'^ENABLE_GET_COMMENTS\s*=.*$': 'ENABLE_GET_COMMENTS = True',
        r'^CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES\s*=.*$': 'CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 30',
        r'^ENABLE_GET_SUB_COMMENTS\s*=.*$': 'ENABLE_GET_SUB_COMMENTS = False',
        r'^SAVE_DATA_OPTION\s*=.*$': 'SAVE_DATA_OPTION = "jsonl"',
        r'^ENABLE_IP_PROXY\s*=.*$': 'ENABLE_IP_PROXY = True',
        r'^IP_PROXY_PROVIDER_NAME\s*=.*$': 'IP_PROXY_PROVIDER_NAME = "static"',
        r'^STATIC_PROXY_URL\s*=.*$': f'STATIC_PROXY_URL = "http://{proxy_server}"',
    }
    for pat, rep in rules.items():
        new = re.sub(pat, rep, src, count=1, flags=re.M)
        if new == src and pat.startswith("^STATIC_PROXY_URL"):
            new = src + f'\nSTATIC_PROXY_URL = "http://{proxy_server}"\n'
        src = new
    open(cfg, "w", encoding="utf-8").write(src)


def run_crawl():
    jdir = os.path.join(MC, "data/douyin/jsonl")
    os.makedirs(jdir, exist_ok=True)
    for f in glob.glob(os.path.join(jdir, "*.jsonl")):
        os.remove(f)
    subprocess.run([VENV, "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "search"],
                   cwd=MC, timeout=900, check=True,
                   env={**os.environ, "NO_PROXY": "localhost,127.0.0.1"},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jdir


def build_result(jdir):
    comments = load_jsonl(os.path.join(jdir, "search_comments_*.jsonl"))
    titles, hashtags, videos, vid_seen = {}, {}, [], set()
    for d in load_jsonl(os.path.join(jdir, "search_contents_*.jsonl")):
        aid = d.get("aweme_id")
        title = d.get("title") or ""
        if aid:
            titles[aid] = title[:24]
        for tag in re.findall(r"#([^#\s\n]+)", title):
            tag = tag.strip()
            if 1 < len(tag) <= 12:
                hashtags[tag] = hashtags.get(tag, 0) + 1
        if aid and aid not in vid_seen:
            vid_seen.add(aid)
            videos.append({
                "aweme_id": aid, "title": title, "nickname": d.get("nickname") or "",
                "likes": d.get("liked_count", 0), "comment_count": d.get("comment_count", 0),
                "url": d.get("aweme_url") or f"https://www.douyin.com/video/{aid}",
                "cover": d.get("cover_url") or "", "ip": d.get("ip_location") or "",
                "download_url": d.get("video_download_url") or "", "leads_count": 0,
            })
    related = [t for t, _ in sorted(hashtags.items(), key=lambda x: -x[1])][:24]
    leads, spam, chat, seen = [], 0, 0, set()
    for c in comments:
        t = (c.get("content") or "").strip()
        if not t:
            continue
        if is_spam(t):
            spam += 1; continue
        if len(re.sub(r"\[[^\]]+\]", "", t).strip()) < 2:
            chat += 1; continue
        if is_high(t):
            key = (c.get("user_id"), t)
            if key in seen:
                continue
            seen.add(key)
            sec = c.get("sec_uid") or ""
            aid = c.get("aweme_id") or ""
            leads.append({
                "nickname": c.get("nickname"), "douyin_id": c.get("user_unique_id") or "",
                "sec_uid": sec, "profile": f"https://www.douyin.com/user/{sec}" if sec else "",
                "ip": c.get("ip_location"), "content": c.get("content"),
                "source": titles.get(aid, ""), "aweme_id": aid, "like": c.get("like_count", 0),
            })
        else:
            chat += 1
    leads.sort(key=lambda x: (len(x["content"]), x["like"]), reverse=True)
    lc = {}
    for l in leads:
        if l["aweme_id"]:
            lc[l["aweme_id"]] = lc.get(l["aweme_id"], 0) + 1
    for v in videos:
        v["leads_count"] = lc.get(v["aweme_id"], 0)
    videos.sort(key=lambda v: v["leads_count"], reverse=True)
    return {"total": len(comments), "leads_count": len(leads), "spam": spam, "chat": chat,
            "leads": leads, "videos": videos, "related_keywords": related}


def handle_search(job):
    jid, kw, cnt = job["id"], job["keyword"], job.get("count") or 10
    jdir = os.path.join(MC, "data/douyin/jsonl")
    for attempt in range(1, 4):  # 青果短效IP不稳，失败换IP重试
        server, pxip, area, isp = extract_qg()
        log(f"[job {jid}] 第{attempt}次 代理出口 {pxip} {area}{isp}")
        try:
            if not prep_login(server):
                log(f"[job {jid}] 登录态注入失败，换IP重试"); continue
            log(f"[job {jid}] 登录态就绪，开爬「{kw}」x{cnt}")
            patch_config(kw, cnt, server)
            crawl_ok = True
            try:
                run_crawl()
            except subprocess.CalledProcessError:
                crawl_ok = False  # 中途失败，看有没有抓到部分数据
            res = build_result(jdir)
            got = res["leads_count"] > 0 or len(res["videos"]) > 0
            if not got and attempt < 3:
                log(f"[job {jid}] 爬取{'中断' if not crawl_ok else ''}且无数据，换IP重试 {attempt}/3")
                continue
            if not crawl_ok and got:
                log(f"[job {jid}] 爬取中断但已抓到部分数据，采用")
            log(f"[job {jid}] ✅ {res['leads_count']} 客户 / {len(res['videos'])} 视频")
            http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                        "result": json.dumps(res, ensure_ascii=False)})
            return
        except Exception as e:
            log(f"[job {jid}] 第{attempt}次异常 {str(e)[:90]}，换IP重试")
            continue
    raise RuntimeError("搜索失败（青果代理连续不稳，3次都没成，稍后重试或检查青果余额）")


FILES_DIR = "/home/ubuntu/leadgen-server/files"
TRANS_DIR = "/home/ubuntu/leadgen-server/transcribe_out"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
_WHISPER = None


def whisper_model():
    global _WHISPER
    if _WHISPER is None:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    return _WHISPER


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def download_file(url, out_path, proxy=None, retries=4):
    MAX = 300 * 1024 * 1024  # 300MB 上限，防内存/磁盘炸
    if os.path.exists(out_path) and os.path.getsize(out_path) > 100_000:
        return True
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
    handler = (urllib.request.ProxyHandler({"http": f"http://{proxy}", "https": f"http://{proxy}"})
               if proxy else urllib.request.ProxyHandler({}))   # proxy=None 时不走系统代理
    opener = urllib.request.build_opener(handler)
    for _ in range(retries):
        try:
            with opener.open(req, timeout=120) as r:
                cl = r.headers.get("Content-Length")
                if cl and int(cl) > MAX:
                    log("  文件过大跳过"); return False
                size = 0
                with open(out_path, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX:
                            log("  文件超限中止")
                            f.close()
                            if os.path.exists(out_path):
                                os.remove(out_path)
                            return False
                        f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 50_000:
                return True
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception as e:
            log(f"  下载重试: {str(e)[:80]}")
            time.sleep(2)
    return False


def fetch_video(aid, url, out_path):
    """先直连下载，失败走青果代理。"""
    if download_file(url, out_path):
        return True
    log("  直连失败 → 走青果代理")
    server, *_ = extract_qg()
    return download_file(url, out_path, proxy=server)


def handle_download(job):
    jid = job["id"]
    pl = json.loads(job.get("payload") or "{}")
    aid, url = pl.get("aweme_id"), pl.get("url")
    os.makedirs(FILES_DIR, exist_ok=True)
    out = os.path.join(FILES_DIR, f"{aid}.mp4")
    log(f"[job {jid}] 提取视频 {aid}")
    if not fetch_video(aid, url, out):
        raise RuntimeError("视频下载失败（链接可能已过期）")
    size = os.path.getsize(out)
    http_post("/api/complete", {"token": TOKEN, "job_id": jid, "result": json.dumps(
        {"type": "download", "file": f"files/{aid}.mp4", "size": size, "aweme_id": aid},
        ensure_ascii=False)})
    log(f"[job {jid}] ✅ 视频 {size // 1024}KB")


def handle_transcribe(job):
    jid = job["id"]
    pl = json.loads(job.get("payload") or "{}")
    aid, url = pl.get("aweme_id"), pl.get("url")
    os.makedirs(TRANS_DIR, exist_ok=True)
    mp4 = os.path.join(TRANS_DIR, f"{aid}.mp4")
    wav = os.path.join(TRANS_DIR, f"{aid}.wav")
    log(f"[job {jid}] 提取文案 {aid}：下载视频")
    if not fetch_video(aid, url, mp4):
        raise RuntimeError("视频下载失败（链接可能已过期）")
    log(f"[job {jid}] 提取音频")
    subprocess.run([ffmpeg_exe(), "-y", "-i", mp4, "-ar", "16000", "-ac", "1", "-vn", wav],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"[job {jid}] 语音识别(ASR)…")
    segments, info = whisper_model().transcribe(wav, language="zh", vad_filter=True)
    text = "".join(s.text for s in segments).strip()
    for f in (mp4, wav):
        try:
            os.remove(f)
        except Exception:
            pass
    http_post("/api/complete", {"token": TOKEN, "job_id": jid, "result": json.dumps(
        {"type": "transcribe", "text": text, "char_count": len(text), "aweme_id": aid},
        ensure_ascii=False)})
    log(f"[job {jid}] ✅ 文案 {len(text)} 字")


def patch_config_account(creator_input, proxy_server):
    cfg = os.path.join(MC, "config/base_config.py")
    s = open(cfg, encoding="utf-8").read()
    s = re.sub(r'^PLATFORM\s*=.*$', 'PLATFORM = "dy"', s, count=1, flags=re.M)
    s = re.sub(r'CRAWLER_TYPE = \(\s*"[^"]+"', 'CRAWLER_TYPE = (\n    "creator"', s, count=1)
    s = re.sub(r'^HEADLESS\s*=.*$', 'HEADLESS = True', s, count=1, flags=re.M)
    s = re.sub(r'^ENABLE_CDP_MODE\s*=.*$', 'ENABLE_CDP_MODE = False', s, count=1, flags=re.M)
    s = re.sub(r'^ENABLE_GET_COMMENTS\s*=.*$', 'ENABLE_GET_COMMENTS = True', s, count=1, flags=re.M)
    s = re.sub(r'^CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES\s*=.*$',
               'CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 100', s, count=1, flags=re.M)
    s = re.sub(r'^ENABLE_GET_SUB_COMMENTS\s*=.*$', 'ENABLE_GET_SUB_COMMENTS = False', s, count=1, flags=re.M)
    s = re.sub(r'^SAVE_DATA_OPTION\s*=.*$', 'SAVE_DATA_OPTION = "jsonl"', s, count=1, flags=re.M)
    if proxy_server:
        s = re.sub(r'^ENABLE_IP_PROXY\s*=.*$', 'ENABLE_IP_PROXY = True', s, count=1, flags=re.M)
        s = re.sub(r'^IP_PROXY_PROVIDER_NAME\s*=.*$', 'IP_PROXY_PROVIDER_NAME = "static"', s, count=1, flags=re.M)
        s = re.sub(r'^STATIC_PROXY_URL\s*=.*$', f'STATIC_PROXY_URL = "http://{proxy_server}"', s, count=1, flags=re.M)
    else:
        s = re.sub(r'^ENABLE_IP_PROXY\s*=.*$', 'ENABLE_IP_PROXY = False', s, count=1, flags=re.M)
    s = re.sub(r'^CRAWLER_MAX_NOTES_COUNT\s*=.*$', 'CRAWLER_MAX_NOTES_COUNT = 100', s, count=1, flags=re.M)
    open(cfg, "w", encoding="utf-8").write(s)
    # DY_CREATOR_ID_LIST 在 config/dy_config.py（不在 base_config）
    dyc = os.path.join(MC, "config/dy_config.py")
    d = open(dyc, encoding="utf-8").read()
    d = re.sub(r'DY_CREATOR_ID_LIST\s*=\s*\[.*?\]',
               f'DY_CREATOR_ID_LIST = [\n    "{creator_input}",\n]', d, count=1, flags=re.S)
    open(dyc, "w", encoding="utf-8").write(d)


def build_account_result(jdir):
    creators = load_jsonl(os.path.join(jdir, "creator_creators_*.jsonl"))
    works_all = load_jsonl(os.path.join(jdir, "creator_contents_*.jsonl"))
    comments = load_jsonl(os.path.join(jdir, "creator_comments_*.jsonl"))
    c = creators[0] if creators else {}
    sec = works_all[0].get("sec_uid") if works_all else ""
    works, seen = [], set()
    for w in works_all:
        if sec and w.get("sec_uid") != sec:
            continue
        aid = w.get("aweme_id")
        if aid in seen:
            continue
        seen.add(aid)
        works.append({
            "aweme_id": aid, "title": w.get("title") or w.get("desc") or "",
            "cover": w.get("cover_url") or "", "likes": w.get("liked_count"),
            "comments": w.get("comment_count"), "shares": w.get("share_count"),
            "collects": w.get("collected_count"), "create_time": w.get("create_time"),
            "url": w.get("aweme_url") or "", "download_url": w.get("video_download_url") or "",
        })
    return {
        "type": "account", "nickname": c.get("nickname"),
        "douyin_id": works_all[0].get("user_unique_id") if works_all else "",
        "fans": c.get("fans"), "likes": c.get("interaction"),
        "works_count": len(works), "comments_count": len(comments),
        "ip": (c.get("ip_location") or "").replace("IP属地：", ""),
        "desc": c.get("desc") or "", "avatar": c.get("avatar") or "",
        "profile": f"https://www.douyin.com/user/{sec}" if sec else "",
        "works": works,
    }


def handle_account(job):
    jid, target = job["id"], job["keyword"]
    log(f"[job {jid}] 账号深扒（直连机房IP，深采接口放行）{target[:46]}")
    if not prep_login():          # 不走代理：深采放行机房IP
        raise RuntimeError("登录态注入失败（cookie 可能过期）")
    patch_config_account(target, None)
    jdir = os.path.join(MC, "data/douyin/jsonl")
    os.makedirs(jdir, exist_ok=True)
    for f in glob.glob(os.path.join(jdir, "*.jsonl")):
        os.remove(f)
    subprocess.run([VENV, "main.py", "--platform", "dy", "--lt", "qrcode", "--type", "creator"],
                   cwd=MC, timeout=1800, check=True,
                   env={**os.environ, "NO_PROXY": "localhost,127.0.0.1"},
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    res = build_account_result(jdir)
    log(f"[job {jid}] ✅ 账号「{res.get('nickname')}」作品{res.get('works_count')}/评论{res.get('comments_count')}")
    http_post("/api/complete", {"token": TOKEN, "job_id": jid,
                                "result": json.dumps(res, ensure_ascii=False)})


def main():
    log(f"server_worker 启动，轮询 {SERVER}")
    while True:
        try:
            r = http_get(f"/api/claim?token={TOKEN}")
            job = r.get("job")
            if not job:
                time.sleep(POLL); continue
            mode = job.get("mode") or "search"
            log(f"领到任务 #{job['id']} mode={mode} kw={job.get('keyword')}")
            try:
                if mode == "transcribe":
                    handle_transcribe(job)
                elif mode == "download_video":
                    handle_download(job)
                elif mode == "account":
                    handle_account(job)
                else:
                    handle_search(job)
            except subprocess.TimeoutExpired:
                http_post("/api/fail", {"token": TOKEN, "job_id": job["id"], "error": "爬取超时(代理IP可能过期，重试)"})
            except Exception as e:
                log(f"[job {job['id']}] ❌ {e}")
                http_post("/api/fail", {"token": TOKEN, "job_id": job["id"], "error": str(e)[:300]})
        except Exception as e:
            log(f"轮询错误: {e}")
            time.sleep(POLL)


if __name__ == "__main__":
    main()
