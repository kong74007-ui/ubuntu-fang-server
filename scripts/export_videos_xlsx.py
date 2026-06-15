#!/usr/bin/env python3
"""
视频级数据导出器 —— 把 MediaCrawler 的 search_contents jsonl 导出为 xlsx，
对齐「抖音医美数据.xlsx」的 18 列格式（视频维度全字段）。

用法：python export_videos_xlsx.py <search_contents_*.jsonl> <输出.xlsx>
"""
import sys
import glob
import json
from datetime import datetime, timezone, timedelta
import openpyxl

CST = timezone(timedelta(hours=8))
HEADERS = ["序号", "视频ID", "标题", "描述", "发布时间", "用户ID", "抖音号", "昵称",
           "点赞", "收藏", "评论数", "分享", "IP属地", "视频链接", "用户主页",
           "封面", "视频下载", "来源关键词"]


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def main():
    pattern, out = sys.argv[1], sys.argv[2]
    rows = []
    for p in glob.glob(pattern):
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "抖音视频"
    ws.append(HEADERS)
    for i, d in enumerate(rows, 1):
        sec = d.get("sec_uid") or ""
        ws.append([
            i,
            d.get("aweme_id"),
            d.get("title"),
            d.get("desc"),
            fmt_time(d.get("create_time")),
            d.get("user_id"),
            d.get("user_unique_id"),
            d.get("nickname"),
            d.get("liked_count"),
            d.get("collected_count"),
            d.get("comment_count"),
            d.get("share_count"),
            d.get("ip_location"),
            d.get("aweme_url"),
            f"https://www.douyin.com/user/{sec}" if sec else "",
            d.get("cover_url"),
            d.get("video_download_url"),
            d.get("source_keyword"),
        ])
    # 列宽微调
    for col, w in {"C": 40, "D": 40, "N": 36, "O": 36, "P": 30, "Q": 30}.items():
        ws.column_dimensions[col].width = w
    wb.save(out)
    print(f"✅ 导出 {len(rows)} 条视频 → {out}")


if __name__ == "__main__":
    main()
