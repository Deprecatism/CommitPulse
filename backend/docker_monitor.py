"""Collect performance metrics from running Docker containers.

The monitor talks to the Docker Engine API directly over its unix socket (or a
``tcp://`` ``DOCKER_HOST``) using ``httpx`` so no extra dependency is required.
Each running container is recorded as its own system with ``source="docker"``,
mirroring the local ``SystemMetricsMonitor`` so the same dashboard, history and
websocket pipeline can display it.

Container stats are mapped onto the existing metric schema:

- CPU: fraction of total host CPU capacity used by the container (0-100).
- Memory: working-set usage vs. the container's memory limit.
- Network: cumulative bytes sent/received across all container interfaces.
- Disk: the container's writable-layer size vs. its total on-disk size
  (only collected when ``DOCKER_COLLECT_DISK_USAGE`` is enabled, as computing
  it makes the daemon walk the filesystem on every poll).
- Uptime: seconds since the container started.

Commit identity is read from standard OCI image labels
(``org.opencontainers.image.revision`` / ``.source`` / ``.version`` / ``.title``)
so containers built by CI slot straight into CommitPulse's commit comparisons.
"""

import logging
import os
import re
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

if __package__:
    from .metrics_store import MetricsStore, SystemMetricsReport
else:
    from metrics_store import MetricsStore, SystemMetricsReport

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = "/var/run/docker.sock"
_ZERO_TIME_PREFIX = "0001-01-01"


def _clamp_percent(value: float) -> float:
    return max(0.0, min(value, 100.0))


def _cpu_percent(stats: dict[str, Any]) -> float:
    cpu = stats.get("cpu_stats") or {}
    precpu = stats.get("precpu_stats") or {}
    if not isinstance(cpu, dict) or not isinstance(precpu, dict):
        return 0.0
    cpu_usage = (cpu.get("cpu_usage") or {}).get("total_usage", 0)
    precpu_usage = (precpu.get("cpu_usage") or {}).get("total_usage", 0)
    cpu_delta = float(cpu_usage) - float(precpu_usage)
    system_delta = float(cpu.get("system_cpu_usage", 0)) - float(
        precpu.get("system_cpu_usage", 0)
    )
    if system_delta > 0 and cpu_delta > 0:
        return _clamp_percent((cpu_delta / system_delta) * 100.0)
    return 0.0


def _memory(stats: dict[str, Any]) -> tuple[int, int, float]:
    memory = stats.get("memory_stats") or {}
    if not isinstance(memory, dict):
        return 0, 0, 0.0
    usage = int(memory.get("usage", 0) or 0)
    limit = int(memory.get("limit", 0) or 0)
    detail = memory.get("stats") or {}
    # cgroup v2 exposes "inactive_file"; cgroup v1 uses "total_inactive_file"
    # (falling back to "cache"). Subtracting it yields the working set, which is
    # what `docker stats` reports.
    cache = 0
    if isinstance(detail, dict):
        cache = int(
            detail.get("inactive_file")
            or detail.get("total_inactive_file")
            or detail.get("cache")
            or 0
        )
    used = max(usage - cache, 0)
    percent = _clamp_percent(used / limit * 100.0) if limit > 0 else 0.0
    return used, limit, percent


def _network(stats: dict[str, Any]) -> tuple[int, int]:
    networks = stats.get("networks") or {}
    if not isinstance(networks, dict):
        return 0, 0
    sent = 0
    received = 0
    for interface in networks.values():
        if isinstance(interface, dict):
            sent += int(interface.get("tx_bytes", 0) or 0)
            received += int(interface.get("rx_bytes", 0) or 0)
    return sent, received


