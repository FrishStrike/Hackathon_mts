import httpx
import asyncio
import re
import json
from urllib.parse import quote_plus
from models.schemas import ProductCard
import os

OXYLABS_USERNAME = os.getenv("OXYLABS_USERNAME")
OXYLABS_PASSWORD = os.getenv("OXYLABS_PASSWORD")
OXYLABS_URL = "https://realtime.oxylabs.io/v1/queries"

REQUEST_TIMEOUT = httpx.Timeout(90.0, connect=15.0)


# ===========================================================================
# Главная точка входа
# ===========================================================================

async def search_marketplace(query: str, filters: dict) -> list[ProductCard]:
    """
    Ищет товары на Яндекс.Маркете.
    Fallback: Google Shopping → демо-данные.
    """
    search_query = _build_query(query, filters)

    all_products = await _search_yandex_market(search_query, filters)

    if not all_products:
        print("[marketplace] ЯМ пуст, пробуем Google Shopping...")
        all_products = await _search_google_shopping(search_query, filters)

    if not all_products:
        print("[marketplace] Все источники недоступны, генерируем демо-данные")
        return _demo_products(query, filters)

    all_products.sort(key=lambda p: (p.price is None, p.price or 0))
    return all_products


# ===========================================================================
# Яндекс.Маркет — несколько стратегий парсинга, до 50 товаров
# ===========================================================================

