"""GitHub Actions dashboard API.

This router is intentionally self-contained: every route lives under the
``/api/github`` prefix so it never overlaps with the metrics endpoints in
``main.py``. All calls to GitHub are proxied through the backend so the
personal access token never has to reach the browser.

Configure it with environment variables:

- ``GITHUB_TOKEN`` (or ``GH_TOKEN``): token used for authenticated requests.
  Public repositories work without one, but private repositories and the
  re-run/cancel/dispatch actions require a token with the ``actions`` scope.
- ``GITHUB_REPOSITORY`` (or ``GITHUB_DEFAULT_REPO``): default ``owner/name``
  repository shown when the client does not pass ``?repo=``.
"""

import os
import re
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

GITHUB_API_BASE = "https://api.github.com"
REPO_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
REQUEST_TIMEOUT_SECONDS = 15.0

router = APIRouter(prefix="/api/github", tags=["github"])

RepoQuery = Annotated[str | None, Query(max_length=140, pattern=REPO_PATTERN)]
IdPath = Annotated[int, Path(ge=1)]


class DispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1, max_length=255)
    inputs: dict[str, str] | None = Field(default=None)


def _token() -> str | None:
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def _default_repo() -> str | None:
    repo = os.getenv("GITHUB_REPOSITORY") or os.getenv("GITHUB_DEFAULT_REPO")
    return repo if repo and re.match(REPO_PATTERN, repo) else None


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "CommitPulse-Actions",
    }
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _resolve_repo(repo: str | None) -> str:
    resolved = repo or _default_repo()
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail=(
                "No repository selected. Provide ?repo=owner/name or set the "
                "GITHUB_REPOSITORY environment variable."
            ),
        )
    if not re.match(REPO_PATTERN, resolved):
        raise HTTPException(
            status_code=400, detail="Repository must use the 'owner/name' form."
        )
    return resolved


def _require_token() -> None:
    if not _token():
        raise HTTPException(
            status_code=401,
            detail=(
                "This action requires a GITHUB_TOKEN with the 'actions' scope "
                "configured on the backend."
            ),
        )


async def _github_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
) -> Any:
    url = f"{GITHUB_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.request(
                method, url, headers=_headers(), params=params, json=json
            )
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502, detail=f"Could not reach GitHub: {error}"
        ) from error

    if response.status_code == 401:
        raise HTTPException(
            status_code=502,
            detail="GitHub rejected the request. Check the configured GITHUB_TOKEN.",
        )
    if (
        response.status_code == 403
        and response.headers.get("X-RateLimit-Remaining") == "0"
    ):
        raise HTTPException(
            status_code=429,
            detail=(
                "GitHub API rate limit reached. Set GITHUB_TOKEN to raise the "
                "limit."
            ),
        )
    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=(
                "Repository or resource not found. Private repositories require "
                "a GITHUB_TOKEN with access."
            ),
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub error {response.status_code}: {response.text[:200]}",
        )
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def _serialize_run(run: dict[str, Any]) -> dict[str, Any]:
    actor = run.get("actor") or {}
    return {
        "id": run.get("id"),
        "name": run.get("name"),
        "display_title": run.get("display_title") or run.get("name"),
        "run_number": run.get("run_number"),
        "run_attempt": run.get("run_attempt"),
        "event": run.get("event"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "head_branch": run.get("head_branch"),
        "head_sha": run.get("head_sha"),
        "workflow_id": run.get("workflow_id"),
        "actor_login": actor.get("login"),
        "actor_avatar_url": actor.get("avatar_url"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "run_started_at": run.get("run_started_at"),
        "html_url": run.get("html_url"),
    }


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "html_url": job.get("html_url"),
    }


@router.get("/config")
def github_config() -> dict[str, Any]:
    return {"authenticated": bool(_token()), "default_repo": _default_repo()}


@router.get("/workflows")
async def list_workflows(repo: RepoQuery = None) -> dict[str, Any]:
    resolved = _resolve_repo(repo)
    data = await _github_request(
        "GET", f"/repos/{resolved}/actions/workflows", params={"per_page": 100}
    )
    workflows = [
        {
            "id": workflow.get("id"),
            "name": workflow.get("name"),
            "path": workflow.get("path"),
            "state": workflow.get("state"),
            "badge_url": workflow.get("badge_url"),
            "html_url": workflow.get("html_url"),
        }
        for workflow in data.get("workflows", [])
    ]
    return {
        "repo": resolved,
        "total_count": data.get("total_count", len(workflows)),
        "workflows": workflows,
    }


@router.get("/runs")
async def list_runs(
    repo: RepoQuery = None,
    workflow_id: Annotated[int | None, Query(ge=1)] = None,
    branch: Annotated[str | None, Query(max_length=255)] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    per_page: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    resolved = _resolve_repo(repo)
    params: dict[str, Any] = {"per_page": per_page}
    if branch:
        params["branch"] = branch
    if status:
        params["status"] = status
    path = (
        f"/repos/{resolved}/actions/runs"
        if workflow_id is None
        else f"/repos/{resolved}/actions/workflows/{workflow_id}/runs"
    )
    data = await _github_request("GET", path, params=params)
    runs = [_serialize_run(run) for run in data.get("workflow_runs", [])]
    return {
        "repo": resolved,
        "workflow_id": workflow_id,
        "total_count": data.get("total_count", len(runs)),
        "runs": runs,
    }


@router.get("/runs/{run_id}/jobs")
async def list_jobs(run_id: IdPath, repo: RepoQuery = None) -> dict[str, Any]:
    resolved = _resolve_repo(repo)
    data = await _github_request(
        "GET", f"/repos/{resolved}/actions/runs/{run_id}/jobs", params={"per_page": 100}
    )
    jobs = [_serialize_job(job) for job in data.get("jobs", [])]
    return {"run_id": run_id, "total_count": data.get("total_count", len(jobs)), "jobs": jobs}


@router.post("/workflows/{workflow_id}/dispatch", status_code=202)
async def dispatch_workflow(
    workflow_id: IdPath,
    body: Annotated[DispatchRequest, Body()],
    repo: RepoQuery = None,
) -> dict[str, Any]:
    _require_token()
    resolved = _resolve_repo(repo)
    payload: dict[str, Any] = {"ref": body.ref}
    if body.inputs:
        payload["inputs"] = body.inputs
    await _github_request(
        "POST",
        f"/repos/{resolved}/actions/workflows/{workflow_id}/dispatches",
        json=payload,
    )
    return {"status": "dispatched", "workflow_id": workflow_id, "ref": body.ref}


@router.post("/runs/{run_id}/rerun", status_code=201)
async def rerun_run(run_id: IdPath, repo: RepoQuery = None) -> dict[str, Any]:
    _require_token()
    resolved = _resolve_repo(repo)
    await _github_request("POST", f"/repos/{resolved}/actions/runs/{run_id}/rerun")
    return {"status": "queued", "run_id": run_id}


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: IdPath, repo: RepoQuery = None) -> dict[str, Any]:
    _require_token()
    resolved = _resolve_repo(repo)
    await _github_request("POST", f"/repos/{resolved}/actions/runs/{run_id}/cancel")
    return {"status": "cancelling", "run_id": run_id}
