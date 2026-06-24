from decimal import Decimal

from django.utils import timezone

from standalone.models import Enrollment, PracticeAttempt, PracticeAttemptQuestion
from standalone.services.preview import _engagement_metrics_from_answer_dates


def _decimal_percent(value: float) -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal("0.01"))


def refresh_enrollment_metrics(enrollment: Enrollment) -> None:
    today = timezone.localdate()
    previous_scores = {
        "mastery": enrollment.mastery_score,
        "coverage": enrollment.coverage_score,
        "engagement": enrollment.engagement_score,
        "target": enrollment.target_score,
    }
    course = enrollment.course
    blocks = list(course.blocks.prefetch_related("learning_objectives").order_by("order", "created_at"))
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
        mastery = coverage = engagement = target = Decimal("0.00")
    else:
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
                    "mastery": correct_count * 100 / completed_count if completed_count else 0.0,
                    "coverage": covered_objectives * 100 / total_objectives if total_objectives else 0.0,
                    "engagement": float(engagement_metrics["engagement"]),
                    "target": min(100, completed_count * 100 / target_question_count),
                }
            )

        block_count = len(block_scores)
        mastery = _decimal_percent(sum(score["mastery"] for score in block_scores) / block_count)
        coverage = _decimal_percent(sum(score["coverage"] for score in block_scores) / block_count)
        engagement = _decimal_percent(sum(score["engagement"] for score in block_scores) / block_count)
        target = _decimal_percent(sum(score["target"] for score in block_scores) / block_count)

    enrollment.mastery_score = mastery
    enrollment.coverage_score = coverage
    enrollment.engagement_score = engagement
    enrollment.target_score = target
    enrollment.mastery_delta = mastery - previous_scores["mastery"]
    enrollment.coverage_delta = coverage - previous_scores["coverage"]
    enrollment.engagement_delta = engagement - previous_scores["engagement"]
    enrollment.target_delta = target - previous_scores["target"]
    enrollment.save(
        update_fields=[
            "mastery_score",
            "coverage_score",
            "engagement_score",
            "target_score",
            "mastery_delta",
            "coverage_delta",
            "engagement_delta",
            "target_delta",
            "updated_at",
        ]
    )
