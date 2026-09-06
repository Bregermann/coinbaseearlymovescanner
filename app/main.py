from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.analysis.scoring import primary_score
from app.config import get_settings
from app.database.connection import init_db, session_scope
from app.database.repositories import StatusRepository
from app.logging import configure_logging
from app.scanner import ScannerService

settings = get_settings()
configure_logging(settings.log_level)
scanner = ScannerService(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scanner.start()
    try:
        yield
    finally:
        await scanner.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/status")
async def status() -> dict[str, object]:
    await init_db(settings)
    async with session_scope(settings) as session:
        values = await StatusRepository(session).all_status()
    values["runtime"] = {
        "products": len(scanner.products),
        "latest_quotes": len(scanner.latest_quotes),
        "ws_connected_chunks": scanner.ws_client.connected_chunks if scanner.ws_client else 0,
    }
    return values


@app.get("/candidates")
async def candidates() -> list[dict[str, object]]:
    ranked = await scanner.scan_once(persist_and_alert=False)
    return [
        {
            "symbol": item.symbol,
            "product_id": item.product_id,
            "status": item.status.value,
            "price": item.quote.price,
            "score": primary_score(item),
            "risk_adjusted_opportunity_score": primary_score(item),
            "early_move_score": item.score.early_move_score,
            "quote_age_seconds": item.quote.quote_age_seconds,
            "reasons": item.score.reasons,
        }
        for item in ranked
    ]


def main() -> None:
    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=False)


if __name__ == "__main__":
    main()
