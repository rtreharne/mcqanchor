from django.utils import timezone

from celery import shared_task

from standalone.models import ContentAsset, Course, CourseBlock, CourseImport
from standalone.services.content import (
    ingest_content_asset,
    regenerate_block_descriptions_and_objectives,
    regenerate_course_descriptions_and_objectives,
)
from standalone.services.course_imports import (
    queue_course_import_analysis,
    queue_course_import_creation,
    run_course_import_worker_once,
)


def run_content_asset_processing(asset_id: int) -> None:
    try:
        asset = ContentAsset.objects.select_related("block", "block__course").get(pk=asset_id)
    except ContentAsset.DoesNotExist:
        return
    try:
        ingest_content_asset(asset)
        regenerate_course_descriptions_and_objectives(asset.block.course)
    except Exception as exc:  # noqa: BLE001
        asset.processing_status = ContentAsset.ProcessingStatus.FAILED
        asset.processing_error = str(exc)
        asset.save(update_fields=["processing_status", "processing_error", "updated_at"])
        raise


def _set_block_regeneration_state(block_id: int, status: str, progress: int, error: str = "") -> None:
    CourseBlock.objects.filter(pk=block_id).update(
        regeneration_status=status,
        regeneration_progress=progress,
        regeneration_error=error,
        updated_at=timezone.now(),
    )


def _map_regeneration_progress(progress: int, start: int = 70, end: int = 100) -> int:
    bounded_progress = max(0, min(progress, 100))
    return min(end, start + round((end - start) * (bounded_progress / 100)))


def run_block_creation_processing(block_id: int) -> None:
    try:
        block = CourseBlock.objects.select_related("course").prefetch_related("assets", "learning_objectives").get(pk=block_id)
    except CourseBlock.DoesNotExist:
        return

    assets = list(block.assets.order_by("pk"))
    _set_block_regeneration_state(block_id, CourseBlock.RegenerationStatus.RUNNING, 10)

    processed_count = 0
    asset_errors: list[str] = []

    try:
        total_assets = len(assets)
        for index, asset in enumerate(assets, start=1):
            try:
                ingest_content_asset(asset)
                processed_count += 1
            except Exception as exc:  # noqa: BLE001
                asset.processing_status = ContentAsset.ProcessingStatus.FAILED
                asset.processing_error = str(exc)
                asset.save(update_fields=["processing_status", "processing_error", "updated_at"])
                asset_errors.append(f"{asset.original_filename}: {exc}")

            ingestion_progress = 10 + round((55 * index) / max(total_assets, 1))
            _set_block_regeneration_state(block_id, CourseBlock.RegenerationStatus.RUNNING, min(ingestion_progress, 65))

        if processed_count == 0:
            _set_block_regeneration_state(
                block_id,
                CourseBlock.RegenerationStatus.FAILED,
                65 if assets else 10,
                "None of the uploaded files could be processed." if asset_errors else "No uploaded files were available to process.",
            )
            return

        regenerate_block_descriptions_and_objectives(
            block,
            progress_callback=lambda progress: _set_block_regeneration_state(
                block_id,
                CourseBlock.RegenerationStatus.RUNNING,
                _map_regeneration_progress(progress),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        current_progress = CourseBlock.objects.filter(pk=block_id).values_list("regeneration_progress", flat=True).first() or 10
        _set_block_regeneration_state(
            block_id,
            CourseBlock.RegenerationStatus.FAILED,
            max(current_progress, 10),
            str(exc),
        )
        raise

    _set_block_regeneration_state(block_id, CourseBlock.RegenerationStatus.IDLE, 0)


def run_block_regeneration(block_id: int) -> None:
    try:
        block = CourseBlock.objects.select_related("course").prefetch_related("assets", "learning_objectives").get(pk=block_id)
    except CourseBlock.DoesNotExist:
        return

    _set_block_regeneration_state(block_id, CourseBlock.RegenerationStatus.RUNNING, 10)
    try:
        regenerate_block_descriptions_and_objectives(
            block,
            progress_callback=lambda progress: _set_block_regeneration_state(
                block_id,
                CourseBlock.RegenerationStatus.RUNNING,
                progress,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        current_progress = CourseBlock.objects.filter(pk=block_id).values_list("regeneration_progress", flat=True).first() or 10
        _set_block_regeneration_state(
            block_id,
            CourseBlock.RegenerationStatus.FAILED,
            max(current_progress, 10),
            str(exc),
        )
        raise

    _set_block_regeneration_state(block_id, CourseBlock.RegenerationStatus.IDLE, 0)


def _set_course_import_state(import_id: int, status: str, progress: int, error: str = "") -> None:
    CourseImport.objects.filter(pk=import_id).update(
        status=status,
        progress=max(0, min(100, progress)),
        error=error,
        updated_at=timezone.now(),
    )

def run_course_import_analysis(import_id: int) -> None:
    queue_course_import_analysis(import_id)
    run_course_import_worker_once(import_id, chain_successor=False)


def run_course_import_block_creation(
    import_id: int,
    selected_chapter_ids: list[int] | None,
    *,
    queue_block_processing: bool = False,
) -> None:
    if selected_chapter_ids is not None:
        queue_course_import_creation(import_id, selected_chapter_ids)
    run_course_import_worker_once(import_id, chain_successor=False)


@shared_task(ignore_result=True)
def process_content_asset_task(asset_id: int) -> None:
    run_content_asset_processing(asset_id)


@shared_task(ignore_result=True)
def regenerate_course_content_task(course_id: int) -> None:
    course = Course.objects.prefetch_related("blocks__assets", "blocks__learning_objectives").get(pk=course_id)
    regenerate_course_descriptions_and_objectives(course)


@shared_task(ignore_result=True)
def regenerate_block_content_task(block_id: int) -> None:
    run_block_regeneration(block_id)


@shared_task(ignore_result=True)
def process_block_creation_task(block_id: int) -> None:
    run_block_creation_processing(block_id)


@shared_task(ignore_result=True)
def analyze_course_pdf_import_task(import_id: int) -> None:
    run_course_import_analysis(import_id)


@shared_task(ignore_result=True)
def create_blocks_from_course_import_task(import_id: int, selected_chapter_ids: list[int]) -> None:
    run_course_import_block_creation(import_id, selected_chapter_ids)
