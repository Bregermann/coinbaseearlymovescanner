from datetime import datetime, timezone

from app.config import Settings
from app.database.connection import close_db, init_db, session_scope
from app.database.repositories import MarketRepository
from app.types import CandlePoint


async def test_bulk_candle_upsert_updates_existing_row(tmp_path):
    database = tmp_path / "scanner-test.db"
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database.as_posix()}")
    start = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    first = CandlePoint("TEST-USD", start, 3600, 1.0, 1.1, 0.9, 1.0, 100.0, 100.0)
    updated = CandlePoint("TEST-USD", start, 3600, 1.0, 1.2, 0.8, 1.1, 200.0, 220.0)
    try:
        await init_db(settings)
        async with session_scope(settings) as session:
            assert await MarketRepository(session).upsert_candles([first], source="test") == 1
        async with session_scope(settings) as session:
            assert await MarketRepository(session).upsert_candles([updated], source="test") == 1
        async with session_scope(settings) as session:
            rows = await MarketRepository(session).recent_candles("TEST-USD", start)
        assert len(rows) == 1
        assert rows[0].high == 1.2
        assert rows[0].close == 1.1
        assert rows[0].quote_volume == 220.0
        async with session_scope(settings) as session:
            repo = MarketRepository(session)
            earliest, latest = await repo.candle_bounds("TEST-USD", 3600)
            await repo.mark_history_bootstrap_completed("TEST-USD", 3600, earliest, latest)
        async with session_scope(settings) as session:
            assert await MarketRepository(session).history_bootstrap_completed("TEST-USD", 3600)
    finally:
        await close_db()
