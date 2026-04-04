# ML-dev 1 — LLM Agent + MCP
FastAPI :8001 | Агентная система поиска товаров и новостей

## Быстрый старт

### 1. Настройка окружения
```bash
cp .env.example .env
# Вставь свой DeepSeek API key в .env
```

### 2. Установка зависимостей
```bash
pip install -r requirements.txt
```

### 3. Запуск
```bash
uvicorn main:app --port 8001 --reload
```

### Или через Docker
```bash
docker build -t ml-dev-1 .
docker run -p 8001:8001 --env-file .env ml-dev-1
```

## API

### POST /agent/run
```json
{
  "query": "Найди самый дешёвый новый iPhone с 256 ГБ. Собери новости про Apple за неделю."
}
```

Ответ:
```json
{
  "success": true,
  "result": {
    "summary": "Найден iPhone 15 128GB за 52 990₽...",
    "product": {
      "title": "Apple iPhone 15 256GB",
      "price": 64990.0,
      "url": "https://market.yandex.ru/...",
      "source": "Яндекс.Маркет"
    },
    "news": [...],
    "sources": [...]
  },
  "logs": [
    {"step": 1, "action": "NLP парсинг запроса", ...},
    {"step": 2, "action": "Вызов инструмента", "tool": "search_marketplace", ...},
    ...
  ]
}
```

### GET /health
Проверка работоспособности сервиса.

## Архитектура

```
POST /agent/run
    │
    ├─► nlp_parser.py     — DeepSeek разбирает запрос на подзадачи
    │
    ├─► orchestrator.py   — ReAct loop: LLM + tool calling
    │       │
    │       ├─► tools/marketplace.py  — httpx + BS4 → Яндекс.Маркет
    │       ├─► tools/news.py         — httpx + BS4 → Google News RSS
    │       └─► tools/validator.py    — проверка соответствия фильтрам
    │
    └─► agent/logger.py   — трассировка всех шагов агента
```

## Технологии
- Python 3.11+
- FastAPI
- DeepSeek V3 (tool calling / ReAct)
- httpx + BeautifulSoup4
- Pydantic v2
- Docker
