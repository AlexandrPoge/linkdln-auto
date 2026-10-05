"""Run public-source searches now and periodically while the local server is open."""

from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Any, Callable

from app.vacancies.sync import SearchRepository, SearchSource, run_searches


class SearchRunner:
    def __init__(self, repository: SearchRepository, sources: tuple[SearchSource, ...],
                 interval_seconds: int = 6 * 60 * 60,
                 search_fn: Callable[[SearchRepository, tuple[SearchSource, ...]], dict[str, Any]] = run_searches):
        if not sources or interval_seconds <= 0:
            raise ValueError("search runner requires sources and a positive interval")
        self.repository = repository
        self.sources = sources
        self.interval_seconds = interval_seconds
        self.search_fn = search_fn
        self._lock = Lock()
        self._stop = Event()
        self._running = False
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._report: dict[str, Any] | None = None
        self._error: str | None = None
        self._scheduler: Thread | None = None

    def trigger(self) -> bool:
        with self._lock:
            if self._running or self._stop.is_set():
                return False
            self._running = True
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._error = None
        Thread(target=self._work, name="job-search", daemon=True).start()
        return True

    def _work(self) -> None:
        try:
            report = self.search_fn(self.repository, self.sources)
            error = None
        except Exception as exc:
            report = None
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._report = report
            self._error = error
            self._running = False
            self._finished_at = datetime.now(timezone.utc).isoformat()

    def start(self) -> None:
        if self._scheduler is not None:
            raise ValueError("search runner has already started")
        self.trigger()
        self._scheduler = Thread(target=self._schedule, name="job-search-schedule", daemon=True)
        self._scheduler.start()

    def _schedule(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.trigger()

    def stop(self) -> None:
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join(timeout=2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._running, "started_at": self._started_at,
                    "finished_at": self._finished_at, "report": self._report,
                    "error": self._error, "interval_seconds": self.interval_seconds}
