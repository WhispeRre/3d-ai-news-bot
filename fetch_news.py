#!/usr/bin/env python3
"""
资讯采集入口：并发采集 config.sources 里启用的信源，统一做方向匹配与过滤、历史去重、
跨源去重和数量截断，输出 output/raw_news_YYYYMMDD.json，
供 ai_report.py（AI 整理，首选）或 generate_report.py（模板整理，降级）使用。

调试用法：
  python3 fetch_news.py --only hf_papers,arxiv_3d --dry-run --show 5
  python3 fetch_news.py --no-seen        # 忽略历史去重，看完整候选
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

from collectors import COLLECTORS, CollectContext
from common import (RELIABILITY_RANK, NewsItem, TrackMatcher, call_with_retry, item_key, load_config,
                    load_env_var, load_seen_keys, local_now, raw_path, title_key, today_str, write_json)
from feishu import send_alert

# 只在采集阶段使用的内部字段，不写进原始数据
INTERNAL_META = ('match_text', 'force_keep', 'prefiltered')


def github_token() -> str:
    """Actions 里用 GITHUB_TOKEN；本地没配时借用 gh CLI 的登录态，匿名额度（60 次/小时）不够跑一轮"""
    token = load_env_var('GITHUB_TOKEN')
    if token or not shutil.which('gh'):
        return token
    try:
        out = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=10)
    except Exception:
        return ''
    return out.stdout.strip() if out.returncode == 0 else ''


def run_source(src: dict, ctx: CollectContext) -> dict:
    logs: List[str] = []
    started = time.time()
    result = {'src': src, 'items': [], 'error': '', 'logs': logs}
    collector = COLLECTORS.get(src.get('type', ''))
    if collector is None:
        result['error'] = f"未知信源类型 {src.get('type')}"
    else:
        cfg = ctx.config.get('collect', {})
        try:
            result['items'] = call_with_retry(collector, src, ctx, logs.append,
                                              retries=src.get('retries', cfg.get('retries', 2)),
                                              delay=cfg.get('retry_delay', 3))
        except Exception as e:
            result['error'] = f'{type(e).__name__}: {e}'[:300]
    result['elapsed'] = time.time() - started
    return result


def pick_primary(preset: List[str], matched: List[str], defaults: List[str]) -> str:
    """主方向：采集器指定的优先；其次是命中方向里属于信源默认方向的；再其次按 config.tracks 顺序取第一个"""
    if preset:
        return preset[0]
    for tid in matched:
        if tid in defaults:
            return tid
    if matched:
        return matched[0]
    return defaults[0] if defaults else ''


def filter_items(items: List[NewsItem], src: dict, ctx: CollectContext) -> List[NewsItem]:
    """
    方向匹配 + 信源级过滤：
    - filter: "all" 全收；"any" 命中任一方向才收；方向列表则命中其中之一才收
    - require: 额外要求命中的关键词（可用 @集合名）
    - exclude_titles: 标题命中这些关键词就丢弃
    采集器已自行过滤的条目（prefiltered）和强制保留的条目（force_keep）跳过信源级过滤
    """
    require = ctx.keywords(src.get('require'))
    exclude_titles = ctx.keywords(src.get('exclude_titles'))
    filt = src.get('filter', 'all')
    defaults = src.get('default_tracks', [])
    kept = []
    for item in items:
        text = f"{item.title} {item.summary} {item.meta.get('match_text', '')}"
        matched = ctx.matcher.match(text)
        preset = list(item.tracks)
        if not (item.meta.get('prefiltered') or item.meta.get('force_keep')):
            if exclude_titles and exclude_titles.search(item.title):
                continue
            if require and not require.search(text):
                continue
            if filt == 'any' and not matched:
                continue
            if isinstance(filt, list) and not set(matched) & set(filt):
                continue
        item.tracks = (preset + [t for t in matched if t not in preset]) or list(defaults)
        item.meta['primary_track'] = pick_primary(preset, matched, defaults)
        kept.append(item)
    return kept


def dedupe(items: List[NewsItem]) -> List[NewsItem]:
    """跨源去重：同一链接或同一标题只保留最权威的一条，其余来源记进 meta.also_in（说明关注度更高）"""
    items = sorted(items, key=lambda x: (RELIABILITY_RANK.get(x.reliability, 0), x.score), reverse=True)
    index: Dict[str, NewsItem] = {}
    out = []
    for item in items:
        keys = [item_key(item.url)]
        if item.meta.get('link_url'):
            keys.append(item_key(item.meta['link_url']))
        tk = title_key(item.title)
        if len(tk) >= 12:
            keys.append(f'title:{tk}')
        dup = next((index[k] for k in keys if k in index), None)
        if dup is not None:
            also = dup.meta.setdefault('also_in', [])
            if item.source != dup.source and item.source not in also:
                also.append(item.source)
            for k in ('upvotes', 'points', 'stars', 'likes', 'discussion_url'):
                if k in item.meta and k not in dup.meta:
                    dup.meta[k] = item.meta[k]
            dup.tracks += [t for t in item.tracks if t not in dup.tracks]
            continue
        for k in keys:
            index[k] = item
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description='采集资讯候选')
    parser.add_argument('--only', help='只采集这些信源 id（逗号分隔）')
    parser.add_argument('--dry-run', action='store_true', help='只打印统计，不写文件')
    parser.add_argument('--no-seen', action='store_true', help='忽略历史去重')
    parser.add_argument('--show', type=int, default=0, help='每个信源打印前 N 条保留的条目')
    args = parser.parse_args()

    config = load_config()
    collect_cfg = config.get('collect', {})
    ctx = CollectContext(
        config=config,
        matcher=TrackMatcher(config['tracks']),
        keyword_sets=config.get('keyword_sets', {}),
        summary_chars=collect_cfg.get('summary_chars', 280),
        default_max_age_hours=collect_cfg.get('default_max_age_hours', 48),
        github_token=github_token(),
    )

    sources = [s for s in config['sources'] if s.get('enabled', True)]
    if args.only:
        wanted = {x.strip() for x in args.only.split(',') if x.strip()}
        sources = [s for s in config['sources'] if s['id'] in wanted]
        missing = wanted - {s['id'] for s in sources}
        if missing:
            print(f"⚠️ 没有这些信源：{', '.join(sorted(missing))}")
    if not sources:
        print('❌ 没有要采集的信源')
        return 1

    seen = set() if args.no_seen else load_seen_keys()
    print(f"🚀 {config.get('name', '每日资讯')}：采集 {len(sources)} 个信源"
          f"（历史记录 {len(seen)} 条，GitHub token {'已配置' if ctx.github_token else '未配置'}）")

    started = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=collect_cfg.get('workers', 6)) as pool:
        futures = [pool.submit(run_source, src, ctx) for src in sources]
        for future in as_completed(futures):
            results.append(future.result())
    order = {s['id']: i for i, s in enumerate(sources)}
    results.sort(key=lambda r: order[r['src']['id']])

    track_section = {tid: sec['key'] for sec in config['sections'] for tid in sec['tracks']}
    fallback_section = config['sections'][-1]['key']
    candidates, stats = [], {}
    print()
    for r in results:
        src = r['src']
        fetched = r['items']
        kept = filter_items(fetched, src, ctx)
        fresh = [i for i in kept if item_key(i.url) not in seen]
        fresh.sort(key=lambda x: x.score, reverse=True)
        fresh = fresh[:src.get('max_items', 10)]
        for item in fresh:
            item.section = track_section.get(item.meta.get('primary_track', ''), fallback_section)
        candidates.extend(fresh)
        stats[src['id']] = {'name': src['name'], 'fetched': len(fetched), 'matched': len(kept),
                            'new': len(fresh), 'error': r['error'], 'seconds': round(r['elapsed'], 1)}

        mark = '❌' if r['error'] else '✅'
        print(f"{mark} {src['name']}：采集 {len(fetched)} → 过滤后 {len(kept)} → 新增入选 {len(fresh)}"
              f"（{r['elapsed']:.1f}s）")
        for line in r['logs']:
            print(f'     · {line}')
        if r['error']:
            print(f"     ✗ {r['error']}")
        for item in fresh[:args.show]:
            print(f"     → [{item.score}] {item.title[:90]}  {','.join(item.tracks[:3])}")

    items = dedupe(candidates)
    items.sort(key=lambda x: x.score, reverse=True)
    items = items[:collect_cfg.get('max_total_items', 220)]
    section_order = {sec['key']: i for i, sec in enumerate(config['sections'])}
    items.sort(key=lambda x: (section_order.get(x.section, 99), -x.score))
    for item in items:
        for k in INTERNAL_META:
            item.meta.pop(k, None)

    failed = [s for s in stats.values() if s['error']]
    print(f"\n🔍 候选 {len(candidates)} 条 → 跨源去重后 {len(items)} 条；"
          f"失败信源 {len(failed)}/{len(stats)}；总耗时 {time.time() - started:.0f}s")
    by_section: Dict[str, int] = {}
    for item in items:
        by_section[item.section] = by_section.get(item.section, 0) + 1
    for sec in config['sections']:
        print(f"   {sec['title']}：{by_section.get(sec['key'], 0)} 条")

    if not args.dry_run:
        date = today_str(config)
        write_json(raw_path(date), {
            'date': date,
            'generated_at': local_now(config).isoformat(timespec='seconds'),
            'total_count': len(items),
            'sections': [{'key': s['key'], 'title': s['title'], 'tracks': s['tracks']} for s in config['sections']],
            'tracks': {tid: t['name'] for tid, t in config['tracks'].items()},
            'source_stats': stats,
            'items': [i.to_dict() for i in items],
        })
        print(f'\n✅ 已写入 {raw_path(date)}')

    ratio = collect_cfg.get('alert_failed_ratio', 0.3)
    if not args.only and failed and len(failed) >= ratio * len(stats):
        detail = '\n'.join(f"- {s['name']}：{s['error']}" for s in failed)
        send_alert(config, f'{len(failed)}/{len(stats)} 个信源采集失败', detail)
    return 1 if len(failed) == len(stats) else 0


if __name__ == '__main__':
    sys.exit(main())
