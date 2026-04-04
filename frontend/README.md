# Browser AI Assistant (Chrome Extension)

Расширение для управления **реальным браузером** через команды естественного языка.

## UI (Юми) внутри расширения

React-фронтенд собирается в `frontend/dist`, затем копируется в расширение:
- `extension/ui/index.html` (страница Side Panel)
- `extension/assets/*` (JS/CSS)
- `extension/avatars/*`, `extension/fonts/*`, `extension/favicon.svg` (публичные ассеты)

## Как собрать и запустить

1) Собрать фронт:

```bash
cd frontend
npm run build
npm run copy:ext
```

2) Установить расширение:
- Chrome → `chrome://extensions`
- включить **Developer mode**
- **Load unpacked** → выбрать папку `extension/`

3) Открыть любой сайт → нажать на иконку расширения → откроется Side Panel.

## Как пользоваться

- В поле **Команда** можно:
  - вставить JSON-массив действий (он выполнится напрямую), или
  - написать естественный язык — тогда расширение попробует получить план из `POST http://localhost:8080/api/plan` (если реализуете).

## Формат действий (минимум)

```json
[
  { "type": "navigate", "url": "https://example.com" },
  { "type": "click", "selector": "a" },
  { "type": "type", "selector": "input[name=q]", "text": "hello", "clear": true },
  { "type": "scroll", "deltaY": 800 },
  { "type": "waitFor", "selector": ".result", "timeoutMs": 15000 }
]
```

## Дальше по ТЗ

Чтобы соответствовать ТЗ полностью (универсально для любых сайтов), нужен планировщик на LLM:
- эндпоинт `POST /api/plan` должен превращать prompt → массив действий.
- желательно шаговый режим: после каждого шага отдавать LLM состояние страницы (DOM/скрин/доступные элементы).