async def _search_yandex_market(query: str, filters: dict) -> list[ProductCard]:
    encoded = quote_plus(query)

    # Запускаем несколько страниц параллельно для большего охвата
    urls = [
        f"https://market.yandex.ru/search?text={encoded}&how=aprice&page=1",
        f"https://market.yandex.ru/search?text={encoded}&how=aprice&page=2",
        f"https://market.yandex.ru/search?text={encoded}&how=aprice&page=3",
    ]

    tasks = [_fetch_yandex_page(url, filters) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    products: list[ProductCard] = []
    seen_titles: set[str] = set()

    for res in results:
        if isinstance(res, Exception):
            print(f"[ЯМ] Ошибка страницы: {res}")
            continue
        for p in res:
            key = p.title.lower().strip()
            if key not in seen_titles:
                seen_titles.add(key)
                products.append(p)

    print(f"[ЯМ] Итого уникальных товаров: {len(products)}")
    return products


async def _fetch_yandex_page(url: str, filters: dict) -> list[ProductCard]:
    page_num = url.split("page=")[-1] if "page=" in url else "1"
    products: list[ProductCard] = []

    # --- Попытка 1: Oxylabs parse:true с xpath ---
    payload_parsed = {
        "source": "universal",
        "url": url,
        "render": "html",
        "parse": True,
        "context": [{"key": "country", "value": "RU"}],
        "parsing_instructions": {
            "products": {
                "_fns": [{"_fn": "xpath", "_args": ["//*[@data-auto='snippet-link']"]}],
                "_items": {
                    "title": {
                        "_fns": [
                            {"_fn": "xpath", "_args": [".//*[@data-auto='snippet-title']//text()"]},
                            {"_fn": "join", "_args": [""]}
                        ]
                    },
                    "price": {
                        "_fns": [
                            {"_fn": "xpath", "_args": ["./ancestor::*[1]//*[contains(@class,'ds-text-color-price-term')]//text()"]},
                            {"_fn": "join", "_args": [""]}
                        ]
                    },
                    "url": {
                        "_fns": [
                            {"_fn": "xpath", "_args": ["./@href"]},
                            {"_fn": "join", "_args": [""]}
                        ]
                    },
                }
            }
        }
    }

    data = await _oxylabs_request(payload_parsed)
    if data:
        try:
            content = data["results"][0]["content"]
            items = content.get("products", []) if isinstance(content, dict) else []
            print(f"[ЯМ DEBUG] parse:true стр.{page_num}: получено {len(items)} элементов")
            for item in items:
                title = (item.get("title") or "").strip()
                if not title:
                    continue
                price = _parse_price(item.get("price"))
                path = item.get("url") or ""
                full_url = (
                    f"https://market.yandex.ru{path}"
                    if path.startswith("/")
                    else path
                )
                if _apply_filters(title, price, filters):
                    products.append(ProductCard(
                        title=title, price=price, currency="₽",
                        url=full_url, source="Яндекс.Маркет",
                    ))
        except Exception as e:
            print(f"[ЯМ DEBUG] parse:true ошибка: {e}")

    if products:
        print(f"[ЯМ DEBUG] parse:true дал {len(products)} товаров на стр.{page_num}")
        return products

    # --- Попытка 2: raw HTML + агрессивный парсинг JSON-блоков ---
    payload_html = {
        "source": "universal",
        "url": url,
        "render": "html",
        "parse": False,
        "context": [{"key": "country", "value": "RU"}],
    }

    data = await _oxylabs_request(payload_html)
    if not data:
        return []

    try:
        html = data["results"][0]["content"]
        if not isinstance(html, str) or len(html) < 500:
            print(f"[ЯМ DEBUG] стр.{page_num}: HTML слишком короткий ({len(html) if isinstance(html, str) else 'не строка'})")
            return []

        print(f"[ЯМ DEBUG] стр.{page_num}: HTML получен, размер={len(html)}")

        # ДАМП структуры: ищем какие data-auto атрибуты есть в HTML
        data_autos = list(dict.fromkeys(re.findall(r'data-auto="([^"]+)"', html)))[:30]
        print(f"[ЯМ DEBUG] стр.{page_num}: data-auto атрибуты: {data_autos}")

        # ДАМП: первые 3 script-тега без type — смотрим начало каждого
        scripts = re.findall(r'<script(?![^>]*type)[^>]*>(.{0,300})', html)
        for i, s in enumerate(scripts[:5]):
            print(f"[ЯМ DEBUG] script[{i}]: {s[:200].strip()}")

        # Regex по реальным селекторам ЯМ
        # Название: data-auto="snippet-title" title="..."
        # Цена: class="...ds-text_color_price-term..."  (подчёркивание, не дефис!)
        title_matches = re.findall(r'data-auto="snippet-title"[^>]*title="([^"]+)"', html)
        price_matches = re.findall(r'ds-text_color_price-term[^>]*>([\d\s\u00a0]+)<', html)

        # href стоит ДО data-auto в реальном HTML ЯМ:
        # <a href="/card/..." ... data-auto="snippet-link">
        link_matches = re.findall(r'href="(/card/[^"]+)"[^>]*data-auto="snippet-link"', html)
        if not link_matches:
            # на всякий случай — data-auto до href
            link_matches = re.findall(r'data-auto="snippet-link"[^>]*href="([^"]+)"', html)
        if not link_matches:
            link_matches = re.findall(r'href="(/card/[^"]+)"', html)
        link_matches = list(dict.fromkeys(l.replace('&amp;', '&') for l in link_matches))

        print(f"[ЯМ DEBUG] стр.{page_num}: найдено titles={len(title_matches)}, links={len(link_matches)}, prices={len(price_matches)}")

        if title_matches and link_matches:
            for i, title in enumerate(title_matches[:50]):
                title = title.strip()
                if len(title) < 3:
                    continue
                path = link_matches[i] if i < len(link_matches) else ""
                full_url = f"https://market.yandex.ru{path}" if path.startswith("/") else path
                price = _parse_price(price_matches[i]) if i < len(price_matches) else None
                if _apply_filters(title, price, filters):
                    products.append(ProductCard(
                        title=title, price=price, currency="₽",
                        url=full_url, source="Яндекс.Маркет",
                    ))
            if products:
                print(f"[ЯМ DEBUG] стр.{page_num}: regex дал {len(products)} товаров")
                return products
        elif title_matches and not link_matches:
            # Есть названия но нет ссылок — создаём без ссылок, хотя бы товары
            print(f"[ЯМ DEBUG] стр.{page_num}: ссылки не найдены, создаём товары без URL")
            search_url = f"https://market.yandex.ru/search?text={quote_plus(title_matches[0].split()[0])}"
            for i, title in enumerate(title_matches[:50]):
                title = title.strip()
                if len(title) < 3:
                    continue
                price = _parse_price(price_matches[i]) if i < len(price_matches) else None
                if _apply_filters(title, price, filters):
                    products.append(ProductCard(
                        title=title, price=price, currency="₽",
                        url=search_url, source="Яндекс.Маркет",
                    ))
            if products:
                print(f"[ЯМ DEBUG] стр.{page_num}: без-url режим дал {len(products)} товаров")
                return products

        # Последний резерв — ld+json и html-паттерны
        ld_blocks = re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL
        )
        for block in ld_blocks:
            try:
                obj = json.loads(block)
                if obj.get("@type") == "ItemList":
                    for el in obj.get("itemListElement", []):
                        item = el.get("item", el)
                        p = _ld_json_to_product(item, "Яндекс.Маркет", filters)
                        if p:
                            products.append(p)
            except Exception:
                continue

        if not products:
            products = _parse_html_products(
                html, filters,
                link_pattern=r'href="(/product--[^"?#]+)"',
                price_pattern=r'(\d[\d\s]{3,8})\s*₽',
                title_pattern=r'aria-label="([^"]{10,120})"',
                base_url="https://market.yandex.ru",
                source="Яндекс.Маркет",
            )

        if products:
            print(f"[ЯМ DEBUG] стр.{page_num}: резервные стратегии дали {len(products)} товаров")
        else:
            print(f"[ЯМ DEBUG] стр.{page_num}: все стратегии провалились")

    except Exception as e:
        print(f"[ЯМ] parse error стр.{page_num}: {type(e).__name__}: {e}")

    return products


