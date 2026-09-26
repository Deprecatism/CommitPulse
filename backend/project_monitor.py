"""Track the performance of a local repository's own process tree.

A "tracked project" is a local repository directory. The monitor isolates the
processes that belong to that project and aggregates their CPU, memory, disk I/O
and uptime into a single system (``source="project"``) so it flows through the
same store/history/dashboard/websocket pipeline as every other source.

Processes are attributed to a project in two complementary ways:

1. **Launched** – if the project defines a ``command``, the backend can spawn it
   (in its own process group, rooted at the repository path) and track that
   process together with every descendant it forks.
2. **Discovered** – any process whose working directory lives inside the
   repository (optionally also matched by command line) is attached, along with
   its descendants. The backend's own process tree is always excluded so the
   monitor never measures itself.

Commit identity is read from the repository's Git ``HEAD`` so samples line up
with CommitPulse's commit comparisons.
"""

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

if __package__:
    from .metrics_store import MetricsStore, SystemMetricsReport
else:
    from metrics_store import MetricsStore, SystemMetricsReport

logger = logging.getLogger(__name__)

_SYSTEM_ID_PATTERN = re.compile(r"[^A-Za-z0-9._-]")


def _clamp_percent(value: float) -> float:
    return max(0.0, min(value, 100.0))


def _slugify(name: str) -> str:
    slug = _SYSTEM_ID_PATTERN.sub("-", name.strip()).strip("-._")
    slug = re.sub(r"^[^A-Za-z0-9]+", "", slug)
    return (slug or "project")[:128]


def _is_within(candidate: str | None, root: str) -> bool:
    if not candidate:
        return False
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return False
    return real == root or real.startswith(root + os.sep)


