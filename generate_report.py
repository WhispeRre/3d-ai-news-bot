#!/usr/bin/env python3
"""
模板整理（降级方案）：AI 不可用时按 section 和 score 生成日报，保证"采集 → 整理 → 推送"链路始终可用。
没有改写摘要和语义去重，质量明显低于 AI 整理。
"""

from __future__ import annotations

import sys

from common import clip, load_config, raw_path, read_json, report_path, today_str

KIND_TAGS = {'paper': '【论文】', 'repo': '【开源】', 'model': '【模型】'}
TOP_KINDS = ('official', 'release', 'media', 'model', 'kol')


def format_item(item: dict) -> str:
    lines = ['', f"**{KIND_TAGS.get(item['kind'], '')}{item['title'].strip()}**"]
    summary = clip(item.get('summary') or '', 100)
    if summary:
        lines.append(summary)
    lines.append(f"🔗 [{item['source']}]({item['url']})")
    return '\n'.join(lines)


def main() -> int:
    config = load_config()
    date = today_str(config)
    raw = read_json(raw_path(date), None)
    if raw is None:
        print(f'❌ 找不到原始数据 {raw_path(date)}，请先运行 fetch_news.py')
        return 1
    items = raw.get('items') or []
    if not items:
        print('ℹ️ 今天没有候选资讯，跳过日报生成')
        return 0

    report = config.get('report', {})
    by_score = sorted(items, key=lambda x: x.get('score', 0), reverse=True)
    top = [i for i in by_score if i['kind'] in TOP_KINDS][:report.get('top_picks', 3)]
    used = {i['url'] for i in top}
    papers_left = report.get('max_papers', 3)

    parts = [f"# 📰 {config.get('name', '每日资讯')} · {date[:4]}-{date[4:6]}-{date[6:]}", '',
             '今天 AI 整理不可用，以下是按热度自动挑选的候选，未经编辑。']
    if top:
        parts += ['', f'## 🏆 今日 TOP {len(top)}'] + [format_item(i) for i in top]
    count = len(top)
    for sec in config['sections']:
        chosen = []
        for item in by_score:
            if len(chosen) >= sec.get('max_items', 3):
                break
            # 社区帖子质量参差，模板模式不收
            if item['section'] != sec['key'] or item['url'] in used or item['kind'] == 'community':
                continue
            if item['kind'] == 'paper':
                if papers_left <= 0:
                    continue
                papers_left -= 1
            chosen.append(item)
        if chosen:
            parts += ['', f"## {sec['title']}"] + [format_item(i) for i in chosen]
            count += len(chosen)
    parts += ['', '---', f"📊 本期从 {len(items)} 条候选中精选 {count} 条 · "
                          f"🤖 {config.get('bot_name', '资讯机器人')}自动生成（模板整理）"]

    content = '\n'.join(parts) + '\n'
    with open(report_path(date), 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'✅ 模板日报已生成：{report_path(date)}（{len(content)} 字符）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
