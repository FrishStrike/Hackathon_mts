from models.schemas import ProductCard, ParsedQuery
import re


def validate_product(product: ProductCard, parsed: ParsedQuery) -> tuple[bool, list[str]]:
    """
    Проверяет соответствие найденного товара пользовательским ограничениям.
    Возвращает (is_valid, list_of_issues).
    """
    issues = []
    filters = parsed.product_filters
    title_lower = product.title.lower()

    # Проверка объёма памяти
    storage = filters.get("storage")
    if storage:
        storage_normalized = storage.lower().replace(" ", "").replace("гб", "gb")
        title_normalized = title_lower.replace(" ", "").replace("гб", "gb")
        if storage_normalized not in title_normalized and storage.lower() not in title_lower:
            issues.append(f"Объём памяти {storage} не найден в названии товара")

    # Проверка состояния (новый/б.у.)
    condition = filters.get("condition")
    if condition:
        if condition.lower() in ("новый", "new"):
            bad_keywords = ["б/у", "бу", "восстановленный", "refurbished", "used"]
            if any(kw in title_lower for kw in bad_keywords):
                issues.append("Товар может быть б/у, хотя требуется новый")

    # Проверка бренда
    brand = filters.get("brand")
    if brand and brand.lower() not in title_lower:
        issues.append(f"Бренд '{brand}' не найден в названии товара")

    # Проверка максимальной цены
    max_price = filters.get("max_price")
    if max_price and product.price:
        try:
            if product.price > float(str(max_price).replace(" ", "")):
                issues.append(f"Цена {product.price}₽ превышает максимум {max_price}₽")
        except ValueError:
            pass

    return len(issues) == 0, issues


def find_cheapest(products: list[ProductCard], parsed: ParsedQuery) -> ProductCard | None:
    """Находит самый дешёвый товар, прошедший валидацию."""
    valid_products = []

    for product in products:
        is_valid, issues = validate_product(product, parsed)
        if is_valid and product.price is not None:
            valid_products.append(product)

    if not valid_products:
        # Если строгая валидация ничего не нашла — берём просто с ценой
        valid_products = [p for p in products if p.price is not None]

    if not valid_products:
        return products[0] if products else None

    return min(valid_products, key=lambda p: p.price)
