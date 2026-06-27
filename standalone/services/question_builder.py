from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from standalone.models import ContentChunk, Course, CourseConfig, LearningObjective, QuestionBankItem
from standalone.services.course_imports import course_import_work_is_active


@dataclass(frozen=True)
class QuestionGenerationBudget:
    daily_pairs: int
    total_pairs: int
    daily_cap: int
    total_cap: int

    @property
    def daily_remaining(self) -> int:
        return max(0, self.daily_cap - self.daily_pairs)

    @property
    def total_remaining(self) -> int:
        return max(0, self.total_cap - self.total_pairs)

    @property
    def can_generate(self) -> bool:
        return self.daily_remaining > 0 and self.total_remaining > 0

    @property
    def blocked_reason(self) -> str:
        if self.total_remaining <= 0:
            return "total_cap_reached"
        if self.daily_remaining <= 0:
            return "daily_cap_reached"
        return ""

    @property
    def message(self) -> str:
        if self.total_remaining <= 0:
            return "This course question bank has reached its total stored-pair cap. Increase the cap or remove old questions before generating more."
        if self.daily_remaining <= 0:
            return "This course has reached today's question-bank generation cap. Try again after the next daily reset."
        return ""


@dataclass(frozen=True)
class PracticeValidationReadiness:
    ready: bool
    ready_count: int
    threshold: int

    @property
    def detail(self) -> str:
        if self.ready:
            return (
                f"Practice validation is ready. "
                f"{self.ready_count:,} approved released practice questions are available."
            )
        return (
            f"Practice validation unlocks at {self.threshold:,} approved released practice questions. "
            f"Currently ready: {self.ready_count:,}."
        )


@dataclass(frozen=True)
class BuilderTarget:
    block_id: int
    objective_id: int
    block_order: int
    objective_position: int
    objective_coverage: int
    block_coverage: int


@dataclass(frozen=True)
class QuestionBankBuilderPassResult:
    course_id: int
    generated: bool
    practice_question_id: int | None = None
    validation_question_id: int | None = None
    skipped_reason: str = ""
    error: str = ""


def _practice_question_queryset(course: Course):
    return QuestionBankItem.objects.filter(
        course=course,
        bank_type=QuestionBankItem.BankType.PRACTICE,
    )


def _validation_question_queryset(course: Course):
    return QuestionBankItem.objects.filter(
        course=course,
        bank_type=QuestionBankItem.BankType.VALIDATION,
    )


def practice_bank_count(course: Course) -> int:
    return _practice_question_queryset(course).count()


def validation_bank_count(course: Course) -> int:
    return _validation_question_queryset(course).count()


def released_practice_bank_count(course: Course, *, approved_only: bool = False, today=None) -> int:
    today = today or timezone.localdate()
    queryset = _practice_question_queryset(course).filter(block__available_from__lte=today)
    if approved_only:
        queryset = queryset.filter(status=QuestionBankItem.Status.APPROVED)
    return queryset.count()


def course_question_generation_budget(course: Course, *, now=None) -> QuestionGenerationBudget:
    now = now or timezone.now()
    today = timezone.localdate(now)
    practice_questions = _practice_question_queryset(course)
    return QuestionGenerationBudget(
        daily_pairs=practice_questions.filter(created_at__date=today).count(),
        total_pairs=practice_questions.count(),
        daily_cap=max(0, int(course.config.question_bank_builder_daily_pair_cap or 0)),
        total_cap=max(0, int(course.config.question_bank_builder_total_pair_cap or 0)),
    )


def practice_validation_readiness(course: Course, *, today=None) -> PracticeValidationReadiness:
    threshold = max(1, int(settings.PRACTICE_VALIDATION_READY_THRESHOLD or 1000))
    ready_count = released_practice_bank_count(course, approved_only=True, today=today)
    return PracticeValidationReadiness(
        ready=ready_count >= threshold,
        ready_count=ready_count,
        threshold=threshold,
    )


def practice_validation_unavailable_message(course: Course, *, today=None) -> str:
    return practice_validation_readiness(course, today=today).detail


def live_generation_unavailable_message(course: Course, *, now=None) -> str:
    budget = course_question_generation_budget(course, now=now)
    if budget.message:
        return (
            f"{budget.message} "
            f"Stored practice questions: {budget.total_pairs:,}. "
            f"Remaining today: {budget.daily_remaining:,}."
        )
    return "This course question bank is still warming up. Try again shortly."


def question_bank_builder_status(config: CourseConfig, *, now=None) -> dict[str, str]:
    now = now or timezone.now()
    if not config.question_bank_builder_enabled:
        return {"label": "Paused", "class_name": "is-paused"}
    if config.question_bank_builder_lease_expires_at and config.question_bank_builder_lease_expires_at > now:
        return {"label": "Running", "class_name": "is-running"}
    if config.question_bank_builder_last_error:
        return {"label": "Attention", "class_name": "is-failed"}
    return {"label": "Active", "class_name": "is-ready"}


def acquire_question_bank_builder_lease(config_id: int, *, now=None) -> bool:
    now = now or timezone.now()
    lease_seconds = max(60, int(settings.QUESTION_BANK_BUILDER_LEASE_SECONDS or 300))
    updated = (
        CourseConfig.objects.filter(
            pk=config_id,
            question_bank_builder_enabled=True,
        )
        .filter(
            Q(question_bank_builder_lease_expires_at__isnull=True)
            | Q(question_bank_builder_lease_expires_at__lte=now)
        )
        .update(
            question_bank_builder_lease_expires_at=now + timedelta(seconds=lease_seconds),
            question_bank_builder_last_run_at=now,
        )
    )
    return updated == 1


