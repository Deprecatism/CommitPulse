# CommitPulse

A starter project that combines:

- Astro for the frontend
- FastAPI for the Python API

The frontend calls the Python backend at `http://localhost:8000` to fetch data.

## Project structure

```text
CommitPulse/
├── backend/
│   ├── __init__.py
│   ├── main.py
│   └── requirements.txt
├── public/
├── src/
├── package.json
├── astro.config.mjs
├── tsconfig.json
├── .gitignore
└── README.md
```

## Prerequisites

- Node.js 22+ recommended
- Python 3.10+
- npm

## 1) Install frontend dependencies

From the project root:

```bash
npm install
```

## 2) Create and activate a Python virtual environment

```bash
cd backend
python -m venv .venv
```

On macOS/Linux:

```bash
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

## 3) Install Python dependencies

```bash
pip install -r requirements.txt
```

This includes FastAPI, Uvicorn, `psutil` for local system polling, and the HTTP
client used for local API testing.

## 4) Start the backend

From the `backend` directory:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at:

- http://localhost:8000/api/health
- http://localhost:8000/api/message
- http://localhost:8000/api/systems
- http://localhost:8000/api/systems/{system_id}/metrics
- http://localhost:8000/api/systems/{system_id}/dashboard
- WebSocket: ws://localhost:8000/api/live
- http://localhost:8000/api/projects
- http://localhost:8000/api/github/config
- http://localhost:8000/api/github/workflows?repo=owner/name
- http://localhost:8000/api/github/runs?repo=owner/name

External systems can report usage to `POST /api/webhooks/metrics/{system_id}` or
`POST /api/systems/{system_id}/metrics`. Reports include commit identity and
metrics; saved reports are pushed to dashboard clients over `/api/live`. Set
`METRICS_WEBHOOK_SECRET` to require the `X-Webhook-Token` header on report
ingestion. The backend also polls its own host with `psutil` every 5 seconds by
default, recording it as `backend-local` with source `local` and the current Git
commit and repository folder name when available. Set `METRICS_REPOSITORY_NAME`,
`METRICS_COMMIT_SHA`, `METRICS_COMMIT_MESSAGE`, and `METRICS_BRANCH` to override
that metadata; without Git metadata or overrides, the SHA defaults to `unknown`.
Both sources are stored in `backend/metrics.sqlite3`; set `METRICS_DB_PATH`
before startup to use a different SQLite database. Set
`METRICS_INTERVAL_SECONDS` to change the local polling interval, or
`METRICS_POLLING_ENABLED=false` to disable local polling while leaving external
reporting enabled. View history at `GET /api/systems/{system_id}/metrics`; the
optional `source=local` or `source=reported` query filters it. Commit
comparisons are available at `GET /api/systems/{system_id}/dashboard`. List
systems and sources at `GET /api/systems`. The `source` filter accepts `local`,
`reported`, `docker`, or `project`.

Set `PUBLIC_API_URL` when building the frontend if the backend is not reachable
on the same hostname at port 8000. Configure `FRONTEND_ORIGINS` as a comma-
separated list of frontend origins when deploying away from localhost.

## Docker container monitoring

When a Docker daemon is reachable, the backend polls every running container and
records it as its own system with source `docker`, using the same metric schema
as everything else. It connects to the Docker Engine API over its unix socket
(`/var/run/docker.sock`) or a `tcp://` `DOCKER_HOST`, so no extra dependency is
needed. Container stats are mapped as:

- CPU: fraction of total host CPU capacity used by the container (0-100).
- Memory: working-set usage vs. the container's memory limit.
- Network: cumulative bytes sent/received across all container interfaces.
- Disk: writable-layer size vs. total on-disk size (only when
  `DOCKER_COLLECT_DISK_USAGE=true`, since it makes the daemon walk the
  filesystem on every poll).
- Uptime: seconds since the container started.

Commit identity is read from standard OCI image labels
(`org.opencontainers.image.revision` / `.source` / `.version` / `.title`), so
containers built by CI drop straight into CommitPulse's commit comparisons.

Environment variables:

- `DOCKER_MONITORING_ENABLED` (default `true`): the monitor disables itself
  gracefully if the daemon cannot be reached.
- `DOCKER_INTERVAL_SECONDS` (defaults to `METRICS_INTERVAL_SECONDS`).
- `DOCKER_COLLECT_DISK_USAGE` (default `false`).
- `DOCKER_SOCKET_PATH` / `DOCKER_HOST` to point at a non-default daemon.

## Local project monitoring

To track a repository you run locally, register it as a project and CommitPulse
will isolate and aggregate the CPU, memory, disk I/O and uptime of that
project's process tree, recording it as source `project` with the repository's
Git `HEAD` as commit identity. Processes are attributed to a project by:

- **Launched processes** – if the project defines a `command`, the backend can
  spawn it in its own process group rooted at the repo, and tracks it plus every
  descendant it forks.
