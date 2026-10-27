#!/usr/bin/env python3
"""
各类信源的采集函数，签名统一为 collect_xxx(src, ctx, log) -> List[NewsItem]：
- src：config.sources 里的一项
- ctx：CollectContext，提供方向匹配器、关键词集合、GitHub token 等
- log：记录日志（并发采集时按信源归组打印）

采集函数只负责拿到"时间窗口内的候选"；方向过滤、历史去重、数量截断由 fetch_news.py 统一处理。
"""

from __future__ import annotations

import html
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

import requests

from common import (ARXIV_ID_RE, NewsItem, TrackMatcher, clean_text, clip, compile_keywords,
                    entry_datetime, fetch_feed, http_get, iso, load_env_var, parse_iso,
                    resolve_keywords, utcnow, within_hours)

Log = Callable[[str], None]


@dataclass
class CollectContext:
    config: dict
    matcher: TrackMatcher
    keyword_sets: Dict[str, List[str]]
    summary_chars: int = 280
    default_max_age_hours: int = 48
    github_token: str = ''
    stats: Dict[str, dict] = field(default_factory=dict)

    def keywords(self, value) -> Optional[re.Pattern]:
        return compile_keywords(resolve_keywords(value, self.keyword_sets))


def make_item(src: dict, ctx: CollectContext, *, title: str, url: str, summary: str = '',
              published: Optional[datetime] = None, score_bonus: int = 0,
              tracks: Optional[List[str]] = None, meta: Optional[dict] = None,
              match_text: str = '') -> NewsItem:
    summary = clean_text(summary)
    item_meta = dict(meta or {})
    # 摘要会被截短，方向匹配用更长的原文
    item_meta['match_text'] = f'{match_text} {summary}'[:2000]
    return NewsItem(
        title=clip(clean_text(title), 200),
        url=url,
        source=src['name'],
        source_id=src['id'],
        kind=src.get('kind', 'media'),
        reliability=src.get('reliability', 'high'),
        summary=clip(summary, ctx.summary_chars),
        published_at=iso(published),
        score=int(src.get('score', 50) + score_bonus),
        tracks=list(tracks or []),
        meta=item_meta,
    )


def markdown_to_text(text: str) -> str:
    text = re.sub(r'^---\n.*?\n---\n', '', text or '', flags=re.S)  # YAML front matter
    text = re.sub(r'```.*?```', ' ', text, flags=re.S)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'[#>*`|]+', ' ', text)
    return clean_text(text)


# ---------------------------------------------------------------- RSS / Atom

def collect_rss(src, ctx, log):
    feed = fetch_feed(src['url'], timeout=src.get('timeout', 25))
    max_age = src.get('max_age_hours', ctx.default_max_age_hours)
    items = []
    for entry in feed.entries:
        published = entry_datetime(entry)
        # 没有发布时间的条目无法判断新旧，跳过，避免把旧文当新闻
        if not within_hours(published, max_age):
            continue
        title, link = entry.get('title', ''), entry.get('link', '')
        if not title or not link:
            continue
        summary = entry.get('summary', '') or entry.get('description', '')
        items.append(make_item(src, ctx, title=title, url=link, summary=summary, published=published))
    log(f'feed 共 {len(feed.entries)} 条，{max_age} 小时内 {len(items)} 条')
    return items


# ---------------------------------------------------------------- 论文

_ARXIV_PREFIX_RE = re.compile(r'^arXiv:\S+\s+Announce Type:\s*\S+\s*Abstract:\s*', re.I)


def collect_arxiv_rss(src, ctx, log):
    """arXiv 每日公告 RSS（周末不发公告，返回空属正常）"""
    items, seen = [], set()
    for i, cat in enumerate(src.get('categories', ['cs.GR'])):
        if i:
            time.sleep(1)
        feed = fetch_feed(f'https://rss.arxiv.org/rss/{cat}', timeout=40)
        new_count = 0
        for entry in feed.entries:
            if (entry.get('arxiv_announce_type') or 'new').startswith('replace'):
                continue
            m = ARXIV_ID_RE.search(entry.get('link', ''))
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            new_count += 1
            title = clean_text(entry.get('title', ''))
            abstract = _ARXIV_PREFIX_RE.sub('', clean_text(entry.get('summary', '')))
            tags = [t.get('term') for t in entry.get('tags', []) if t.get('term')]
            bonus = 5 if tags[:1] == ['cs.GR'] else 0
            if '3d_agent' in ctx.matcher.match(f'{title} {abstract}'):
                bonus += 15
            items.append(make_item(
                src, ctx, title=title, url=f'https://arxiv.org/abs/{m.group(1)}', summary=abstract,
                published=entry_datetime(entry), score_bonus=bonus,
                meta={'arxiv_id': m.group(1), 'authors': clip(clean_text(entry.get('author', '')), 120),
                      'categories': tags[:4]},
            ))
        log(f'{cat}：公告 {len(feed.entries)} 条，其中新论文 {new_count} 篇')
    return items


