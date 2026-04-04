import json
import asyncio
from openai import AsyncOpenAI, RateLimitError, APIStatusError
from models.schemas import ParsedQuery
import os

PARSE_SYSTEM_PROMPT = """Ты — NLP-парсер запросов для агента поиска товаров.
Твоя задача: извлечь из запроса пользователя структурированные данные.

Верни ТОЛЬКО валидный JSON без markdown-обёртки, без пояснений.

Формат ответа:
{
  "product_query": "строка для поиска товара или null если товар не нужен",
  "product_filters": {
    "storage": "объём памяти если указан, иначе null",
    "condition": "новый/б.у. если указан, иначе null",
    "max_price": "максимальная цена если указана, иначе null",
    "brand": "бренд если указан, иначе null"
  },
  "news_topic": null,
  "news_days": 0
}
"""

# Те же слоты что и в orchestrator — все ключи и модели
LLM_SLOTS = [
    (os.getenv("GROQ_API_KEY_1"), "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_1"), "llama-3.1-8b-instant",    "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_2"), "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_2"), "llama-3.1-8b-instant",    "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_2"), "gemma2-9b-it",            "https://api.groq.com/openai/v1"),
    (os.getenv("QWEN_API_KEY"),   "qwen-plus",  "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    (os.getenv("QWEN_API_KEY"),   "qwen-turbo", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
]


async def parse_query(user_query: str) -> ParsedQuery:
    last_exc = None

    for api_key, model, base_url in LLM_SLOTS:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_query},
                ],
                temperature=0.0,
                max_tokens=500,
            )
            raw = response.choices[0].message.content.strip()
            # Убираем markdown-обёртку если модель всё же добавила
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()

            data = json.loads(raw)
            return ParsedQuery(
                product_query=data.get("product_query"),
                product_filters=data.get("product_filters", {}),
                news_topic=data.get("news_topic"),
                news_days=data.get("news_days", 7),
                raw_query=user_query,
            )

        except RateLimitError as e:
            print(f"[NLP] RateLimit ({model}): переключаемся...")
            last_exc = e
            await asyncio.sleep(1)
        except APIStatusError as e:
            print(f"[NLP] APIError {e.status_code} ({model}): переключаемся...")
            last_exc = e
            await asyncio.sleep(1)
        except json.JSONDecodeError as e:
            print(f"[NLP] JSONDecodeError ({model}): {e}, переключаемся...")
            last_exc = e
            await asyncio.sleep(0.5)

    # Все слоты исчерпаны — делаем простой fallback-парсинг без LLM
    print("[NLP] Все LLM-слоты исчерпаны, используем fallback-парсинг")
    return _fallback_parse(user_query)


def _fallback_parse(query: str) -> ParsedQuery:
    """Простой парсинг по ключевым словам если все LLM недоступны."""
    import re
    q = query.lower()

    # Бренд
    brand = None
    for b in ("apple", "samsung", "xiaomi", "huawei", "sony", "lg", "nokia"):
        if b in q:
            brand = b
            break

    # Память
    storage = None
    m = re.search(r'(\d+)\s*(?:gb|гб)', q)
    if m:
        storage = f"{m.group(1)}GB"

    # Макс цена
    max_price = None
    m = re.search(r'до\s*([\d\s]+)\s*(?:руб|₽|р\.)', q)
    if m:
        max_price = m.group(1).replace(" ", "")

    # Состояние
    condition = None
    if any(w in q for w in ["новый", "new", "новая"]):
        condition = "новый"
    elif any(w in q for w in ["б/у", "бу", "used", "бывший"]):
        condition = "б.у."

    # Новости
    news_topic = None
    if any(w in q for w in ["новост", "news", "релиз", "анонс", "выход"]):
        news_topic = query

    return ParsedQuery(
        product_query=query,
        product_filters={
            "brand": brand,
            "storage": storage,
            "max_price": max_price,
            "condition": condition,
        },
        news_topic=news_topic,
        news_days=7,
        raw_query=query,
    )