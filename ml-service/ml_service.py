import asyncio
import json
import os
import re
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from json_repair import repair_json
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
import uuid
from fastapi.responses import StreamingResponse

# Хранилище результатов, трейсов и SSE-очередей
results = {}
traces = {}
stream_queues: dict = {}  # query_id -> asyncio.Queue для SSE-стрима
load_dotenv()
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8080")
MAX_STEPS = 8               # было 15 — большинство задач решается за 3-5 шагов
MAX_TOOL_OUTPUT = 8_000     # символов — ~2K токенов, меньше → быстрее inference

SYSTEM_PROMPT = """You are Yumi, an anime girl mascot of our project. You are cheerful, friendly and a little playful.
You have access to browser tools and can control the browser like a human — navigate, click, type, scroll, fill forms, and interact with any website.
Always respond in the same language the user used.
Stay in character as Yumi at all times — use cute expressions, emojis, and a warm friendly tone.

RULES:
ЗАПРЕЩЕНО использовать browser_wait_for с текстом — он вешает агента на 30 секунд.
Используй browser_snapshot чтобы увидеть страницу, затем сразу действуй.
NEVER use Google, DuckDuckGo or Bing — they ALL block headless browsers with captcha.
INSTEAD go directly to reliable sources:
- Facts/encyclopedia → https://ru.wikipedia.org/wiki/TOPIC
- Currency rates → https://www.cbr.ru/currency_base/daily/
- Shopping → wildberries.ru, ozon.ru, avito.ru
- News → https://lenta.ru or https://ria.ru
- Weather → https://pogoda.mail.ru

1. NEVER use any search engine. Navigate DIRECTLY to the relevant website.
2. For Wikipedia: use https://ru.wikipedia.org/wiki/SEARCH_TERM (replace spaces with _)
3. After navigating, use browser_evaluate with `() => document.body.innerText` to get page text IMMEDIATELY — do NOT use browser_snapshot (it's slow and verbose).
4. Once you have the page text, extract the answer and call done RIGHT AWAY — no extra steps.
5. If a page returns 404 or empty, try a slightly different URL once, then answer from memory.
6. SPEED IS CRITICAL: answer in as few steps as possible. Max allowed steps: 8."""

class MLRequest(BaseModel):
    query_id: str
    text: str

# Единственный клиент на весь процесс — не создаём при каждом вызове
_llm_client = OpenAI(
    api_key=os.getenv("QWEN_API_KEY"),
    base_url="https://api.zveno.ai/v1",
)

async def create_completion(messages, tools):
    """Async-обёртка: выносим синхронный HTTP-вызов в thread pool,
    чтобы не блокировать asyncio event loop."""
    import asyncio, time
    def _call():
        for attempt in range(3):
            try:
                return _llm_client.chat.completions.create(
                    model="google/gemini-2.0-flash-001",
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=1024,  # было 4096 — для action-шагов хватит 1K
                )
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(10)   # было 30s — сократили
                else:
                    raise e
    return await asyncio.get_running_loop().run_in_executor(None, _call)

async def send_trace(query_id: str, step: str, status: str, detail: str = ""):
    if query_id not in traces:
        traces[query_id] = []
    event = {"step": step, "status": status, "detail": detail}
    traces[query_id].append(event)
    q = stream_queues.get(query_id)
    if q is not None:
        await q.put(event)

async def send_screenshot(query_id: str, session, label: str = ""):
    """Снимает скриншот браузера и отправляет через SSE.
    НЕ хранит в traces — слишком большой размер (base64 изображение)."""
    try:
        result = await session.call_tool("browser_screenshot", {})
        for item in result.content:
            if hasattr(item, 'data') and hasattr(item, 'mimeType'):
                mime = item.mimeType or 'image/png'
                b64 = item.data  # уже в base64
                event = {
                    "step": "browser",
                    "status": "screenshot",
                    "detail": label,
                    "img": f"data:{mime};base64,{b64}",
                }
                # Только в SSE-очередь, не в traces (чтобы не хранить мегабайты)
                q = stream_queues.get(query_id)
                if q is not None:
                    await q.put(event)
                return
    except Exception as e:
        print(f"⚠️ Screenshot failed: {e}", flush=True)

def _wants_browser_action(task: str) -> bool:
    """Возвращает True если пользователь явно просит выполнить действие в браузере:
    зайти на сайт, нажать кнопку, заполнить форму, открыть страницу и т.д."""
    p = task.lower()
    # Прямые команды на действие в браузере
    action_triggers = [
        "зайди", "зайти", "открой", "открыть", "перейди", "перейти",
        "нажми", "нажать", "кликни", "кликнуть", "клик ",
        "введи", "ввести", "напиши в поле", "заполни", "заполнить",
        "скролл", "прокрути", "прокрутить", "листай",
        "найди на сайте", "найди на странице",
        "закрой вкладку", "открой вкладку", "новая вкладка",
        "go to", "open ", "navigate to", "click ", "type in",
        "browse", "visit ",
    ]
    # URL-паттерны — если пользователь дал конкретный URL
    has_url = "http://" in p or "https://" in p or "www." in p or ".ru" in p or ".com" in p or ".org" in p
    has_action_word = any(w in p for w in ["зайди", "зайти", "открой", "открыть", "перейди",
                                            "перейти", "go to", "open", "navigate", "visit"])
    if has_url and has_action_word:
        return True
    return any(trigger in p for trigger in action_triggers)


