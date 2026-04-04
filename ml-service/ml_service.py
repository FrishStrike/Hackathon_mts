import asyncio
import json
import os
import re
import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from json_repair import repair_json
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import OpenAI
import uuid
from fastapi.responses import StreamingResponse
import asyncio

# Хранилище результатов и трейсов
results = {}
traces = {}
load_dotenv()
app = FastAPI()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8080")
MAX_STEPS = 15

SYSTEM_PROMPT = """You are Yumi, an anime girl mascot of our project. You are cheerful, friendly and a little playful.
You have access to browser tools and can control the browser like a human — navigate, click, type, scroll, fill forms, and interact with any website.
Always respond in the same language the user used.
Stay in character as Yumi at all times — use cute expressions, emojis, and a warm friendly tone.

RULES:
ЗАПРЕЩЕНО использовать browser_wait_for с текстом — он вешает агента на 30 секунд. 
Используй browser_snapshot чтобы увидеть страницу, затем сразу действуй.
СТРОГО ЗАПРЕЩЕНО: никогда не используй google.com — он блокирует headless браузеры капчей. Используй ТОЛЬКО duckduckgo.com
1. To search the web, navigate to: https://duckduckgo.com/?q=your+search+query
2. Never use Google.
3. After navigating, use browser_snapshot to see the page and find elements to interact with.
4. If browser_snapshot returns empty content, use browser_evaluate with: () => document.body.innerText
5. If a page doesn't load after 2 attempts, try a different approach.
6. For actions (click, type, scroll) — always take a snapshot first to find the right element ref.
7. Complete the full task the user asked — don't stop halfway."""

class MLRequest(BaseModel):
    query_id: str
    text: str

def create_completion(messages, tools):
    import time
    client = OpenAI(
        api_key=os.getenv("QWEN_API_KEY"),
        base_url="https://api.zveno.ai/v1",
    )
    for attempt in range(3):
        try:
            return client.chat.completions.create(
                model="qwen/qwen3-30b-a3b-instruct-2507",
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=4096,
            )
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                time.sleep(30)
            else:
                raise e

async def send_trace(query_id: str, step: str, status: str, detail: str = ""):
    if query_id not in traces:
        traces[query_id] = []
    traces[query_id].append({"step": step, "status": status, "detail": detail})

async def run_agent(query_id: str, task: str) -> dict:
    print(f"🚀 Агент запущен: {task}", flush=True)

    server_params = StdioServerParameters(
        command="npx",
        args=["@playwright/mcp@latest", "--headless"],
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

            # Первый шаг — сразу идём на DuckDuckGo
            search_url = f"https://duckduckgo.com/?q={task.replace(' ', '+')}"
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "init_nav",
                    "type": "function",
                    "function": {
                        "name": "browser_navigate",
                        "arguments": json.dumps({"url": search_url})
                    }
                }]},
            ]
            init_result = await session.call_tool("browser_navigate", {"url": search_url})
            init_texts = [b.text for b in init_result.content if hasattr(b, 'text')]
            messages.append({
                "role": "tool",
                "tool_call_id": "init_nav",
                "content": "\n".join(init_texts),
            })

            await send_trace(query_id, "browsing", "in_progress", "Юми ищет информацию...")

            sources = []
            step = 0

            while step < MAX_STEPS:
                step += 1
                print(f"📍 Шаг {step}/{MAX_STEPS}", flush=True)
                response = create_completion(messages, tools)
                msg = response.choices[0].message
                print(f"💬 {msg.content[:200] if msg.content else 'None'}", flush=True)

                if msg.content and '<think>' in msg.content:
                    msg.content = re.sub(r'<think>.*?</think>', '', msg.content, flags=re.DOTALL).strip()

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                        for tc in (msg.tool_calls or [])
                    ] or None
                })

                if not msg.tool_calls:
                    await send_trace(query_id, "done", "done", "Готово!")
                    return {
                        "status": "completed",
                        "item": {"title": task, "price": "", "url": "", "specs": {}},
                        "trace": [msg.content],
                        "sources": sources,
                    }

                for tool_call in msg.tool_calls:
                    name = tool_call.function.name
                    print(f"🔧 {name}({tool_call.function.arguments[:100]})", flush=True)
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = json.loads(repair_json(tool_call.function.arguments))

                    # Собираем источники
                    if name == "browser_navigate" and "url" in args:
                        url = args["url"]
                        if "duckduckgo" not in url and url not in sources:
                            sources.append(url)
                        await send_trace(query_id, "navigate", "in_progress", f"Открываю {url}")

                    result = await session.call_tool(name, args)
                    texts = [b.text for b in result.content if hasattr(b, 'text')]
                    result_text = "\n".join(texts)
                    print(f"   → {result_text[:200]}", flush=True)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    })

            return {
                "status": "completed",
                "trace": ["Достигнут лимит шагов"],
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

    messages = [
        {"role": "system", "content": """You are a browser automation agent controlling a real Chrome browser.
You receive the current page URL, visible text, and interactive elements.
You control the browser by returning ONE action at a time as a JSON object.

Available actions:
- {"type": "navigate", "url": "https://..."}
- {"type": "click", "selector": "css_selector"}
- {"type": "type", "selector": "css_selector", "text": "...", "clear": true}
- {"type": "scroll", "deltaY": 800}
- {"type": "waitFor", "selector": "css_selector", "timeoutMs": 5000}
- {"type": "done", "answer": "your final answer to user"}

Rules:
- Look at the interactive elements list to find the right selector — do NOT guess selectors
- If you see the element you need in the interactive elements list, use its exact selector
- Never use google.com. For web search use https://duckduckgo.com/?q=query
- For Wildberries: https://www.wildberries.ru/catalog/0/search.aspx?search=QUERY&priceU=PRICE00
- For Ozon: https://www.ozon.ru/search/?text=QUERY&price_to=PRICE
- Return {"type": "done", "answer": "..."} only when you have found the answer
- Return ONLY a valid JSON object, nothing else"""},
        {"role": "user", "content": f"Task: {req.prompt}\n\n{page_context}{history_text}\n\nWhat is the next action?"},
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

    # По умолчанию — DuckDuckGo
    import urllib.parse
    return f"https://duckduckgo.com/?q={urllib.parse.quote(prompt)}"


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
    results[query_id] = {"status": "pending", "id": query_id}

    async def run_and_store():
        try:
            result = await run_agent(query_id, req.get("query", ""))
            if result:
                results[query_id] = {**result, "id": query_id}
            else:
                results[query_id] = {"status": "failed", "id": query_id}
            print(f"✅ [{query_id[:8]}] готово: {results[query_id]}", flush=True)
        except Exception as e:
            print(f"❌ [{query_id[:8]}]: {e}", flush=True)
            import traceback;
            traceback.print_exc()
            results[query_id] = {"status": "failed", "error": str(e), "id": query_id}

    loop = asyncio.get_event_loop()
    loop.create_task(run_and_store())
    return {"request_id": query_id, "status": "processing"}


@app.get("/api/result/{query_id}")
async def api_result(query_id: str):
    result = results.get(query_id, {"status": "pending", "id": query_id})
    print(f"📦 RESULT [{query_id[:8]}]: {result}", flush=True)
    return result


@app.post("/internal/trace")
async def internal_trace(req: dict):
    query_id = req.get("query_id")
    event = req.get("event", {})
    if query_id:
        if query_id not in traces:
            traces[query_id] = []
        traces[query_id].append(event)
    return {"ok": True}