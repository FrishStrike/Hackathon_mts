import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.fetcher import fetch_all
from src.dedup import deduplicate
from src.ranker import rank

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-news-aggregator"}


# ---------- POST /news ----------

class NewsRequest(BaseModel):
    topic: str
    limit: int = 10


class NewsItemOut(BaseModel):
    title: str
    url: str
    date: str
    snippet: str
    source: str
    relevance: float = 0.0


class NewsResponse(BaseModel):
    news: list[NewsItemOut]


@app.post("/news", response_model=NewsResponse)
async def get_news(req: NewsRequest):
    raw = await fetch_all()
    unique = deduplicate(raw)
    ranked = rank(unique, req.topic, limit=req.limit)

    news_out = []
    for item, score in ranked:
        news_out.append(
            NewsItemOut(
                title=item.title,
                url=item.url.split("?")[0],
                date=item.date,
                snippet=item.snippet,
                source=item.source,
                relevance=score,
            )
        )
    return NewsResponse(news=news_out)


# ---------- POST /aggregate ----------

class ProductItem(BaseModel):
    title: str
    price: float
    currency: str = "RUB"
    url: str
    image_url: str | None = None
    specs: dict = {}
    reasoning: str | None = None


class AggregateRequest(BaseModel):
    topic: str
    limit: int = 10
    item: ProductItem | None = None


class AggregateNewsOut(BaseModel):
    title: str
    url: str
    date: str | None
    snippet: str
    source: str
    relevance: float


class AggregateResponse(BaseModel):
    query: str
    item: ProductItem | None
    news: list[AggregateNewsOut]
    sources: list[str]
    meta: dict


@app.post("/aggregate", response_model=AggregateResponse)
async def aggregate(req: AggregateRequest):
    start = time.time()

    raw = await fetch_all()
    deduped = deduplicate(raw)
    ranked = rank(deduped, req.topic, limit=req.limit)

    news = [
        AggregateNewsOut(
            title=it.title,
            url=it.url.split("?")[0],
            date=it.date,
            snippet=it.snippet,
            source=it.source,
            relevance=round(score, 3),
        )
        for it, score in ranked
    ]

    sources: list[str] = []
    if req.item:
        sources.append(req.item.url.split("?")[0])
    sources.extend([n.url for n in news])
    sources = list(dict.fromkeys(sources))  # dedup с сохранением порядка

    cached = raw and (time.time() - start) < 0.5

    return AggregateResponse(
        query=req.topic,
        item=req.item,
        news=news,
        sources=sources,
        meta={
            "news_count": len(news),
            "fetch_time_ms": int((time.time() - start) * 1000),
            "cached": cached,
        },
    )


# ---------- POST /process ----------
# Единая точка входа для Go-воркера
# Контракт: models.MLRequest -> models.ResultPayload

class ProcessRequest(BaseModel):
    query_id: str = ""
    text: str = ""


@app.post("/process")
async def process(req: ProcessRequest):
    try:
        raw = await fetch_all()
        deduped = deduplicate(raw)
        ranked = rank(deduped, req.text, limit=10)

        news = [
            {
                "title": it.title,
                "url": it.url.split("?")[0],
                "date": it.date,
                "summary": it.snippet,
            }
            for it, score in ranked
        ]

        sources: list[str] = [n["url"] for n in news]
        sources = list(dict.fromkeys(sources))

        return {
            "status": "completed",
            "item": None,
            "news": news,
            "sources": sources,
            "trace": [],
            "error": "",
        }
    except Exception as e:
        return {
            "status": "failed",
            "item": None,
            "news": [],
            "sources": [],
            "trace": [],
            "error": str(e),
        }
