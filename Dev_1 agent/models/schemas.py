from pydantic import BaseModel
from typing import Optional


class ParsedQuery(BaseModel):
    product_query: Optional[str] = None       # "iPhone 256GB новый"
    product_filters: dict = {}                 # {"storage": "256GB", "condition": "новый"}
    news_topic: Optional[str] = None           # "Apple"
    news_days: int = 7                         # за последние N дней
    raw_query: str = ""


class ProductCard(BaseModel):
    title: str
    price: Optional[float] = None
    currency: str = "₽"
    condition: Optional[str] = None
    storage: Optional[str] = None
    color: Optional[str] = None
    seller: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    delivery: Optional[str] = None
    url: str
    source: str = "Яндекс.Маркет"


class NewsItem(BaseModel):
    title: str
    summary: Optional[str] = None
    published_at: Optional[str] = None
    url: str
    source: str


class AgentLog(BaseModel):
    step: int
    action: str
    tool: Optional[str] = None
    input: Optional[dict] = None
    output_preview: Optional[str] = None
    status: str = "ok"  # ok | error


class FinalResult(BaseModel):
    product: Optional[ProductCard] = None
    news: list[NewsItem] = []
    summary: str = ""
    sources: list[str] = []
