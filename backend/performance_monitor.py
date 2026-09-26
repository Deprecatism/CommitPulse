from datetime import datetime, timezone
import logging
import os
import threading
import time
from collections.abc import Callable

import psutil

if __package__:
    from .metrics_store import MetricsStore, SystemMetricsReport
else:
    from metrics_store import MetricsStore, SystemMetricsReport


class SystemMetricsMonitor:
    def __init__(
        self,
        store: MetricsStore,
        interval_seconds: float = 5.0,
        system_id: str = "backend-local",
        commit_sha: str = "unknown",
        commit_message: str = "Local backend monitoring",
        branch: str | None = None,
        commit_metadata_provider: Callable[[], tuple[str, str, str | None]]
        | None = None,
        on_sample: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")

        self.store = store
        self.interval_seconds = interval_seconds
        self.system_id = system_id
        self.commit_sha = commit_sha
        self.commit_message = commit_message
        self.branch = branch
        self.commit_metadata_provider = commit_metadata_provider
        self.on_sample = on_sample
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        psutil.cpu_percent(interval=None)
        self._record_sample()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return

        self._stop_event.set()
        self._thread.join(timeout=self.interval_seconds + 1)
        if not self._thread.is_alive():
            self._thread = None

    def _collect_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self._record_sample()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to record local system metrics"
                )

    def _record_sample(self) -> None:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        network = psutil.net_io_counters()
        commit_sha, commit_message, branch = (
            self.commit_metadata_provider()
            if self.commit_metadata_provider is not None
            else (self.commit_sha, self.commit_message, self.branch)
        )
        report = SystemMetricsReport(
            commit_sha=commit_sha,
            commit_message=commit_message,
            branch=branch,
            timestamp=datetime.now(timezone.utc),
            cpu_percent=psutil.cpu_percent(interval=None),
            memory_percent=memory.percent,
            memory_used_bytes=memory.used,
            memory_total_bytes=memory.total,
            disk_percent=disk.percent,
            disk_used_bytes=disk.used,
            disk_total_bytes=disk.total,
            network_sent_bytes=network.bytes_sent if network else 0,
            network_received_bytes=network.bytes_recv if network else 0,
            uptime_seconds=max(0.0, time.time() - psutil.boot_time()),
        )
        sample = self.store.save(self.system_id, report, source="local")
        if self.on_sample is not None:
            self.on_sample(sample)
