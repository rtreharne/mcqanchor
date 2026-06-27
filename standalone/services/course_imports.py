from __future__ import annotations

import os
import socket
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from standalone.models import BlockConfig, ContentAsset, CourseImport, CourseImportChapter, CourseBlock
from standalone.services.content import refresh_course_summary_from_blocks
from standalone.services.pdf_import import analyze_pdf_chapters, extract_pdf_page_range


RUNNABLE_IMPORT_STATUSES = {
    CourseImport.Status.QUEUED_ANALYSIS,
    CourseImport.Status.ANALYZING,
    CourseImport.Status.QUEUED_CREATION,
    CourseImport.Status.CREATING,
}
TERMINAL_IMPORT_STATUSES = {
    CourseImport.Status.READY,
    CourseImport.Status.PAUSED,
    CourseImport.Status.ATTENTION,
    CourseImport.Status.COMPLETED,
    CourseImport.Status.FAILED,
}
ACTIVE_IMPORT_STATUSES = {
    CourseImport.Status.QUEUED_ANALYSIS,
    CourseImport.Status.ANALYZING,
    CourseImport.Status.QUEUED_CREATION,
    CourseImport.Status.CREATING,
}


def _worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _lease_timeout_seconds() -> int:
    return max(120, int(getattr(settings, "COURSE_IMPORT_STEP_TIMEOUT_SECONDS", 900) or 900))


def _lease_expires_at(now=None):
    current_time = now or timezone.now()
    return current_time + timedelta(seconds=_lease_timeout_seconds())


def _chapter_asset_filename(chapter: CourseImportChapter) -> str:
    base = slugify(chapter.title)[:70] or f"chapter-{chapter.order}"
    return f"{chapter.order:02d}-{base}.txt"