def _needs_browser(task: str) -> bool:
    """Браузер нужен для маркетплейсов ИЛИ прямых браузерных команд."""
    p = task.lower()
    marketplaces = ["wildberries", "wb", "вб", "вайлдберриз",
                    "ozon", "озон",
                    "avito", "авито"]
    return any(w in p for w in marketplaces)


async def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """Ищет в DuckDuckGo HTML, возвращает [{title, url, snippet}]."""
    from bs4 import BeautifulSoup
    import urllib.parse
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "ru-ru"},
                headers=headers,
            )
        soup = BeautifulSoup(r.text, "html.parser")
        results = []
        for div in soup.select(".result"):
            a = div.select_one("a.result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
            if not href.startswith("http") or "duckduckgo" in href:
                continue
            title = a.get_text(strip=True)
            snippet_el = div.select_one(".result__snippet")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break
        print(f"🔍 DDG нашёл: {[r['url'] for r in results]}", flush=True)
        return results
    except Exception as e:
        print(f"⚠️ DDG search failed: {e}", flush=True)
        return []


async def _fetch_page(url: str) -> str:
    """Читает страницу: сначала Jina Reader (обходит капчу), потом прямой httpx."""
    # Пробуем Jina Reader
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(
                f"https://r.jina.ai/{url}",
                headers={"Accept": "text/plain", "X-Return-Format": "text"},
            )
        if r.status_code == 200 and len(r.text) > 100:
            return r.text[:MAX_TOOL_OUTPUT]
    except Exception as e:
        print(f"⚠️ Jina failed for {url}: {e}", flush=True)

    # Fallback: прямой запрос
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:MAX_TOOL_OUTPUT]
    except Exception as e:
        print(f"⚠️ Direct fetch failed for {url}: {e}", flush=True)
        return ""


async def _fetch_context(task: str) -> tuple[list[dict], str]:
    """Ищет по всему интернету. Возвращает (search_results, aggregated_text)."""

    # Курс валют → ЦБ РФ напрямую
    p = task.lower()
    if any(w in p for w in ["курс", "доллар", "евро", "юань", "валют", "рубл"]):
        cbr_url = "https://www.cbr.ru/scripts/XML_daily.asp"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(cbr_url)
            return (
                [{"title": "ЦБ РФ — официальные курсы валют", "url": cbr_url, "snippet": "Официальные курсы валют Банка России"}],
                r.text[:MAX_TOOL_OUTPUT],
            )
        except Exception:
            pass

    # Общий поиск
    # Для запросов о ценах/товарах ищем больше результатов
    price_keywords = ["цена", "стоимость", "купить", "почём", "сколько стоит", "price", "cost"]
    is_price_query = any(w in p for w in price_keywords)
    max_results = 7 if is_price_query else 5
    search_results = await _ddg_search(task, max_results=max_results)
    if not search_results:
        return [], ""

    # Параллельно читаем первые страницы (больше для ценовых запросов)
    fetch_count = 4 if is_price_query else 3
    fetch_tasks = [_fetch_page(item["url"]) for item in search_results[:fetch_count]]
    pages = await asyncio.gather(*fetch_tasks, return_exceptions=True)

    combined = ""
    valid_results = []
    for item, text in zip(search_results[:fetch_count], pages):
        if isinstance(text, str) and text.strip():
            combined += f"\n\n--- Источник: {item['url']} ---\n{text[:2500]}"
            valid_results.append(item)
        else:
            # Даже если текст не получили — добавляем в results для фронта
            valid_results.append(item)

    # Добавляем остальные результаты только в search_results без текста
    for item in search_results[fetch_count:]:
        valid_results.append(item)

    return valid_results, combined[:MAX_TOOL_OUTPUT]


