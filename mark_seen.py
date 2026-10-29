#!/usr/bin/env python3
"""
推送成功后运行（当天没有日报时也运行）：
1. 把今天候选里的全部条目记入 state/seen.json，以后采集自动过滤。默认保留 60 天；
   配了 memory.seen_days 的（GitHub 精选）只冷却这么多天，过后没入选的条目可以再回到候选里。
   AI 没选中的也要记，否则它们会天天回到候选里；
2. 配了 memory.pushed_days 的，把今天日报推荐过的条目记入 state/<profile>/pushed.json，这么多天内不再推；
3. 把今天日报的条目标题记入 state/recent_topics.json（保留 7 天），供 AI 做跨天事件去重——
   同一事件不同渠道的连日报道链接各不相同，靠标题清单让 AI 在语义层判断；
4. 学习简报还要把今天讲过的知识点记入 state/<profile>/curriculum.json，明天接着讲下一个。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import timedelta

from common import (CURRICULUM_FILE, DEFAULT_SEEN_DAYS, PUSHED_FILE, SEEN_FILE, TOPICS_FILE, item_key, load_config,
                    local_now, pick_topic, raw_path, read_json, report_path, today_str, write_json)

# 跨天去重的记忆窗口覆盖新闻跟进周期即可
TOPICS_RETENTION_DAYS = 7
TITLE_LINE_RE = re.compile(r'^(?:\*\*(.+?)\*\*|## (\d+[.、].+))$')
LINK_RE = re.compile(r'\]\((https?://[^)\s]+)\)')


def report_titles(path: str) -> list:
    """
    日报里独占一行的加粗文本就是条目标题；GitHub 精选每个项目一节，带序号的 ## 标题就是项目名。
    去掉序号和【论文】这类标签
    """
    titles = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            m = TITLE_LINE_RE.match(line.strip())
            if not m:
                continue
            title = re.sub(r'^\d+[.、]\s*', '', m.group(1) or m.group(2)).strip()
            title = re.sub(r'^【[^】]{1,6}】', '', title).strip()
            if title:
                titles.append(title)
    return titles


def report_links(path: str) -> list:
    with open(path, 'r', encoding='utf-8') as f:
        return LINK_RE.findall(f.read())


def main() -> int:
    config = load_config()
    date = today_str(config)
    raw = read_json(raw_path(date), None)
    if raw is None:
        print(f'❌ 找不到 {raw_path(date)}')
        return 1
    now = local_now(config)
    memory = config.get('memory', {})
    has_report = os.path.exists(report_path(date))

    seen_days = memory.get('seen_days', DEFAULT_SEEN_DAYS)
    seen_cutoff = (now - timedelta(days=seen_days)).strftime('%Y%m%d')
    seen = {k: d for k, d in read_json(SEEN_FILE, {}).get('items', {}).items() if d >= seen_cutoff}
    candidates = {item_key(item['url']) for item in raw.get('items', [])}
    added = len(candidates - set(seen))
    # 记最近一次进候选的日期，冷却期从这天算
    seen.update(dict.fromkeys(candidates, date))
    write_json(SEEN_FILE, {'updated_at': now.isoformat(timespec='seconds'), 'items': seen})
    print(f'✅ 去重记录：新增 {added} 条，共 {len(seen)} 条（保留 {seen_days} 天）')

    pushed_days = memory.get('pushed_days')
    if pushed_days:
        pushed_cutoff = (now - timedelta(days=pushed_days)).strftime('%Y%m%d')
        pushed = {k: d for k, d in read_json(PUSHED_FILE, {}).get('items', {}).items() if d >= pushed_cutoff}
        # 只记属于候选的链接，讨论帖这类附带链接不算
        today = {k for k in map(item_key, report_links(report_path(date))) if k in candidates} if has_report else set()
        pushed.update(dict.fromkeys(today, date))
        write_json(PUSHED_FILE, {'updated_at': now.isoformat(timespec='seconds'), 'items': pushed})
        print(f'✅ 推送记录：今天推荐 {len(today)} 个，{pushed_days} 天内共 {len(pushed)} 个')

    topics_cutoff = (now - timedelta(days=TOPICS_RETENTION_DAYS)).strftime('%Y%m%d')
    days = {d: t for d, t in read_json(TOPICS_FILE, {}).get('days', {}).items() if d >= topics_cutoff}
    titles = report_titles(report_path(date)) if has_report else []
    if titles:
        days[date] = titles
    write_json(TOPICS_FILE, {'updated_at': now.isoformat(timespec='seconds'), 'days': days})
    print(f'✅ 跨天去重记录：今天记入 {len(titles)} 个标题（保留 {TOPICS_RETENTION_DAYS} 天）')

    topic = pick_topic(config, date) if config.get('report', {}).get('style') == 'learning' else None
    if topic and has_report:
        with open(report_path(date), 'r', encoding='utf-8') as f:
            taught = '每日知识点' in f.read()
        if taught:
            history = read_json(CURRICULUM_FILE, {}).get('history', {})
            history[date] = topic['id']
            write_json(CURRICULUM_FILE, {'updated_at': now.isoformat(timespec='seconds'), 'history': history})
            print(f"✅ 知识点进度：今天讲「{topic['title']}」，累计 {len(history)} 讲")
    return 0


if __name__ == '__main__':
    sys.exit(main())
