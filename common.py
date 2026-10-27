#!/usr/bin/env python3
"""
公共工具：资讯数据结构、HTTP 与 feed 抓取、关键词与方向匹配、URL 归一化、
日期、环境变量，以及产物和状态文件的读写。
"""

from __future__ import annotations

import calendar
import html
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 部分站点（Reddit RSS、nitter 等）会拒绝非浏览器 UA
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
}

RELIABILITY_RANK = {'official': 3, 'high': 2, 'medium': 1}


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    source_id: str
    kind: str           # official / paper / model / repo / release / media / kol / community
    reliability: str    # official / high / medium
    summary: str = ''
    published_at: str = ''
    score: int = 0
    tracks: List[str] = field(default_factory=list)
    section: str = ''
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 配置与环境

def load_config(path: Optional[str] = None) -> dict:
    with open(path or os.path.join(BASE_DIR, 'config.json'), 'r', encoding='utf-8') as f:
        return json.load(f)


def load_env_var(name: str) -> str:
    """读取环境变量，取不到时尝试项目目录下的 .env 文件（KEY=VALUE 格式）"""
    value = os.environ.get(name, '')
    if value:
        return value
    env_file = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_file):
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or '=' not in line:
                    continue
                k, _, v = line.partition('=')
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return ''


def local_now(config: dict) -> datetime:
    return datetime.now(ZoneInfo(config.get('timezone', 'Asia/Shanghai')))


def today_str(config: dict) -> str:
    """日报日期（按配置时区），所有脚本用它拼 raw_news_/news_ 文件名，保证跨时区一致"""
    return local_now(config).strftime('%Y%m%d')


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- HTTP 与 feed

def http_get(url: str, params: Optional[dict] = None, headers: Optional[dict] = None,
             timeout: int = 20) -> requests.Response:
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)
    resp = requests.get(url, params=params, headers=merged, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_feed(url: str, timeout: int = 20) -> feedparser.FeedParserDict:
    """带浏览器 UA 抓取并解析 RSS/Atom；XML 不规范但能解析出条目时照常使用"""
    feed = feedparser.parse(http_get(url, timeout=timeout).content)
    if not feed.entries and feed.bozo:
        raise ValueError(f'feed 解析失败: {feed.get("bozo_exception")}')
    return feed


def call_with_retry(func: Callable, *args, retries: int = 2, delay: int = 3):
    for attempt in range(retries + 1):
        try:
            return func(*args)
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(delay * (attempt + 1))


# ---------------------------------------------------------------- 文本与时间

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def clean_text(text: str) -> str:
    """去掉 HTML 标签、反转义实体、压缩空白"""
    text = _TAG_RE.sub(' ', text or '')
    return _WS_RE.sub(' ', html.unescape(text)).strip()


def clip(text: str, limit: int) -> str:
    text = (text or '').strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def entry_datetime(entry) -> Optional[datetime]:
    t = entry.get('published_parsed') or entry.get('updated_parsed')
    if not t:
        return None
    return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: Optional[datetime]) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') if dt else ''


def within_hours(dt: Optional[datetime], hours: float) -> bool:
    return dt is not None and utcnow() - dt <= timedelta(hours=hours)


# ---------------------------------------------------------------- URL 归一化（去重用）

_TRACKING_PARAM_RE = re.compile(r'^(utm_.*|ref|ref_src|source|via|fbclid|gclid|mc_.*|s|si)$')
ARXIV_ID_RE = re.compile(r'(?:arxiv\.org/(?:abs|pdf|html)/|huggingface\.co/papers/)(\d{4}\.\d{4,5})', re.I)


def item_key(url: str) -> str:
    """同一篇论文的 arXiv / HF Papers 链接归为同一个键，其余链接去掉跟踪参数后规范化"""
    m = ARXIV_ID_RE.search(url or '')
    if m:
        return f'arxiv:{m.group(1)}'
    parts = urlsplit((url or '').strip())
    host = parts.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    if host in ('twitter.com', 'mobile.twitter.com'):
        host = 'x.com'
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAM_RE.match(k)])
    return urlunsplit(('https', host, parts.path.rstrip('/') or '/', query, ''))


