from decimal import Decimal

from django.utils import timezone

from standalone.models import Enrollment, PracticeAttempt, QuestionBankItem


def refresh_enrollment_metrics(enrollment: Enrollment) -> None:
    today = timezone.localdate()
    official_attempts = enrollment.practice_attempts.filter(attempt_type=PracticeAttempt.AttemptType.PRACTICE)
    previous_scores = {
        "mastery": enrollment.mastery_score,
        "coverage": enrollment.coverage_score,
        "engagement": enrollment.engagement_score,
        "target": enrollment.target_score,
    }

    asked_questions = QuestionBankItem.objects.filter(
        attempt_questions__attempt__enrollment=enrollment,
        attempt_questions__attempt__attempt_type=PracticeAttempt.AttemptType.PRACTICE,
    ).distinct()
    total_questions = asked_questions.count() or 1
    correct_questions = asked_questions.filter(
        attempt_questions__attempt__enrollment=enrollment,
        attempt_questions__is_correct=True,
    ).distinct().count()

    mastery = Decimal(correct_questions * 100 / total_questions).quantize(Decimal("0.01"))

    total_objectives = enrollment.course.learning_objectives.filter(block__available_from__lte=today).count() or 1
    covered_objectives = enrollment.course.learning_objectives.filter(
        block__available_from__lte=today,
        question_bank_items__attempt_questions__attempt__enrollment=enrollment
    ).distinct().count()
    coverage = Decimal(covered_objectives * 100 / total_objectives).quantize(Decimal("0.01"))

    engagement = Decimal(min(100, official_attempts.count() * 10)).quantize(Decimal("0.01"))

    released_blocks = enrollment.course.blocks.filter(available_from__lte=today).count() or 1
    touched_blocks = enrollment.course.blocks.filter(available_from__lte=today, practice_attempts__enrollment=enrollment).distinct().count()
    target = Decimal(touched_blocks * 100 / released_blocks).quantize(Decimal("0.01"))

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
