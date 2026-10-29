#!/usr/bin/env python3
"""
GitHub 精选的仓库处理（配置里有 collect.repos 时由 fetch_news.py 调用）：
- as_repo_items：每个信源采完后调用，条目统一指向仓库首页；Hacker News、Reddit 的帖子找不到 GitHub 链接就丢弃
- prepare_repos：跨源去重后调用，用 GitHub API 补全仓库详情 → 合并重复 → 按规则剔除可疑仓库
  → 按上升速度打分，各信源保底名额后截断成候选池 → 补 README 摘录（AI 判断"是什么、怎么上手"主要靠它）
"""

from __future__ import annotations

import base64
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import requests

from collectors import CollectContext, github_get
from common import NewsItem, clip, item_key, iso, parse_iso, utcnow

Log = Callable[[str], None]

_REPO_URL_RE = re.compile(r'^https?://(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)', re.I)
# github.com 下这些一级路径是站点页面，不是用户或机构
_RESERVED_OWNERS = {
    'about', 'apps', 'collections', 'customer-stories', 'enterprise', 'events', 'explore', 'features', 'login',
    'marketplace', 'new', 'notifications', 'orgs', 'pricing', 'readme', 'search', 'security', 'settings', 'site',
    'sponsors', 'topics', 'trending', 'users',
}
# 仓库名以版本号结尾的多是假"发布包"，例如 relay-v2.2.0
_VERSION_NAME_RE = re.compile(r'[-_.]v?\d+(?:\.\d+){1,3}$', re.I)
# 合并同一仓库的重复条目时，从被合并的条目补过来的字段
_MERGE_KEYS = ('stars_today', 'stars_week', 'points', 'post_title', 'discussion_url', 'subreddit')
_WORKERS = 8


def repo_of(url: str) -> str:
    """链接指向的仓库 owner/repo（仓库里的子页面也算），不是仓库链接时返回空串"""
    m = _REPO_URL_RE.match((url or '').strip())
    if not m or m.group(1).lower() in _RESERVED_OWNERS:
        return ''
    name = re.sub(r'\.git$', '', m.group(2).rstrip('.'))
    return f'{m.group(1)}/{name}' if name else ''


def as_repo_items(items: List[NewsItem]) -> List[NewsItem]:
    """条目统一指向仓库首页；社区帖子取它分享的 GitHub 链接（分享链接或正文里的），帖子标题和链接留作参考"""
    out = []
    for item in items:
        links = [item.url, item.meta.get('link_url', '')] + item.meta.pop('github_links', [])
        repo = next((r for r in map(repo_of, links) if r), '')
        if not repo:
            continue
        if item.kind == 'community':
            item.meta['post_title'] = item.title
            item.meta.setdefault('discussion_url', item.url)
        item.meta.pop('link_url', None)
        item.meta['repo'] = repo
        item.kind = 'repo'
        item.url = f'https://github.com/{repo}'
        out.append(item)
    return out


# ---------------------------------------------------------------- GitHub API

def _get_repo(ctx: CollectContext, repo: str) -> dict:
    return github_get(ctx, f'/repos/{repo}')


def _get_user(ctx: CollectContext, login: str) -> dict:
    return github_get(ctx, f'/users/{login}')


def _get_readme(ctx: CollectContext, repo: str) -> str:
    data = github_get(ctx, f'/repos/{repo}/readme')
    return base64.b64decode(data.get('content') or '').decode('utf-8', errors='replace')


def _fetch_all(ctx: CollectContext, keys: List[str], fetch: Callable) -> Tuple[Dict[str, Optional[object]], List[str]]:
    """并发调接口；不存在或被下架（404 / 451）记为 None，其他错误记进 errors"""
    def run(key):
        try:
            return key, fetch(ctx, key), ''
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in (404, 451):
                return key, None, ''
            return key, None, f'{key}：{e}'
        except Exception as e:
            return key, None, f'{key}：{type(e).__name__}: {e}'

    results, errors = {}, []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        for key, data, error in pool.map(run, keys):
            results[key] = data
            if error:
                errors.append(error)
    return results, errors


# ---------------------------------------------------------------- 规则与打分

def _days_since(value: Optional[str], now: datetime) -> int:
    dt = parse_iso(value)
    return max((now - dt).days, 0) if dt else 0


def _reject_reason(repo: dict, owner: Optional[dict], shady, cfg: dict, now: datetime) -> str:
    """明显不该推的仓库：返回剔除原因"""
    if repo.get('fork'):
        return '复刻仓库'
    if repo.get('archived') or repo.get('disabled'):
        return '已归档'
    if repo.get('stargazers_count', 0) < cfg.get('min_stars', 20):
        return '星数太少'
    if shady and shady.search(f"{repo['name']} {repo.get('description') or ''}"):
        return '命中排除词（破解、外挂、越狱、币圈等）'
    if _VERSION_NAME_RE.search(repo['name']):
        return '名字像安装包'
    if owner and not repo.get('description') and _days_since(owner.get('created_at'), now) < 30:
        return '新账号且没有描述'
    return ''