def title_key(title: str) -> str:
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', (title or '').lower())[:40]


# ---------------------------------------------------------------- 关键词与方向匹配

_ASCII_ALNUM = re.compile(r'[A-Za-z0-9]')


def keyword_regex(keyword: str) -> str:
    """
    关键词转正则（统一忽略大小写）：
    - 空格和连字符可互换且可省略："image-to-3d" 能匹配 "Image to 3D"
    - 英文关键词按整词匹配，"agent" 不会命中 "reagent"；末尾加 * 表示允许后缀，"agent*" 能命中 "agents"、"agentic"
    - 中文关键词按子串匹配
    """
    kw = keyword.strip()
    wildcard = kw.endswith('*')
    if wildcard:
        kw = kw[:-1]
    tokens = [t for t in re.split(r'[\s\-_]+', kw) if t]
    if not tokens:
        return ''
    body = r'[\s\-_]*'.join(re.escape(t) for t in tokens)
    if wildcard:
        body += r'[A-Za-z0-9]*'
    prefix = r'(?<![A-Za-z0-9])' if _ASCII_ALNUM.match(tokens[0][0]) else ''
    suffix = r'(?![A-Za-z0-9])' if not wildcard and _ASCII_ALNUM.match(tokens[-1][-1]) else ''
    return prefix + body + suffix


def compile_keywords(keywords: Optional[Iterable[str]]) -> Optional[re.Pattern]:
    parts = [p for p in (keyword_regex(k) for k in keywords or []) if p]
    if not parts:
        return None
    return re.compile('|'.join(f'(?:{p})' for p in parts), re.IGNORECASE)


def resolve_keywords(value, keyword_sets: Dict[str, List[str]]) -> List[str]:
    """支持直接写关键词列表，或用 "@集合名" 引用 config.keyword_sets 里的公共列表"""
    if not value:
        return []
    out: List[str] = []
    for v in [value] if isinstance(value, str) else value:
        if v.startswith('@'):
            out.extend(keyword_sets.get(v[1:], []))
        else:
            out.append(v)
    return out


class TrackMatcher:
    """把一段文本映射到命中的方向（按 config.tracks 的顺序返回）"""

    def __init__(self, tracks: Dict[str, dict]):
        self.order = list(tracks)
        self.include = {tid: compile_keywords(cfg.get('keywords')) for tid, cfg in tracks.items()}
        self.exclude = {tid: compile_keywords(cfg.get('exclude')) for tid, cfg in tracks.items()}
        self.combine = {tid: cfg.get('combine', []) for tid, cfg in tracks.items()}

    def match(self, text: str) -> List[str]:
        hits = set()
        for tid in self.order:
            pattern = self.include[tid]
            if pattern is None:
                continue
            excluded = self.exclude[tid]
            if pattern.search(excluded.sub(' ', text) if excluded else text):
                hits.add(tid)
        for tid, groups in self.combine.items():
            if tid not in hits and any(all(g in hits for g in group) for group in groups):
                hits.add(tid)
        return [tid for tid in self.order if tid in hits]


# ---------------------------------------------------------------- 产物与状态文件

# output/ 放每天的原始数据和日报（不入库，Actions 归档为 artifact）；
# state/ 放跨天的去重记忆（Actions 推送成功后提交回仓库）
OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
STATE_DIR = os.path.join(BASE_DIR, 'state')
SEEN_FILE = os.path.join(STATE_DIR, 'seen.json')
TOPICS_FILE = os.path.join(STATE_DIR, 'recent_topics.json')


def raw_path(date: str) -> str:
    return os.path.join(OUTPUT_DIR, f'raw_news_{date}.json')


def report_path(date: str) -> str:
    return os.path.join(OUTPUT_DIR, f'news_{date}.md')


def read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f'⚠️ 读取 {os.path.relpath(path, BASE_DIR)} 失败（{e}），按空处理')
        return default


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_seen_keys() -> set:
    """已进入过候选的条目键（item_key），由 mark_seen.py 在推送成功后维护"""
    return set(read_json(SEEN_FILE, {}).get('items', {}))
