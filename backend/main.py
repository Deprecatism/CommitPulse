import asyncio
import hmac
import os
import subprocess
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Annotated, Literal, cast

from fastapi import FastAPI, Header, HTTPException, Path, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketDisconnect

if __package__:
    from .docker_monitor import DockerMetricsMonitor
    from .live_updates import LiveUpdateHub
    from .metrics_store import MetricsStore, SystemMetricsReport
    from .performance_monitor import SystemMetricsMonitor
    from .project_monitor import ProjectMetricsMonitor
    from .projects_api import configure as configure_projects
    from .projects_api import router as projects_router
else:
    from docker_monitor import DockerMetricsMonitor
    from live_updates import LiveUpdateHub
    from metrics_store import MetricsStore, SystemMetricsReport
    from performance_monitor import SystemMetricsMonitor
    from project_monitor import ProjectMetricsMonitor
    from projects_api import configure as configure_projects
    from projects_api import router as projects_router


def _env_flag(name: str, default: str) -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"{name} must be a boolean value")
    return value in {"1", "true", "yes", "on"}

metrics_store = MetricsStore(
    database_path=os.getenv("METRICS_DB_PATH"),
)
metrics_interval_seconds = float(os.getenv("METRICS_INTERVAL_SECONDS", "5"))
metrics_polling_enabled = _env_flag("METRICS_POLLING_ENABLED", "true")
docker_monitoring_enabled = _env_flag("DOCKER_MONITORING_ENABLED", "true")
docker_interval_seconds = float(
    os.getenv("DOCKER_INTERVAL_SECONDS", str(metrics_interval_seconds))
)
docker_collect_disk_usage = _env_flag("DOCKER_COLLECT_DISK_USAGE", "false")
project_monitoring_enabled = _env_flag("PROJECT_MONITORING_ENABLED", "true")
project_interval_seconds = float(
    os.getenv("PROJECT_INTERVAL_SECONDS", str(metrics_interval_seconds))
)
project_allow_launch = _env_flag("PROJECTS_ALLOW_LAUNCH", "false")
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


def current_commit_metadata() -> tuple[str, str, str | None]:
    git_commit = git_value("log", "-1", "--format=%H%n%s")
    git_sha, _, git_message = git_commit.partition("\n") if git_commit else ("", "", "")
    return (
        os.getenv("METRICS_COMMIT_SHA") or git_sha or "unknown",
        os.getenv("METRICS_COMMIT_MESSAGE")
        or git_message
        or "Local backend monitoring",
        os.getenv("METRICS_BRANCH") or git_value("branch", "--show-current"),
    )


def current_repository_name() -> str | None:
    override = os.getenv("METRICS_REPOSITORY_NAME")
    repository_root = git_value("rev-parse", "--show-toplevel")
    return override or (FilePath(repository_root).name if repository_root else None)


def commits_in_current_repository(commit_shas: list[str]) -> set[str] | None:
    if not commit_shas:
        return set()
    try:
        result = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
            cwd=FilePath(__file__).resolve().parent.parent,
            input="\n".join(commit_shas),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {
        object_name
        for line in result.stdout.splitlines()
        if len(parts := line.rsplit(" ", 1)) == 2
        for object_name, object_type in [parts]
        if object_type == "commit"
    }


def broadcast_local_sample(sample: dict[str, object]) -> None:
    live_updates.broadcast_from_thread({"type": "metrics.updated", "data": sample})


metrics_monitor = SystemMetricsMonitor(
    metrics_store,
    interval_seconds=metrics_interval_seconds,
    commit_metadata_provider=current_commit_metadata,
    on_sample=broadcast_local_sample,
    repository_name=current_repository_name(),
)

docker_monitor = DockerMetricsMonitor(
    metrics_store,
    interval_seconds=docker_interval_seconds,
    on_sample=broadcast_local_sample,
    collect_disk_usage=docker_collect_disk_usage,
)

project_monitor = ProjectMetricsMonitor(
    metrics_store,
    interval_seconds=project_interval_seconds,
    on_sample=broadcast_local_sample,
    config_path=os.getenv("PROJECTS_CONFIG_PATH"),
    allow_launch=project_allow_launch,
)
configure_projects(project_monitor)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    live_updates.bind_event_loop(asyncio.get_running_loop())
    metrics_store.initialize()
    if metrics_polling_enabled:
        metrics_monitor.start()
    if docker_monitoring_enabled:
        docker_monitor.start()
    if project_monitoring_enabled:
        project_monitor.load()
        project_monitor.seed_from_env(os.getenv("TRACKED_PROJECTS"))
        project_monitor.start()
    try:
        yield
    finally:
        if metrics_polling_enabled:
            metrics_monitor.stop()
        if docker_monitoring_enabled:
            docker_monitor.stop()
        if project_monitoring_enabled:
            project_monitor.stop()


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

app.include_router(projects_router)


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
    source: Annotated[
        Literal["local", "reported", "docker", "project"] | None, Query()
    ] = None,
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
    source: Annotated[
        Literal["local", "reported", "docker", "project"] | None, Query()
    ] = None,
) -> dict[str, object]:
    dashboard = metrics_store.dashboard(system_id, limit, source)
    if system_id == "backend-local" and source == "local":
        commits_value = dashboard["commits"]
        if isinstance(commits_value, list):
            commits = cast(list[dict[str, object]], commits_value)
            commit_shas = [
                str(commit["commit_sha"])
                for commit in commits
                if "commit_sha" in commit
            ]
            valid_shas = commits_in_current_repository(commit_shas)
            if valid_shas is not None:
                valid_commits: list[dict[str, object]] = [
                    commit
                    for commit in commits
                    if str(commit.get("commit_sha")) in valid_shas
                ]
                for index, commit in enumerate(valid_commits):
                    commit["previous"] = (
                        valid_commits[index + 1]["current"]
                        if index + 1 < len(valid_commits)
                        else None
                    )
                dashboard["commits"] = valid_commits
                dashboard["current"] = (
                    valid_commits[0]["current"] if valid_commits else None
                )
                dashboard["previous"] = (
                    valid_commits[1]["current"] if len(valid_commits) > 1 else None
                )
    return dashboard


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