VISUAL_BROWSER_PROMPT = """Ты — Юми, весёлая аниме-ассистентка, управляющая настоящим браузером.
Ты ОБЯЗАНА использовать инструменты для выполнения действий. НИКОГДА не описывай что бы ты сделала — ДЕЛАЙ это.
Всегда отвечай на русском языке.
Оставайся в образе Юми — используй милые выражения, эмодзи и тёплый дружелюбный тон.

ИНСТРУМЕНТЫ:
- browser_navigate(url) — перейти по URL. ИСПОЛЬЗУЙ ЭТО ПЕРВЫМ для любого запроса "зайди на сайт".
- browser_evaluate(function) — выполнить JS. Используй `() => document.body.innerText` чтобы прочитать текст страницы.
- browser_click(selector) — нажать на элемент
- browser_type(selector, text) — ввести текст в поле
- browser_screenshot() — сделать скриншот

КРИТИЧЕСКИЕ ПРАВИЛА:
1. Когда пользователь говорит "зайди на X" / "открой X" — СРАЗУ вызывай browser_navigate.
2. НИКОГДА не отвечай просто текстом если пользователь попросил тебя ЧТО-ТО СДЕЛАТЬ. Сначала используй инструмент.
3. После навигации используй browser_evaluate чтобы прочитать страницу, затем опиши что видишь.
4. НИКОГДА не используй Google/Bing/DuckDuckGo — они блокируют headless-браузеры.
5. Отвечай чистым текстом (без tool_calls) ТОЛЬКО когда ты ЗАВЕРШИЛА задачу и хочешь сообщить результат.
6. Если страница не загрузилась — попробуй ещё раз с другим вариантом URL.
7. Если browser не найден или инструмент недоступен — НЕ СДАВАЙСЯ, попробуй другой подход."""


def _should_browse_visually(task: str) -> bool:
    """Возвращает True для запросов, где полезен визуальный браузинг
    (цены, электроника, текущие события) — но без конкретного маркетплейса."""
    p = task.lower()
    # Не дублируем маркетплейс-запросы (они идут через _needs_browser)
    if _needs_browser(task):
        return False
    triggers = [
        "цена", "стоимость", "почём", "сколько стоит",
        "iphone", "айфон", "samsung", "самсунг", "pixel", "xiaomi", "хуавей", "huawei",
        "смартфон", "ноутбук", "планшет", "наушники", "телефон", "телевизор",
        "вышел", "выпущен", "релиз", "анонс", "новинк",
        "купить", "заказать", "интернет-магазин",
    ]
    return any(w in p for w in triggers)


SITE_ALIASES = {
    "хабр": "https://habr.com",
    "habr": "https://habr.com",
    "гитхаб": "https://github.com",
    "github": "https://github.com",
    "ютуб": "https://youtube.com",
    "youtube": "https://youtube.com",
    "вк": "https://vk.com",
    "вконтакте": "https://vk.com",
    "телеграм": "https://web.telegram.org",
    "telegram": "https://web.telegram.org",
    "реддит": "https://reddit.com",
    "reddit": "https://reddit.com",
    "яндекс": "https://ya.ru",
    "yandex": "https://ya.ru",
    "кинопоиск": "https://kinopoisk.ru",
    "mail": "https://mail.ru",
    "мейл": "https://mail.ru",
    "лента": "https://lenta.ru",
    "пикабу": "https://pikabu.ru",
    "pikabu": "https://pikabu.ru",
    "stackoverflow": "https://stackoverflow.com",
    "стэковерфлоу": "https://stackoverflow.com",
}


def _build_visual_start_url(prompt: str) -> str:
    """Выбирает стартовую страницу для визуального агента."""
    import urllib.parse
    p = prompt.lower()

    # Если в промпте есть прямой URL — извлекаем его и используем
    url_match = re.search(r'(https?://[^\s,\'"]+)', prompt)
    if url_match:
        return url_match.group(1)

    # Домен без протокола: "зайди на habr.com" → https://habr.com
    domain_match = re.search(r'(?:на\s+|to\s+|visit\s+)((?:www\.)?[a-zA-Z0-9-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)', prompt, re.IGNORECASE)
    if domain_match:
        domain = domain_match.group(1)
        if not domain.startswith("http"):
            domain = "https://" + domain
        return domain

    # Популярные сайты по алиасу: "зайди на хабр" → habr.com
    # Извлекаем слово после "на/to/visit"
    site_match = re.search(r'(?:на\s+|to\s+|visit\s+)(\S+)', p)
    if site_match:
        site_name = site_match.group(1).strip('.,!?')
        if site_name in SITE_ALIASES:
            return SITE_ALIASES[site_name]
        # Пробуем как домен: "зайди на habr" → https://habr.com
        if re.match(r'^[a-zA-Z][a-zA-Z0-9-]*$', site_name):
            return f"https://{site_name}.com"

    # Проверяем алиасы в любом месте промпта
    for alias, url in SITE_ALIASES.items():
        if alias in p:
            return url

    # Электроника → DNS (быстрый поиск, без капчи)
    electronics = [
        "iphone", "айфон", "samsung", "самсунг", "pixel", "xiaomi",
        "смартфон", "ноутбук", "планшет", "наушники", "телефон",
        "телевизор", "компьютер", "видеокарт", "процессор",
    ]
    if any(w in p for w in electronics):
        query = re.sub(
            r'(цена|стоимость|купить|почём|сколько\s+стоит|найди|покажи)',
            '', p, flags=re.IGNORECASE
        ).strip(' ,.')
        return f"https://www.dns-shop.ru/search/?q={urllib.parse.quote(query)}"

    # Общие ценовые запросы → Citilink
    if any(w in p for w in ["цена", "стоимость", "купить"]):
        return f"https://www.citilink.ru/search/?text={urllib.parse.quote(prompt)}"

    # По умолчанию → пустая страница, агент сам решит куда идти
    return ""


