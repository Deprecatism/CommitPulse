import asyncio
import hmac
import os
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Path, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect

if __package__:
    from .live_updates import LiveUpdateHub
    from .metrics_store import MetricsStore, SystemMetricsReport
    from .performance_monitor import SystemMetricsMonitor
else:
    from live_updates import LiveUpdateHub
    from metrics_store import MetricsStore, SystemMetricsReport
    from performance_monitor import SystemMetricsMonitor

metrics_store = MetricsStore(
    database_path=os.getenv("METRICS_DB_PATH"),
)
metrics_interval_seconds = float(os.getenv("METRICS_INTERVAL_SECONDS", "5"))
metrics_polling_setting = os.getenv("METRICS_POLLING_ENABLED", "true").strip().lower()
if metrics_polling_setting not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
    raise ValueError("METRICS_POLLING_ENABLED must be a boolean value")
metrics_polling_enabled = metrics_polling_setting in {"1", "true", "yes", "on"}
webhook_secret = os.getenv("METRICS_WEBHOOK_SECRET")
live_updates = LiveUpdateHub()


def git_value(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=FilePath(__file__).resolve().parent.parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def broadcast_local_sample(sample: dict[str, object]) -> None:
    live_updates.broadcast_from_thread({"type": "metrics.updated", "data": sample})


metrics_monitor = SystemMetricsMonitor(
    metrics_store,
    interval_seconds=metrics_interval_seconds,
    commit_sha=os.getenv("METRICS_COMMIT_SHA")
    or git_value("rev-parse", "HEAD")
    or "unknown",
    commit_message=os.getenv("METRICS_COMMIT_MESSAGE")
    or git_value("log", "-1", "--format=%s")
    or "Local backend monitoring",
    branch=os.getenv("METRICS_BRANCH") or git_value("branch", "--show-current"),
    on_sample=broadcast_local_sample,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    live_updates.bind_event_loop(asyncio.get_running_loop())
    metrics_store.initialize()
    if metrics_polling_enabled:
        metrics_monitor.start()
    try:
        yield
    finally:
        if metrics_polling_enabled:
            metrics_monitor.stop()


app = FastAPI(title="CommitPulse API", version="1.0.0", lifespan=lifespan)

frontend_origins = [
    origin.strip()
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:4321,http://127.0.0.1:4321",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "commitpulse-api"}


@app.get("/api/systems")
def systems() -> list[dict[str, object]]:
    return metrics_store.systems()


async def save_report(system_id: str, report: SystemMetricsReport) -> dict[str, object]:
    saved = metrics_store.save(system_id, report, source="reported")
    await live_updates.broadcast({"type": "metrics.updated", "data": saved})
    return saved


@app.post("/api/systems/{system_id}/metrics", status_code=201)
async def report_metrics(
    system_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    report: SystemMetricsReport,
    webhook_token: Annotated[str | None, Header(alias="X-Webhook-Token")] = None,
) -> dict[str, object]:
    if webhook_secret and not hmac.compare_digest(webhook_token or "", webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook token")
    return await save_report(system_id, report)


@app.post("/api/webhooks/metrics/{system_id}", status_code=201)
async def metrics_webhook(
    system_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    report: SystemMetricsReport,
    webhook_token: Annotated[str | None, Header(alias="X-Webhook-Token")] = None,
) -> dict[str, object]:
    if webhook_secret and not hmac.compare_digest(webhook_token or "", webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook token")
    return await save_report(system_id, report)


@app.get("/api/systems/{system_id}/metrics")
def metrics_history(
    system_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    source: Annotated[Literal["local", "reported"] | None, Query()] = None,
) -> dict[str, object]:
    history = metrics_store.history(system_id, limit, source)
    return {
        "system_id": system_id,
        "current": history[-1] if history else None,
        "history": history,
    }


@app.get("/api/systems/{system_id}/dashboard")
def system_dashboard(
    system_id: Annotated[
        str,
        Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    source: Annotated[Literal["local", "reported"] | None, Query()] = None,
) -> dict[str, object]:
    return metrics_store.dashboard(system_id, limit, source)


@app.websocket("/api/live")
async def live_metrics(websocket: WebSocket) -> None:
    await live_updates.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        live_updates.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
