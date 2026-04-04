import httpx
from bs4 import BeautifulSoup
from models.schemas import NewsItem
from datetime import datetime, timedelta
import re

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def search_news(topic: str, days: int = 7) -> list[NewsItem]:
    """Ищем новости через Google News RSS (бесплатно, без API)."""
    news = await _google_news_rss(topic, days)
    if not news:
        news = await _rbc_news(topic)
    return news


async def _google_news_rss(topic: str, days: int) -> list[NewsItem]:
    """Google News RSS — самый простой способ получить новости."""
    url = f"https://news.google.com/rss/search?q={topic}&hl=ru&gl=RU&ceid=RU:ru"

    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception as e:
            print(f"[news] Google RSS error: {e}")
            return []

    soup = BeautifulSoup(response.text, "xml")
    items = []
    cutoff = datetime.now() - timedelta(days=days)

    for item in soup.find_all("item", limit=10):
        title = item.find("title")
        link = item.find("link")
        pub_date = item.find("pubDate")
        source = item.find("source")

        if not title or not link:
            continue

        # Парсим дату
        published_at = None
        if pub_date:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date.get_text())
                if dt.replace(tzinfo=None) < cutoff:
                    continue
                published_at = dt.strftime("%d.%m.%Y")
            except Exception:
                published_at = pub_date.get_text(strip=True)

        items.append(
            NewsItem(
                title=title.get_text(strip=True),
                url=link.get_text(strip=True) if link.string else link.next_sibling or "",
                published_at=published_at,
                source=source.get_text(strip=True) if source else "Google News",
            )
        )

    return items


async def _rbc_news(topic: str) -> list[NewsItem]:
    """Фолбэк: поиск на RBC."""
    url = f"https://www.rbc.ru/search/?query={topic}&project=rbcnews"

    async with httpx.AsyncClient(headers=HEADERS, timeout=15) as client:
        try:
            response = await client.get(url)
        except Exception:
            return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = []

    for article in soup.find_all("div", class_=re.compile(r"search-item"), limit=8):
        title_el = article.find("span", class_=re.compile(r"title"))
        link_el = article.find("a", href=True)
        date_el = article.find("span", class_=re.compile(r"date"))

        if not title_el:
            continue

        url = link_el["href"] if link_el else ""
        if url and not url.startswith("http"):
            url = f"https://www.rbc.ru{url}"

        items.append(
            NewsItem(
                title=title_el.get_text(strip=True),
                url=url,
                published_at=date_el.get_text(strip=True) if date_el else None,
                source="RBC",
            )
        )

    return items