async def run_visual_agent(query_id: str, task: str) -> dict:
    """Автономный визуальный агент — открывает реальный браузер, серфит страницы
    и показывает пользователю каждый шаг через скриншоты в превью-панели."""
    print(f"👁️ Визуальный агент запущен: {task}", flush=True)

    start_url = _build_visual_start_url(task)
    await send_trace(query_id, "search", "processing", f"Открываю браузер...")

    server_params = StdioServerParameters(
        command="npx",
        args=["@playwright/mcp@latest", "--headless", "--browser", "chromium"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = await session.list_tools()
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.inputSchema,
                    }
                }
                for t in mcp_tools.tools
            ]

            # Первая навигация (если URL определён)
            if start_url:
                await send_trace(query_id, "browser", "processing", f"Перехожу на {start_url}")
                await session.call_tool("browser_navigate", {"url": start_url})
                await send_screenshot(query_id, session, f"Открыта: {start_url}")

                eval_result = await session.call_tool(
                    "browser_evaluate", {"function": "() => document.body.innerText"}
                )
                page_text = "\n".join(
                    b.text for b in eval_result.content if hasattr(b, "text")
                )[:MAX_TOOL_OUTPUT]

                messages = [
                    {"role": "system", "content": VISUAL_BROWSER_PROMPT},
                    {"role": "user", "content": task},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "init_nav",
                        "type": "function",
                        "function": {
                            "name": "browser_navigate",
                            "arguments": json.dumps({"url": start_url}),
                        }
                    }]},
                    {
                        "role": "tool",
                        "tool_call_id": "init_nav",
                        "content": f"URL: {start_url}\n\n{page_text}",
                    },
                ]
            else:
                # Нет предопределённого URL — агент сам решит куда идти
                await send_trace(query_id, "browser", "processing", "Юми думает куда перейти...")
                messages = [
                    {"role": "system", "content": VISUAL_BROWSER_PROMPT},
                    {"role": "user", "content": task},
                ]

            sources = [start_url]
            step = 0

            while step < MAX_STEPS:
                step += 1
                print(f"👁️ Шаг {step}/{MAX_STEPS}", flush=True)
                response = await create_completion(messages, tools)
                msg = response.choices[0].message

                if msg.content:
                    msg.content = re.sub(r"<think>.*?</think>", "", msg.content, flags=re.DOTALL).strip()
                    msg.content = re.sub(r"^[^\x00-\x7Fа-яёА-ЯЁ\s\{\"\[]+", "", msg.content).strip()

                # Финальный ответ без tool_calls
                if msg.content and not msg.tool_calls:
                    # Проверяем JSON done-формат
                    clean = re.sub(r"^[^{\[]*", "", msg.content).strip()
                    try:
                        parsed = json.loads(clean) if clean else None
                        if isinstance(parsed, dict) and parsed.get("type") == "done":
                            answer = parsed.get("answer", msg.content)
                            print(f"✅ Визуальный ответ (JSON): {answer[:200]}", flush=True)
                            return {"status": "completed", "item": None, "trace": [], "answer": answer, "sources": sources, "news": []}
                    except Exception:
                        pass
                    answer = msg.content
                    print(f"✅ Визуальный ответ: {answer[:200]}", flush=True)
                    return {"status": "completed", "item": None, "trace": [], "answer": answer, "sources": sources, "news": []}

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in (msg.tool_calls or [])
                    ] or None,
                })

                if not msg.tool_calls:
                    return {"status": "completed", "item": None, "trace": [], "answer": msg.content or "Задача выполнена.", "sources": sources, "news": []}

                for tool_call in msg.tool_calls:
                    name = tool_call.function.name
                    print(f"👁️🔧 {name}({tool_call.function.arguments[:80]})", flush=True)
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = json.loads(repair_json(tool_call.function.arguments))

                    # Трейс для пользователя
                    if name == "browser_navigate" and "url" in args:
                        url = args["url"]
                        if url not in sources:
                            sources.append(url)
                        await send_trace(query_id, "browser", "processing", f"Перехожу: {url}")
                    elif name == "browser_click":
                        await send_trace(query_id, "browser", "processing", "Нажимаю на элемент...")
                    elif name == "browser_type":
                        await send_trace(query_id, "browser", "processing", f"Ввожу: {args.get('text', '')[:40]}")

                    result = await session.call_tool(name, args)
                    texts = [b.text for b in result.content if hasattr(b, "text")]
                    result_text = "\n".join(texts)
                    truncated = result_text[:MAX_TOOL_OUTPUT]
                    if len(result_text) > MAX_TOOL_OUTPUT:
                        truncated += "\n...[обрезано]"
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": truncated})

                    # Скриншот после каждого браузерного действия
                    if name in ("browser_navigate", "browser_click", "browser_type"):
                        short = args.get("url", args.get("selector", args.get("text", "")))[:50]
                        await send_screenshot(query_id, session, label=f"{name}: {short}")

            return {
                "status": "completed",
                "trace": [],
                "answer": "Достигнут лимит шагов. Попробуй уточнить запрос.",
                "sources": sources,
                "news": [],
            }


