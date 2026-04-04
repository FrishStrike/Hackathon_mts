from dotenv import load_dotenv
load_dotenv()

import os
import json
import httpx
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from agent.orchestrator import run_agent

app = FastAPI(title="ML-сервис — LLM Agent", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8080")


class AgentRequest(BaseModel):
    query: str


class MLRequest(BaseModel):
    query_id: str
    text: str


async def send_trace(query_id: str, step: str, status: str, detail: str = ""):
    try:
        payload = {
            "query_id": query_id,
            "event": {"step": step, "status": status, "detail": detail}
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{BACKEND_URL}/internal/trace", json=payload)
    except Exception as e:
        print(f"[trace] Не удалось отправить трейс ({step}): {e}")


@app.post("/process")
async def process(request: MLRequest):
    query_id = request.query_id
    text = request.text

    if not text.strip():
        raise HTTPException(status_code=400, detail="text cannot be empty")

    await send_trace(query_id, "nlp", "started", "Разбираю запрос...")

    try:
        await send_trace(query_id, "search", "in_progress", "Ищу товары на маркетплейсе...")

        result, logs = await run_agent(text)

        await send_trace(query_id, "llm", "in_progress", "Формирую ответ...")

        product = result.get("product")
        item = None
        if product:
            item = {
                "title": product.get("title", ""),
                "price": str(product.get("price", "")) + " " + product.get("currency", "₽"),
                "url": product.get("url", ""),
                "specs": {
                    k: str(v) for k, v in product.items()
                    if k not in ("title", "price", "currency", "url", "source")
                    and v is not None
                }
            }

        payload = {
            "status": "completed",
            "item": item,
            "news": [],
            "trace": [
                f"[{log.get('step')}] {log.get('action')}" +
                (f" → {log.get('tool')}" if log.get('tool') else "")
                for log in logs
            ],
            "sources": result.get("sources", []),
            "summary": result.get("summary", ""),
        }

        await send_trace(query_id, "completed", "done", "Готово!")
        return JSONResponse(content=payload)

    except Exception as e:
        await send_trace(query_id, "error", "failed", str(e))
        return JSONResponse(
            status_code=500,
            content={
                "status": "failed",
                "error": str(e),
                "item": None,
                "news": [],
                "trace": [],
                "sources": [],
            }
        )


@app.post("/agent/run")
async def agent_run(request: AgentRequest):
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    result, logs = await run_agent(request.query)
    return JSONResponse(
        content={"success": True, "result": result, "logs": logs},
        media_type="application/json; charset=utf-8"
    )


@app.get("/health")
async def health():
    return {"status": "ok"}