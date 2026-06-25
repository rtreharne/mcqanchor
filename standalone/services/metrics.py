from decimal import Decimal

from django.utils import timezone

from standalone.models import Enrollment, PracticeAttempt, PracticeAttemptQuestion
from standalone.services.preview import _engagement_metrics_from_answer_dates
from standalone.services.practice_scoring import combine_block_practice_metrics


def _decimal_percent(value: float) -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal("0.01"))


def enrollment_practice_metrics_snapshot(enrollment: Enrollment) -> dict:
    today = timezone.localdate()
    course = enrollment.course
    blocks = list(course.blocks.select_related("config").prefetch_related("learning_objectives").order_by("order", "created_at"))
    allow_pre_engagement = bool(getattr(course.config, "allow_pre_engagement", False))
    metric_blocks = [
        block
        for block in blocks
        if block.available_from <= today
        or (
            allow_pre_engagement
            and PracticeAttemptQuestion.objects.filter(
                attempt__enrollment=enrollment,
                attempt__attempt_type=PracticeAttempt.AttemptType.PRACTICE,
                question__block=block,
            ).exists()
        )
    ] or blocks

    if not metric_blocks:
        return {
            "mastery": 0.0,
            "coverage": 0.0,
            "engagement": 0.0,
            "overall": 0.0,
            "weights": {},
        }

    block_scores = []
    for block in metric_blocks:
        answers = PracticeAttemptQuestion.objects.filter(
            attempt__enrollment=enrollment,
            attempt__attempt_type=PracticeAttempt.AttemptType.PRACTICE,
            question__block=block,
        ).select_related("question")
        completed_count = answers.count()
        correct_count = answers.filter(is_correct=True).count()
        total_objectives = block.learning_objectives.count()
        covered_objectives = answers.filter(
            is_correct=True,
            question__learning_objective__isnull=False,
        ).values("question__learning_objective_id").distinct().count()
        target_question_count = max(1, block.preview_target_question_count)
        engagement_metrics = _engagement_metrics_from_answer_dates(
            course,
            block,
            [answer.created_at.date() for answer in answers],
            target_question_count=target_question_count,
        )
        block_scores.append(
            {
                "block": block,
                "metrics": {
                    "mastery": correct_count * 100 / completed_count if completed_count else 0.0,
                    "coverage": covered_objectives * 100 / total_objectives if total_objectives else 0.0,
                    "engagement": float(engagement_metrics["engagement"]),
                },
            }
        )

    return combine_block_practice_metrics(course, block_scores)


def refresh_enrollment_metrics(enrollment: Enrollment) -> None:
    previous_scores = {
        "mastery": enrollment.mastery_score,
        "coverage": enrollment.coverage_score,
        "engagement": enrollment.engagement_score,
    }
    snapshot = enrollment_practice_metrics_snapshot(enrollment)
    mastery = _decimal_percent(snapshot["mastery"])
    coverage = _decimal_percent(snapshot["coverage"])
    engagement = _decimal_percent(snapshot["engagement"])

    enrollment.mastery_score = mastery
    enrollment.coverage_score = coverage
    enrollment.engagement_score = engagement
    enrollment.mastery_delta = mastery - previous_scores["mastery"]
    enrollment.coverage_delta = coverage - previous_scores["coverage"]
    enrollment.engagement_delta = engagement - previous_scores["engagement"]
    enrollment.save(
        update_fields=[
            "mastery_score",
            "coverage_score",
            "engagement_score",
            "mastery_delta",
            "coverage_delta",
            "engagement_delta",
            "updated_at",
        ]
    )
