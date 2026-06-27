from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from django.conf import settings

from standalone.services.local_jobs import enqueue_local_job


_TASK_LABELS = {
    "content_asset_processing": "File processing",
    "block_regeneration": "Block regeneration",
    "block_creation_processing": "Block processing",
    "course_import_analysis": "PDF import analysis",
    "course_import_block_creation": "Block creation",
}


def local_background_job_strategy() -> str:
    strategy = str(getattr(settings, "LOCAL_BACKGROUND_JOB_STRATEGY", "thread") or "thread").strip().lower()
    if strategy not in {"thread", "subprocess"}:
        return "thread"
    return strategy


def local_background_job_label(task_name: str) -> str:
    return _TASK_LABELS.get(task_name, "Background job")


def _run_registered_task(task_name: str, *args) -> None:
    from standalone.tasks import (
        run_block_creation_processing,
        run_block_regeneration,
        run_content_asset_processing,
        run_course_import_analysis,
        run_course_import_block_creation,
    )

    if task_name == "content_asset_processing":
        run_content_asset_processing(int(args[0]))
        return
    if task_name == "block_regeneration":
        run_block_regeneration(int(args[0]))
        return
    if task_name == "block_creation_processing":
        run_block_creation_processing(int(args[0]))
        return
    if task_name == "course_import_analysis":
        run_course_import_analysis(int(args[0]))
        return
    if task_name == "course_import_block_creation":
        selected_chapter_ids = [int(chapter_id) for chapter_id in list(args[1] or [])] if len(args) > 1 else None
        run_course_import_block_creation(
            int(args[0]),
            selected_chapter_ids,
            queue_block_processing=True,
        )
        return
    raise ValueError(f"Unknown background task: {task_name}")


def _spawn_registered_task_subprocess(task_name: str, *args) -> None:
    manage_py = Path(settings.BASE_DIR) / "manage.py"
    subprocess.Popen(
        [
            sys.executable,
            str(manage_py),
            "run_background_task",
            task_name,
            json.dumps(list(args)),
        ],
        cwd=settings.BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def enqueue_registered_background_task(task_name: str, *args) -> None:
    if local_background_job_strategy() == "subprocess":
        _spawn_registered_task_subprocess(task_name, *args)
        return
    enqueue_local_job(task_name, _run_registered_task, task_name, *args)