async def run_agent(query_id: str, task: str) -> dict:
    print(f"🚀 Агент запущен: {task}", flush=True)
    await send_trace(query_id, "search", "processing", "Юми ищет информацию...")

    # ── Прямая браузерная команда: зайди на сайт, нажми, заполни ──
    if _wants_browser_action(task):
        print("🌐 Браузерная команда → визуальный агент", flush=True)
        return await run_visual_agent(query_id, task)

    # ── Визуальный браузерный путь: цены, электроника, новинки ──
    if _should_browse_visually(task):
        print("👁️ Визуальный путь (цены/электроника)", flush=True)
        return await run_visual_agent(query_id, task)

    # ── Быстрый путь: без браузера (факты, курсы, энциклопедия) ──
    if not _needs_browser(task):
        print("⚡ Быстрый путь (без браузера)", flush=True)
        try:
            search_results, page_text = await _fetch_context(task)
        except Exception as e:
            print(f"⚠️ Fetch failed: {e}", flush=True)
            search_results, page_text = [], ""

        sources = [r["url"] for r in search_results]

        prompt_with_context = task
        if page_text:
            await send_trace(query_id, "analyzed", "processing", "Изучаю найденные страницы...")
            prompt_with_context = (
                f"Вопрос пользователя: {task}\n\n"
                f"Данные из интернета (актуальные, только что получены):\n{page_text}\n\n"
                f"ВАЖНО: Отвечай СТРОГО на основе данных выше. "
                f"Если в данных есть цены, товары, даты — используй именно их, не опираясь на свои внутренние знания. "
                f"Не говори 'не вышел', 'не известно' если в данных есть конкретная информация. "
                f"Отвечай на том же языке что и вопрос. Будь Юми — дружелюбной и немного игривой."
            )

        def _answer():
            return _llm_client.chat.completions.create(
                model="google/gemini-2.0-flash-001",
                messages=[
                    {"role": "system", "content": "You are Yumi, a cheerful anime girl assistant. Answer concisely and helpfully. Always respond in the same language the user used."},
                    {"role": "user", "content": prompt_with_context},
                ],
                max_tokens=1024,
            )

        response = await asyncio.get_running_loop().run_in_executor(None, _answer)
        answer = response.choices[0].message.content or "Не удалось получить ответ."
        answer = re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
        print(f"✅ Быстрый ответ: {answer[:200]}", flush=True)

        # news — результаты поиска для отображения на фронте
        news = [
            {"title": r["title"], "url": r["url"], "snippet": r.get("snippet", ""), "date": ""}
            for r in search_results
        ]
        return {"status": "completed", "item": None, "trace": [], "answer": answer, "sources": sources, "news": news}

    # ── Медленный путь: браузер для шопинга ──
    print("🌐 Браузерный путь (шопинг)", flush=True)
    server_params = StdioServerParameters(
        command="npx",
        args=["@playwright/mcp@latest", "--headless", "--browser", "chromium"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = await session.list_tools()
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.inputSchema,
                    }
                }
                for tool in mcp_tools.tools
            ]

            import urllib.parse
            start_url = _build_first_nav_url(task)
            await session.call_tool("browser_navigate", {"url": start_url})
            eval_result = await session.call_tool(
                "browser_evaluate", {"function": "() => document.body.innerText"}
            )
            page_text = "\n".join(
                b.text for b in eval_result.content if hasattr(b, 'text')
            )[:MAX_TOOL_OUTPUT]

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "init_eval",
                    "type": "function",
                    "function": {
                        "name": "browser_evaluate",
                        "arguments": json.dumps({"function": "() => document.body.innerText"})
                    }
                }]},
                {
                    "role": "tool",
                    "tool_call_id": "init_eval",
                    "content": f"URL: {start_url}\n\n{page_text}",
                }
            ]

            sources = [start_url]
            step = 0

            while step < MAX_STEPS:
                step += 1
                print(f"📍 Шаг {step}/{MAX_STEPS}", flush=True)
                response = await create_completion(messages, tools)
                msg = response.choices[0].message
                print(f"💬 {msg.content[:200] if msg.content else 'None'}", flush=True)

                if msg.content:
                    msg.content = re.sub(r'<think>.*?</think>', '', msg.content, flags=re.DOTALL).strip()
                    msg.content = re.sub(r'^[^\x00-\x7Fа-яёА-ЯЁ\s\{\"\[]+', '', msg.content).strip()

                if msg.content and not msg.tool_calls:
                    clean = re.sub(r'^[^{\[]*', '', msg.content).strip()
                    try:
                        parsed = json.loads(clean) if clean else None
                        if isinstance(parsed, dict) and parsed.get("type") == "done":
                            answer = parsed.get("answer", msg.content)
                            print(f"✅ Финальный ответ (JSON): {answer[:300]}", flush=True)
                            return {"status": "completed", "item": None, "trace": [], "answer": answer, "sources": sources}
                    except Exception:
                        pass

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in (msg.tool_calls or [])
                    ] or None
                })

                if not msg.tool_calls:
                    answer = msg.content or "Задача выполнена."
                    print(f"✅ Финальный ответ: {answer[:300]}", flush=True)
                    return {"status": "completed", "item": None, "trace": [], "answer": answer, "sources": sources}

                for tool_call in msg.tool_calls:
                    name = tool_call.function.name
                    print(f"🔧 {name}({tool_call.function.arguments[:100]})", flush=True)
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = json.loads(repair_json(tool_call.function.arguments))

                    if name == "browser_navigate" and "url" in args:
                        url = args["url"]
                        if url not in sources:
                            sources.append(url)
                        await send_trace(query_id, "analyzed", "processing", f"Открываю {url}")
                    elif name in ("browser_click", "browser_type"):
                        await send_trace(query_id, "compared", "processing", "Взаимодействую со страницей...")

                    result = await session.call_tool(name, args)

                    # Скриншот после ключевых действий — чтобы пользователь видел что делает агент
                    if name in ("browser_navigate", "browser_click", "browser_type"):
                        short = args.get("url", args.get("selector", args.get("text", "")))[:60]
                        await send_screenshot(query_id, session, label=f"{name}: {short}")
                    texts = [b.text for b in result.content if hasattr(b, 'text')]
                    result_text = "\n".join(texts)
                    truncated = result_text[:MAX_TOOL_OUTPUT]
                    if len(result_text) > MAX_TOOL_OUTPUT:
                        truncated += f"\n...[обрезано]"
                    print(f"   → {truncated[:200]}", flush=True)
                    messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": truncated})

            return {
                "status": "completed",
                "trace": [],
                "answer": "Достигнут лимит шагов. Попробуй уточнить запрос.",
                "sources": sources,
            }

