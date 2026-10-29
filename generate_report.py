#!/usr/bin/env python3
"""
模板整理（降级方案）：AI 不可用时按 section 和 score 生成日报，保证"采集 → 整理 → 推送"链路始终可用。
GitHub 精选（report.style 为 repos）按上升速度挑仓库，同一类别和老牌大项目限量。
没有改写摘要和语义去重，质量明显低于 AI 整理。
"""

from __future__ import annotations

import sys
from typing import List, Tuple

from common import clip, load_config, pick_topic, raw_path, read_json, report_heading, report_path, today_str

KIND_TAGS = {'paper': '【论文】', 'repo': '【开源】', 'model': '【模型】'}
TOP_KINDS = ('official', 'release', 'media', 'model', 'kol')


def format_item(item: dict) -> str:
    lines = ['', f"**{KIND_TAGS.get(item['kind'], '')}{item['title'].strip()}**"]
    summary = clip(item.get('summary') or '', 100)
    if summary:
        lines.append(summary)
    lines.append(f"🔗 [{item['source']}]({item['url']})")
    return '\n'.join(lines)


def repo_parts(config: dict, items: List[dict]) -> Tuple[List[str], int]:
    report = config.get('report', {})
    tags = {tid: t.get('tag', t['name']) for tid, t in config['tracks'].items()}
    per_track, giants_left = report.get('max_per_track', 2), report.get('max_giants', 1)
    chosen, by_track = [], {}
    for item in sorted(items, key=lambda x: x.get('score', 0), reverse=True):
        if len(chosen) >= report.get('max_items_total', 5):
            break
        meta = item.get('meta') or {}
        track = meta.get('primary_track', '')
        # 没有 AI 改写，没描述的仓库读者看不出是做什么的
        if {'清单类仓库', '没有描述'} & set(meta.get('flags', [])) or by_track.get(track, 0) >= per_track:
            continue
        if meta.get('giant'):
            if giants_left <= 0:
                continue
            giants_left -= 1
        by_track[track] = by_track.get(track, 0) + 1
        chosen.append(item)

    parts = []
    for i, item in enumerate(chosen, 1):
        meta = item.get('meta') or {}
        tag = f'【{tags[meta["primary_track"]]}】' if meta.get('primary_track') in tags else ''
        parts += ['', f"## {i}.{tag}{clip(item['title'], 100)}", item.get('summary') or '']
        # 许可证不明确已经写在数据行里
        flags = [f for f in meta.get('flags', []) if f != '许可证不明确']
        if flags:
            parts.append('⚠️ ' + '、'.join(flags))
        parts.append(f"🔗 [GitHub]({item['url']})")
    return parts, len(chosen)


def news_parts(config: dict, date: str, items: List[dict]) -> Tuple[List[str], int]:
    report = config.get('report', {})
    by_score = sorted(items, key=lambda x: x.get('score', 0), reverse=True)
    top = [i for i in by_score if i['kind'] in TOP_KINDS][:report.get('top_picks', 3)]
    used = {i['url'] for i in top}
    papers_left = report.get('max_papers', 3)

    parts = []
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
    topic = pick_topic(config, date) if report.get('style') == 'learning' else None
    if topic:
        parts += ['', f"## 💡 每日知识点：{topic['title']}", topic.get('points', '')]
        if topic.get('refs'):
            parts.append('📎 延伸阅读：' + ' · '.join(f"[{r['title']}]({r['url']})" for r in topic['refs']))
    return parts, count


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

    if config.get('report', {}).get('style') == 'repos':
        intro = '今天 AI 整理不可用，以下是按上升速度自动挑选的仓库，未经编辑。'
        parts, count = repo_parts(config, items)
        stats = f'今天从 {len(items)} 个候选中精选 {count} 个'
    else:
        intro = '今天 AI 整理不可用，以下是按热度自动挑选的候选，未经编辑。'
        parts, count = news_parts(config, date, items)
        stats = f'本期从 {len(items)} 条候选中精选 {count} 条'
    content = '\n'.join(
        [f"{report_heading(config)} · {date[:4]}-{date[4:6]}-{date[6:]}", '', intro] + parts
        + ['', '---', f"📊 {stats} · 🤖 {config.get('bot_name', '资讯机器人')}自动生成（模板整理）"]) + '\n'
    with open(report_path(date), 'w', encoding='utf-8') as f:
        f.write(content)
    print(f'✅ 模板日报已生成：{report_path(date)}（{len(content)} 字符）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
