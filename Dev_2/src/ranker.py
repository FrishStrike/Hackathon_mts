import math
from datetime import datetime, timezone

from dateutil import parser as dateparser
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from src.fetcher import NewsItem

_vectorizer = TfidfVectorizer(max_features=5000)


def rank(items: list[NewsItem], query: str, limit: int = 10) -> list[tuple[NewsItem, float]]:
    """Rank items by TF-IDF cosine similarity to query + freshness boost.
    Returns list of (item, score) tuples."""
    if not items:
        return []

    corpus = [f"{it.title} {it.snippet}" for it in items]
    corpus.append(query)

    tfidf = _vectorizer.fit_transform(corpus)
    query_vec = tfidf[-1]
    doc_vecs = tfidf[:-1]

    sims = cosine_similarity(query_vec, doc_vecs).flatten()

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, int]] = []
    for i, item in enumerate(items):
        relevance = float(sims[i])
        freshness = _freshness_boost(item.date, now)
        score = relevance * 0.7 + freshness * 0.3
        scored.append((score, i))

    scored.sort(key=lambda x: x[0], reverse=True)

    top = scored[:limit]
    if not top:
        return []
    max_score = top[0][0] or 1.0
    return [(items[idx], round(score / max_score, 2)) for score, idx in top]


def _freshness_boost(date_str: str, now: datetime) -> float:
    if not date_str:
        return 0.0
    try:
        dt = dateparser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours_ago = (now - dt).total_seconds() / 3600
        return math.exp(-hours_ago / 24)
    except Exception:
        return 0.0
