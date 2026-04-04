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

load_dotenv()
app = FastAPI()

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8080")
MAX_STEPS = 15

SYSTEM_PROMPT = """You are Yumi, an anime girl mascot of our project. You are cheerful, friendly and a little playful.
You have access to browser tools and can control the browser like a human — navigate, click, type, scroll, fill forms, and interact with any website.
Always respond in the same language the user used.
Stay in character as Yumi at all times — use cute expressions, emojis, and a warm friendly tone.

RULES:
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
    """Отправляем SSE событие в бек"""
    async with httpx.AsyncClient() as client:
        try:
            await client.post(f"{BACKEND_URL}/internal/trace", json={
                "query_id": query_id,
                "event": {"step": step, "status": status, "detail": detail}
            })
        except Exception:
            pass

async def run_agent(query_id: str, task: str) -> dict:
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
                response = create_completion(messages, tools)
                msg = response.choices[0].message

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
    client = OpenAI(
        api_key=os.getenv("QWEN_API_KEY"),
        base_url="https://api.zveno.ai/v1",
    )

    try:
        response = client.chat.completions.create(
            model="qwen/qwen3-30b-a3b-instruct-2507",
            messages=[
                {"role": "system", "content": """You are a browser automation planner.
Convert the user's request into a JSON array of browser actions for a Chrome extension to execute.

Available action types:
- {"type": "navigate", "url": "https://..."}
- {"type": "click", "selector": "css_selector"}
- {"type": "type", "selector": "css_selector", "text": "text to type", "clear": true}
- {"type": "scroll", "deltaY": 800}
- {"type": "waitFor", "selector": "css_selector", "timeoutMs": 5000}

Rules:
- Return ONLY a valid JSON array, no explanation, no markdown, no text before or after
- Use simple, reliable CSS selectors
- Always add waitFor after navigate to wait for page load
- For searches use the site's own search, not DuckDuckGo"""},
                {"role": "user", "content": req.prompt}
            ],
            max_tokens=2048,
        )

        content = response.choices[0].message.content
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        content = re.sub(r'```json|```', '', content).strip()

        try:
            actions = json.loads(content)
        except json.JSONDecodeError:
            actions = json.loads(repair_json(content))

        return {"actions": actions}

    except Exception as e:
        return {"actions": [], "error": str(e)}