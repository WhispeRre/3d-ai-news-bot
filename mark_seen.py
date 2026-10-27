#!/usr/bin/env python3
"""
推送成功后运行（当天没有日报时也运行）：
1. 把今天候选里的全部条目记入 state/seen.json（保留 60 天），以后采集自动过滤。
   AI 没选中的也要记，否则它们会天天回到候选里；
2. 把今天日报的条目标题记入 state/recent_topics.json（保留 7 天），供 AI 做跨天事件去重——
   同一事件不同渠道的连日报道链接各不相同，靠标题清单让 AI 在语义层判断。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import timedelta

from common import (SEEN_FILE, TOPICS_FILE, item_key, load_config, local_now, raw_path, read_json,
                    report_path, today_str, write_json)

SEEN_RETENTION_DAYS = 60
# 跨天去重的记忆窗口覆盖新闻跟进周期即可
TOPICS_RETENTION_DAYS = 7
TITLE_LINE_RE = re.compile(r'^\*\*(.+?)\*\*$')


def report_titles(path: str) -> list:
    """日报里独占一行的加粗文本就是条目标题，去掉序号和【论文】这类标签"""
    titles = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            m = TITLE_LINE_RE.match(line.strip())
            if not m:
                continue
            title = re.sub(r'^\d+[.、]\s*', '', m.group(1)).strip()
            title = re.sub(r'^【[^】]{1,6}】', '', title).strip()
            if title:
                titles.append(title)
    return titles


def main() -> int:
    config = load_config()
    date = today_str(config)
    raw = read_json(raw_path(date), None)
    if raw is None:
        print(f'❌ 找不到 {raw_path(date)}')
        return 1
    now = local_now(config)

    seen_cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).strftime('%Y%m%d')
    seen = {k: d for k, d in read_json(SEEN_FILE, {}).get('items', {}).items() if d >= seen_cutoff}
    added = 0
    for item in raw.get('items', []):
        key = item_key(item['url'])
        if key not in seen:
            seen[key] = date
            added += 1
    write_json(SEEN_FILE, {'updated_at': now.isoformat(timespec='seconds'), 'items': seen})
    print(f'✅ 去重记录：新增 {added} 条，共 {len(seen)} 条（保留 {SEEN_RETENTION_DAYS} 天）')

    topics_cutoff = (now - timedelta(days=TOPICS_RETENTION_DAYS)).strftime('%Y%m%d')
    days = {d: t for d, t in read_json(TOPICS_FILE, {}).get('days', {}).items() if d >= topics_cutoff}
    titles = report_titles(report_path(date)) if os.path.exists(report_path(date)) else []
    if titles:
        days[date] = titles
    write_json(TOPICS_FILE, {'updated_at': now.isoformat(timespec='seconds'), 'days': days})
    print(f'✅ 跨天去重记录：今天记入 {len(titles)} 个标题（保留 {TOPICS_RETENTION_DAYS} 天）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