def _git_value(path: str, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _git_metadata(path: str) -> tuple[str, str, str, str | None]:
    head = _git_value(path, "log", "-1", "--format=%H%n%s")
    sha, _, message = head.partition("\n") if head else ("", "", "")
    commit_sha = sha if len(sha) >= 7 else "unknown"
    commit_message = message or "Local project monitoring"
    branch = _git_value(path, "branch", "--show-current")
    repository_root = _git_value(path, "rev-parse", "--show-toplevel")
    repository_name = Path(repository_root).name if repository_root else Path(path).name
    return commit_sha, commit_message[:500], repository_name[:255], branch


class TrackedProject:
    def __init__(
        self,
        project_id: str,
        name: str,
        path: str,
        command: str | None = None,
        match_cwd: bool = True,
        match_cmdline: bool = False,
    ) -> None:
        self.id = project_id
        self.name = name
        self.path = path
        self.command = command
        self.match_cwd = match_cwd
        self.match_cmdline = match_cmdline

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "command": self.command,
            "match_cwd": self.match_cwd,
            "match_cmdline": self.match_cmdline,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TrackedProject":
        return cls(
            project_id=str(data["id"]),
            name=str(data["name"]),
            path=str(data["path"]),
            command=(str(data["command"]) if data.get("command") else None),
            match_cwd=bool(data.get("match_cwd", True)),
            match_cmdline=bool(data.get("match_cmdline", False)),
        )


class ProjectMetricsMonitor:
    def __init__(
        self,
        store: MetricsStore,
        interval_seconds: float = 5.0,
        on_sample=None,
        config_path: str | Path | None = None,
        allow_launch: bool = False,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")

        self.store = store
        self.interval_seconds = interval_seconds
        self.on_sample = on_sample
        self.allow_launch = allow_launch
        self.config_path = (
            Path(config_path)
            if config_path is not None
            else Path(__file__).with_name("tracked_projects.json")
        )
        self._projects: dict[str, TrackedProject] = {}
        self._launched: dict[str, subprocess.Popen[bytes]] = {}
        self._process_cache: dict[str, dict[int, psutil.Process]] = {}
        self._last_pid_counts: dict[str, int] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ config
    def load(self) -> None:
        if not self.config_path.exists():
            return
        try:
            raw = json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            logger.warning("Could not read tracked projects from %s", self.config_path)
            return
        with self._lock:
            for entry in raw if isinstance(raw, list) else []:
                try:
                    project = TrackedProject.from_dict(entry)
                except (KeyError, TypeError):
                    continue
                self._projects[project.id] = project

    def _persist(self) -> None:
        try:
            self.config_path.write_text(
                json.dumps(
                    [project.to_dict() for project in self._projects.values()],
                    indent=2,
                )
            )
        except OSError:
            logger.warning("Could not persist tracked projects to %s", self.config_path)

    def seed_from_env(self, value: str | None) -> None:
        if not value:
            return
        entries: list[tuple[str, str]] = []
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                for item in json.loads(stripped):
                    entries.append((str(item["name"]), str(item["path"])))
            except (ValueError, KeyError, TypeError):
                logger.warning("Ignoring malformed TRACKED_PROJECTS JSON")
        else:
            for chunk in stripped.split(","):
                if not chunk.strip():
                    continue
                name, separator, path = chunk.partition("=")
                if not separator:
                    path = name
                    name = Path(path.strip()).name
                entries.append((name.strip(), path.strip()))
        for name, path in entries:
            try:
                self.add(name=name, path=path)
            except ValueError:
                continue

    # -------------------------------------------------------------------- CRUD
    def list(self) -> list[TrackedProject]:
        with self._lock:
            return list(self._projects.values())

    def get(self, project_id: str) -> TrackedProject | None:
        with self._lock:
            return self._projects.get(project_id)

    def add(
        self,
        name: str,
        path: str,
        command: str | None = None,
        match_cwd: bool = True,
        match_cmdline: bool = False,
    ) -> TrackedProject:
        resolved = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(resolved):
            raise ValueError(f"Path is not an existing directory: {path}")
        with self._lock:
            base = _slugify(name)
            project_id = base
            suffix = 2
            while project_id in self._projects:
                project_id = f"{base}-{suffix}"[:128]
                suffix += 1
            project = TrackedProject(
                project_id=project_id,
                name=name,
                path=resolved,
                command=command,
                match_cwd=match_cwd,
                match_cmdline=match_cmdline,
            )
            self._projects[project_id] = project
            self._persist()
        return project

    def remove(self, project_id: str) -> bool:
        with self._lock:
            if project_id not in self._projects:
                return False
            self.terminate(project_id)
            del self._projects[project_id]
            self._process_cache.pop(project_id, None)
            self._last_pid_counts.pop(project_id, None)
            self._persist()
        return True

    # ---------------------------------------------------------------- launching
    def launch(self, project_id: str) -> None:
        if not self.allow_launch:
            raise PermissionError(
                "Launching commands is disabled. Set PROJECTS_ALLOW_LAUNCH=true."
            )
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise KeyError(project_id)
            if not project.command:
                raise ValueError("This project has no command to launch.")
            existing = self._launched.get(project_id)
            if existing is not None and existing.poll() is None:
                raise RuntimeError("A launched process is already running.")
            self._launched[project_id] = subprocess.Popen(
                project.command,
                cwd=project.path,
                shell=True,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def terminate(self, project_id: str) -> bool:
        with self._lock:
            popen = self._launched.pop(project_id, None)
        if popen is None or popen.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(popen.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            popen.terminate()
        return True

    def _launch_running(self, project_id: str) -> bool:
        popen = self._launched.get(project_id)
        return popen is not None and popen.poll() is None

    def status(self, project: TrackedProject) -> dict[str, object]:
        return {
            **project.to_dict(),
            "active_processes": self._last_pid_counts.get(project.id, 0),
            "launch_running": self._launch_running(project.id),
            "launchable": bool(project.command) and self.allow_launch,
        }

    # ------------------------------------------------------------- measurement
    def _self_process_tree(self) -> set[int]:
        # Exclude the backend's own process, everything it spawned (git helpers,
        # launched projects, etc.), and its immediate parent (the uvicorn
        # --reload supervisor). The parent's *other* descendants are left alone
        # so a project started from the same shell as the backend is still
        # discovered.
        pids = {os.getpid()}
        try:
            me = psutil.Process()
            parent = me.parent()
            if parent is not None:
                pids.add(parent.pid)
            try:
                pids.update(child.pid for child in me.children(recursive=True))
            except psutil.Error:
                pass
        except psutil.Error:
            pass
        return pids

    def _resolve_pids(self, project: TrackedProject) -> set[int]:
        pids: set[int] = set()
        popen = self._launched.get(project.id)
        if popen is not None and popen.poll() is None:
            pids.add(popen.pid)

        if project.match_cwd or project.match_cmdline:
            root = os.path.realpath(project.path)
            excluded = self._self_process_tree()
            for process in psutil.process_iter(["pid", "cwd", "cmdline", "exe"]):
                if process.pid in excluded:
                    continue
                info = process.info
                try:
                    if project.match_cwd and _is_within(info.get("cwd"), root):
                        pids.add(process.pid)
                        continue
                    if project.match_cmdline:
                        cmdline = info.get("cmdline") or []
                        if any(root in argument for argument in cmdline) or _is_within(
                            info.get("exe"), root
                        ):
                            pids.add(process.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue

        for pid in list(pids):
            try:
                children = psutil.Process(pid).children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            pids.update(child.pid for child in children)
        return pids

    def _aggregate(
        self, project: TrackedProject, pids: set[int]
    ) -> tuple[float, int, int, float] | None:
        cache = self._process_cache.setdefault(project.id, {})
        cpu_total = 0.0
        rss_total = 0
        io_total = 0
        earliest_start: float | None = None
        alive: set[int] = set()

        for pid in pids:
            process = cache.get(pid)
            if process is None:
                try:
                    process = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    continue
                cache[pid] = process
            try:
                with process.oneshot():
                    # First call for a freshly cached process returns 0.0; later
                    # polls report usage since the previous poll.
                    cpu_total += process.cpu_percent(None)
                    rss_total += process.memory_info().rss
                    start = process.create_time()
                try:
                    counters = process.io_counters()
                    io_total += counters.read_bytes + counters.write_bytes
                except (psutil.AccessDenied, NotImplementedError, AttributeError):
                    pass
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            alive.add(pid)
            earliest_start = start if earliest_start is None else min(earliest_start, start)

        for pid in list(cache):
            if pid not in alive:
                del cache[pid]

        if not alive:
            return None

        cpu_percent = _clamp_percent(cpu_total / (psutil.cpu_count() or 1))
        uptime = max(0.0, time.time() - earliest_start) if earliest_start else 0.0
        return cpu_percent, rss_total, io_total, uptime

    def _record_project(self, project: TrackedProject) -> None:
        pids = self._resolve_pids(project)
        aggregated = self._aggregate(project, pids)
        self._last_pid_counts[project.id] = len(pids) if aggregated else 0
        if aggregated is None:
            return
        cpu_percent, rss_total, io_total, uptime = aggregated
        total_memory = psutil.virtual_memory().total
        commit_sha, commit_message, repository_name, branch = _git_metadata(
            project.path
        )
        report = SystemMetricsReport(
            repository_name=repository_name,
            commit_sha=commit_sha,
            commit_message=commit_message,
            branch=branch,
            timestamp=datetime.now(timezone.utc),
            cpu_percent=cpu_percent,
            memory_percent=_clamp_percent(rss_total / total_memory * 100.0)
            if total_memory
            else 0.0,
            memory_used_bytes=rss_total,
            memory_total_bytes=total_memory,
            disk_percent=0.0,
            disk_used_bytes=io_total,
            disk_total_bytes=0,
            network_sent_bytes=0,
            network_received_bytes=0,
            uptime_seconds=uptime,
        )
        sample = self.store.save(project.id, report, source="project")
        if self.on_sample is not None:
            self.on_sample(sample)

    def _record_all(self) -> None:
        for project in self.list():
            try:
                self._record_project(project)
            except Exception:
                logger.exception("Failed to record metrics for project %s", project.id)

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def _collect_loop(self) -> None:
        self._record_all()
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self._record_all()
            except Exception:
                logger.exception("Failed to record project metrics")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
            if not self._thread.is_alive():
                self._thread = None
        for project_id in list(self._launched):
            self.terminate(project_id)
