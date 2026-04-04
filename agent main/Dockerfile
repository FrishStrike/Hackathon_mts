FROM python:3.12-slim

# Устанавливаем Node.js для playwright-mcp
RUN apt-get update && apt-get install -y nodejs npm && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем файлы
COPY ml_service.py .
COPY .env .

# Устанавливаем Python зависимости
RUN pip install fastapi uvicorn httpx openai python-dotenv mcp groq anyio json-repair

# Устанавливаем playwright-mcp и браузер
RUN npx playwright install chromium

EXPOSE 8001

CMD ["uvicorn", "ml_service:app", "--host", "0.0.0.0", "--port", "8001"]