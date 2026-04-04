import asyncio
import re
import time
from dataclasses import dataclass, field

import feedparser
import httpx
from dateutil import parser as dateparser

from src.sources import RSS_FEEDS

CACHE_TTL = 15 * 60  # 15 minutes


@dataclass
class NewsItem:
    title: str
    url: str
    date: str
    snippet: str
    source: str


@dataclass
class _CacheEntry:
    items: list[NewsItem] = field(default_factory=list)
    ts: float = 0.0


_cache = _CacheEntry()


async def _fetch_one(client: httpx.AsyncClient, url: str) -> list[NewsItem]:
    try:
        resp = await client.get(url, timeout=10)
        feed = feedparser.parse(resp.text)
    except Exception:
        return []

    items: list[NewsItem] = []
    for entry in feed.entries[:30]:
        pub = ""
        if hasattr(entry, "published"):
            try:
                pub = dateparser.parse(entry.published).isoformat()
            except Exception:
                pub = entry.published

        snippet = ""
        if hasattr(entry, "summary"):
            raw = re.sub(r"<[^>]+>", "", entry.summary)
            snippet = re.sub(r"\s+", " ", raw).strip()[:300]

        source_domain = url.split("/")[2].removeprefix("www.").removeprefix("rss.")
        items.append(
            NewsItem(
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                date=pub,
                snippet=snippet,
                source=source_domain,
            )
        )
    return items


async def fetch_all() -> list[NewsItem]:
    now = time.time()
    if _cache.items and (now - _cache.ts) < CACHE_TTL:
        return _cache.items

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[_fetch_one(client, url) for url in RSS_FEEDS],
            return_exceptions=True,
        )

    all_items: list[NewsItem] = []
    for r in results:
        if isinstance(r, list):
            all_items.extend(r)

    _cache.items = all_items
    _cache.ts = now
    return all_items