def _flags(repo: dict, owner: Optional[dict], license_id: str, now: datetime) -> List[str]:
    """不至于剔除、但推荐时要提醒读者的问题"""
    flags = []
    if not license_id:
        flags.append('许可证不明确')
    pushed = _days_since(repo.get('pushed_at'), now)
    if pushed > 60:
        flags.append(f'{pushed} 天没有提交')
    if owner and _days_since(owner.get('created_at'), now) < 90:
        flags.append(f"作者账号注册才 {_days_since(owner.get('created_at'), now)} 天")
    if not repo.get('description'):
        flags.append('没有描述')
    desc = (repo.get('description') or '').lower()
    if repo['name'].lower().startswith('awesome') or 'curated list' in desc or 'awesome list' in desc:
        flags.append('清单类仓库')
    return flags


def _heat(meta: dict) -> float:
    """估算每天新增多少星：上了 Trending 用榜单上的今日 / 本周新增，否则按新仓库建成以来的平均速度"""
    if meta.get('stars_today'):
        return meta['stars_today']
    if meta.get('stars_week'):
        return meta['stars_week'] / 7
    if meta['age_days'] <= 30:
        return meta['stars'] / max(meta['age_days'], 1)
    return 0


def _score(meta: dict) -> int:
    """
    上升速度分：每天新增星数 + 涨幅占总星数的比例 + 新仓库加分 - 老牌大项目减分 + 上榜、多渠道和 Show HN 加分。
    刚建几天的仓库涨幅比例天然拉满，上 Trending 的加分用来抵消，免得榜单上的老项目全被挤出候选
    """
    heat, stars = meta['heat'], max(meta['stars'], 1)
    score = 50 + min(heat // 40, 30) + min(heat * 100 // stars, 20)
    if meta['age_days'] <= 14:
        score += 15
    elif meta['age_days'] <= 60:
        score += 8
    if meta['giant']:
        score -= 15
    trending = meta.get('trending', [])
    if trending:
        score += 15 if '日榜' in trending else 10
    score += 5 * len(meta.get('also_in', [])) + min((meta.get('points') or 0) // 20, 15)
    return int(score)


def readme_excerpt(markdown: str, limit: int) -> str:
    """README 摘录：去掉图片、徽章、HTML 标签和链接地址，保留代码块里的命令（"怎么上手"多半写在那里）"""
    text = re.sub(r'<!--.*?-->', ' ', markdown[:30000], flags=re.S)
    text = re.sub(r'\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)', ' ', text)
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', text)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    lines = []
    for line in text.splitlines():
        line = re.sub(r'\s+', ' ', line).strip()
        if line and not line.startswith('```') and not re.fullmatch(r'[|:\-\s]+', line):
            lines.append(line)
    return clip('\n'.join(lines), limit)


def _pool(items: List[NewsItem], reserve: Dict[str, int], limit: int) -> List[NewsItem]:
    """
    候选池：先按 reserve 给信源各留几个名额（取它自己分数最高的几个），剩下的名额按分数填满。
    不同渠道的分数没法完全拉平（新仓库天然涨得快），留名额保证 Trending、社区推荐这些渠道都有代表
    """
    ranked = sorted(items, key=lambda x: x.score, reverse=True)
    taken: Dict[str, int] = {}
    reserved = []
    for item in ranked:
        if taken.get(item.source_id, 0) < reserve.get(item.source_id, 0):
            taken[item.source_id] = taken.get(item.source_id, 0) + 1
            reserved.append(item)
    picked = {id(i) for i in reserved}
    rest = [i for i in ranked if id(i) not in picked]
    pool = reserved[:limit] + rest[:max(limit - len(reserved), 0)]
    return sorted(pool, key=lambda x: x.score, reverse=True)


def _merge(into: NewsItem, other: NewsItem) -> None:
    also = into.meta.setdefault('also_in', [])
    for source in [other.source] + other.meta.get('also_in', []):
        if source != into.source and source not in also:
            also.append(source)
    for k in _MERGE_KEYS:
        if k in other.meta and k not in into.meta:
            into.meta[k] = other.meta[k]
    if other.meta.get('trending'):
        into.meta['trending'] = list(dict.fromkeys(into.meta.get('trending', []) + other.meta['trending']))


def _fill(item: NewsItem, repo: dict, owner: Optional[dict], ctx: CollectContext, cfg: dict, now: datetime) -> None:
    """用仓库详情改写条目：标题、摘要、创建时间、各项数据、类别和分数"""
    m = item.meta
    full, desc = repo['full_name'], (repo.get('description') or '').strip()
    license_id = (repo.get('license') or {}).get('spdx_id') or ''
    license_id = '' if license_id == 'NOASSERTION' else license_id
    created = parse_iso(repo.get('created_at'))
    m.update({
        'repo': full, 'stars': repo.get('stargazers_count', 0), 'forks': repo.get('forks_count', 0),
        'language': repo.get('language') or '', 'license': license_id, 'topics': (repo.get('topics') or [])[:8],
        'homepage': repo.get('homepage') or '', 'created_at': f'{created:%Y-%m-%d}' if created else '',
        'age_days': _days_since(repo.get('created_at'), now), 'pushed_days': _days_since(repo.get('pushed_at'), now),
        'owner_type': (repo.get('owner') or {}).get('type', ''),
    })
    m['heat'] = round(_heat(m))
    m['giant'] = m['stars'] >= cfg.get('giant_stars', 20000) and m['age_days'] >= cfg.get('giant_age_days', 180)
    flags = _flags(repo, owner, license_id, now)
    if flags:
        m['flags'] = flags

    item.title = clip(f'{full}：{desc}' if desc else full, 200)
    facts = [f"⭐ {m['stars']:,}", f"今日 +{m['stars_today']:,}" if m.get('stars_today') else '',
             f"本周 +{m['stars_week']:,}" if m.get('stars_week') else '',
             f"Show HN {m['points']} 分" if m.get('points') and 'news.ycombinator.com' in m.get('discussion_url', '') else '',
             f"创建于 {m['created_at']}" if m['created_at'] else '', m['language'], license_id or '许可证不明确']
    item.summary = ' · '.join(f for f in facts if f)
    item.published_at = iso(created)
    item.tracks = ctx.matcher.match(f"{full} {desc} {' '.join(m['topics'])} {m.get('post_title', '')}")
    m['primary_track'] = item.tracks[0] if item.tracks else ''
    item.score = _score(m)


# ---------------------------------------------------------------- 主流程

def prepare_repos(items: List[NewsItem], ctx: CollectContext, seen: set, cfg: dict, limit: int,
                  reserve: Dict[str, int], log: Log) -> List[NewsItem]:
    """
    补全、过滤、打分，返回 limit 个候选（按分数从高到低）；reserve 是各信源保底的名额（信源 id → 个数）。
    接口全部失败时抛 RuntimeError
    """
    now = utcnow()
    items = sorted(items, key=lambda x: x.score, reverse=True)[:cfg.get('enrich_limit', 80)]
    details, errors = _fetch_all(ctx, [i.meta['repo'] for i in items], _get_repo)
    if items and errors and not any(details.values()):
        raise RuntimeError(f'仓库详情全部获取失败：{errors[0]}')
    logins = sorted({d['owner']['login'] for d in details.values() if d})
    owners, owner_errors = _fetch_all(ctx, logins, _get_user)
    log(f'{len(items)} 个仓库补全详情：成功 {sum(1 for d in details.values() if d)} 个'
        + (f'，出错 {len(errors)} 个（如 {errors[0]}）' if errors else '')
        + (f'；作者信息出错 {len(owner_errors)} 个' if owner_errors else ''))

    shady = ctx.keywords(cfg.get('exclude'))
    dropped: Dict[str, int] = {}
    # 改名、大小写不同的链接补全后才认得出是同一个仓库，先合并再打分
    merged: Dict[str, Tuple[NewsItem, dict]] = {}
    for item in items:
        repo = details.get(item.meta['repo'])
        reason = '不存在或已下架' if not repo else ''
        if repo:
            item.url = repo['html_url']
            if item_key(item.url) in seen:
                reason = '近期推过或刚进过候选'
            else:
                reason = _reject_reason(repo, owners.get(repo['owner']['login']), shady, cfg, now)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        key = repo['full_name'].lower()
        if key in merged:
            _merge(merged[key][0], item)
        else:
            merged[key] = (item, repo)
    for item, repo in merged.values():
        _fill(item, repo, owners.get(repo['owner']['login']), ctx, cfg, now)
    if dropped:
        log('剔除：' + '，'.join(f'{reason} {n} 个' for reason, n in dropped.items()))

    kept = _pool([i for i, _ in merged.values()], reserve, limit)
    readmes, readme_errors = _fetch_all(ctx, [i.meta['repo'] for i in kept], _get_readme)
    failed = {e.split('：', 1)[0] for e in readme_errors}
    out, no_readme = [], 0
    for item in kept:
        text = readmes.get(item.meta['repo'])
        if text is None:
            # 接口出错不算仓库的问题，照样保留
            if item.meta['repo'] in failed:
                out.append(item)
            else:
                no_readme += 1
            continue
        excerpt = readme_excerpt(text, cfg.get('readme_chars', 1500))
        item.meta['readme'] = excerpt
        if len(excerpt) < 300:
            item.meta.setdefault('flags', []).append('README 很简略')
        out.append(item)
    by_source: Dict[str, int] = {}
    for item in out:
        by_source[item.source] = by_source.get(item.source, 0) + 1
    log(f'候选池 {len(kept)} 个补 README'
        + (f'，没有 README 剔除 {no_readme} 个' if no_readme else '')
        + (f'，README 获取出错 {len(readme_errors)} 个（如 {readme_errors[0]}）' if readme_errors else '')
        + '；来自 ' + '、'.join(f'{s} {n} 个' for s, n in by_source.items()))
    return out
