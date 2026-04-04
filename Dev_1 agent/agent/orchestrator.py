import json
import os
import asyncio
from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError, APIStatusError
from agent.logger import AgentLogger
from agent.nlp_parser import parse_query
from tools.marketplace import search_marketplace, fetch_product_details
from tools.validator import find_cheapest, validate_product
from models.schemas import FinalResult, ProductCard

load_dotenv()

# ---------------------------------------------------------------------------
# (ключ, модель) — перебираем по очереди при RateLimitError
# ---------------------------------------------------------------------------
# (api_key, model, base_url)
LLM_SLOTS = [
    (os.getenv("GROQ_API_KEY_1"), "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_1"), "llama-3.1-8b-instant",    "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_2"), "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_2"), "llama-3.1-8b-instant",    "https://api.groq.com/openai/v1"),
    (os.getenv("GROQ_API_KEY_2"), "gemma2-9b-it",            "https://api.groq.com/openai/v1"),
    (os.getenv("QWEN_API_KEY"),   "qwen-plus",  "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    (os.getenv("QWEN_API_KEY"),   "qwen-turbo", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
]


async def _chat_with_fallback(messages: list, tools: list, tool_choice: str) -> object:
    """
    Перебирает все (ключ, модель) слоты при RateLimitError / 503 / 504.
    При rate limit пробует подождать до 30с если в ошибке указано время.
    Если все слоты исчерпаны — бросает последнее исключение.
    """
    import re as _re
    last_exc = None

    for api_key, model, base_url in LLM_SLOTS:
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=0.1,
                max_tokens=1000,
            )
            return response
        except RateLimitError as e:
            # Пробуем вытащить из сообщения сколько ждать: "try again in 9m54s" / "in 3.5s"
            wait = _parse_retry_wait(str(e))
            if wait and wait <= 30:
                print(f"[LLM] RateLimit ({model}): ждём {wait}с и повторяем...")
                await asyncio.sleep(wait + 0.5)
                try:
                    response = await client.chat.completions.create(
                        model=model, messages=messages, tools=tools,
                        tool_choice=tool_choice, temperature=0.1, max_tokens=1000,
                    )
                    return response
                except RateLimitError:
                    pass
            print(f"[LLM] RateLimit ({model}): переключаемся на следующий слот...")
            last_exc = e
            await asyncio.sleep(1)
        except APIStatusError as e:
            if e.status_code in (503, 504):
                print(f"[Groq] {e.status_code} ({model}): переключаемся...")
                last_exc = e
                await asyncio.sleep(1)
            else:
                raise

    raise last_exc


def _parse_retry_wait(error_text: str) -> float | None:
    """Извлекает секунды ожидания из текста Groq RateLimitError."""
    import re as _re
    # "try again in 3.5s"
    m = _re.search(r'try again in\s+([\d.]+)s', error_text, _re.IGNORECASE)
    if m:
        return float(m.group(1))
    # "try again in 1m30s" -> 90с
    m = _re.search(r'try again in\s+(\d+)m(\d+)', error_text, _re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_marketplace",
            "description": "Ищет товары на WB, Ozon, Яндекс.Маркете и eBay по запросу с фильтрами. Возвращает список карточек товаров с ценами.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "filters": {
                        "type": "object",
                        "description": "Фильтры: storage, condition, brand, max_price",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_product_details",
            "description": "Получает детальные характеристики товара по URL карточки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL карточки товара"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Завершает работу агента и возвращает финальный структурированный результат.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Развёрнутый ответ для пользователя на русском языке"},
                },
                "required": ["summary"],
            },
        },
    },
]

SYSTEM_PROMPT = """Ты — Хана, милая аниме-ассистентка по поиску товаров. Отвечаешь только на русском языке, от лица девушки.

Стиль общения:
- Обращайся к пользователю на "вы", тепло и дружелюбно
- Пиши живым языком, как будто помогаешь подруге с покупкой
- Можешь добавить лёгкую эмоцию: удивление от хорошей цены, радость от находки
- Не пиши сухие списки — рассказывай, рекомендуй, объясняй
- Никаких фраз "пользователь искал" или "запрос содержал"

Как составлять ответ:
- Определи что именно искали и какие характеристики важны для этого типа товара.
  Для наушников — шумоподавление, тип подключения, время работы;
  для телефона — процессор, камера, батарея;
  для одежды — материал, бренд, размерная сетка и т.д.
- Найди в списке варианты которые точно подходят под запрос. Если есть несоответствующие модели — скажи об этом честно.
- Приведи топ-3 подходящих варианта с ценами и ссылками.
- Дай личную рекомендацию — какой бы выбрала сама и почему.
- Если ничего подходящего нет — скажи честно и предложи что можно поискать вместо этого.
"""