@app.post("/process")
async def process(req: MLRequest):
    try:
        result = await run_agent(req.query_id, req.text)
        return result
    except Exception as e:
        return {"status": "failed", "error": str(e)}

@app.get("/health")
async def health():
    return {"status": "ok"}

class PlanRequest(BaseModel):
    prompt: str

@app.post("/api/plan")
async def plan(req: PlanRequest):
    # Сначала пробуем построить URL детерминировано (без LLM)
    first_url = _build_first_nav_url(req.prompt)
    actions = [
        {"type": "navigate", "url": first_url},
        {"type": "waitFor", "selector": "body", "timeoutMs": 8000},
        {"type": "scroll", "deltaY": 400},
    ]
    print(f"PLAN: deterministic URL = {first_url}")
    return {"actions": actions}
    
class StepRequest(BaseModel):
    prompt: str
    url: str
    page_text: str
    interactive_elements: str = ""
    history: list = []
    open_tabs: dict = {}

@app.post("/api/step")
async def step(req: StepRequest):
    print(f"STEP REQUEST: url={req.url}, history={req.history}, page_text_len={len(req.page_text)}")
    client = OpenAI(
        api_key=os.getenv("QWEN_API_KEY"),
        base_url="https://api.zveno.ai/v1",
    )

    # --- Определяем, первый ли это шаг ---
    is_first_step = len(req.history) == 0
    current_url = req.url or ""
    is_on_blank = not current_url or current_url in ("", "about:blank", "chrome://newtab/")
    is_on_extension = "chrome-extension://" in current_url or "claude.ai" in current_url or "zveno.ai" in current_url

    # Если первый шаг или мы на пустой/нерелевантной странице — сразу навигируем
    if is_first_step or is_on_blank or is_on_extension:
        nav_url = _build_first_nav_url(req.prompt)
        print(f"STEP: forcing first navigation to {nav_url}")
        return {"type": "navigate", "url": nav_url}

    # --- Формируем контекст страницы для модели ---
    page_context = f"Current URL: {req.url}\n"
    if req.page_text:
        # Обрезаем до 2000 символов чтобы не перегружать контекст
        trimmed = req.page_text[:2000]
        page_context += f"Page content (trimmed):\n{trimmed}\n"

    if req.interactive_elements:
        page_context += f"\nInteractive elements on page:\n{req.interactive_elements[:1500]}\n"

    history_text = ""
    if req.history:
        history_text = "\nPrevious actions taken:\n"
        for i, act in enumerate(req.history):
            history_text += f"  Step {i+1}: {json.dumps(act)}\n"

    # Формируем информацию об открытых вкладках
    tabs_text = ""
    if hasattr(req, 'open_tabs') and req.open_tabs:
        tabs_text = "\nCurrently open tabs:\n"
        for alias, info in req.open_tabs.items():
            tabs_text += f'  "{alias}": {info.get("url", "about:blank")} — {info.get("title", "")}\n'

    messages = [
        {"role": "system", "content": """You are a browser automation agent controlling a real Chrome browser.
You receive the current page URL, visible text, and interactive elements.
You control the browser by returning ONE action at a time as a JSON object.

Available actions:
— Page interaction:
- {"type": "navigate", "url": "https://...", "alias": "tab_alias"}  (alias optional, defaults to current tab)
- {"type": "click", "selector": "css_selector"}
- {"type": "type", "selector": "css_selector", "text": "...", "clear": true}
- {"type": "scroll", "deltaY": 800}
- {"type": "hover", "selector": "css_selector"}
- {"type": "select", "selector": "css_selector", "value": "option_value"}
- {"type": "press", "key": "Enter"}
- {"type": "focus", "selector": "css_selector"}
- {"type": "waitFor", "selector": "css_selector", "timeoutMs": 5000}

— Tab management:
- {"type": "newTab", "url": "https://...", "alias": "my_tab"}
- {"type": "switchTab", "alias": "my_tab"}
- {"type": "closeTab", "alias": "my_tab"}

— Finish:
- {"type": "done", "answer": "your final answer to user"}

Rules:
- Look at the interactive elements list to find the right selector — do NOT guess selectors
- If you see the element you need in the interactive elements list, use its exact selector
- Never use google.com or duckduckgo.com (captcha). For web search use https://www.bing.com/search?q=query
- For Wildberries: https://www.wildberries.ru/catalog/0/search.aspx?search=QUERY&priceU=PRICE00
- For Ozon: https://www.ozon.ru/search/?text=QUERY&price_to=PRICE
- Use newTab to open pages in parallel (e.g. compare prices on two sites)
- Use switchTab to go back to a previously opened tab
- Use hover for dropdown menus and tooltips
- Use select for <select> dropdowns
- Return {"type": "done", "answer": "..."} only when you have found the answer
- Return ONLY a valid JSON object, nothing else"""},
        {"role": "user", "content": f"Task: {req.prompt}\n\n{page_context}{history_text}{tabs_text}\n\nWhat is the next action?"},
    ]

    response = client.chat.completions.create(
        model="google/gemini-2.0-flash-001",
        messages=messages,
        max_tokens=512,
    )

    content = response.choices[0].message.content
    print(f"STEP RAW: {content}")
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    content = re.sub(r'```json|```', '', content).strip()

    if not content:
        return {"type": "navigate", "url": f"https://duckduckgo.com/?q={req.prompt.replace(' ', '+')}"}

    try:
        action = json.loads(content)
    except:
        try:
            action = json.loads(repair_json(content))
        except:
            return {"type": "navigate", "url": f"https://duckduckgo.com/?q={req.prompt.replace(' ', '+')}"}

    return action


