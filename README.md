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
systems and sources at `GET /api/systems`.

Set `PUBLIC_API_URL` when building the frontend if the backend is not reachable
on the same hostname at port 8000. Configure `FRONTEND_ORIGINS` as a comma-
separated list of frontend origins when deploying away from localhost.

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