# ===========================================================================
# Google Shopping — подстраховка
# ===========================================================================

async def _search_google_shopping(query: str, filters: dict) -> list[ProductCard]:
    payload = {
        "source": "google_shopping_search",
        "query": query,
        "parse": True,
        "context": [{"key": "sort_by", "value": "p"}],
        "pages": 3,  # несколько страниц для большего охвата
    }

    data = await _oxylabs_request(payload)
    if not data:
        return []

    products: list[ProductCard] = []
    try:
        organic = data["results"][0]["content"]["results"]["organic"]
    except (KeyError, IndexError, TypeError):
        print("[Google Shopping] Неожиданная структура ответа")
        return []

    for item in organic:
        try:
            title = item.get("title", "")
            if not title:
                continue

            price = _parse_price(item.get("price_str") or item.get("price"))
            if price and price < 2000:
                price = round(price * 90, 2)

            url = (
                item.get("url")
                or item.get("merchant", {}).get("url")
                or f"https://www.google.com/search?q={quote_plus(query)}+купить"
            )
            if "google.com/search?ibp" in url:
                url = f"https://www.google.com/search?q={quote_plus(query)}+купить"

            if _apply_filters(title, price, filters):
                products.append(ProductCard(
                    title=title, price=price, currency="₽",
                    rating=_safe_float(item.get("rating")),
                    reviews_count=_safe_int(item.get("reviews_count")),
                    seller=(item.get("merchant") or {}).get("name"),
                    url=url, source="Google Shopping",
                ))
        except Exception as e:
            print(f"[Google Shopping] item error: {e}")

    print(f"[Google Shopping] Найдено товаров: {len(products)}")
    return products


# ===========================================================================
# fetch_product_details
# ===========================================================================

async def fetch_product_details(url: str) -> dict:
    if not url or "google.com" in url:
        return {}

    payload = {"source": "universal", "url": url, "render": "html"}
    data = await _oxylabs_request(payload)
    if not data:
        return {}

    try:
        html = data["results"][0]["content"]
        if not isinstance(html, str):
            return {}

        details = {}

        for block in re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.DOTALL
        ):
            try:
                obj = json.loads(block)
                if obj.get("@type") == "Product":
                    details["Название"] = obj.get("name", "")
                    offers = obj.get("offers", {})
                    if isinstance(offers, dict):
                        details["Цена"] = _parse_price(offers.get("price"))
                        details["Продавец"] = (
                            offers.get("seller") or {}
                        ).get("name", "")
                    details["Рейтинг"] = _safe_float(
                        obj.get("aggregateRating", {}).get("ratingValue")
                    )
                    break
            except Exception:
                continue

        if not details.get("Цена"):
            m = re.search(r'(\d[\d\s]{3,9})\s*₽', html)
            if m:
                details["Цена"] = _parse_price(m.group(1))
        if not details.get("Продавец"):
            m = re.search(r'(?:Продавец|Seller)[:\s]+<[^>]+>([^<]{2,60})<', html)
            if m:
                details["Продавец"] = m.group(1).strip()
        m = re.search(
            r'(?:Доставка|delivery)[:\s"]+([^<"]{5,80})', html, re.IGNORECASE
        )
        if m:
            details["Доставка"] = m.group(1).strip()

        return details
    except Exception as e:
        print(f"[fetch_details] error: {e}")
        return {}


# ===========================================================================
# Вспомогательные утилиты
# ===========================================================================