def release_question_bank_builder_lease(config_id: int) -> None:
    CourseConfig.objects.filter(pk=config_id).update(question_bank_builder_lease_expires_at=None)


def ordered_builder_targets(course: Course) -> list[BuilderTarget]:
    eligible_block_ids = set(
        ContentChunk.objects.filter(course=course, asset__include_in_generation=True).values_list("block_id", flat=True)
    )
    if not eligible_block_ids:
        return []

    objectives = list(
        LearningObjective.objects.filter(course=course, block_id__in=eligible_block_ids)
        .select_related("block")
        .order_by("block__order", "position", "pk")
    )
    if not objectives:
        return []

    objective_ids = [objective.pk for objective in objectives]
    coverage_rows = (
        _practice_question_queryset(course)
        .filter(
            status=QuestionBankItem.Status.APPROVED,
            learning_objective_id__in=objective_ids,
        )
        .values("learning_objective_id")
        .annotate(total=Count("id"))
    )
    objective_coverage = {int(row["learning_objective_id"]): int(row["total"] or 0) for row in coverage_rows}

    block_rows = (
        _practice_question_queryset(course)
        .filter(
            status=QuestionBankItem.Status.APPROVED,
            block_id__in=eligible_block_ids,
        )
        .values("block_id")
        .annotate(total=Count("id"))
    )
    block_coverage = {int(row["block_id"]): int(row["total"] or 0) for row in block_rows}

    targets = [
        BuilderTarget(
            block_id=objective.block_id,
            objective_id=objective.pk,
            block_order=int(objective.block.order or 0),
            objective_position=int(objective.position or 0),
            objective_coverage=objective_coverage.get(objective.pk, 0),
            block_coverage=block_coverage.get(objective.block_id, 0),
        )
        for objective in objectives
    ]
    targets.sort(
        key=lambda target: (
            target.objective_coverage,
            target.block_coverage,
            target.block_order,
            target.objective_position,
            target.objective_id,
        )
    )
    return targets


def run_course_question_bank_builder_pass(course_id: int, *, now=None) -> QuestionBankBuilderPassResult:
    now = now or timezone.now()
    if course_import_work_is_active():
        return QuestionBankBuilderPassResult(course_id=course_id, generated=False, skipped_reason="course_import_active")
    config = CourseConfig.objects.select_related("course").filter(course_id=course_id).first()
    if config is None:
        return QuestionBankBuilderPassResult(course_id=course_id, generated=False, skipped_reason="missing_config")
    if not config.question_bank_builder_enabled:
        return QuestionBankBuilderPassResult(course_id=course_id, generated=False, skipped_reason="paused")
    if not acquire_question_bank_builder_lease(config.pk, now=now):
        return QuestionBankBuilderPassResult(course_id=course_id, generated=False, skipped_reason="lease_unavailable")

    try:
        config = CourseConfig.objects.select_related("course").get(pk=config.pk)
        course = config.course
        budget = course_question_generation_budget(course, now=now)
        if not budget.can_generate:
            CourseConfig.objects.filter(pk=config.pk).update(question_bank_builder_last_error="")
            return QuestionBankBuilderPassResult(
                course_id=course.pk,
                generated=False,
                skipped_reason=budget.blocked_reason,
            )

        targets = ordered_builder_targets(course)
        if not targets:
            CourseConfig.objects.filter(pk=config.pk).update(question_bank_builder_last_error="")
            return QuestionBankBuilderPassResult(course_id=course.pk, generated=False, skipped_reason="no_targets")

        from standalone.services.questions import (
            QuestionGenerationUnavailableError,
            generate_question_pair_for_block,
        )

        last_error = ""
        for target in targets:
            block = course.blocks.filter(pk=target.block_id).first()
            if block is None:
                continue
            try:
                practice, validation = generate_question_pair_for_block(
                    block,
                    preferred_objective_ids=[target.objective_id],
                    strict_preferred_objectives=True,
                    include_future_blocks=True,
                    relax_similarity_checks=True,
                    raise_generation_errors=True,
                )
            except QuestionGenerationUnavailableError as exc:
                CourseConfig.objects.filter(pk=config.pk).update(question_bank_builder_last_error="")
                return QuestionBankBuilderPassResult(
                    course_id=course.pk,
                    generated=False,
                    skipped_reason="budget_blocked",
                    error=str(exc),
                )
            except Exception as exc:  # keeps the minute loop resilient per course
                last_error = str(exc)
                continue
            if practice is None or validation is None:
                continue
            CourseConfig.objects.filter(pk=config.pk).update(
                question_bank_builder_last_generated_at=now,
                question_bank_builder_last_error="",
            )
            return QuestionBankBuilderPassResult(
                course_id=course.pk,
                generated=True,
                practice_question_id=practice.pk,
                validation_question_id=validation.pk,
            )

        CourseConfig.objects.filter(pk=config.pk).update(question_bank_builder_last_error=last_error)
        return QuestionBankBuilderPassResult(
            course_id=course.pk,
            generated=False,
            skipped_reason="generation_failed",
            error=last_error,
        )
    finally:
        release_question_bank_builder_lease(config.pk)


def run_question_bank_builder_cycle(*, now=None) -> list[QuestionBankBuilderPassResult]:
    now = now or timezone.now()
    course_ids = list(
        CourseConfig.objects.filter(question_bank_builder_enabled=True)
        .order_by("course_id")
        .values_list("course_id", flat=True)
    )
    return [run_course_question_bank_builder_pass(course_id, now=now) for course_id in course_ids]