- **Discovered processes** – any process whose working directory is inside the
  repository (optionally also matched by command line), plus its descendants.
  The backend's own process tree is always excluded so it never measures itself.

Manage projects from the dashboard's "Local projects" panel or via the REST API:

- `GET /api/projects` — list tracked projects with live process counts.
- `POST /api/projects` — `{ "name", "path", "command?", "match_cwd?",
  "match_cmdline?" }` (the `path` must be an existing directory).
- `DELETE /api/projects/{id}` — stop tracking.
- `POST /api/projects/{id}/launch` and `.../terminate` — start/stop the
  project's command (requires `PROJECTS_ALLOW_LAUNCH=true`).

Environment variables:

- `PROJECT_MONITORING_ENABLED` (default `true`).
- `PROJECT_INTERVAL_SECONDS` (defaults to `METRICS_INTERVAL_SECONDS`).
- `PROJECTS_ALLOW_LAUNCH` (default `false`): enables the launch/terminate
  endpoints, which run the configured command. Leave it off unless you trust the
  clients of the API, since it executes shell commands on the host.
- `PROJECTS_CONFIG_PATH` (default `backend/tracked_projects.json`): where the
  project list is persisted.
- `TRACKED_PROJECTS`: seed projects at startup, either as a JSON array of
  `{ "name", "path" }` objects or a comma-separated list of `name=/path` pairs.

## GitHub Actions dashboard

A second, self-contained view lives at http://localhost:4321/actions (linked from
the telemetry header). It provides a togglable workflow menu plus a live-ish list
of GitHub Actions runs, mirroring the telemetry dashboard's layout. All GitHub
calls are proxied through the backend under the `/api/github/*` prefix so the
token never reaches the browser:

- `GET /api/github/config` — reports whether a token is set and the default repo.
- `GET /api/github/workflows?repo=owner/name` — list workflows.
- `GET /api/github/runs?repo=owner/name&workflow_id=&status=&branch=` — list runs.
- `GET /api/github/runs/{run_id}/jobs?repo=owner/name` — list a run's jobs.
- `POST /api/github/workflows/{workflow_id}/dispatch?repo=owner/name` — trigger a
  `workflow_dispatch` run (body: `{"ref": "main", "inputs": {...}}`).
- `POST /api/github/runs/{run_id}/rerun?repo=owner/name` — re-run.
- `POST /api/github/runs/{run_id}/cancel?repo=owner/name` — cancel.

Configure it with environment variables before starting the backend:

- `GITHUB_TOKEN` (or `GH_TOKEN`): personal access token. Public repositories work
  read-only without one (subject to GitHub's rate limits); private repositories
  and the re-run/cancel/dispatch actions require a token with the `actions`
  scope.
- `GITHUB_REPOSITORY` (or `GITHUB_DEFAULT_REPO`): default `owner/name` shown when
  the client does not pass `?repo=`. The UI also remembers the last repository
  you loaded and lets you switch repositories from the header.

Example:

```bash
export GITHUB_TOKEN=ghp_your_token_here
export GITHUB_REPOSITORY=owner/name
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Example report:

```bash
COMMIT_SHA=$(git rev-parse HEAD)
COMMIT_MESSAGE=$(git log -1 --format=%s)
BRANCH=$(git branch --show-current)
REPOSITORY_NAME=$(basename "$(git rev-parse --show-toplevel)")
export COMMIT_SHA COMMIT_MESSAGE BRANCH REPOSITORY_NAME

curl -X POST http://localhost:8000/api/webhooks/metrics/build-agent-01 \
  -H 'Content-Type: application/json' \
  -d "$(python - <<'PY'
import json
import os

print(json.dumps({
  'repository_name': os.environ['REPOSITORY_NAME'],
    'commit_sha': os.environ['COMMIT_SHA'],
    'commit_message': os.environ['COMMIT_MESSAGE'],
    'branch': os.environ['BRANCH'],
    'cpu_percent': 32.5,
    'memory_percent': 64.2,
    'memory_used_bytes': 6871947673,
    'memory_total_bytes': 10737418240,
    'disk_percent': 41.0,
    'disk_used_bytes': 4398046511,
    'disk_total_bytes': 10737418240,
    'network_sent_bytes': 429496729,
    'network_received_bytes': 858993459,
    'uptime_seconds': 86400,
}))
PY
)"
```

The `timestamp` field is optional; when omitted, the backend records the report
time in UTC. Reports are validated and compared by commit within each system.
Each system ID is limited to 128 letters, numbers, dots, underscores, or
hyphens.

## 5) Start the Astro frontend

Open a second terminal in the project root and run:

```bash
npm run dev
```

Then open:

- http://localhost:4321

## Optional: build for production

```bash
npm run build
```

To preview the production build:

```bash
npm run preview
```

## Common troubleshooting

- If the frontend says the API is unavailable, make sure the Python server is
  still running on port 8000.
- If Python is not recognized, install Python and reopen the terminal.
- If npm is not recognized, restart the terminal after installing Node.js.
