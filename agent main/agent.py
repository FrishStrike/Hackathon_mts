import json
from json_repair import repair_json
import os
from groq import Groq
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import anyio

load_dotenv()

SYSTEM_PROMPT = """You are Yumi, an anime girl mascot of our project. You are cheerful, friendly and a little playful.
You have access to browser tools and can browse the web to find information.
Always respond in the same language the user used.
Stay in character as Yumi at all times — use cute expressions, emojis, and a warm friendly tone.

CRITICAL RULES:
1. To search the web, ALWAYS navigate directly to: https://duckduckgo.com/?q=your+search+query
   NEVER try to fill search forms manually.
2. Never use Google.
3. After navigating, use browser_snapshot to read the page content.
4. If browser_snapshot returns empty or truncated content, use browser_evaluate with this function to get page text:
   () => document.body.innerText
5. If a page doesn't load properly after 2 attempts, go back to DuckDuckGo and try a different source.
6. Never click 'Купить' or add items to cart. Only read information."""

def create_completion(messages, tools):
    import time
    from openai import OpenAI
    client = OpenAI(
        api_key=os.getenv("QWEN_API_KEY"),
        base_url="https://api.zveno.ai/v1",
    )
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="qwen/qwen3-30b-a3b-instruct-2507",
                messages=messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=4096,
            )
            print("🔄 Используем Qwen (ZvenoAI)")
            return response
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                print(f"⏳ Rate limit, ждём 30 сек... (попытка {attempt+1}/3)")
                time.sleep(30)
            else:
                raise e

async def run_agent(task: str):
    server_params = StdioServerParameters(
        command="npx",
        args=["@playwright/mcp@latest"],  # убрать --headless
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = await session.list_tools()
            print("📦 Доступные инструменты:")
            for t in mcp_tools.tools:
                print(f"   - {t.name}")

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

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task},
            ]

            print(f"\n🤖 Агент запущен: {task}\n")

            MAX_STEPS = 15
            step = 0

            while step < MAX_STEPS:
                step += 1
                print(f"📍 Шаг {step}/{MAX_STEPS}")
                response = create_completion(messages, tools)
                msg = response.choices[0].message

                # Убираем thinking блок
                if msg.content and '<think>' in msg.content:
                    import re
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
                    print("✅ Ответ агента:")
                    print(msg.content)
                    break

                for tool_call in msg.tool_calls:
                    name = tool_call.function.name
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = json.loads(repair_json(tool_call.function.arguments))
                    print(f"🔧 {name}({tool_call.function.arguments[:80]})")

                    result = await session.call_tool(name, args)
                    texts = [b.text for b in result.content if hasattr(b, 'text')]
                    result_text = "\n".join(texts)
                    print(f"   → {result_text[:300]}\n")

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_text,
                    })
            else:
                print(f"⚠️ Достигнут лимит {MAX_STEPS} шагов. Агент остановлен.")

if __name__ == "__main__":
    print("Скрипт запустился")
    task = input("Введи задание: ")
    print(f"Задание: {task}")
    anyio.run(run_agent, task)
    print("Готово")