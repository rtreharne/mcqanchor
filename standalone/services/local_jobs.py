from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

from django.conf import settings
from django.db import close_old_connections


JobCallback = Callable[..., None]
_JOB_QUEUE: queue.Queue[tuple[str, JobCallback, tuple, dict]] = queue.Queue()
_WORKER_LOCK = threading.Lock()
_WORKER_THREAD: threading.Thread | None = None
_STATUS_LOCK = threading.Lock()
_CURRENT_JOB_NAME = ""
_LAST_JOB_NAME = ""
_LAST_EVENT = "idle"
_LAST_ERROR = ""


def _job_pause_seconds() -> float:
    return max(0.0, float(getattr(settings, "LOCAL_BACKGROUND_JOB_PAUSE_SECONDS", 1.0) or 0.0))


def _log(event: str, *, job_name: str, **payload) -> None:
    details = " ".join(f"{key}={value}" for key, value in payload.items())
    suffix = f" {details}" if details else ""
    print(f"[local-jobs] event={event} job={job_name}{suffix}", flush=True)


def _set_status(*, current_job_name: str | None = None, last_job_name: str | None = None, last_event: str | None = None, last_error: str | None = None) -> None:
    global _CURRENT_JOB_NAME, _LAST_JOB_NAME, _LAST_EVENT, _LAST_ERROR
    with _STATUS_LOCK:
        if current_job_name is not None:
            _CURRENT_JOB_NAME = current_job_name
        if last_job_name is not None:
            _LAST_JOB_NAME = last_job_name
        if last_event is not None:
            _LAST_EVENT = last_event
        if last_error is not None:
            _LAST_ERROR = last_error


def _worker_loop() -> None:
    while True:
        job_name, callback, args, kwargs = _JOB_QUEUE.get()
        try:
            _set_status(current_job_name=job_name, last_job_name=job_name, last_event="running", last_error="")
            _log("started", job_name=job_name, queued=_JOB_QUEUE.qsize())
            close_old_connections()
            try:
                callback(*args, **kwargs)
            finally:
                close_old_connections()
            _log("completed", job_name=job_name, queued=_JOB_QUEUE.qsize())
            _set_status(current_job_name="", last_job_name=job_name, last_event="completed", last_error="")
        except Exception as exc:  # noqa: BLE001
            _log("failed", job_name=job_name, queued=_JOB_QUEUE.qsize(), error=str(exc))
            _set_status(current_job_name="", last_job_name=job_name, last_event="failed", last_error=str(exc))
        finally:
            _JOB_QUEUE.task_done()
            pause_seconds = _job_pause_seconds()
            if pause_seconds > 0:
                time.sleep(pause_seconds)


def _ensure_worker_started() -> None:
    global _WORKER_THREAD
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return
        _WORKER_THREAD = threading.Thread(
            target=_worker_loop,
            name="standalone-local-job-worker",
            daemon=True,
        )
        _WORKER_THREAD.start()


def enqueue_local_job(job_name: str, callback: JobCallback, *args, **kwargs) -> None:
    _ensure_worker_started()
    _JOB_QUEUE.put((job_name, callback, args, kwargs))
    _log("queued", job_name=job_name, queued=_JOB_QUEUE.qsize())
    _set_status(last_job_name=job_name, last_event="queued")


def local_job_status() -> dict[str, object]:
    with _STATUS_LOCK:
        current_job_name = _CURRENT_JOB_NAME
        last_job_name = _LAST_JOB_NAME
        last_event = _LAST_EVENT
        last_error = _LAST_ERROR
        worker_alive = bool(_WORKER_THREAD is not None and _WORKER_THREAD.is_alive())
    queued_count = _JOB_QUEUE.qsize()
    running = bool(current_job_name)
    return {
        "worker_alive": worker_alive,
        "running": running,
        "queued_count": queued_count,
        "current_job_name": current_job_name,
        "last_job_name": last_job_name,
        "last_event": last_event,
        "last_error": last_error,
    }