def _parse_docker_time(value: object) -> float | None:
    if not isinstance(value, str) or not value or value.startswith(_ZERO_TIME_PREFIX):
        return None
    text = value.replace("Z", "+00:00")
    # Docker emits nanosecond precision; datetime only understands microseconds.
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _system_id(name: str, container_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", name.lstrip("/"))
    cleaned = re.sub(r"^[^A-Za-z0-9]+", "", cleaned)
    if not cleaned:
        cleaned = container_id[:12]
    return cleaned[:128]


def _repository_from_source(source: str | None) -> str | None:
    if not source:
        return None
    trimmed = source.rstrip("/").removesuffix(".git")
    return trimmed.rsplit("/", 1)[-1] or None


def _commit_metadata(
    labels: dict[str, str], name: str, image: str
) -> tuple[str, str, str, str | None]:
    sha = (
        labels.get("org.opencontainers.image.revision")
        or labels.get("commit_sha")
        or labels.get("org.label-schema.vcs-ref")
        or ""
    )
    commit_sha = sha if len(sha) >= 7 else "unknown"
    repository_name = (
        labels.get("org.opencontainers.image.title")
        or _repository_from_source(labels.get("org.opencontainers.image.source"))
        or _repository_from_source(labels.get("org.label-schema.vcs-url"))
        or image
        or name.lstrip("/")
    )[:255]
    commit_message = (
        labels.get("commit_message")
        or labels.get("org.opencontainers.image.description")
        or f"Docker container {name.lstrip('/')}"
    )[:500]
    branch_value = labels.get("branch") or labels.get(
        "org.opencontainers.image.version"
    )
    branch = branch_value[:255] if branch_value else None
    return commit_sha, repository_name, commit_message, branch


class DockerMetricsMonitor:
    def __init__(
        self,
        store: MetricsStore,
        interval_seconds: float = 5.0,
        on_sample: Callable[[dict[str, object]], None] | None = None,
        collect_disk_usage: bool = False,
        socket_path: str | None = None,
        docker_host: str | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")

        self.store = store
        self.interval_seconds = interval_seconds
        self.on_sample = on_sample
        self.collect_disk_usage = collect_disk_usage
        self.socket_path = socket_path
        self.docker_host = docker_host
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None
        self._started_at: dict[str, float] = {}

    def _build_client(self) -> httpx.Client:
        docker_host = self.docker_host or os.getenv("DOCKER_HOST")
        if docker_host and docker_host.startswith("tcp://"):
            base_url = "http://" + docker_host[len("tcp://") :]
            return httpx.Client(base_url=base_url, timeout=10.0)
        socket_path = self.socket_path or os.getenv("DOCKER_SOCKET_PATH")
        if not socket_path and docker_host and docker_host.startswith("unix://"):
            socket_path = docker_host[len("unix://") :]
        socket_path = socket_path or DEFAULT_SOCKET_PATH
        transport = httpx.HTTPTransport(uds=socket_path)
        return httpx.Client(transport=transport, base_url="http://docker", timeout=10.0)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        try:
            client = self._build_client()
            client.get("/_ping", timeout=2.0).raise_for_status()
        except (httpx.HTTPError, OSError) as error:
            logger.warning(
                "Docker monitoring disabled: could not reach the Docker daemon (%s)",
                error,
            )
            return

        self._client = client
        self._stop_event.clear()
        self._record_all()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
            if not self._thread.is_alive():
                self._thread = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def _collect_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self._record_all()
            except Exception:
                logger.exception("Failed to record Docker container metrics")

    def _list_containers(self) -> list[dict[str, Any]]:
        assert self._client is not None
        params = {"size": "1"} if self.collect_disk_usage else None
        response = self._client.get("/containers/json", params=params)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    def _container_uptime(self, container_id: str) -> float:
        started = self._started_at.get(container_id)
        if started is None and self._client is not None:
            try:
                info = self._client.get(f"/containers/{container_id}/json").json()
                started = _parse_docker_time(
                    (info.get("State") or {}).get("StartedAt")
                )
            except (httpx.HTTPError, ValueError, KeyError):
                started = None
            if started is not None:
                self._started_at[container_id] = started
        if started is None:
            return 0.0
        return max(0.0, datetime.now(timezone.utc).timestamp() - started)

    def _record_all(self) -> None:
        if self._client is None:
            return
        containers = self._list_containers()
        active_ids: set[str] = set()
        for container in containers:
            container_id = str(container.get("Id", ""))
            if not container_id:
                continue
            active_ids.add(container_id)
            try:
                system_id, report = self._build_report(container, container_id)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                logger.exception(
                    "Failed to build metrics for container %s", container_id[:12]
                )
                continue
            sample = self.store.save(system_id, report, source="docker")
            if self.on_sample is not None:
                self.on_sample(sample)

        for cached_id in list(self._started_at):
            if cached_id not in active_ids:
                del self._started_at[cached_id]

    def _build_report(
        self, container: dict[str, Any], container_id: str
    ) -> tuple[str, SystemMetricsReport]:
        assert self._client is not None
        names = container.get("Names") or []
        name = str(names[0]) if isinstance(names, list) and names else container_id[:12]
        image = str(container.get("Image", "") or "")
        labels_value = container.get("Labels") or {}
        labels = (
            {str(key): str(value) for key, value in labels_value.items()}
            if isinstance(labels_value, dict)
            else {}
        )

        stats_response = self._client.get(
            f"/containers/{container_id}/stats", params={"stream": "false"}
        )
        stats_response.raise_for_status()
        stats = stats_response.json()

        used, limit, memory_percent = _memory(stats)
        sent, received = _network(stats)
        commit_sha, repository_name, commit_message, branch = _commit_metadata(
            labels, name, image
        )

        disk_used = 0
        disk_total = 0
        disk_percent = 0.0
        if self.collect_disk_usage:
            disk_used = max(int(container.get("SizeRw", 0) or 0), 0)
            disk_total = max(int(container.get("SizeRootFs", 0) or 0), 0)
            disk_percent = (
                _clamp_percent(disk_used / disk_total * 100.0)
                if disk_total > 0
                else 0.0
            )

        report = SystemMetricsReport(
            repository_name=repository_name,
            commit_sha=commit_sha,
            commit_message=commit_message,
            branch=branch,
            timestamp=datetime.now(timezone.utc),
            cpu_percent=_cpu_percent(stats),
            memory_percent=memory_percent,
            memory_used_bytes=used,
            memory_total_bytes=limit,
            disk_percent=disk_percent,
            disk_used_bytes=disk_used,
            disk_total_bytes=disk_total,
            network_sent_bytes=sent,
            network_received_bytes=received,
            uptime_seconds=self._container_uptime(container_id),
        )
        return _system_id(name, container_id), report