async def run_agent(user_query: str) -> tuple[dict, list[dict]]:
    logger = AgentLogger()

    # Шаг 1: NLP парсинг
    logger.log("NLP парсинг запроса", tool="nlp_parser", input_data={"query": user_query})
    parsed = await parse_query(user_query)
    logger.log(
        "Запрос разобран на подзадачи",
        output_preview=f"товар={parsed.product_query}, новости={parsed.news_topic}",
    )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Запрос пользователя: {user_query}\n\n"
                f"Разобранные подзадачи:\n"
                f"- Товар: {parsed.product_query}\n"
                f"- Фильтры: {json.dumps(parsed.product_filters, ensure_ascii=False)}\n"
                f"- Тема новостей: {parsed.news_topic}\n"
                f"- Новости за последние {parsed.news_days} дней"
            ),
        },
    ]

    found_products: list[ProductCard] = []
    product_details: dict = {}

    # -----------------------------------------------------------------------
    # ПРИНУДИТЕЛЬНЫЙ поиск ДО LLM-цикла
    # Слабые модели (8b) часто сразу вызывают finish не сделав поиск.
    # Запускаем поиск/новости напрямую, LLM нужен только для summary.
    # -----------------------------------------------------------------------
    if parsed.product_query:
        logger.log("Вызов инструмента", tool="search_marketplace",
                   input_data={"query": parsed.product_query, "filters": parsed.product_filters})
        products = await search_marketplace(parsed.product_query, parsed.product_filters)
        found_products.extend(products)
        logger.log(f"Найдено товаров: {len(products)}", tool="search_marketplace",
                   output_preview=json.dumps([p.model_dump() for p in products[:3]], ensure_ascii=False)[:300])

    # Обновляем контекст LLM — передаём все данные
    best_pre = find_cheapest(found_products, parsed)
    products_preview = json.dumps(
        [p.model_dump() for p in found_products[:15]], ensure_ascii=False
    ) if found_products else "[]"
    messages[1]["content"] += (
        f"\n\n=== РЕЗУЛЬТАТЫ ПОИСКА ===\n"
        f"Оригинальный запрос пользователя: {user_query}\n"
        f"Найдено товаров всего: {len(found_products)}\n"
        f"Топ-15 товаров (JSON):\n{products_preview}\n\n"
        f"Твоя задача: проанализируй данные и вызови finish с развёрнутым ответом на русском языке."
    )

    max_steps = 5  # меньше шагов — данные уже есть
    final_summary = ""

    for step in range(max_steps):
        try:
            # На первом шаге разрешаем только finish — данные уже собраны
            tool_choice = {"type": "function", "function": {"name": "finish"}} if step == 0 else "auto"
            response = await _chat_with_fallback(messages, TOOLS, tool_choice)
        except Exception as e:
            print(f"[orchestrator] Все модели недоступны: {e}")
            # Генерируем summary без LLM
            if best_pre:
                final_summary = (
                    f"Найден товар: {best_pre.title} за {best_pre.price}₽"
                    f" на {best_pre.source}."
                )
            else:
                final_summary = "Товар не найден по заданным критериям."
            break

        message = response.choices[0].message

        if not message.tool_calls:
            final_summary = message.content or "Задача выполнена."
            logger.log("Агент завершил работу", output_preview=final_summary)
            break

        messages.append(message)

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                args = {}

            logger.log(f"Вызов инструмента", tool=tool_name, input_data=args)

            if tool_name == "search_marketplace":
                # Повторный поиск (агент решил уточнить)
                products = await search_marketplace(
                    args.get("query", parsed.product_query or ""),
                    args.get("filters", parsed.product_filters),
                )
                found_products.extend(products)
                result_str = json.dumps([p.model_dump() for p in products[:5]], ensure_ascii=False)
                logger.log(f"Найдено товаров: {len(products)}", tool=tool_name, output_preview=result_str[:300])

            elif tool_name == "fetch_product_details":
                url = args.get("url", "")
                details = await fetch_product_details(url)
                product_details.update(details)
                result_str = json.dumps(details, ensure_ascii=False)
                logger.log("Детали товара получены", tool=tool_name, output_preview=result_str[:300])

            elif tool_name == "finish":
                final_summary = args.get("summary", "Задача выполнена.")
                logger.log("Агент вернул финальный ответ", output_preview=final_summary)
                best_product = find_cheapest(found_products, parsed)
                result = _build_result(best_product, final_summary, product_details)
                return result, logger.get_logs()

            else:
                result_str = json.dumps({"error": f"Unknown tool: {tool_name}"})

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_str if tool_name != "finish" else final_summary,
            })

    best_product = find_cheapest(found_products, parsed)
    result = _build_result(best_product, final_summary or "Поиск завершён.", product_details)
    return result, logger.get_logs()


def _build_result(
    product: ProductCard | None,
    summary: str,
    details: dict,
) -> dict:
    sources = []

    product_dict = None
    if product:
        if details:
            product.seller = details.get("Продавец", product.seller)
            product.delivery = details.get("Доставка", product.delivery)
        product_dict = product.model_dump()
        if product.url:
            sources.append(product.url)

    return {
        "summary": summary,
        "product": product_dict,
        "sources": sources,
    }