def _build_first_nav_url(prompt: str) -> str:
    """Определяем URL для первого шага на основе промпта."""
    p = prompt.lower()

    # Wildberries
    if "wildberries" in p or "wb" in p or "вб" in p or "вайлдберриз" in p:
        import urllib.parse
        # Извлекаем цену если есть
        price = _extract_price(p)
        # Убираем упоминания магазина и цены для поискового запроса
        query = re.sub(r'(на\s+)?(wildberries|wb|вб|вайлдберриз)', '', p, flags=re.IGNORECASE)
        query = re.sub(r'до\s+\d+\s*(к|тыс|тысяч)?\s*(руб|рублей|₽)?', '', query, flags=re.IGNORECASE)
        query = re.sub(r'\d+\s*(к|тыс|тысяч)\s*(руб|рублей|₽)?', '', query, flags=re.IGNORECASE)
        query = query.strip(' ,.')
        url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={urllib.parse.quote(query)}"
        if price:
            url += f"&priceU={price}00"
        return url

    # Ozon
    if "ozon" in p or "озон" in p:
        import urllib.parse
        price = _extract_price(p)
        query = re.sub(r'(на\s+)?(ozon|озон)', '', p, flags=re.IGNORECASE)
        query = re.sub(r'до\s+\d+\s*(к|тыс|тысяч)?\s*(руб|рублей|₽)?', '', query, flags=re.IGNORECASE)
        query = re.sub(r'\d+\s*(к|тыс|тысяч)\s*(руб|рублей|₽)?', '', query, flags=re.IGNORECASE)
        query = query.strip(' ,.')
        url = f"https://www.ozon.ru/search/?text={urllib.parse.quote(query)}"
        if price:
            url += f"&price_to={price}"
        return url

    # Avito
    if "avito" in p or "авито" in p:
        import urllib.parse
        price = _extract_price(p)
        query = re.sub(r'(на\s+)?(avito|авито)', '', p, flags=re.IGNORECASE)
        query = re.sub(r'до\s+\d+\s*(к|тыс|тысяч)?\s*(руб|рублей|₽)?', '', query, flags=re.IGNORECASE)
        query = re.sub(r'\d+\s*(к|тыс|тысяч)\s*(руб|рублей|₽)?', '', query, flags=re.IGNORECASE)
        query = query.strip(' ,.')
        url = f"https://www.avito.ru/rossiya?q={urllib.parse.quote(query)}"
        if price:
            url += f"&pmax={price}"
        return url

    # По умолчанию — Wikipedia (поисковики блокируют headless капчей)
    import urllib.parse
    return f"https://ru.wikipedia.org/wiki/{urllib.parse.quote(prompt.replace(' ', '_'))}"


