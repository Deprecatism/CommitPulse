"""REST API for managing tracked local repositories.

All routes live under ``/api/projects`` and operate on a shared
``ProjectMetricsMonitor`` supplied via :func:`configure`.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

if __package__:
    from .project_monitor import ProjectMetricsMonitor
else:
    from project_monitor import ProjectMetricsMonitor

router = APIRouter(prefix="/api/projects", tags=["projects"])

_monitor: ProjectMetricsMonitor | None = None

IdPath = Annotated[
    str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=1024)
    command: str | None = Field(default=None, max_length=2048)
    match_cwd: bool = True
    match_cmdline: bool = False


def configure(monitor: ProjectMetricsMonitor) -> None:
    global _monitor
    _monitor = monitor


def _get_monitor() -> ProjectMetricsMonitor:
    if _monitor is None:
        raise HTTPException(status_code=503, detail="Project monitoring is not enabled.")
    return _monitor


@router.get("")
def list_projects() -> dict[str, object]:
    monitor = _get_monitor()
    return {
        "launch_enabled": monitor.allow_launch,
        "projects": [monitor.status(project) for project in monitor.list()],
    }


@router.post("", status_code=201)
def create_project(body: ProjectCreate) -> dict[str, object]:
    monitor = _get_monitor()
    try:
        project = monitor.add(
            name=body.name,
            path=body.path,
            command=body.command,
            match_cwd=body.match_cwd,
            match_cmdline=body.match_cmdline,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return monitor.status(project)


@router.get("/{project_id}")
def get_project(project_id: IdPath) -> dict[str, object]:
    monitor = _get_monitor()
    project = monitor.get(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return monitor.status(project)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: IdPath) -> None:
    monitor = _get_monitor()
    if not monitor.remove(project_id):
        raise HTTPException(status_code=404, detail="Project not found")


@router.post("/{project_id}/launch", status_code=202)
def launch_project(project_id: IdPath) -> dict[str, object]:
    monitor = _get_monitor()
    try:
        monitor.launch(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Project not found") from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except (ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    project = monitor.get(project_id)
    return monitor.status(project) if project else {"status": "launched"}


@router.post("/{project_id}/terminate", status_code=202)
def terminate_project(project_id: IdPath) -> dict[str, object]:
    monitor = _get_monitor()
    if monitor.get(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    stopped = monitor.terminate(project_id)
    return {"status": "terminated" if stopped else "not_running"}