def _spawn_course_import_worker(import_id: int) -> None:
    manage_py = Path(settings.BASE_DIR) / "manage.py"
    subprocess.Popen(
        [
            sys.executable,
            str(manage_py),
            "run_course_import_worker",
            str(import_id),
            "--once",
        ],
        cwd=settings.BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _selected_batch_queryset(course_import: CourseImport):
    return course_import.chapters.filter(
        Q(selected=True) | Q(status__in=[CourseImportChapter.Status.QUEUED, CourseImportChapter.Status.PROCESSING])
    )


def _creation_progress(course_import: CourseImport) -> int:
    total = max(1, course_import.chapters.count())
    done = course_import.chapters.filter(
        status__in=[CourseImportChapter.Status.COMPLETED, CourseImportChapter.Status.FAILED]
    ).count()
    return min(100, 5 + round((90 * done) / total))


def _update_import_counts(import_id: int) -> CourseImport:
    course_import = CourseImport.objects.get(pk=import_id)
    processed = course_import.chapters.filter(status=CourseImportChapter.Status.COMPLETED).count()
    failed = course_import.chapters.filter(status=CourseImportChapter.Status.FAILED).count()
    CourseImport.objects.filter(pk=import_id).update(
        processed_chapter_count=processed,
        failed_chapter_count=failed,
        updated_at=timezone.now(),
    )
    return CourseImport.objects.get(pk=import_id)


def _set_import_state(
    import_id: int,
    *,
    status: str | None = None,
    progress: int | None = None,
    error: str | None = None,
    current_step: str | None = None,
    current_chapter_id: int | None = None,
    heartbeat: bool = False,
    clear_lease: bool = False,
) -> None:
    updates: dict[str, object] = {"updated_at": timezone.now()}
    if status is not None:
        updates["status"] = status
    if progress is not None:
        updates["progress"] = max(0, min(100, progress))
    if error is not None:
        updates["error"] = error
    if current_step is not None:
        updates["current_step"] = current_step
    if current_chapter_id is not None:
        updates["current_chapter_id"] = current_chapter_id or None
    if heartbeat:
        now = timezone.now()
        updates["heartbeat_at"] = now
        updates["lease_expires_at"] = _lease_expires_at(now)
    if clear_lease:
        updates["lease_owner"] = ""
        updates["lease_expires_at"] = None
        updates["heartbeat_at"] = None
    CourseImport.objects.filter(pk=import_id).update(**updates)


def _release_import_lease(import_id: int, *, owner: str) -> None:
    CourseImport.objects.filter(pk=import_id, lease_owner=owner).update(
        lease_owner="",
        lease_expires_at=None,
        heartbeat_at=None,
        updated_at=timezone.now(),
    )


def course_import_lease_is_active(course_import: CourseImport, *, now=None) -> bool:
    current_time = now or timezone.now()
    return bool(course_import.lease_owner and course_import.lease_expires_at and course_import.lease_expires_at > current_time)


def course_import_has_runnable_work(course_import: CourseImport) -> bool:
    if course_import.status in {CourseImport.Status.QUEUED_ANALYSIS, CourseImport.Status.ANALYZING}:
        return True
    if course_import.status in {CourseImport.Status.QUEUED_CREATION, CourseImport.Status.CREATING}:
        return course_import.chapters.filter(status__in=[CourseImportChapter.Status.QUEUED, CourseImportChapter.Status.PROCESSING]).exists()
    return False


def course_import_work_is_active(*, course_id: int | None = None) -> bool:
    queryset = CourseImport.objects.filter(status__in=ACTIVE_IMPORT_STATUSES)
    if course_id is not None:
        queryset = queryset.filter(course_id=course_id)
    return queryset.exists()


def resume_course_import(import_id: int) -> bool:
    course_import = CourseImport.objects.filter(pk=import_id).first()
    if course_import is None:
        return False
    if course_import.chapters.filter(status__in=[CourseImportChapter.Status.QUEUED, CourseImportChapter.Status.PROCESSING]).exists():
        _set_import_state(
            import_id,
            status=CourseImport.Status.QUEUED_CREATION,
            progress=_creation_progress(course_import),
            error="",
            current_step="Resuming queued chapter import.",
            current_chapter_id=0,
            clear_lease=True,
        )
        ensure_course_import_worker_running(import_id)
        return True
    if course_import.status == CourseImport.Status.PAUSED and not course_import.chapters.exists():
        queue_course_import_analysis(import_id)
        return True
    return False


def ensure_course_import_worker_running(import_id: int) -> bool:
    course_import = CourseImport.objects.filter(pk=import_id).first()
    if course_import is None:
        return False
    if course_import.status not in RUNNABLE_IMPORT_STATUSES:
        return False
    if course_import_lease_is_active(course_import):
        return False
    if course_import.status in {CourseImport.Status.QUEUED_CREATION, CourseImport.Status.CREATING} and not course_import_has_runnable_work(course_import):
        return False
    _spawn_course_import_worker(course_import.pk)
    return True


def queue_course_import_analysis(import_id: int) -> None:
    CourseImport.objects.filter(pk=import_id).update(
        status=CourseImport.Status.QUEUED_ANALYSIS,
        progress=5,
        error="",
        current_step="Queued for chapter detection.",
        current_chapter=None,
        updated_at=timezone.now(),
    )
    ensure_course_import_worker_running(import_id)


def queue_course_import_creation(import_id: int, selected_chapter_ids: list[int]) -> None:
    course_import = CourseImport.objects.get(pk=import_id)
    selected_ids = {int(chapter_id) for chapter_id in selected_chapter_ids}
    if not selected_ids:
        raise ValueError("Select at least one chapter before creating blocks.")

    with transaction.atomic():
        valid_ids = set(
            course_import.chapters.filter(
                pk__in=selected_ids,
                created_block__isnull=True,
                status__in=[CourseImportChapter.Status.PENDING, CourseImportChapter.Status.SKIPPED],
            ).values_list("pk", flat=True)
        )
        if not valid_ids:
            raise ValueError("Select at least one chapter before creating blocks.")
        course_import.chapters.filter(created_block__isnull=True, status=CourseImportChapter.Status.QUEUED).update(
            status=CourseImportChapter.Status.PENDING,
            selected=False,
            updated_at=timezone.now(),
        )
        course_import.chapters.filter(created_block__isnull=True, status=CourseImportChapter.Status.PROCESSING).update(
            status=CourseImportChapter.Status.QUEUED,
            selected=True,
            updated_at=timezone.now(),
        )
        course_import.chapters.filter(created_block__isnull=True, pk__in=valid_ids).update(
            selected=True,
            status=CourseImportChapter.Status.QUEUED,
            last_error="",
            updated_at=timezone.now(),
        )
        course_import.chapters.exclude(pk__in=valid_ids).filter(
            created_block__isnull=True,
            status__in=[CourseImportChapter.Status.PENDING, CourseImportChapter.Status.SKIPPED],
        ).update(selected=False, updated_at=timezone.now())

        _set_import_state(
            course_import.pk,
            status=CourseImport.Status.QUEUED_CREATION,
            progress=_creation_progress(course_import),
            error="",
            current_step="Queued selected chapters for block creation.",
            current_chapter_id=0,
            clear_lease=True,
        )

    _update_import_counts(course_import.pk)
    ensure_course_import_worker_running(course_import.pk)


def pause_course_import(import_id: int) -> None:
    _set_import_state(
        import_id,
        status=CourseImport.Status.PAUSED,
        current_step="Paused. Resume when ready.",
        current_chapter_id=0,
        clear_lease=True,
    )


def retry_failed_course_import_chapters(import_id: int) -> None:
    course_import = CourseImport.objects.get(pk=import_id)
    with transaction.atomic():
        updated = course_import.chapters.filter(
            status=CourseImportChapter.Status.FAILED,
            created_block__isnull=True,
        ).update(
            status=CourseImportChapter.Status.QUEUED,
            selected=True,
            attempt_count=0,
            last_error="",
            updated_at=timezone.now(),
        )
        if not updated:
            return
        _set_import_state(
            import_id,
            status=CourseImport.Status.QUEUED_CREATION,
            progress=_creation_progress(course_import),
            error="",
            current_step="Retrying failed chapters.",
            current_chapter_id=0,
            clear_lease=True,
        )
    _update_import_counts(import_id)
    ensure_course_import_worker_running(import_id)


def _claim_import_lease(import_id: int, *, owner: str, now=None) -> CourseImport | None:
    current_time = now or timezone.now()
    claimed = (
        CourseImport.objects.filter(pk=import_id, status__in=RUNNABLE_IMPORT_STATUSES)
        .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=current_time))
        .update(
            lease_owner=owner,
            lease_expires_at=_lease_expires_at(current_time),
            heartbeat_at=current_time,
            updated_at=current_time,
        )
    )
    if claimed != 1:
        return None
    return CourseImport.objects.select_related("course", "uploaded_by", "current_chapter").get(pk=import_id)


