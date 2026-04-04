from src.fetcher import NewsItem


def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """Remove duplicates by URL and by near-identical titles."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    result: list[NewsItem] = []

    for item in items:
        clean_url = item.url.split("?")[0].rstrip("/")
        norm_title = item.title.lower().strip()

        if clean_url in seen_urls:
            continue
        if norm_title in seen_titles:
            continue

        seen_urls.add(clean_url)
        seen_titles.add(norm_title)
        result.append(item)

    return result
