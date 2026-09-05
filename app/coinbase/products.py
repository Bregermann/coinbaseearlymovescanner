from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from app.types import ProductInfo

STABLE_ASSETS = {"USD", "USDC", "USDT", "DAI", "PYUSD", "EURC", "GUSD", "USDP", "TUSD"}
SUPPORTED_QUOTES = {"USD", "USDC"}


def parse_product(raw: dict[str, Any]) -> ProductInfo | None:
    product_id = str(raw.get("product_id") or raw.get("productId") or "").upper()
    if not product_id or "-" not in product_id:
        return None
    base_from_id, quote_from_id = product_id.rsplit("-", 1)
    base = str(raw.get("base_currency_id") or raw.get("base_currency") or base_from_id).upper()
    quote = str(raw.get("quote_currency_id") or raw.get("quote_currency") or quote_from_id).upper()
    price = _to_float(raw.get("price"))
    volume_24h = _to_float(raw.get("volume_24h") or raw.get("volume_24hour"))
    quote_volume_24h = price * volume_24h if price is not None and volume_24h is not None else None
    return ProductInfo(
        product_id=product_id,
        base_currency=base,
        quote_currency=quote,
        price=price,
        volume_24h=volume_24h,
        quote_volume_24h=quote_volume_24h,
        status=str(raw.get("status") or raw.get("trading_status") or ""),
        trading_disabled=_truthy(raw.get("trading_disabled") or raw.get("is_disabled")),
        cancel_only=_truthy(raw.get("cancel_only")),
        limit_only=_truthy(raw.get("limit_only")),
        post_only=_truthy(raw.get("post_only")),
        raw=raw,
    )


def canonicalize_spot_products(raw_products: Iterable[dict[str, Any]]) -> list[ProductInfo]:
    by_asset: dict[str, list[ProductInfo]] = defaultdict(list)
    for raw in raw_products:
        if str(raw.get("product_type") or raw.get("productType") or "SPOT").upper() != "SPOT":
            continue
        product = parse_product(raw)
        if product is None or not is_qualifying_product(product):
            continue
        by_asset[product.base_currency].append(product)

    selected: list[ProductInfo] = []
    for asset, products in by_asset.items():
        products.sort(key=_product_preference)
        selected.append(products[0])
    selected.sort(key=lambda item: item.base_currency)
    return selected


def is_qualifying_product(product: ProductInfo) -> bool:
    if product.quote_currency not in SUPPORTED_QUOTES:
        return False
    if product.base_currency in STABLE_ASSETS and product.quote_currency in STABLE_ASSETS:
        return False
    if product.trading_disabled or product.cancel_only:
        return False
    status = (product.status or "").upper()
    if status and status not in {"ONLINE", "TRADING", "ACTIVE", ""}:
        return False
    return True


def _product_preference(product: ProductInfo) -> tuple[int, int, str]:
    quote_rank = 0 if product.quote_currency == "USD" else 1
    limit_rank = 1 if product.limit_only or product.post_only else 0
    return (quote_rank, limit_rank, product.product_id)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).lower() in {"true", "1", "yes"}


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