def _analysis_preview_text(file_path: str, start_page: int) -> str:
    preview_text = extract_pdf_page_range(file_path, start_page, start_page)
    return preview_text[:1200].strip()


def _run_analysis_step(course_import: CourseImport) -> bool:
    _set_import_state(
        course_import.pk,
        status=CourseImport.Status.ANALYZING,
        progress=15,
        error="",
        current_step="Detecting chapters from the uploaded PDF.",
        current_chapter_id=0,
        heartbeat=True,
    )
    candidates = analyze_pdf_chapters(course_import.source_file.path)
    if not candidates:
        _set_import_state(
            course_import.pk,
            status=CourseImport.Status.FAILED,
            progress=100,
            error="No readable chapter sections could be detected from this PDF.",
            current_step="",
            current_chapter_id=0,
        )
        return False

    with transaction.atomic():
        course_import.chapters.all().delete()
        for index, candidate in enumerate(candidates, start=1):
            preview_text = (candidate.extracted_text or "").strip()
            if not preview_text:
                preview_text = _analysis_preview_text(course_import.source_file.path, candidate.start_page)
            CourseImportChapter.objects.create(
                course_import=course_import,
                title=candidate.title,
                order=index,
                start_page=candidate.start_page,
                end_page=candidate.end_page,
                confidence=candidate.confidence,
                extracted_text="",
                preview_text=preview_text,
                selected=False,
                status=CourseImportChapter.Status.PENDING,
            )

    _set_import_state(
        course_import.pk,
        status=CourseImport.Status.READY,
        progress=100,
        error="",
        current_step="Review and select up to the next batch of chapters to import.",
        current_chapter_id=0,
    )
    _update_import_counts(course_import.pk)
    return False


