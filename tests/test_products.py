from app.coinbase.products import canonicalize_spot_products


def test_canonicalize_spot_products_prefers_usd_and_excludes_stables_disabled():
    raw = [
        {"product_id": "XYZ-USDC", "base_currency_id": "XYZ", "quote_currency_id": "USDC", "product_type": "SPOT", "status": "ONLINE"},
        {"product_id": "XYZ-USD", "base_currency_id": "XYZ", "quote_currency_id": "USD", "product_type": "SPOT", "status": "ONLINE"},
        {"product_id": "USDT-USDC", "base_currency_id": "USDT", "quote_currency_id": "USDC", "product_type": "SPOT", "status": "ONLINE"},
        {"product_id": "BAD-USD", "base_currency_id": "BAD", "quote_currency_id": "USD", "product_type": "SPOT", "trading_disabled": True},
        {"product_id": "PERP-USD", "base_currency_id": "PERP", "quote_currency_id": "USD", "product_type": "FUTURE", "status": "ONLINE"},
    ]

    products = canonicalize_spot_products(raw)

    assert [product.product_id for product in products] == ["XYZ-USD"]
