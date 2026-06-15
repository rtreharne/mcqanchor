from celery import shared_task

from standalone.models import ContentAsset, Course, CourseBlock
from standalone.services.content import (
    ingest_content_asset,
    regenerate_block_descriptions_and_objectives,
    regenerate_course_descriptions_and_objectives,
)


@shared_task(ignore_result=True)
def process_content_asset_task(asset_id: int) -> None:
    asset = ContentAsset.objects.select_related("block", "block__course").get(pk=asset_id)
    try:
        ingest_content_asset(asset)
        regenerate_course_descriptions_and_objectives(asset.block.course)
    except Exception as exc:  # noqa: BLE001
        asset.processing_status = ContentAsset.ProcessingStatus.FAILED
        asset.processing_error = str(exc)
        asset.save(update_fields=["processing_status", "processing_error", "updated_at"])
        raise


@shared_task(ignore_result=True)
def regenerate_course_content_task(course_id: int) -> None:
    course = Course.objects.prefetch_related("blocks__assets", "blocks__learning_objectives").get(pk=course_id)
    regenerate_course_descriptions_and_objectives(course)


@shared_task(ignore_result=True)
def regenerate_block_content_task(block_id: int) -> None:
    block = CourseBlock.objects.select_related("course").prefetch_related("assets", "learning_objectives").get(pk=block_id)
    regenerate_block_descriptions_and_objectives(block)