def _create_or_reuse_import_block(course_import: CourseImport, chapter: CourseImportChapter) -> CourseBlock:
    if chapter.created_block_id:
        return CourseBlock.objects.get(pk=chapter.created_block_id)

    last_block = course_import.course.blocks.order_by("-order", "-pk").first()
    next_order = (last_block.order + 1) if last_block else 1
    block = CourseBlock.objects.create(
        course=course_import.course,
        title=chapter.title,
        order=next_order,
        regeneration_status=CourseBlock.RegenerationStatus.QUEUED,
        regeneration_progress=5,
        regeneration_error="",
    )
    BlockConfig.objects.get_or_create(block=block)
    chapter.created_block = block
    chapter.save(update_fields=["created_block", "updated_at"])
    return block


def _ensure_import_asset(course_import: CourseImport, chapter: CourseImportChapter, block: CourseBlock) -> ContentAsset:
    asset = block.assets.order_by("pk").first()
    if asset is not None:
        return asset
    asset = ContentAsset(
        block=block,
        uploaded_by=course_import.uploaded_by,
        original_filename=_chapter_asset_filename(chapter),
        extension=".txt",
        include_in_generation=True,
    )
    asset.file.save(_chapter_asset_filename(chapter), ContentFile(chapter.extracted_text.encode("utf-8")), save=False)
    asset.save()
    return asset


def _next_queued_chapter(course_import: CourseImport) -> CourseImportChapter | None:
    return course_import.chapters.filter(status=CourseImportChapter.Status.QUEUED).order_by("order", "pk").first()


def _finalize_creation_phase(course_import: CourseImport) -> bool:
    refresh_course_summary_from_blocks(course_import.course)
    has_failed = course_import.chapters.filter(status=CourseImportChapter.Status.FAILED).exists()
    has_pending = course_import.chapters.filter(
        created_block__isnull=True,
        status__in=[CourseImportChapter.Status.PENDING, CourseImportChapter.Status.SKIPPED],
    ).exists()
    next_status = CourseImport.Status.COMPLETED
    next_step = "All selected chapters have been converted into course blocks."
    if has_failed:
        next_status = CourseImport.Status.ATTENTION
        next_step = "Some chapters need attention before the import is fully complete."
    elif has_pending:
        next_status = CourseImport.Status.READY
        next_step = "Select the next batch of chapters to convert into course blocks."
    _set_import_state(
        course_import.pk,
        status=next_status,
        progress=100,
        error="",
        current_step=next_step,
        current_chapter_id=0,
    )
    _update_import_counts(course_import.pk)
    return False