def _extract_price(text: str) -> int | None:
    """Извлекает цену из текста. '2к' -> 2000, '5000' -> 5000."""
    # "до 2к рублей", "до 2 к", "до 2000"
    m = re.search(r'до\s+(\d+)\s*(к|тыс|тысяч)', text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000
    m = re.search(r'до\s+(\d+)\s*(руб|рублей|₽)?', text, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        return val if val > 100 else val * 1000
    # просто число с "к"
    m = re.search(r'(\d+)\s*(к|тыс|тысяч)\s*(руб|рублей|₽)?', text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 1000
    return None

@app.get("/api/health")
async def api_health():
    return {"status": "ok"}

@app.get("/api/history")
async def api_history():
    return []


@app.post("/api/query")
async def api_query(req: dict):
    query_id = str(uuid.uuid4())
    query_text = req.get("query", "")
    results[query_id] = {"status": "pending", "id": query_id}

    async def run_and_store():
        try:
            result = await run_agent(query_id, query_text)
            if result:
                results[query_id] = {**result, "id": query_id}
                print(f"✅ [{query_id[:8]}] готово: {results[query_id]}", flush=True)
                answer_text = result.get("answer", "")
                await send_trace(query_id, "completed", "done", answer_text)
            else:
                results[query_id] = {"status": "failed", "id": query_id}
                await send_trace(query_id, "completed", "failed", "Агент не вернул результат")
        except Exception as e:
            print(f"❌ [{query_id[:8]}]: {e}", flush=True)
            import traceback;
            traceback.print_exc()
            results[query_id] = {"status": "failed", "error": str(e), "id": query_id}
            await send_trace(query_id, "completed", "failed", str(e)[:200])

    asyncio.get_running_loop().create_task(run_and_store())
    return {"request_id": query_id, "status": "processing"}


@app.post("/api/browser-done/{query_id}")
async def browser_done(query_id: str, req: dict):
    """Расширение сообщает что выполнило браузерную команду."""
    answer = req.get("answer", "Готово!")
    results[query_id] = {
        "status": "completed",
        "id": query_id,
        "answer": answer,
        "sources": req.get("sources", []),
        "trace": [],
        "news": [],
    }
    await send_trace(query_id, "completed", "done", answer)
    print(f"✅ [{query_id[:8]}] browser-done: {answer[:100]}", flush=True)
    return {"ok": True}


@app.get("/api/result/{query_id}")
async def api_result(query_id: str):
    result = results.get(query_id, {"status": "pending", "id": query_id})
    print(f"📦 RESULT [{query_id[:8]}]: {result}", flush=True)
    # Фронт ожидает { payload: <result> } — оборачиваем
    if result.get("status") == "pending":
        return result  # pending отдаём без обёртки — фронт polling-ит пока не completed
    return {"payload": result}


@app.get("/api/stream/{query_id}")
async def api_stream(query_id: str):
    """SSE-стрим трейсов агента для конкретного запроса.
    Фронт подписывается сразу после POST /api/query и получает
    события в реальном времени: step + status + detail.
    """
    queue: asyncio.Queue = asyncio.Queue()
    stream_queues[query_id] = queue

    # Если агент уже успел написать трейсы до подключения SSE — сразу отдаём их
    for event in traces.get(query_id, []):
        await queue.put(event)

    async def generate():
        try:
            while True:
                # Проверяем, не завершился ли агент раньше, чем клиент подключился
                result = results.get(query_id)
                if result and result.get("status") in ("completed", "failed") and queue.empty():
                    status = "done" if result.get("status") == "completed" else "failed"
                    detail = ""
                    trace_list = result.get("trace")
                    if isinstance(trace_list, list) and trace_list:
                        detail = trace_list[0]
                    yield f"data: {json.dumps({'step': 'completed', 'status': status, 'detail': detail})}\n\n"
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {json.dumps(event)}\n\n"

                # Завершаем стрим при финальных событиях
                if event.get("step") == "completed" or event.get("status") in ("done", "failed"):
                    break
        finally:
            stream_queues.pop(query_id, None)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/internal/trace")
async def internal_trace(req: dict):
    query_id = req.get("query_id")
    event = req.get("event", {})
    if query_id:
        if query_id not in traces:
            traces[query_id] = []
        traces[query_id].append(event)
    return {"ok": True}