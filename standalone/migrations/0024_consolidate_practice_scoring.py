from decimal import Decimal
import math

import django.core.validators
from django.db import migrations, models
from django.utils import timezone


def _decimal_percent(value: float) -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal("0.01"))


def _engagement_half_life_days(course_config) -> int | None:
    value = getattr(course_config, "engagement_half_life_days", None)
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _engagement_release_date(block_config):
    value = getattr(block_config, "release_date", None)
    if value is None:
        return None
    if timezone.is_aware(value):
        return timezone.localtime(value).date()
    return value.date()


def _engagement_score(course_config, block_config, answer_dates: list, target_question_count: int) -> float:
    completed_count = len(answer_dates)
    raw_score = min(100.0, completed_count * 100.0 / max(1, target_question_count))
    release_date = _engagement_release_date(block_config)
    half_life_days = _engagement_half_life_days(course_config)
    if release_date is None or half_life_days is None:
        return round(raw_score, 2)
    weighted_count = sum(
        math.pow(0.5, max(0, (answered_on - release_date).days) / max(1, half_life_days))
        for answered_on in answer_dates
    )
    return round(min(raw_score, weighted_count * 100.0 / max(1, target_question_count)), 2)


def consolidate_practice_scoring(apps, schema_editor):
    CourseConfig = apps.get_model("standalone", "CourseConfig")
    CourseBlock = apps.get_model("standalone", "CourseBlock")
    BlockConfig = apps.get_model("standalone", "BlockConfig")
    Enrollment = apps.get_model("standalone", "Enrollment")
    PracticeAttempt = apps.get_model("standalone", "PracticeAttempt")
    PracticeAttemptQuestion = apps.get_model("standalone", "PracticeAttemptQuestion")

    for config in CourseConfig.objects.all():
        config.engagement_weight = int(config.engagement_weight or 0) + int(config.target_weight or 0)
        config.save(update_fields=["engagement_weight", "updated_at"])

    today = timezone.localdate()
    practice_attempt_type = "practice"
    block_config_map = {config.block_id: config for config in BlockConfig.objects.all()}

    for enrollment in Enrollment.objects.select_related("course"):
        course_config = CourseConfig.objects.filter(course_id=enrollment.course_id).first()
        if course_config is None:
            continue
        blocks = list(CourseBlock.objects.filter(course_id=enrollment.course_id).order_by("order", "created_at"))

        metric_blocks = []
        for block in blocks:
            has_pre_engagement_answers = False
            if bool(getattr(course_config, "allow_pre_engagement", False)):
                has_pre_engagement_answers = PracticeAttemptQuestion.objects.filter(
                    attempt__enrollment_id=enrollment.pk,
                    attempt__attempt_type=practice_attempt_type,
                    question__block_id=block.pk,
                ).exists()
            if block.available_from <= today or has_pre_engagement_answers:
                metric_blocks.append(block)
        if not metric_blocks:
            metric_blocks = blocks

        if not metric_blocks:
            mastery = coverage = engagement = Decimal("0.00")
        else:
            mastery_scores = []
            coverage_scores = []
            engagement_scores = []
            for block in metric_blocks:
                answers = PracticeAttemptQuestion.objects.filter(
                    attempt__enrollment_id=enrollment.pk,
                    attempt__attempt_type=practice_attempt_type,
                    question__block_id=block.pk,
                )
                completed_count = answers.count()
                correct_count = answers.filter(is_correct=True).count()
                total_objectives = block.learning_objectives.count()
                covered_objectives = answers.filter(
                    is_correct=True,
                    question__learning_objective__isnull=False,
                ).values("question__learning_objective_id").distinct().count()
                block_config = block_config_map.get(block.pk)
                target_question_count = max(1, int(getattr(block_config, "target_question_count", 20) or 20))
                answer_dates = [answer.created_at.date() for answer in answers]
                mastery_scores.append(correct_count * 100 / completed_count if completed_count else 0.0)
                coverage_scores.append(covered_objectives * 100 / total_objectives if total_objectives else 0.0)
                engagement_scores.append(
                    _engagement_score(course_config, block_config, answer_dates, target_question_count)
                )

            block_count = len(metric_blocks)
            mastery = _decimal_percent(sum(mastery_scores) / block_count)
            coverage = _decimal_percent(sum(coverage_scores) / block_count)
            engagement = _decimal_percent(sum(engagement_scores) / block_count)

        enrollment.mastery_score = mastery
        enrollment.coverage_score = coverage
        enrollment.engagement_score = engagement
        enrollment.mastery_delta = Decimal("0.00")
        enrollment.coverage_delta = Decimal("0.00")
        enrollment.engagement_delta = Decimal("0.00")
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


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0023_blockproject_projectassignment_projectartifact_and_more"),
    ]

    operations = [
        migrations.RunPython(consolidate_practice_scoring, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="courseconfig",
            name="target_weight",
        ),
        migrations.AlterField(
            model_name="courseconfig",
            name="engagement_weight",
            field=models.PositiveSmallIntegerField(
                default=30,
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)],
            ),
        ),
        migrations.RemoveField(
            model_name="blockconfig",
            name="target_weight_override",
        ),
        migrations.RemoveField(
            model_name="enrollment",
            name="target_score",
        ),
        migrations.RemoveField(
            model_name="enrollment",
            name="target_delta",
        ),
        migrations.RemoveField(
            model_name="practiceattempt",
            name="target_delta",
        ),
    ]