def _run_creation_step(course_import: CourseImport) -> bool:
    chapter = _next_queued_chapter(course_import)
    if chapter is None:
        return _finalize_creation_phase(course_import)

    attempt_count = int(chapter.attempt_count or 0) + 1
    CourseImportChapter.objects.filter(pk=chapter.pk).update(
        status=CourseImportChapter.Status.PROCESSING,
        attempt_count=attempt_count,
        last_error="",
        updated_at=timezone.now(),
    )
    _set_import_state(
        course_import.pk,
        status=CourseImport.Status.CREATING,
        progress=_creation_progress(course_import),
        error="",
        current_step=f"Creating a block from {chapter.title}.",
        current_chapter_id=chapter.pk,
        heartbeat=True,
    )

    try:
        chapter.refresh_from_db()
        if not chapter.extracted_text.strip():
            chapter.extracted_text = extract_pdf_page_range(
                course_import.source_file.path,
                chapter.start_page,
                chapter.end_page,
            )
            chapter.save(update_fields=["extracted_text", "updated_at"])
        if not chapter.extracted_text.strip():
            raise ValueError(f"No readable text could be extracted from {chapter.title}.")

        block = _create_or_reuse_import_block(course_import, chapter)
        _ensure_import_asset(course_import, chapter, block)
        from standalone.tasks import run_block_creation_processing

        run_block_creation_processing(block.pk)

        CourseImportChapter.objects.filter(pk=chapter.pk).update(
            status=CourseImportChapter.Status.COMPLETED,
            selected=False,
            last_error="",
            updated_at=timezone.now(),
        )
    except Exception as exc:  # noqa: BLE001
        max_retries = max(1, int(getattr(settings, "COURSE_IMPORT_MAX_RETRIES", 2) or 2))
        next_status = (
            CourseImportChapter.Status.QUEUED
            if attempt_count < max_retries
            else CourseImportChapter.Status.FAILED
        )
        CourseImportChapter.objects.filter(pk=chapter.pk).update(
            status=next_status,
            selected=(next_status == CourseImportChapter.Status.QUEUED),
            last_error=str(exc),
            updated_at=timezone.now(),
        )
        _set_import_state(
            course_import.pk,
            status=CourseImport.Status.QUEUED_CREATION,
            progress=_creation_progress(course_import),
            error=str(exc) if next_status == CourseImportChapter.Status.FAILED else "",
            current_step=f"Retry queued for {chapter.title}." if next_status == CourseImportChapter.Status.QUEUED else f"{chapter.title} failed and the worker will continue.",
            current_chapter_id=0,
        )
        _update_import_counts(course_import.pk)
        refreshed = CourseImport.objects.get(pk=course_import.pk)
        if refreshed.status == CourseImport.Status.PAUSED:
            return False
        if refreshed.chapters.filter(status=CourseImportChapter.Status.QUEUED).exists():
            return True
        return _finalize_creation_phase(refreshed)

    _update_import_counts(course_import.pk)
    refreshed = CourseImport.objects.get(pk=course_import.pk)
    if refreshed.status == CourseImport.Status.PAUSED:
        return False
    if refreshed.chapters.filter(status=CourseImportChapter.Status.QUEUED).exists():
        _set_import_state(
            course_import.pk,
            status=CourseImport.Status.QUEUED_CREATION,
            progress=_creation_progress(refreshed),
            error="",
            current_step="Continuing with the next queued chapter.",
            current_chapter_id=0,
        )
        return True
    return _finalize_creation_phase(refreshed)


def run_course_import_worker_once(import_id: int | None = None, *, chain_successor: bool = True) -> bool:
    owner = _worker_owner()
    if import_id is None:
        import_id = (
            CourseImport.objects.filter(status__in=RUNNABLE_IMPORT_STATUSES)
            .order_by("updated_at", "pk")
            .values_list("pk", flat=True)
            .first()
        )
        if import_id is None:
            return False

    course_import = _claim_import_lease(import_id, owner=owner)
    if course_import is None:
        return False

    continue_work = False
    try:
        if course_import.status in {CourseImport.Status.QUEUED_ANALYSIS, CourseImport.Status.ANALYZING}:
            continue_work = _run_analysis_step(course_import)
        elif course_import.status in {CourseImport.Status.QUEUED_CREATION, CourseImport.Status.CREATING}:
            continue_work = _run_creation_step(course_import)
    finally:
        _release_import_lease(course_import.pk, owner=owner)

    if chain_successor and continue_work:
        refreshed = CourseImport.objects.filter(pk=course_import.pk).first()
        if refreshed is not None and refreshed.status in RUNNABLE_IMPORT_STATUSES and not course_import_lease_is_active(refreshed):
            _spawn_course_import_worker(refreshed.pk)
    return True