async def _oxylabs_request(payload: dict) -> dict | None:
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, verify=False
    ) as client:
        try:
            r = await client.post(
                OXYLABS_URL, json=payload,
                auth=(OXYLABS_USERNAME, OXYLABS_PASSWORD),
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            print(f"[Oxylabs] HTTP {e.response.status_code}: {e.response.text[:200]}")
        except Exception as e:
            print(f"[Oxylabs] {type(e).__name__}: {e}")
    return None


def _build_query(query: str, filters: dict) -> str:
    q = query
    if filters.get("storage"):
        s = (
            str(filters["storage"])
            .lower()
            .replace("gb", "")
            .replace("гб", "")
            .strip()
        )
        if s not in q.lower():
            q += f" {s}gb"
    if filters.get("brand") and filters["brand"].lower() not in q.lower():
        q = f"{filters['brand']} {q}"
    return q


def _parse_price(value) -> float | None:
    if value is None:
        return None
    try:
        clean = re.sub(
            r'[^\d.]', '',
            str(value)
            .replace(",", ".")
            .replace("\xa0", "")
            .replace(" ", ""),
        )
        if not clean:
            return None
        price = float(clean)
        return price if price > 0 else None
    except (ValueError, TypeError):
        return None


def _apply_filters(title: str, price: float | None, filters: dict) -> bool:
    tl = title.lower()

    storage = filters.get("storage")
    if storage:
        s = str(storage).lower().replace("гб", "gb").replace(" ", "")
        t = tl.replace("гб", "gb").replace(" ", "")
        if s not in t:
            return False

    max_price = filters.get("max_price")
    if max_price and price is not None:
        try:
            if price > float(str(max_price).replace(" ", "")):
                return False
        except ValueError:
            pass

    condition = filters.get("condition", "")
    if condition and condition.lower() in ("новый", "new"):
        if any(
            kw in tl
            for kw in ["б/у", "бу ", "восстановл", "refurbished", "used"]
        ):
            return False

    return True


def _ld_json_to_product(
    item: dict, source: str, filters: dict
) -> ProductCard | None:
    title = item.get("name", "")
    if not title:
        return None
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price_raw = (
        offers.get("price") or offers.get("lowPrice") or item.get("price")
    )
    price = _parse_price(price_raw)
    currency = offers.get("priceCurrency", "RUB")
    if currency == "USD" and price and price < 5000:
        price = round(price * 90, 2)
    url = item.get("url", offers.get("url", ""))
    if not _apply_filters(title, price, filters):
        return None
    return ProductCard(
        title=title, price=price, currency="₽",
        rating=_safe_float(
            (item.get("aggregateRating") or {}).get("ratingValue")
        ),
        reviews_count=_safe_int(
            (item.get("aggregateRating") or {}).get("reviewCount")
        ),
        seller=(
            (offers.get("seller") or {}).get("name")
            if isinstance(offers.get("seller"), dict)
            else None
        ),
        url=url, source=source,
    )


def _deep_find_products(
    data, required_keys: set, alt_keys: set, _depth=0
) -> list[dict]:
    if _depth > 15:
        return []
    found = []
    if isinstance(data, dict):
        has_required = required_keys & set(data.keys())
        has_alt = alt_keys & set(data.keys())
        if has_required or (len(has_alt) >= 2):
            found.append(data)
        for v in data.values():
            found.extend(
                _deep_find_products(v, required_keys, alt_keys, _depth + 1)
            )
    elif isinstance(data, list):
        for v in data:
            found.extend(
                _deep_find_products(v, required_keys, alt_keys, _depth + 1)
            )
    return found


def _parse_html_products(
    html: str, filters: dict,
    link_pattern: str, price_pattern: str, title_pattern: str,
    base_url: str, source: str,
) -> list[ProductCard]:
    links = list(dict.fromkeys(re.findall(link_pattern, html)))
    prices = [_parse_price(p) for p in re.findall(price_pattern, html)]
    titles = re.findall(title_pattern, html)

    products = []
    for i, path in enumerate(links[:50]):  # увеличили до 50
        title = titles[i].strip() if i < len(titles) else f"Товар {i+1}"
        title = re.sub(r'\s+', ' ', title)
        if len(title) < 5:
            continue
        price = prices[i] if i < len(prices) else None
        full_url = f"{base_url}{path}" if path.startswith("/") else path
        if _apply_filters(title, price, filters):
            products.append(ProductCard(
                title=title, price=price, currency="₽",
                url=full_url, source=source,
            ))
    return products


def _safe_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _safe_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


# ===========================================================================
# Демо-данные (последний fallback)
# ===========================================================================

def _demo_products(query: str, filters: dict) -> list[ProductCard]:
    brand = filters.get("brand", "")
    storage = filters.get("storage", "128")
    base = f"{brand} {query}".strip() if brand else query
    return [
        ProductCard(
            title=f"{base} {storage}GB (новый)", price=89990.0, currency="₽",
            rating=4.7, reviews_count=1243,
            url="https://market.yandex.ru/search?text=" + quote_plus(query),
            source="[ДЕМО] Яндекс.Маркет", seller="ООО ТехноМаркет",
        ),
        ProductCard(
            title=f"{base} {storage}GB Black", price=92500.0, currency="₽",
            rating=4.5, reviews_count=876,
            url="https://market.yandex.ru/search?text=" + quote_plus(query),
            source="[ДЕМО] Яндекс.Маркет", seller="Apple Premium",
        ),
        ProductCard(
            title=f"{base} {storage}GB White", price=94000.0, currency="₽",
            rating=4.6, reviews_count=543,
            url="https://market.yandex.ru/search?text=" + quote_plus(query),
            source="[ДЕМО] Яндекс.Маркет", seller="iStore",
        ),
    ]