def collect_hf_daily_papers(src, ctx, log):
    """HF Daily Papers：社区精选论文，自带点赞数，是论文热度的主信号"""
    min_upvotes = src.get('min_upvotes', 0)
    items, seen = [], set()
    for offset in range(src.get('days_back', 2)):
        day = (utcnow() - timedelta(days=offset)).date().isoformat()
        data = http_get('https://huggingface.co/api/daily_papers',
                        params={'date': day, 'limit': 100}, timeout=30).json()
        for entry in data:
            paper = entry.get('paper') or {}
            pid = paper.get('id')
            upvotes = int(paper.get('upvotes') or 0)
            if not pid or pid in seen or upvotes < min_upvotes:
                continue
            seen.add(pid)
            abstract = clean_text(paper.get('summary', ''))
            meta = {'upvotes': upvotes, 'arxiv_id': pid}
            if paper.get('githubRepo'):
                meta['github_repo'] = paper['githubRepo']
                meta['github_stars'] = paper.get('githubStars')
            org = entry.get('organization') or paper.get('organization')
            if isinstance(org, dict) and (org.get('fullname') or org.get('name')):
                meta['organization'] = org.get('fullname') or org.get('name')
            items.append(make_item(
                src, ctx, title=paper.get('title') or entry.get('title', ''),
                url=f'https://huggingface.co/papers/{pid}',
                summary=paper.get('ai_summary') or abstract,
                published=parse_iso(paper.get('publishedAt')),
                score_bonus=min(upvotes // 4, 45), meta=meta,
                match_text=' '.join(paper.get('ai_keywords') or []) + ' ' + abstract,
            ))
        log(f'{day}：{len(data)} 篇')
    # 全场最热的几篇不论方向都保留，交给 AI 判断
    for item in sorted(items, key=lambda x: -x.meta['upvotes'])[:src.get('always_keep_top', 0)]:
        item.meta['force_keep'] = True
    return items


# ---------------------------------------------------------------- Hugging Face 模型

HF_MODELS_API = 'https://huggingface.co/api/models'


def hf_model_card(model_id: str, limit: int = 600) -> str:
    try:
        text = http_get(f'https://huggingface.co/{model_id}/raw/main/README.md', timeout=15).text
    except Exception:
        return ''
    return markdown_to_text(text)[:limit]


def _hf_model_items(src, ctx, models: List[dict]) -> List[NewsItem]:
    """把模型列表转成条目：过滤量化/去审查等衍生版，补抓前几个模型的简介"""
    noise = ctx.keywords('@model_noise')
    min_likes = src.get('min_likes', 0)
    items = []
    for m in models:
        mid = m.get('id') or m.get('modelId')
        likes = int(m.get('likes') or 0)
        if not mid or likes < min_likes or (noise and noise.search(mid)):
            continue
        tag = m.get('pipeline_tag') or ''
        created = parse_iso(m.get('createdAt'))
        stats = [f'❤️ {likes} likes', f"⬇️ {m.get('downloads', 0)} 次下载"]
        if m.get('trendingScore'):
            stats.insert(1, f"📈 热度 {m['trendingScore']}")
        if created:
            stats.append(f'创建于 {created:%Y-%m-%d}')
        item = make_item(
            src, ctx, title=f'{mid}（{tag}）' if tag else mid, url=f'https://huggingface.co/{mid}',
            summary=' · '.join(stats), published=created, score_bonus=min(likes // 20, 30),
            meta={'likes': likes, 'pipeline_tag': tag, 'trending': m.get('trendingScore')},
            match_text=' '.join(m.get('tags') or []),
        )
        items.append(item)
    items.sort(key=lambda x: x.score, reverse=True)
    for item in items[:src.get('max_items', 5) * 2]:
        card = hf_model_card(item.url.rsplit('huggingface.co/', 1)[-1])
        if card:
            item.summary = clip(f'{card} （{item.summary}）', ctx.summary_chars + 120)
            item.meta['match_text'] += ' ' + card
    return items


def collect_hf_models(src, ctx, log):
    """按任务类型拉热门模型，只保留最近创建的（老模型常年挂在热门榜上）"""
    cutoff = utcnow() - timedelta(days=src.get('max_age_days', 14))
    models, seen = [], set()
    for tag in src['pipeline_tags']:
        data = http_get(HF_MODELS_API, params={'pipeline_tag': tag, 'sort': 'trendingScore',
                                               'direction': -1, 'limit': 40}, timeout=25).json()
        fresh = [m for m in data if (parse_iso(m.get('createdAt')) or cutoff) > cutoff and m['id'] not in seen]
        seen.update(m['id'] for m in fresh)
        models.extend(fresh)
        log(f'{tag}：热门 {len(data)} 个，{src.get("max_age_days", 14)} 天内新建 {len(fresh)} 个')
    return _hf_model_items(src, ctx, models)


def collect_hf_author_models(src, ctx, log):
    """盯重点机构在 HF 上新发的模型"""
    cutoff = utcnow() - timedelta(hours=src.get('max_age_hours', 72))
    models, failed = [], []
    for author in src['authors']:
        try:
            data = http_get(HF_MODELS_API, params={'author': author, 'sort': 'createdAt',
                                                   'direction': -1, 'limit': 10}, timeout=20).json()
        except Exception as e:
            failed.append(f'{author}({type(e).__name__})')
            continue
        models.extend(m for m in data if (parse_iso(m.get('createdAt')) or cutoff) > cutoff)
    if failed:
        log(f'获取失败：{", ".join(failed)}')
    if len(failed) == len(src['authors']):
        raise RuntimeError('所有机构都获取失败')
    log(f'{len(src["authors"])} 个机构近 {src.get("max_age_hours", 72)} 小时新模型 {len(models)} 个')
    return _hf_model_items(src, ctx, models)


# ---------------------------------------------------------------- GitHub

GITHUB_API = 'https://api.github.com'
_search_lock = threading.Lock()
_last_search_at = [0.0]
_SEMVER_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)')


def github_get(ctx, path: str, params: Optional[dict] = None):
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if ctx.github_token:
        headers['Authorization'] = f'Bearer {ctx.github_token}'
    resp = requests.get(f'{GITHUB_API}{path}', params=params, headers=headers, timeout=20)
    if resp.status_code in (403, 429) and resp.headers.get('X-RateLimit-Remaining') == '0':
        raise RuntimeError('GitHub API 额度用完（配置 GITHUB_TOKEN 可提高额度）')
    resp.raise_for_status()
    return resp.json()


def _repo_item(src, ctx, repo: dict) -> NewsItem:
    stars = repo.get('stargazers_count', 0)
    desc = clean_text(repo.get('description') or '')
    created = parse_iso(repo.get('created_at'))
    facts = [f'⭐ {stars}', repo.get('language') or '', f'创建于 {created:%Y-%m-%d}' if created else '']
    return make_item(
        src, ctx, title=f"{repo['full_name']}：{desc}" if desc else repo['full_name'],
        url=repo['html_url'], summary=' · '.join(f for f in facts if f),
        published=created, score_bonus=min(stars // 20, 30),
        meta={'stars': stars, 'language': repo.get('language'), 'topics': (repo.get('topics') or [])[:8]},
        match_text=' '.join(repo.get('topics') or []),
    )


def collect_github_search(src, ctx, log):
    """搜索近几天新建、星数过线的仓库；搜索接口限流严格，所有搜索请求全局串行"""
    since = (utcnow() - timedelta(days=src.get('created_within_days', 7))).date().isoformat()
    spacing = 2.2 if ctx.github_token else 6.5  # 有 token 30 次/分钟，无 token 10 次/分钟
    items, seen = [], set()
    for query in src['queries']:
        with _search_lock:
            wait = _last_search_at[0] + spacing - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_search_at[0] = time.time()
            data = github_get(ctx, '/search/repositories', {
                'q': f'{query} created:>={since} stars:>={src.get("min_stars", 10)}',
                'sort': 'stars', 'order': 'desc', 'per_page': src.get('per_query', 8),
            })
        new = [r for r in data.get('items', []) if r['full_name'] not in seen and not r.get('fork')]
        seen.update(r['full_name'] for r in new)
        items.extend(_repo_item(src, ctx, r) for r in new)
        log(f'"{query}"：{data.get("total_count", 0)} 个结果，取 {len(new)} 个')
    return items


def collect_github_new_repos(src, ctx, log):
    """盯重点机构新建的仓库（研究代码通常不发 Release，新建仓库是最早的信号）"""
    cutoff = utcnow() - timedelta(days=src.get('created_within_days', 7))
    min_stars = src.get('min_stars', 10)
    items, failed = [], []
    for org in src['orgs']:
        try:
            repos = github_get(ctx, f'/users/{org}/repos',
                               {'sort': 'created', 'direction': 'desc', 'per_page': 10})
        except Exception as e:
            failed.append(f'{org}({type(e).__name__})')
            continue
        for repo in repos:
            created = parse_iso(repo.get('created_at'))
            if not created or created < cutoff:
                break
            if repo.get('fork') or repo.get('archived') or repo.get('stargazers_count', 0) < min_stars:
                continue
            items.append(_repo_item(src, ctx, repo))
    if failed:
        log(f'获取失败：{", ".join(failed)}')
    if len(failed) == len(src['orgs']):
        raise RuntimeError('所有机构都获取失败')
    log(f'{len(src["orgs"])} 个机构，{src.get("created_within_days", 7)} 天内新仓库（≥{min_stars}⭐）{len(items)} 个')
    return items


def collect_github_releases(src, ctx, log):
    max_age = src.get('max_age_hours', 72)
    items, failed = [], []
    for cfg in src['repos']:
        repo = cfg['repo']
        try:
            # 一个仓库发多个包时（monorepo），主包的版本可能排在其他包后面，per_page 要配大一些
            releases = github_get(ctx, f'/repos/{repo}/releases', {'per_page': cfg.get('per_page', 5)})
        except Exception as e:
            failed.append(f'{repo}({type(e).__name__})')
            continue
        pattern = re.compile(cfg['tag_pattern']) if cfg.get('tag_pattern') else None
        for rel in releases:
            tag = rel.get('tag_name') or ''
            published = parse_iso(rel.get('published_at'))
            if rel.get('draft') or not within_hours(published, max_age):
                continue
            if rel.get('prerelease') and src.get('skip_prerelease', True):
                continue
            if pattern and not pattern.search(tag):
                continue
            semver = _SEMVER_RE.search(tag)
            # 补丁版本（x.y.z 且 z≠0）多为修 bug，日报只关心大版本和次版本
            if src.get('skip_patch') and semver and int(semver.group(3)) != 0:
                continue
            name = clean_text(rel.get('name') or '') or tag
            title = f'{repo} 发布 {name}' if tag in name else f'{repo} 发布 {tag}：{name}'
            items.append(make_item(
                src, ctx, title=title, url=rel['html_url'], summary=markdown_to_text(rel.get('body') or ''),
                published=published, tracks=cfg.get('tracks'), meta={'repo': repo, 'tag': tag},
            ))
    if failed:
        log(f'获取失败：{", ".join(failed)}')
    if len(failed) == len(src['repos']):
        raise RuntimeError('所有仓库的 Releases 都获取失败')
    log(f'{len(src["repos"])} 个仓库，{max_age} 小时内的大/次版本发布 {len(items)} 个')
    return items


# ---------------------------------------------------------------- 官网网页抓取

_SITEMAP_ENTRY_RE = re.compile(r'<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>')
_OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', re.I)
_OG_DESC_RE = re.compile(r'<meta[^>]+(?:property="og:description"|name="description")[^>]+content="([^"]*)"', re.I)
_PUBLISHED_TIME_RE = re.compile(r'<meta[^>]+property="article:published_time"[^>]+content="([^"]*)"', re.I)


def collect_anthropic_news(src, ctx, log):
    """
    Anthropic 官网没有 RSS，列表页结构也常改版：从 sitemap 取最近更新的文章（带精确时间），
    再从文章页的 og 标签取标题和摘要。
    整站重新发布会批量刷新 lastmod（几十篇旧文挤在一两分钟内），所以扎堆的条目排到后面先扫正常更新的，
    再用文章页的发布日期确认确实是新文章
    """
    max_age = src.get('max_age_hours', 72)
    paths = '|'.join(re.escape(p) for p in src.get('paths', ['news']))
    url_re = re.compile(rf'^https://www\.anthropic\.com/(?:{paths})/[a-zA-Z0-9-]+$')
    sitemap = http_get('https://www.anthropic.com/sitemap.xml', timeout=25).text
    entries = [(url, parse_iso(lastmod)) for url, lastmod in _SITEMAP_ENTRY_RE.findall(sitemap) if url_re.match(url)]
    if not entries:
        raise RuntimeError('sitemap 里没有匹配到文章，结构可能变了')
    recent = [e for e in entries if within_hours(e[1], max_age)]
    stamps = [t.timestamp() for _, t in recent]

    def crowded(t: datetime) -> bool:
        return sum(abs(s - t.timestamp()) <= 120 for s in stamps) >= 5

    recent.sort(key=lambda e: (crowded(e[1]), -e[1].timestamp()))
    items, stale = [], 0
    for url, updated in recent[:src.get('scan_top', 6)]:
        page = http_get(url, timeout=20).text
        title = _OG_TITLE_RE.search(page)
        if not title:
            continue
        published = _PUBLISHED_TIME_RE.search(page)
        published = parse_iso(published.group(1)) if published else None
        # 发布时间只精确到日期，多放宽一天
        if published and not within_hours(published, max_age + 24):
            stale += 1
            continue
        desc = _OG_DESC_RE.search(page)
        items.append(make_item(src, ctx, title=html.unescape(title.group(1)), url=url,
                               summary=html.unescape(desc.group(1)) if desc else '', published=updated))
    log(f'sitemap 共 {len(entries)} 篇，{max_age} 小时内更新 {len(recent)} 篇'
        + (f'，跳过 {stale} 篇被刷新的旧文' if stale else ''))
    return items


# ---------------------------------------------------------------- 社区

def collect_hackernews(src, ctx, log):
    """Algolia 搜索 API：一次请求拿到时间窗口内、分数过线的全部热帖，再按方向关键词过滤"""
    hours = src.get('max_age_hours', 36)
    since = int(time.time()) - hours * 3600
    data = http_get('https://hn.algolia.com/api/v1/search', params={
        'tags': 'story', 'hitsPerPage': 300,
        'numericFilters': f'created_at_i>{since},points>={src.get("min_points", 80)}',
    }, timeout=20).json()
    items = []
    for hit in data.get('hits', []):
        title, sid = hit.get('title'), hit.get('objectID')
        if not title or not sid:
            continue
        points = hit.get('points') or 0
        discussion = f'https://news.ycombinator.com/item?id={sid}'
        items.append(make_item(
            src, ctx, title=title, url=hit.get('url') or discussion,
            summary=f"👍 {points} points · 💬 {hit.get('num_comments') or 0} 条评论",
            published=parse_iso(hit.get('created_at')), score_bonus=min(points // 20, 35),
            meta={'points': points, 'discussion_url': discussion},
        ))
    log(f'近 {hours} 小时热帖 {len(items)} 条')
    return items


# RSS 内容里 [link] 指向帖子分享的外部链接（自发帖则指回 Reddit 本身）
_REDDIT_LINK_RE = re.compile(r'<a href="([^"]+)">\[link\]</a>')
_REDDIT_HOSTS_RE = re.compile(r'^https?://([a-z0-9-]+\.)?(reddit\.com|redd\.it)/', re.I)
_REDDIT_SUB_RE = re.compile(r'/r/([A-Za-z0-9_]+)/')


def _fetch_reddit_feed(url: str, log: Log):
    try:
        return fetch_feed(url)
    except requests.HTTPError as e:
        # Reddit 429 是 IP 级限流，退避后重试一次
        if e.response is not None and e.response.status_code == 429:
            log('被限流，30 秒后重试')
            time.sleep(30)
            return fetch_feed(url)
        raise


def _entry_subreddit(entry) -> str:
    tags = entry.get('tags') or []
    if tags and tags[0].get('term'):
        return tags[0]['term'].lower()
    m = _REDDIT_SUB_RE.search(entry.get('link', ''))
    return m.group(1).lower() if m else ''


def collect_reddit(src, ctx, log):
    """
    Reddit RSS（JSON API 已封）。限流是 IP 级的，所以榜单参数相同的子版合并成一个请求（r/a+b+c），
    再按子版拆开，各自应用过滤规则，按子版内名次估分
    """
    groups: Dict[tuple, List[dict]] = {}
    for sub in src['subreddits']:
        listing = sub.get('listing', 'hot')
        groups.setdefault((listing, sub.get('t', 'day') if listing == 'top' else ''), []).append(sub)

    items, failed = [], []
    for gi, ((listing, t), subs) in enumerate(groups.items()):
        if gi:
            time.sleep(5)
        url = f"https://www.reddit.com/r/{'+'.join(s['name'] for s in subs)}/{listing}/.rss?limit=100"
        if t:
            url += f'&t={t}'
        try:
            feed = _fetch_reddit_feed(url, log)
        except Exception as e:
            failed.extend(f"r/{s['name']}({type(e).__name__})" for s in subs)
            continue
        buckets: Dict[str, list] = {}
        for entry in feed.entries:
            buckets.setdefault(_entry_subreddit(entry), []).append(entry)
        for sub in subs:
            items.extend(_reddit_sub_items(src, ctx, log, sub, buckets.get(sub['name'].lower(), [])))
    if failed:
        log(f'获取失败：{", ".join(failed)}')
    if len(failed) == len(src['subreddits']):
        raise RuntimeError('所有子版都获取失败')
    return items


def _reddit_sub_items(src, ctx, log, sub: dict, entries: list) -> List[NewsItem]:
    name = sub['name']
    require = ctx.keywords(sub.get('require'))
    filt = sub.get('filter', 'all')
    items = []
    for rank, entry in enumerate(entries):
        content = entry.get('summary', '')
        text = re.sub(r'submitted by\s+/u/\S+.*$', '', clean_text(content)).strip()
        title = entry.get('title', '')
        full = f'{title} {text}'
        matched = ctx.matcher.match(full)
        if require and not require.search(full):
            continue
        if filt == 'any' and not matched:
            continue
        if isinstance(filt, list) and not set(matched) & set(filt):
            continue
        meta = {'subreddit': name, 'rank': rank + 1, 'prefiltered': True}
        link = _REDDIT_LINK_RE.search(content)
        if link and not _REDDIT_HOSTS_RE.match(html.unescape(link.group(1))):
            meta['link_url'] = html.unescape(link.group(1))
        items.append(make_item(
            src, ctx, title=title, url=entry.get('link', ''), summary=text,
            published=entry_datetime(entry), score_bonus=max(15 - rank * 2, 0),
            tracks=matched or sub.get('tracks'), meta=meta,
        ))
        if len(items) >= sub.get('take', 5):
            break
    log(f'r/{name}：榜单 {len(entries)} 条，保留 {len(items)} 条')
    return items


# ---------------------------------------------------------------- X (Twitter)

def _tweets_via_api(account: str, api_cfg: dict, api_key: str, max_age: int) -> Optional[List[dict]]:
    """twitterapi.io：低价套餐有 QPS 限流，429 时退避重试。
    接口一条推文都没返回时给 None（X 可能在搜索里隐藏了该账号），和"有推文但都不在时间窗内"区分开"""
    resp = None
    for attempt in range(3):
        resp = requests.get(f"{api_cfg.get('base_url', 'https://api.twitterapi.io')}/twitter/user/last_tweets",
                            params={'userName': account}, headers={'X-API-Key': api_key}, timeout=20)
        if resp.status_code == 429 and attempt < 2:
            time.sleep(5 * (attempt + 1))
            continue
        break
    resp.raise_for_status()
    data = resp.json()
    if data.get('status') == 'error':
        raise RuntimeError(data.get('msg') or data.get('message') or 'twitterapi.io 返回错误')
    # tweets 可能在顶层或 data 内
    tweets = data.get('tweets') or (data.get('data') or {}).get('tweets') or []
    if not tweets:
        return None
    out = []
    for tw in tweets:
        text = (tw.get('text') or '').strip()
        if tw.get('isReply') or not text or text.startswith('RT @'):
            continue
        try:
            created = datetime.strptime(tw.get('createdAt', ''), '%a %b %d %H:%M:%S %z %Y')
        except ValueError:
            created = None
        if not within_hours(created, max_age):
            continue
        out.append({'text': text, 'url': tw.get('url', ''), 'published': created,
                    'engagement': (tw.get('likeCount') or 0) + (tw.get('retweetCount') or 0) * 2})
    return out


def _tweets_via_nitter(account: str, instances: List[str], max_age: int) -> List[dict]:
    feed = None
    for instance in instances:
        try:
            candidate = fetch_feed(f'{instance}/{account}/rss')
        except Exception:
            continue
        if candidate.entries:
            feed = candidate
            break
    if feed is None:
        raise RuntimeError('所有 nitter 实例均无法获取')
    out = []
    for entry in feed.entries[:20]:
        text = clean_text(entry.get('title', ''))
        published = entry_datetime(entry)
        # nitter 里回复的标题以 "R to @xxx:" 开头
        if text.startswith('R to @') or text.startswith('RT by @') or not within_hours(published, max_age):
            continue
        # nitter 链接转回 x.com 原始链接
        url = re.sub(r'^https?://[^/]+/', 'https://x.com/', entry.get('link', '')).replace('#m', '')
        out.append({'text': clean_text(entry.get('summary', '')) or text, 'url': url,
                    'published': published, 'engagement': 0})
    return out


def collect_twitter(src, ctx, log):
    """
    X 账号推文：twitterapi.io 优先（需 API Key），单账号失败或返回空时回退 nitter RSS。
    每个账号组单独配置过滤规则（filter: all / any / 方向列表）和默认方向。
    """
    api_cfg = src.get('api_provider') or {}
    api_key = load_env_var(api_cfg.get('api_key_env', 'TWITTERAPI_IO_KEY')) if api_cfg else ''
    instances = src.get('nitter_instances', ['https://nitter.net'])
    max_age = src.get('max_age_hours', 36)
    log(f"采集途径：{api_cfg.get('name', 'API') + '（nitter 回退）' if api_key else 'nitter RSS（未配置 API Key）'}")

    items, failed, total = [], [], 0
    for group in src.get('account_groups', {}).values():
        filt = group.get('filter', 'all')
        for account in group.get('accounts', []):
            total += 1
            tweets = None
            if api_key:
                try:
                    tweets = _tweets_via_api(account, api_cfg, api_key, max_age)
                except Exception as e:
                    log(f'@{account} API 失败（{type(e).__name__}: {clip(str(e), 80)}），回退 nitter')
            if tweets is None:
                try:
                    tweets = _tweets_via_nitter(account, instances, max_age)
                except Exception:
                    failed.append(f'@{account}')
                    continue
                finally:
                    time.sleep(1)
            for tw in tweets:
                matched = ctx.matcher.match(tw['text'])
                if filt == 'any' and not matched:
                    continue
                if isinstance(filt, list) and not set(matched) & set(filt):
                    continue
                items.append(make_item(
                    src, ctx, title=f"@{account}：{clip(tw['text'], 120)}", url=tw['url'], summary=tw['text'],
                    published=tw['published'],
                    score_bonus=group.get('score', src.get('score', 60)) - src.get('score', 60)
                    + min(tw['engagement'] // 100, 20),
                    tracks=matched or group.get('tracks'),
                    meta={'account': account, 'group': group.get('name', ''), 'engagement': tw['engagement'],
                          'prefiltered': True},
                ))
    if failed:
        log(f'{len(failed)}/{total} 个账号获取失败：{", ".join(failed)}')
    if total and len(failed) == total:
        raise RuntimeError('所有账号都获取失败，twitterapi.io 和 nitter 可能都已失效')
    if failed and not items:
        raise RuntimeError(f'0 条推文且 {len(failed)}/{total} 个账号失败，采集途径可能部分失效')
    log(f'{total} 个账号，{max_age} 小时内保留 {len(items)} 条')
    return items


COLLECTORS: Dict[str, Callable] = {
    'rss': collect_rss,
    'arxiv_rss': collect_arxiv_rss,
    'hf_daily_papers': collect_hf_daily_papers,
    'hf_models': collect_hf_models,
    'hf_author_models': collect_hf_author_models,
    'github_search': collect_github_search,
    'github_new_repos': collect_github_new_repos,
    'github_releases': collect_github_releases,
    'anthropic_news': collect_anthropic_news,
    'hackernews': collect_hackernews,
    'reddit': collect_reddit,
    'twitter': collect_twitter,
}
