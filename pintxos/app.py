"""FastAPI application."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from pintxos.db import db, init_db
from pintxos.poll import scheduler, start_scheduler

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # ponytail: env flag so TestClient/CI don't spawn a real poller thread.
    if os.environ.get("PINTXOS_NO_SCHEDULER") != "1":
        start_scheduler()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="pintxos", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    with db() as conn:
        feeds = conn.execute("SELECT COUNT(*) AS n FROM feeds").fetchone()["n"]
    return {"ok": True, "feeds": feeds}
