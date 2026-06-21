import hashlib
import hmac
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from standalone.models import (
    CourseBlock,
    Enrollment,
    EnrollmentQuestionState,
    PracticeAttempt,
    PracticeAttemptQuestion,
    QuestionBankItem,
    ValidationAttempt,
    ValidationAttemptMessage,
    ValidationAttemptQuestion,
    ValidationAuditPrompt,
    ValidationBooking,
    ValidationEvent,
)
from standalone.services.preview import (
    PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS,
    WAQ_ALIGNMENT_THRESHOLD,
    _draft_written_answer_alignment,
    _feedback_text,
    _grade_question_response,
    _grade_written_answer_response,
    _normalize_submitted_answers,
    _normalize_written_answer_text,
    normalize_explanation_text,
)
from standalone.services.questions import (
    coding_question_matches_expected_language,
    coding_question_quality_sort_key,
    generate_question_pair_for_block,
    preferred_coding_language_for_block,
)


VALIDATION_PRACTICE_DEFAULT_QUESTION_COUNT = 10
VALIDATION_PRACTICE_DEFAULT_TIME_LIMIT_MINUTES = 20
VALIDATION_DRAFT_SESSION_KEY = "standalone_validation_drafts"
VALIDATION_UI_SESSION_KEY = "standalone_validation_ui"
VALIDATION_NAVIGATION_GRACE_SECONDS = 10
VALIDATION_SKIPPED_TEXT = "Skipped."
PRACTICE_SKIPPED_SENTINEL = "__SKIPPED__"
VALIDATION_ROOM_CODE_OPTION_COUNT = 4
VALIDATION_ROOM_CODE_ADJECTIVES = (
    "amber",
    "brisk",
    "calm",
    "daring",
    "eager",
    "fizzy",
    "gentle",
    "hidden",
    "icy",
    "jolly",
    "keen",
    "lively",
    "mellow",
    "nimble",
    "opal",
    "plucky",
    "quiet",
    "rapid",
    "silver",
    "tidy",
    "upbeat",
    "vivid",
    "witty",
    "young",
    "zesty",
)
VALIDATION_ROOM_CODE_ANIMALS = (
    "ant",
    "badger",
    "crane",
    "dolphin",
    "egret",
    "fox",
    "gecko",
    "heron",
    "ibis",
    "jackal",
    "koala",
    "lemur",
    "newt",
    "otter",
    "panda",
    "quail",
    "rabbit",
    "seal",
    "tiger",
    "urchin",
    "viper",
    "walrus",
    "yak",
    "zebra",
)
VALIDATION_OFFICIAL_INSTRUCTION_LINES = (
    "You are about to undertake a validation quiz.",
    "You must use this device only to complete your validation.",
    "This session is invigilated. You may be asked to provide photo ID.",
    "You must not discuss your quiz questions or answers with anyone else undertaking a validation.",
    "Your validation is unique to you.",
    "You are not permitted to use additional resources to complete your validation.",
    "You are not permitted to use generative AI during the validation.",
    "Navigating away from your validation for more than 10 seconds will reset your validation attempt and this will be flagged with your teacher.",
    "You are free to leave when you have completed your validation.",
    "If you need a rest break during the session then please raise your hand so that your invigilator can pause your validation. This is important so that you do not miss any attendance audits.",
)


class ValidationFlowError(Exception):
    pass


@dataclass
class ValidationProgress:
    current_index: int
    total_questions: int
    answered_count: int
    remaining_count: int


def _question_seed(seed_key: str, question_id: int, salt: str) -> str:
    return hashlib.sha256(f"{seed_key}:{salt}:{question_id}".encode("utf-8")).hexdigest()


def _shuffle_options(options: list[str], seed_key: str, question_id: int) -> list[str]:
    deduped = []
    for option in options:
        cleaned = str(option).strip()
        if cleaned and cleaned not in deduped:
            deduped.append(cleaned)
    return [
        item[1]
        for item in sorted(
            [(_question_seed(seed_key, question_id, option), option) for option in deduped],
            key=lambda item: (item[0], item[1]),
        )
    ]


def _validation_seed_key(event: ValidationEvent, enrollment: Enrollment) -> str:
    return f"event:{event.pk}:enrollment:{enrollment.pk}:mode:{event.mode}"


def _released_validation_blocks(course) -> list[CourseBlock]:
    return list(course.blocks.filter(available_from__lte=timezone.localdate()).order_by("order", "created_at"))


def _released_validation_questions(course, *, include_written: bool = True, blocks: list[CourseBlock] | None = None):
    queryset = course.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.VALIDATION,
        status=QuestionBankItem.Status.APPROVED,
        block__available_from__lte=timezone.localdate(),
    ).select_related("block", "learning_objective", "source_chunk")
    if blocks is not None:
        queryset = queryset.filter(block__in=blocks)
    if not include_written:
        queryset = queryset.exclude(question_type=QuestionBankItem.QuestionType.WAQ)
    return queryset


def _released_practice_questions(course, *, include_written: bool = True, blocks: list[CourseBlock] | None = None):
    queryset = course.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        block__available_from__lte=timezone.localdate(),
    ).select_related("block", "learning_objective", "source_chunk")
    if blocks is not None:
        queryset = queryset.filter(block__in=blocks)
    if not include_written:
        queryset = queryset.exclude(question_type=QuestionBankItem.QuestionType.WAQ)
    return queryset


def _filter_mismatched_coding_questions(questions: list[QuestionBankItem]) -> list[QuestionBankItem]:
    preferred_by_block: dict[int, str] = {}
    filtered: list[QuestionBankItem] = []
    for question in questions:
        if not question.is_coding_question:
            filtered.append(question)
            continue
        preferred_language = preferred_by_block.get(question.block_id)
        if preferred_language is None:
            preferred_language = preferred_coding_language_for_block(question.block)
            preferred_by_block[question.block_id] = preferred_language
        if coding_question_matches_expected_language(question, preferred_language):
            filtered.append(question)
    return filtered


def _expand_seen_pair_ids(question_ids: set[int]) -> set[int]:
    expanded = {int(question_id) for question_id in question_ids if question_id}
    if not expanded:
        return expanded
    linked_ids = QuestionBankItem.objects.filter(pk__in=expanded).values_list("linked_question_id", flat=True)
    expanded.update(int(linked_id) for linked_id in linked_ids if linked_id)
    reverse_linked_ids = QuestionBankItem.objects.filter(linked_question_id__in=expanded).values_list("pk", flat=True)
    expanded.update(int(question_id) for question_id in reverse_linked_ids if question_id)
    return expanded


def _seen_question_ids_for_enrollment(enrollment: Enrollment, course) -> set[int]:
    seen_ids = {
        int(question_id)
        for question_id in EnrollmentQuestionState.objects.filter(
            enrollment=enrollment,
            question__course=course,
            times_presented__gt=0,
        ).values_list("question_id", flat=True)
    }
    seen_ids.update(
        int(question_id)
        for question_id in PracticeAttemptQuestion.objects.filter(
            attempt__enrollment=enrollment,
            question__course=course,
        ).values_list("question_id", flat=True)
    )
    seen_ids.update(
        int(question_id)
        for question_id in ValidationAttemptQuestion.objects.filter(
            attempt__enrollment=enrollment,
            question__course=course,
        ).values_list("question_id", flat=True)
    )
    return _expand_seen_pair_ids(seen_ids)


def _scored_validation_questions(course, enrollment: Enrollment, *, include_written: bool = True, blocks: list[CourseBlock] | None = None):
    questions = _filter_mismatched_coding_questions(list(_released_validation_questions(course, include_written=include_written, blocks=blocks)))
    prior_correct_ids = {
        question_id
        for question_id in ValidationAttemptQuestion.objects.filter(
            attempt__enrollment=enrollment,
            attempt__status=ValidationAttempt.Status.COMPLETED,
            is_correct=True,
            question__course=course,
        ).values_list("question_id", flat=True)
    }
    prior_attempt_ids = set(
        ValidationAttemptQuestion.objects.filter(
            attempt__enrollment=enrollment,
            question__course=course,
        ).values_list("question_id", flat=True)
    )
    scored = []
    for question in questions:
        if question.pk in prior_correct_ids:
            continue
        scored.append(
            (
                0 if question.pk not in prior_attempt_ids else 1,
                question.block.order,
                question.learning_objective.position if question.learning_objective_id else 999,
                *coding_question_quality_sort_key(question),
                question.created_at,
                question.pk,
                question,
            )
        )
    scored.sort()
    return [item[-1] for item in scored]


def _scored_practice_validation_questions(course, enrollment: Enrollment, *, include_written: bool = True, blocks: list[CourseBlock] | None = None):
    seen_ids = _seen_question_ids_for_enrollment(enrollment, course)
    questions = _filter_mismatched_coding_questions(list(_released_practice_questions(course, include_written=include_written, blocks=blocks)))
    scored = []
    for question in questions:
        if question.pk in seen_ids or int(question.linked_question_id or 0) in seen_ids:
            continue
        scored.append(
            (
                question.block.order,
                question.learning_objective.position if question.learning_objective_id else 999,
                *coding_question_quality_sort_key(question),
                question.created_at,
                question.pk,
                question,
            )
        )
    scored.sort()
    return [item[-1] for item in scored]


def _pick_locked_questions(
    course,
    enrollment: Enrollment,
    question_count: int,
    *,
    include_written: bool = True,
    seed_key: str = "",
    blocks: list[CourseBlock] | None = None,
):
    available = _scored_validation_questions(course, enrollment, include_written=include_written, blocks=blocks)
    if not available:
        return []
    available_by_type: dict[str, list[QuestionBankItem]] = {
        QuestionBankItem.QuestionType.MCQ: [],
        QuestionBankItem.QuestionType.NUM: [],
        QuestionBankItem.QuestionType.MAQ: [],
        QuestionBankItem.QuestionType.WAQ: [],
    }
    for question in available:
        available_by_type.setdefault(question.question_type, []).append(question)

    for question_type, questions in available_by_type.items():
        if seed_key:
            available_by_type[question_type] = sorted(
                questions,
                key=lambda question: (
                    question.block.order,
                    _question_seed(seed_key, question.pk, f"lock:{question_type}"),
                    question.pk,
                ),
            )
        else:
            available_by_type[question_type] = list(questions)

    selected: list[QuestionBankItem] = []
    selected_type_counts: dict[str, int] = defaultdict(int)
    for _index in range(question_count):
        chosen_question = None
        for question_type in _practice_validation_type_preference(course, selected_type_counts, len(selected), question_count):
            queue = available_by_type.get(question_type) or []
            if queue:
                chosen_question = queue.pop(0)
                break
        if chosen_question is None:
            break
        selected.append(chosen_question)
        selected_type_counts[chosen_question.question_type] += 1
    return selected


def _pick_practice_validation_questions(
    course,
    enrollment: Enrollment,
    question_count: int,
    *,
    include_written: bool = True,
    seed_key: str = "",
    blocks: list[CourseBlock] | None = None,
):
    available = _scored_practice_validation_questions(course, enrollment, include_written=include_written, blocks=blocks)
    if not available:
        return []
    available_by_type: dict[str, list[QuestionBankItem]] = {
        QuestionBankItem.QuestionType.MCQ: [],
        QuestionBankItem.QuestionType.NUM: [],
        QuestionBankItem.QuestionType.MAQ: [],
        QuestionBankItem.QuestionType.WAQ: [],
    }
    for question in available:
        available_by_type.setdefault(question.question_type, []).append(question)

    for question_type, questions in available_by_type.items():
        if seed_key:
            available_by_type[question_type] = sorted(
                questions,
                key=lambda question: (
                    question.block.order,
                    _question_seed(seed_key, question.pk, f"practice-lock:{question_type}"),
                    question.pk,
                ),
            )
        else:
            available_by_type[question_type] = list(questions)

    selected: list[QuestionBankItem] = []
    selected_type_counts: dict[str, int] = defaultdict(int)
    for _index in range(question_count):
        chosen_question = None
        for question_type in _practice_validation_type_preference(course, selected_type_counts, len(selected), question_count):
            queue = available_by_type.get(question_type) or []
            if queue:
                chosen_question = queue.pop(0)
                break
        if chosen_question is None:
            break
        selected.append(chosen_question)
        selected_type_counts[chosen_question.question_type] += 1
    return selected


def _practice_validation_type_targets(course) -> dict[str, float]:
    numeric_target = max(0.0, min(100.0, float(course.config.numeric_ratio_percent or 0)))
    maq_target = max(0.0, min(100.0, float(course.config.maq_ratio_percent or 0)))
    waq_target = max(0.0, min(100.0, float(course.config.waq_ratio_percent or 0)))
    mcq_target = max(0.0, 100.0 - numeric_target - maq_target - waq_target)
    total_target = mcq_target + numeric_target + maq_target + waq_target
    if total_target <= 0:
        return {
            QuestionBankItem.QuestionType.MCQ: 100.0,
            QuestionBankItem.QuestionType.NUM: 0.0,
            QuestionBankItem.QuestionType.MAQ: 0.0,
            QuestionBankItem.QuestionType.WAQ: 0.0,
        }
    return {
        QuestionBankItem.QuestionType.MCQ: (mcq_target * 100.0 / total_target),
        QuestionBankItem.QuestionType.NUM: (numeric_target * 100.0 / total_target),
        QuestionBankItem.QuestionType.MAQ: (maq_target * 100.0 / total_target),
        QuestionBankItem.QuestionType.WAQ: (waq_target * 100.0 / total_target),
    }


def _practice_validation_type_preference(course, selected_type_counts: dict[str, int], selected_count: int, question_count: int) -> list[str]:
    targets = _practice_validation_type_targets(course)
    remaining_slots = max(1, question_count)
    candidates = []
    for question_type in (
        QuestionBankItem.QuestionType.MCQ,
        QuestionBankItem.QuestionType.NUM,
        QuestionBankItem.QuestionType.MAQ,
        QuestionBankItem.QuestionType.WAQ,
    ):
        current_count = int(selected_type_counts.get(question_type, 0) or 0)
        current_ratio = (current_count * 100.0 / max(1, selected_count)) if selected_count else 0.0
        gap = targets[question_type] - current_ratio
        remaining_quota = (targets[question_type] * remaining_slots / 100.0) - current_count
        fallback_priority = {
            QuestionBankItem.QuestionType.MCQ: 0,
            QuestionBankItem.QuestionType.NUM: 1,
            QuestionBankItem.QuestionType.MAQ: 2,
            QuestionBankItem.QuestionType.WAQ: 3,
        }[question_type]
        candidates.append((-remaining_quota, -gap, fallback_priority, question_type))
    candidates.sort()
    return [item[3] for item in candidates]


def _ensure_practice_validation_questions(
    course,
    enrollment: Enrollment,
    question_count: int,
    *,
    include_written: bool = True,
    seed_key: str = "",
):
    blocks = _released_validation_blocks(course)
    if not blocks:
        return []
    selected = _pick_practice_validation_questions(
        course,
        enrollment,
        question_count,
        include_written=include_written,
        seed_key=seed_key,
        blocks=blocks,
    )
    while len(selected) < question_count:
        selected_ids = {question.pk for question in selected}
        seen_ids = _seen_question_ids_for_enrollment(enrollment, course)
        seen_ids.update(selected_ids)
        selected_type_counts: dict[str, int] = defaultdict(int)
        for question in selected:
            selected_type_counts[question.question_type] += 1
        preferred_types = _practice_validation_type_preference(course, selected_type_counts, len(selected), question_count)
        generated = False
        for block in blocks:
            for question_type in preferred_types:
                practice_question, _validation_question = generate_question_pair_for_block(block, question_type=question_type)
                if practice_question is None:
                    continue
                if practice_question.pk in seen_ids or int(practice_question.linked_question_id or 0) in seen_ids:
                    continue
                selected.append(practice_question)
                generated = True
                break
            if generated and len(selected) >= question_count:
                break
        if not generated:
            break
    return selected[:question_count]


def ensure_room_code_secret(event: ValidationEvent) -> None:
    if event.room_code_secret:
        return
    event.room_code_secret = hashlib.sha256(f"validation-event:{event.pk}:{timezone.now().timestamp()}".encode("utf-8")).hexdigest()
    event.save(update_fields=["room_code_secret", "updated_at"])


def minute_bucket(moment=None) -> int:
    current = moment or timezone.now()
    return int(current.timestamp() // 60)


def current_room_code(event: ValidationEvent, *, moment=None) -> str:
    ensure_room_code_secret(event)
    bucket = minute_bucket(moment)
    digest = hmac.new(
        event.room_code_secret.encode("utf-8"),
        str(bucket).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    adjective = VALIDATION_ROOM_CODE_ADJECTIVES[int(digest[:8], 16) % len(VALIDATION_ROOM_CODE_ADJECTIVES)]
    animal = VALIDATION_ROOM_CODE_ANIMALS[int(digest[8:16], 16) % len(VALIDATION_ROOM_CODE_ANIMALS)]
    return f"{adjective}-{animal}"


def room_code_payload(event: ValidationEvent, *, moment=None) -> dict:
    current = moment or timezone.now()
    bucket = minute_bucket(current)
    next_refresh = timezone.datetime.fromtimestamp((bucket + 1) * 60, tz=current.tzinfo)
    seconds_remaining = max(0, int((next_refresh - current).total_seconds()))
    return {
        "code": current_room_code(event, moment=current),
        "seconds_remaining": seconds_remaining,
        "refreshes_at": next_refresh.isoformat(),
    }


def room_code_client_payload(event: ValidationEvent, *, moment=None) -> dict:
    ensure_room_code_secret(event)
    current = moment or timezone.now()
    return {
        "seed": event.room_code_secret,
        "server_now_ms": int(current.timestamp() * 1000),
        "seconds_remaining": room_code_payload(event, moment=current)["seconds_remaining"],
        "option_count": VALIDATION_ROOM_CODE_OPTION_COUNT,
    }


def _ui_store(request) -> dict:
    if request is None:
        return {}
    return request.session.setdefault(VALIDATION_UI_SESSION_KEY, {})


def _ui_attempt_state(request, attempt_id: int) -> dict:
    if request is None:
        return {}
    return _ui_store(request).setdefault(str(attempt_id), {})


def _set_ui_attempt_state(request, attempt_id: int, **updates) -> None:
    if request is None:
        return
    state = _ui_attempt_state(request, attempt_id)
    for key, value in updates.items():
        state[key] = value
    request.session.modified = True


def _validation_instructions_confirmed(request, attempt: ValidationAttempt) -> bool:
    if _attendance_audit_completed(attempt):
        return True
    return bool(_ui_attempt_state(request, attempt.pk).get("instructions_confirmed"))


def _awaiting_next_step(request, attempt: ValidationAttempt) -> bool:
    if request is None:
        return False
    if attempt.status != ValidationAttempt.Status.IN_PROGRESS:
        return False
    return bool(_ui_attempt_state(request, attempt.pk).get("awaiting_next"))


def _clear_next_step(request, attempt: ValidationAttempt) -> None:
    if request is None:
        return
    _set_ui_attempt_state(request, attempt.pk, awaiting_next=False)


def _set_next_step(request, attempt: ValidationAttempt, enabled: bool) -> None:
    if request is None:
        return
    _set_ui_attempt_state(request, attempt.pk, awaiting_next=bool(enabled))


def _practice_ui_key(attempt: PracticeAttempt) -> int:
    return -int(attempt.pk)


def _practice_awaiting_next_step(request, attempt: PracticeAttempt) -> bool:
    if request is None:
        return False
    if attempt.completed_at:
        return False
    return bool(_ui_attempt_state(request, _practice_ui_key(attempt)).get("awaiting_next"))


def _clear_practice_next_step(request, attempt: PracticeAttempt) -> None:
    if request is None:
        return
    _set_ui_attempt_state(request, _practice_ui_key(attempt), awaiting_next=False)


def _set_practice_next_step(request, attempt: PracticeAttempt, enabled: bool) -> None:
    if request is None:
        return
    _set_ui_attempt_state(request, _practice_ui_key(attempt), awaiting_next=bool(enabled))


def _practice_question_messages(request, attempt: PracticeAttempt, question_id: int) -> list[dict]:
    if request is None:
        return []
    state = _ui_attempt_state(request, _practice_ui_key(attempt))
    messages = (state.get("question_messages") or {}).get(str(question_id), [])
    return [dict(message) for message in messages]


def _append_practice_question_message(request, attempt: PracticeAttempt, question_id: int, message: dict) -> None:
    if request is None:
        return
    state = _ui_attempt_state(request, _practice_ui_key(attempt))
    question_messages = state.setdefault("question_messages", {})
    question_messages.setdefault(str(question_id), []).append(message)
    request.session.modified = True


def _draft_store(request) -> dict:
    if request is None:
        return {}
    root = request.session.setdefault(VALIDATION_DRAFT_SESSION_KEY, {})
    return root


def _draft_key(prefix: str, object_id: int, question_id: int) -> str:
    return f"{prefix}:{object_id}:{question_id}"


def _set_draft(request, prefix: str, object_id: int, question_id: int, payload: dict) -> None:
    if request is None:
        return
    store = _draft_store(request)
    store[_draft_key(prefix, object_id, question_id)] = payload
    request.session.modified = True


def _get_draft(request, prefix: str, object_id: int, question_id: int) -> dict:
    if request is None:
        return {}
    return dict(_draft_store(request).get(_draft_key(prefix, object_id, question_id), {}))


def _clear_draft(request, prefix: str, object_id: int, question_id: int) -> None:
    if request is None:
        return
    store = _draft_store(request)
    store.pop(_draft_key(prefix, object_id, question_id), None)
    request.session.modified = True


def _validation_message_sequence(attempt: ValidationAttempt) -> int:
    latest = attempt.messages.order_by("-sequence").first()
    return int(latest.sequence if latest else 0) + 1


def _append_attempt_message(
    attempt: ValidationAttempt,
    role: str,
    kind: str,
    *,
    question: QuestionBankItem | None = None,
    attempt_question: ValidationAttemptQuestion | None = None,
    text: str = "",
    payload: dict | None = None,
    source_blocks: list[str] | None = None,
):
    sequence = _validation_message_sequence(attempt)
    message_payload = {"id": f"validation-message-{attempt.pk}-{sequence}", "role": role, "kind": kind, "text": text, **(payload or {})}
    return ValidationAttemptMessage.objects.create(
        attempt=attempt,
        question=question,
        attempt_question=attempt_question,
        message_id=message_payload["id"],
        sequence=sequence,
        role=role,
        kind=kind,
        text=text,
        payload=message_payload,
        source_blocks=source_blocks or ([question.block.title] if question else []),
    )


def _ordered_attempt_questions(attempt: ValidationAttempt):
    return list(
        attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective")
        .order_by("order", "created_at")
    )


def _current_attempt_question(attempt: ValidationAttempt) -> ValidationAttemptQuestion | None:
    return (
        attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective")
        .filter(answered_at__isnull=True)
        .order_by("order", "created_at")
        .first()
    )


def _latest_answered_attempt_question(attempt: ValidationAttempt) -> ValidationAttemptQuestion | None:
    return (
        attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective")
        .filter(answered_at__isnull=False)
        .order_by("-order", "-created_at")
        .first()
    )


def _validation_progress(attempt: ValidationAttempt) -> ValidationProgress:
    total_questions = attempt.attempt_questions.count()
    answered_count = attempt.attempt_questions.filter(answered_at__isnull=False).count()
    current = _current_attempt_question(attempt)
    current_index = current.order if current else total_questions
    remaining_count = max(0, total_questions - answered_count)
    return ValidationProgress(
        current_index=current_index,
        total_questions=total_questions,
        answered_count=answered_count,
        remaining_count=remaining_count,
    )


def _serialize_question_message(
    question: QuestionBankItem,
    *,
    seed_key: str,
    answered: bool = False,
    flagged: bool = False,
    selected_answers: list[str] | None = None,
    correct_answers: list[str] | None = None,
    submitted_text: str = "",
    alignment_score: int = 0,
    alignment_state: str = "drafting",
    model_answer: str = "",
    model_answer_revealed: bool = False,
    review_visible: bool = False,
    is_correct: bool | None = None,
) -> dict:
    options = _shuffle_options(question.all_answer_options(), seed_key, question.pk)
    payload = {
        "question_id": question.pk,
        "question_type": question.question_type,
        "question_type_label": question.question_type_label(),
        "text": question.stem,
        "options": options,
        "is_numerical": question.is_numeric(),
        "answered": answered,
        "review_visible": bool(review_visible),
        "flagged": flagged,
        "block_label": question.block.title,
        "learning_objective": question.learning_objective.text if question.learning_objective else "General course understanding",
        "is_coding_question": question.is_coding_question,
        "coding_language": question.coding_language,
        "coding_question_kind": question.coding_question_kind,
        "code_snippet": question.code_snippet,
    }
    if answered and review_visible:
        payload["selected_answers"] = list(selected_answers or [])
        payload["correct_answers"] = list(correct_answers or [])
        payload["is_correct"] = bool(is_correct)
        if question.is_written_answer():
            payload["submitted_text"] = submitted_text
            payload["alignment_score"] = alignment_score
            payload["alignment_state"] = alignment_state
            payload["model_answer"] = model_answer
            payload["model_answer_revealed"] = model_answer_revealed
    elif not answered and question.is_written_answer():
        payload["submitted_text"] = submitted_text
        payload["alignment_score"] = alignment_score
        payload["alignment_state"] = alignment_state
        payload["model_answer"] = model_answer if model_answer_revealed else ""
        payload["model_answer_revealed"] = bool(model_answer_revealed)
    return payload


def _question_payload_for_attempt_question(
    attempt_question: ValidationAttemptQuestion,
    *,
    seed_key: str,
    review_visible: bool,
) -> dict:
    question = attempt_question.question
    return _serialize_question_message(
        question,
        seed_key=seed_key,
        answered=bool(attempt_question.answered_at),
        selected_answers=list(attempt_question.selected_answers or []),
        correct_answers=question.correct_answers(),
        submitted_text=attempt_question.answer_text,
        alignment_score=_extract_alignment_score(attempt_question.feedback),
        alignment_state=_extract_alignment_state(attempt_question.feedback),
        model_answer=question.correct_answer if question.is_written_answer() and not attempt_question.is_correct else "",
        model_answer_revealed=question.is_written_answer() and not attempt_question.is_correct and review_visible,
        review_visible=review_visible,
        is_correct=attempt_question.is_correct if bool(attempt_question.answered_at) else None,
    )


def _extract_alignment_score(feedback_text: str) -> int:
    try:
        if feedback_text and feedback_text.startswith("{"):
            payload = json.loads(feedback_text)
            return int(payload.get("alignment_score") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    return 0


def _extract_alignment_state(feedback_text: str) -> str:
    try:
        if feedback_text and feedback_text.startswith("{"):
            payload = json.loads(feedback_text)
            return str(payload.get("alignment_state") or "drafting")
    except (TypeError, ValueError, json.JSONDecodeError):
        return "drafting"
    return "drafting"


def _feedback_payload(text: str, *, correct: bool | None = None, alignment_score: int | None = None, alignment_state: str | None = None) -> str:
    if alignment_score is None and alignment_state is None:
        return text
    return json.dumps(
        {
            "text": text,
            "correct": correct,
            "alignment_score": alignment_score,
            "alignment_state": alignment_state,
        }
    )


def _feedback_text_from_payload(raw_feedback: str) -> str:
    if not raw_feedback:
        return ""
    if not raw_feedback.startswith("{"):
        return raw_feedback
    try:
        payload = json.loads(raw_feedback)
    except json.JSONDecodeError:
        return raw_feedback
    return str(payload.get("text") or "")


def _merge_written_answer_text(existing_text: str, new_text: str) -> str:
    previous = _normalize_written_answer_text(existing_text)
    latest = _normalize_written_answer_text(new_text)
    if not previous:
        return latest
    if not latest:
        return previous
    return f"{previous}\n\n{latest}"


def _practice_component_weights(course) -> dict:
    total = (
        int(course.config.mastery_weight or 0)
        + int(course.config.coverage_weight or 0)
        + int(course.config.engagement_weight or 0)
        + int(course.config.target_weight or 0)
    )
    return {
        "mastery": int(course.config.mastery_weight or 0),
        "coverage": int(course.config.coverage_weight or 0),
        "engagement": int(course.config.engagement_weight or 0),
        "target": int(course.config.target_weight or 0),
        "total": total,
    }


def _weighted_practice_score(course, metrics: dict) -> float:
    weights = _practice_component_weights(course)
    if weights["total"] <= 0:
        return 0.0
    weighted_total = (
        float(metrics.get("mastery", 0) or 0) * weights["mastery"]
        + float(metrics.get("coverage", 0) or 0) * weights["coverage"]
        + float(metrics.get("engagement", 0) or 0) * weights["engagement"]
        + float(metrics.get("target", 0) or 0) * weights["target"]
    )
    return round(weighted_total / weights["total"], 2)


def _enrollment_practice_metrics(enrollment: Enrollment) -> dict:
    metrics = {
        "mastery": float(enrollment.mastery_score or 0),
        "coverage": float(enrollment.coverage_score or 0),
        "engagement": float(enrollment.engagement_score or 0),
        "target": float(enrollment.target_score or 0),
    }
    return {
        **metrics,
        "overall": _weighted_practice_score(enrollment.course, metrics),
    }


def _projected_course_score(course, practice_overall: float, validation_score: float) -> tuple[float, int, float, bool]:
    practice_weight = int(course.config.practice_weight or 0)
    validation_weight = int(course.config.validation_weight or 0)
    total = practice_weight + validation_weight
    if total <= 0:
        return 0.0, 0, 0.0, False
    raw_projected = ((practice_overall * practice_weight) + (validation_score * validation_weight)) / total
    projected = raw_projected
    applied_floor = False
    if practice_overall < 40 and validation_score >= 40 and raw_projected < 40:
        projected = 40.0
        applied_floor = True
    return round(projected, 2), total, round(raw_projected, 2), applied_floor


def _schedule_audit_prompts(attempt: ValidationAttempt) -> None:
    if attempt.mode != ValidationEvent.Mode.DIGITAL_INVIGILATION:
        return
    count = int(attempt.event.audit_prompt_count or 0)
    if count <= 0:
        return
    start_time = attempt.started_at
    duration_seconds = max(60, int((attempt.expires_at - attempt.started_at).total_seconds()))
    fractions = []
    for index in range(count):
        fractions.append(0.3 + ((0.4 / max(1, count - 1)) * index) if count > 1 else 0.5)
    for prompt_index, fraction in enumerate(fractions, start=1):
        due_at = start_time + timedelta(seconds=int(duration_seconds * fraction))
        ValidationAuditPrompt.objects.get_or_create(
            attempt=attempt,
            prompt_index=prompt_index,
            defaults={
                "due_at": due_at,
                "expected_code": current_room_code(attempt.event, moment=due_at),
            },
        )


def _ensure_attendance_audit_prompt(attempt: ValidationAttempt) -> ValidationAuditPrompt:
    now = timezone.now()
    prompt, created = ValidationAuditPrompt.objects.get_or_create(
        attempt=attempt,
        prompt_index=0,
        defaults={
            "due_at": now,
            "expected_code": current_room_code(attempt.event, moment=now),
        },
    )
    if not created and not prompt.answered_at:
        prompt.expected_code = current_room_code(attempt.event, moment=now)
        prompt.save(update_fields=["expected_code", "updated_at"])
    return prompt


def _attendance_audit_completed(attempt: ValidationAttempt) -> bool:
    return attempt.audit_prompts.filter(prompt_index=0, answered_at__isnull=False, is_correct=True).exists()


def _start_official_validation_timer(attempt: ValidationAttempt) -> None:
    started_at = timezone.now()
    session_end_at = attempt.event.session_end_at
    attempt.started_at = started_at
    attempt.expires_at = session_end_at if session_end_at > started_at else started_at
    attempt.save(update_fields=["started_at", "expires_at", "updated_at"])
    _schedule_audit_prompts(attempt)


def _intro_message_text(event: ValidationEvent) -> str:
    return "This validation session is ready. Enter the current room-display code to confirm attendance and begin your validation."


@transaction.atomic
def get_or_create_official_attempt(enrollment: Enrollment, event: ValidationEvent, *, booking: ValidationBooking | None = None) -> ValidationAttempt:
    if event.requires_booking:
        if booking is None:
            booking = ValidationBooking.objects.filter(
                event=event,
                enrollment=enrollment,
                status=ValidationBooking.Status.BOOKED,
            ).first()
        if booking is None:
            raise ValidationFlowError("A confirmed booking is required for this validation.")
    attempt = ValidationAttempt.objects.filter(enrollment=enrollment, event=event).select_related("event", "enrollment", "booking").first()
    if attempt:
        return attempt

    ensure_room_code_secret(event)
    released_blocks = _released_validation_blocks(event.course)
    if not released_blocks:
        raise ValidationFlowError("This course has no released content blocks available for validation yet.")
    started_at = timezone.now()
    session_end_at = event.session_end_at
    expires_at = session_end_at if session_end_at > started_at else started_at
    attempt = ValidationAttempt.objects.create(
        enrollment=enrollment,
        event=event,
        booking=booking,
        mode=event.mode,
        expires_at=expires_at,
        feedback_release_mode=event.feedback_release_mode,
    )
    seed_key = _validation_seed_key(event, enrollment)
    locked_questions = _pick_locked_questions(
        event.course,
        enrollment,
        event.question_count,
        include_written=True,
        seed_key=seed_key,
        blocks=released_blocks,
    )
    if not locked_questions:
        raise ValidationFlowError("No validation questions are available yet for this course.")
    for order, question in enumerate(locked_questions, start=1):
        ValidationAttemptQuestion.objects.create(
            attempt=attempt,
            question=question,
            order=order,
            question_type=question.question_type,
        )
    _ensure_attendance_audit_prompt(attempt)
    return attempt


def _practice_attempt_seed(attempt: PracticeAttempt) -> str:
    return f"practice-validation:{attempt.pk}:course:{attempt.enrollment.course_id}"


@transaction.atomic
def get_or_create_validation_practice_attempt(enrollment: Enrollment) -> PracticeAttempt:
    existing = (
        PracticeAttempt.objects.filter(
            enrollment=enrollment,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
            completed_at__isnull=True,
        )
        .order_by("-started_at")
        .first()
    )
    if existing and existing.attempt_questions.exists():
        return existing

    question_count = VALIDATION_PRACTICE_DEFAULT_QUESTION_COUNT
    upcoming_digital_event = (
        enrollment.course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION)
        .order_by("starts_at")
        .first()
    )
    if upcoming_digital_event:
        question_count = int(upcoming_digital_event.question_count or question_count)

    attempt = PracticeAttempt.objects.create(
        enrollment=enrollment,
        attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        time_limit_minutes=None,
        feedback_visible_immediately=False,
    )
    seed_key = _practice_attempt_seed(attempt)
    locked_questions = _ensure_practice_validation_questions(
        enrollment.course,
        enrollment,
        question_count,
        include_written=True,
        seed_key=seed_key,
    )
    if not locked_questions:
        attempt.delete()
        raise ValidationFlowError("No validation questions are available yet for this course.")
    for order, question in enumerate(locked_questions, start=1):
        PracticeAttemptQuestion.objects.create(
            attempt=attempt,
            question=question,
            order=order,
        )
    return attempt


@transaction.atomic
def restart_validation_practice_attempt(enrollment: Enrollment) -> PracticeAttempt:
    PracticeAttempt.objects.filter(
        enrollment=enrollment,
        attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        completed_at__isnull=True,
    ).delete()
    return get_or_create_validation_practice_attempt(enrollment)


def _practice_elapsed_seconds(attempt: PracticeAttempt) -> int:
    return max(0, int((timezone.now() - attempt.started_at).total_seconds()))


def _practice_expires_at(attempt: PracticeAttempt):
    if attempt.time_limit_minutes is None:
        return None
    return attempt.started_at + timedelta(minutes=max(1, int(attempt.time_limit_minutes or VALIDATION_PRACTICE_DEFAULT_TIME_LIMIT_MINUTES)))


def _practice_is_expired(attempt: PracticeAttempt) -> bool:
    expires_at = _practice_expires_at(attempt)
    if expires_at is None:
        return False
    return timezone.now() >= expires_at


def _mark_practice_complete(attempt: PracticeAttempt) -> None:
    if attempt.completed_at:
        return
    total = max(1, attempt.attempt_questions.count())
    correct = attempt.attempt_questions.filter(is_correct=True).count()
    attempt.score = round((correct * 100) / total, 2)
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["score", "completed_at", "updated_at"])


def _mark_official_complete(attempt: ValidationAttempt) -> None:
    if attempt.completed_at:
        return
    total = max(1, attempt.attempt_questions.count())
    correct = attempt.attempt_questions.filter(is_correct=True).count()
    attempt.score = round((correct * 100) / total, 2)
    attempt.status = ValidationAttempt.Status.COMPLETED
    attempt.completed_at = timezone.now()
    if attempt.feedback_release_mode == ValidationEvent.FeedbackReleaseMode.IMMEDIATE:
        attempt.review_released_at = attempt.completed_at
    attempt.save(update_fields=["score", "status", "completed_at", "review_released_at", "updated_at"])


def _pending_audit_prompt(attempt: ValidationAttempt) -> ValidationAuditPrompt | None:
    if attempt.mode != ValidationEvent.Mode.DIGITAL_INVIGILATION:
        return None
    _ensure_attendance_audit_prompt(attempt)
    now = timezone.now()
    prompt = (
        attempt.audit_prompts.filter(answered_at__isnull=True, due_at__lte=now)
        .order_by("prompt_index", "due_at")
        .first()
    )
    if prompt and prompt.prompt_index == 0:
        prompt.expected_code = current_room_code(attempt.event, moment=now)
        prompt.save(update_fields=["expected_code", "updated_at"])
    if prompt and not prompt.presented_at:
        prompt.presented_at = now
        prompt.message_id = f"validation-audit-{attempt.pk}-{prompt.prompt_index}"
        prompt.save(update_fields=["presented_at", "message_id", "updated_at"])
        _append_attempt_message(
            attempt,
            "assistant",
            "audit",
            text=(
                "What two-word code is currently on the room display?"
                if prompt.prompt_index
                else "Enter the current room-display code to confirm attendance and begin your validation."
            ),
            payload={
                "audit_prompt_id": prompt.pk,
                "placeholder": "Enter the room code...",
                "attendance_audit": prompt.prompt_index == 0,
            },
            source_blocks=[],
        )
    return prompt


def _ensure_next_attempt_question_message(attempt: ValidationAttempt) -> None:
    if attempt.status != ValidationAttempt.Status.IN_PROGRESS:
        return
    if not _attendance_audit_completed(attempt):
        _pending_audit_prompt(attempt)
        return
    if _pending_audit_prompt(attempt):
        return
    current = _current_attempt_question(attempt)
    if current is None:
        _mark_official_complete(attempt)
        if attempt.feedback_release_mode == ValidationEvent.FeedbackReleaseMode.IMMEDIATE:
            _append_attempt_message(
                attempt,
                "assistant",
                "summary",
                text=f"Validation complete. Score: {attempt.score:.1f}%. Review is now available.",
                payload={"completed": True, "score": float(attempt.score), "review_visible": True},
                source_blocks=[],
            )
        else:
            _append_attempt_message(
                attempt,
                "assistant",
                "summary",
                text="Validation complete. Your teacher will release review and score later.",
                payload={"completed": True, "review_visible": False},
                source_blocks=[],
            )
        return
    existing = attempt.messages.filter(attempt_question=current, kind="question").exists()
    if existing:
        return
    seed_key = _validation_seed_key(attempt.event, attempt.enrollment)
    payload = _serialize_question_message(current.question, seed_key=seed_key)
    _append_attempt_message(
        attempt,
        "assistant",
        "question",
        question=current.question,
        attempt_question=current,
        text=current.question.stem,
        payload=payload,
        source_blocks=[current.question.block.title],
    )


def _ensure_official_attempt_ready(attempt: ValidationAttempt) -> None:
    if attempt.status == ValidationAttempt.Status.COMPLETED:
        return
    if not _attendance_audit_completed(attempt):
        _ensure_next_attempt_question_message(attempt)
        return
    if timezone.now() >= attempt.expires_at:
        attempt.status = ValidationAttempt.Status.EXPIRED
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["status", "completed_at", "updated_at"])
        _append_attempt_message(
            attempt,
            "assistant",
            "summary",
            text="This validation session has ended.",
            payload={"completed": True, "timed_out": True, "review_visible": attempt.review_visible},
            source_blocks=[],
        )
        return
    _ensure_next_attempt_question_message(attempt)


def _void_attempt(attempt: ValidationAttempt, reason: str) -> None:
    if attempt.status == ValidationAttempt.Status.VOIDED:
        return
    attempt.status = ValidationAttempt.Status.VOIDED
    attempt.completed_at = timezone.now()
    attempt.invalidated_reason = reason
    attempt.save(update_fields=["status", "completed_at", "invalidated_reason", "updated_at"])
    _append_attempt_message(
        attempt,
        "assistant",
        "summary",
        text=reason,
        payload={"completed": True, "voided": True, "review_visible": False},
        source_blocks=[],
    )


def _normalize_audit_code(code: str) -> str:
    return "-".join(part for part in _normalize_written_answer_text(code).lower().replace(" ", "-").split("-") if part)


def _audit_feedback(is_correct: bool) -> str:
    return "Audit code confirmed." if is_correct else "That code did not match the room display."


def _practice_attempt_question_state(attempt_question: PracticeAttemptQuestion) -> str:
    return "answered" if attempt_question.selected_answer else "pending"


def _practice_current_question(attempt: PracticeAttempt) -> PracticeAttemptQuestion | None:
    for attempt_question in attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective").order_by("order", "created_at"):
        if _practice_attempt_question_state(attempt_question) == "pending":
            return attempt_question
    return None


def _practice_answered_questions(attempt: PracticeAttempt):
    return list(
        attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective")
        .exclude(selected_answer="")
        .order_by("order", "created_at")
    )


def _serialize_practice_question_card(attempt: PracticeAttempt, attempt_question: PracticeAttemptQuestion, *, request=None) -> dict:
    question = attempt_question.question
    draft = _get_draft(request, "practice", attempt.pk, question.pk)
    stored_answer = attempt_question.selected_answer
    skipped = stored_answer == PRACTICE_SKIPPED_SENTINEL
    submitted_answers = _normalize_submitted_answers(
        [] if skipped else (stored_answer.split("\n") if question.is_multiple_answer() else [stored_answer])
    )
    answered = bool(stored_answer)
    review_visible = bool(attempt.feedback_visible_immediately and answered)
    return _serialize_question_message(
        question,
        seed_key=_practice_attempt_seed(attempt),
        answered=answered,
        selected_answers=submitted_answers,
        correct_answers=question.correct_answers(),
        submitted_text=stored_answer if question.is_written_answer() and review_visible and not skipped else "",
        alignment_score=int(draft.get("alignment_score") or _extract_alignment_score(attempt_question.feedback)),
        alignment_state=str(draft.get("alignment_state") or _extract_alignment_state(attempt_question.feedback) or "drafting"),
        model_answer=question.correct_answer if question.is_written_answer() and review_visible and not attempt_question.is_correct else "",
        model_answer_revealed=bool(question.is_written_answer() and review_visible and not attempt_question.is_correct),
        review_visible=review_visible,
        is_correct=attempt_question.is_correct if answered else None,
    )


def _serialize_practice_review_question_card(attempt: PracticeAttempt, attempt_question: PracticeAttemptQuestion, *, request=None) -> dict:
    question = attempt_question.question
    stored_answer = attempt_question.selected_answer
    skipped = stored_answer == PRACTICE_SKIPPED_SENTINEL
    submitted_answers = _normalize_submitted_answers(
        [] if skipped else (stored_answer.split("\n") if question.is_multiple_answer() else [stored_answer])
    )
    return _serialize_question_message(
        question,
        seed_key=_practice_attempt_seed(attempt),
        answered=bool(stored_answer),
        selected_answers=submitted_answers,
        correct_answers=question.correct_answers(),
        submitted_text=stored_answer if question.is_written_answer() and not skipped else "",
        alignment_score=int(_extract_alignment_score(attempt_question.feedback)),
        alignment_state=str(_extract_alignment_state(attempt_question.feedback) or "drafting"),
        model_answer=question.correct_answer if question.is_written_answer() and not attempt_question.is_correct else "",
        model_answer_revealed=bool(question.is_written_answer() and not attempt_question.is_correct),
        review_visible=True,
        is_correct=attempt_question.is_correct if bool(stored_answer) else None,
    )


def _practice_latest_answered_question(attempt: PracticeAttempt) -> PracticeAttemptQuestion | None:
    return (
        attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective")
        .exclude(selected_answer="")
        .order_by("-order", "-created_at")
        .first()
    )


def _practice_validation_review_transcript(attempt: PracticeAttempt, *, request=None) -> list[dict]:
    enrollment = attempt.enrollment
    course = enrollment.course
    validation_score = float(attempt.score or 0)
    practice_metrics = _enrollment_practice_metrics(enrollment)
    projected_score, combined_weight, raw_projected_score, applied_floor = _projected_course_score(
        course,
        practice_metrics["overall"],
        validation_score,
    )
    impact_text = (
        "**Projected overall course score** from this **practice validation**: "
        f"**({practice_metrics['overall']:.1f} x {int(course.config.practice_weight or 0)} + "
        f"{validation_score:.1f} x {int(course.config.validation_weight or 0)}) / "
        f"{combined_weight or 1} = {raw_projected_score:.1f}%**."
    )
    if applied_floor:
        impact_text += (
            " Because your practice score is below **40.0%** but this practice validation is **40.0% or above**, "
            "your projected overall score is lifted to **40.0%**."
        )
    else:
        impact_text += f" Your projected overall score is **{projected_score:.1f}%**."
    impact_text += " This is based on your **practice validation** only. You still need to complete a **real validation**."
    transcript = [
        {
            "id": f"practice-validation-summary-score-{attempt.pk}",
            "role": "assistant",
            "kind": "summary",
            "text": (
                f"**Practice validation complete.** You scored **{validation_score:.1f}%** "
                f"(**{attempt.attempt_questions.filter(is_correct=True).count()} of {attempt.attempt_questions.count()}**)."
            ),
        },
        {
            "id": f"practice-validation-summary-impact-{attempt.pk}",
            "role": "assistant",
            "kind": "summary",
            "text": impact_text,
        },
    ]
    for attempt_question in attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective").order_by("order", "created_at"):
        transcript.append(
            {
                "id": f"practice-validation-review-question-{attempt.pk}-{attempt_question.order}",
                "role": "assistant",
                "kind": "question",
                **_serialize_practice_review_question_card(attempt, attempt_question, request=request),
            }
        )
        if attempt_question.selected_answer and not attempt_question.question.is_written_answer():
            answer_text = (
                VALIDATION_SKIPPED_TEXT
                if attempt_question.selected_answer == PRACTICE_SKIPPED_SENTINEL
                else (
                    attempt_question.selected_answer.replace("\n", ", ")
                    if attempt_question.question.is_multiple_answer()
                    else attempt_question.selected_answer
                )
            )
            transcript.append(
                {
                    "id": f"practice-validation-review-answer-{attempt.pk}-{attempt_question.order}",
                    "role": "user",
                    "kind": "text",
                    "question_id": attempt_question.question_id,
                    "question_type": attempt_question.question.question_type,
                    "text": answer_text,
                }
            )
        feedback_text = _feedback_text_from_payload(attempt_question.feedback)
        if feedback_text and not attempt_question.question.is_written_answer():
            transcript.append(
                {
                    "id": f"practice-validation-review-feedback-{attempt.pk}-{attempt_question.order}",
                    "role": "assistant",
                    "kind": "feedback",
                    "question_id": attempt_question.question_id,
                    "text": feedback_text,
                    "correct": bool(attempt_question.is_correct),
                }
            )
    return transcript


def _serialize_official_question_card(attempt: ValidationAttempt, attempt_question: ValidationAttemptQuestion, *, request=None) -> dict:
    draft = _get_draft(request, "official", attempt.pk, attempt_question.question_id)
    return _serialize_question_message(
        attempt_question.question,
        seed_key=_validation_seed_key(attempt.event, attempt.enrollment),
        answered=bool(attempt_question.answered_at),
        selected_answers=list(attempt_question.selected_answers or []),
        correct_answers=attempt_question.question.correct_answers(),
        submitted_text=attempt_question.answer_text,
        alignment_score=int(draft.get("alignment_score") or _extract_alignment_score(attempt_question.feedback)),
        alignment_state=str(draft.get("alignment_state") or _extract_alignment_state(attempt_question.feedback) or "drafting"),
        model_answer=attempt_question.question.correct_answer if attempt.review_visible and not attempt_question.is_correct else "",
        model_answer_revealed=bool(attempt.review_visible and attempt_question.question.is_written_answer() and not attempt_question.is_correct),
        review_visible=bool(attempt.review_visible),
    )


def _parse_feedback_payload(raw_feedback: str) -> dict:
    if raw_feedback and raw_feedback.startswith("{"):
        try:
            return json.loads(raw_feedback)
        except json.JSONDecodeError:
            return {"text": raw_feedback}
    return {"text": raw_feedback}


def _official_instruction_transcript() -> list[dict]:
    messages = []
    for index, line in enumerate(VALIDATION_OFFICIAL_INSTRUCTION_LINES, start=1):
        messages.append(
            {
                "id": f"validation-instruction-{index}",
                "role": "assistant",
                "kind": "text",
                "text": line,
            }
        )
    return messages


def _official_confirmation_message() -> dict:
    return {
        "id": "validation-confirmation",
        "role": "assistant",
        "kind": "confirm",
        "text": "Please confirm that you have read and understood these instructions.",
        "button_label": "I have read and understood these instructions",
    }


def _official_code_selection_message() -> dict:
    return {
        "id": "validation-code-selection",
        "role": "assistant",
        "kind": "text",
        "text": "When you are ready to begin please select the matching session code from the list below.",
    }


def serialize_validation_practice_session(attempt: PracticeAttempt, *, request=None) -> dict:
    if _practice_is_expired(attempt):
        _mark_practice_complete(attempt)
    if attempt.completed_at:
        return {
            "mode": "validation_practice",
            "attempt_id": attempt.pk,
            "title": f"{attempt.enrollment.course.title} Validation practice",
            "transcript": _practice_validation_review_transcript(attempt, request=request),
            "pending_question": None,
            "pending_audit": None,
            "completed": True,
            "review_visible": True,
            "score": float(attempt.score or 0),
            "time_limit_minutes": 0,
            "expires_at": "",
            "time_remaining_seconds": 0,
            "timer_running": False,
            "show_timer": False,
            "progress": _validation_progress_for_practice(attempt),
            "waq_draft": {},
            "next_available": False,
        }
    current = _practice_current_question(attempt)
    latest_answered = _practice_latest_answered_question(attempt)
    hide_next_question = _practice_awaiting_next_step(request, attempt)
    transcript = [
        {
            "id": f"practice-validation-intro-{attempt.pk}",
            "role": "assistant",
            "kind": "text",
            "text": "Practice validation is untimed. Work through the locked set in order. Feedback is shown at the end.",
        }
    ]
    for attempt_question in attempt.attempt_questions.select_related("question", "question__block", "question__learning_objective").order_by("order", "created_at"):
        question_payload = _serialize_practice_question_card(attempt, attempt_question, request=request)
        transcript.append(
            {
                "id": f"practice-validation-question-{attempt.pk}-{attempt_question.order}",
                "role": "assistant",
                "kind": "question",
                **question_payload,
            }
        )
        transcript.extend(_practice_question_messages(request, attempt, attempt_question.question_id))
    progress = _validation_progress_for_practice(attempt)
    visible_question = None
    if current and not hide_next_question:
        visible_question = current
    elif hide_next_question and latest_answered:
        visible_question = latest_answered
    pending_question = _serialize_practice_question_card(attempt, visible_question, request=request) if visible_question else None
    draft = _get_draft(request, "practice", attempt.pk, visible_question.question_id) if visible_question and visible_question.question.is_written_answer() else {}
    return {
        "mode": "validation_practice",
        "attempt_id": attempt.pk,
        "title": f"{attempt.enrollment.course.title} Validation practice",
        "transcript": transcript,
        "pending_question": pending_question,
        "pending_audit": None,
        "completed": bool(attempt.completed_at),
        "review_visible": False,
        "score": float(attempt.score or 0),
        "time_limit_minutes": 0,
        "expires_at": "",
        "time_remaining_seconds": 0,
        "timer_running": False,
        "show_timer": False,
        "progress": progress,
        "waq_draft": draft,
        "next_available": bool(hide_next_question and (current or latest_answered)),
    }


def _validation_progress_for_practice(attempt: PracticeAttempt) -> dict:
    total_questions = attempt.attempt_questions.count()
    answered_count = attempt.attempt_questions.exclude(selected_answer="").count()
    current = _practice_current_question(attempt)
    return {
        "current_index": current.order if current else total_questions,
        "total_questions": total_questions,
        "answered_count": answered_count,
        "remaining_count": max(0, total_questions - answered_count),
    }


def serialize_official_validation_session(attempt: ValidationAttempt, *, request=None) -> dict:
    attempt = ValidationAttempt.objects.select_related("event", "enrollment", "enrollment__course").get(pk=attempt.pk)
    _ensure_official_attempt_ready(attempt)
    transcript = []
    instructions_confirmed = _validation_instructions_confirmed(request, attempt)
    hide_next_question = _awaiting_next_step(request, attempt)
    message_queryset = attempt.messages.select_related(
        "attempt_question",
        "attempt_question__question",
        "attempt_question__question__block",
        "attempt_question__question__learning_objective",
    ).order_by("sequence", "created_at")
    question_payloads = {
        message.attempt_question_id: _serialize_official_question_card(attempt, message.attempt_question, request=request)
        for message in message_queryset
        if message.attempt_question_id and message.kind == "question"
    }
    current = _current_attempt_question(attempt)
    latest_answered = _latest_answered_attempt_question(attempt)
    for message in message_queryset:
        if message.kind == "audit":
            continue
        payload = dict(message.payload or {})
        if message.role == "user" and message.attempt_question_id and str(payload.get("question_type") or "") != QuestionBankItem.QuestionType.WAQ:
            continue
        if (
            hide_next_question
            and current is not None
            and message.kind == "question"
            and message.attempt_question_id == current.pk
        ):
            continue
        if message.attempt_question_id and message.attempt_question_id in question_payloads:
            payload.setdefault("question_id", message.attempt_question.question_id)
        if message.kind == "question" and message.attempt_question_id:
            payload.update(question_payloads.get(message.attempt_question_id, {}))
            payload.setdefault("text", message.text)
        transcript.append(
            {
                "id": payload.get("id") or message.message_id,
                "role": message.role,
                "kind": message.kind,
                **payload,
            }
        )
    if not _attendance_audit_completed(attempt):
        transcript = _official_instruction_transcript() + transcript
        if not instructions_confirmed:
            transcript.append(_official_confirmation_message())
        else:
            transcript.append(_official_code_selection_message())

    pending_audit = _pending_audit_prompt(attempt) if instructions_confirmed else None
    timer_running = _attendance_audit_completed(attempt)
    visible_question = None
    if current and timer_running and not pending_audit and not hide_next_question:
        visible_question = current
    elif hide_next_question and latest_answered:
        visible_question = latest_answered
    pending_question = _serialize_official_question_card(attempt, visible_question, request=request) if visible_question else None
    draft = _get_draft(request, "official", attempt.pk, visible_question.question_id) if visible_question and visible_question.question.is_written_answer() else {}
    released_blocks = _released_validation_blocks(attempt.enrollment.course)
    pending_audit_bucket = None
    if pending_audit:
        pending_audit_bucket = None if pending_audit.prompt_index == 0 else minute_bucket(pending_audit.due_at)
    return {
        "mode": attempt.mode,
        "attempt_id": attempt.pk,
        "event_id": attempt.event_id,
        "title": "Validation session",
        "course_title": attempt.enrollment.course.title,
        "transcript": transcript,
        "pending_question": pending_question,
        "pending_audit": {
            "id": pending_audit.pk,
            "text": (
                "When you are ready to begin please select the matching session code from the list below."
                if pending_audit.prompt_index == 0
                else "What two-word code is currently on the room display?"
            ),
            "options_mode": "select",
            "option_count": VALIDATION_ROOM_CODE_OPTION_COUNT,
            "attendance_audit": pending_audit.prompt_index == 0,
            "code_bucket": pending_audit_bucket,
        } if pending_audit else None,
        "completed": attempt.status in {ValidationAttempt.Status.COMPLETED, ValidationAttempt.Status.EXPIRED, ValidationAttempt.Status.VOIDED},
        "review_visible": bool(attempt.review_visible),
        "score": float(attempt.score or 0),
        "feedback_release_mode": attempt.feedback_release_mode,
        "time_limit_minutes": 0,
        "expires_at": attempt.expires_at.isoformat() if attempt.expires_at else "",
        "time_remaining_seconds": 0,
        "timer_running": timer_running,
        "show_timer": False,
        "progress": {
            "current_index": _validation_progress(attempt).current_index,
            "total_questions": _validation_progress(attempt).total_questions,
            "answered_count": _validation_progress(attempt).answered_count,
            "remaining_count": _validation_progress(attempt).remaining_count,
        },
        "waq_draft": draft,
        "room_code": None,
        "room_code_client": room_code_client_payload(attempt.event),
        "selected_blocks": [block.title for block in released_blocks],
        "navigation_grace_seconds": VALIDATION_NAVIGATION_GRACE_SECONDS,
        "navigation_warning_count": int(attempt.navigation_warning_count or 0),
        "invalidated_reason": attempt.invalidated_reason,
        "awaiting_attendance_audit": not timer_running,
        "instructions_confirmed": instructions_confirmed,
        "next_available": bool(hide_next_question and (current or latest_answered) and not pending_audit),
        "show_block_switcher": False,
    }


def _save_practice_attempt_answer(
    attempt_question: PracticeAttemptQuestion,
    *,
    selected_answers: list[str],
    answer_text: str,
    is_correct: bool,
    feedback_text: str,
    alignment_score: int | None = None,
    alignment_state: str | None = None,
) -> None:
    if not selected_answers and not answer_text:
        stored_answer = PRACTICE_SKIPPED_SENTINEL
    else:
        stored_answer = answer_text if attempt_question.question.is_written_answer() else "\n".join(selected_answers)
    attempt_question.selected_answer = stored_answer
    attempt_question.is_correct = is_correct
    attempt_question.feedback = _feedback_payload(
        feedback_text,
        correct=is_correct,
        alignment_score=alignment_score,
        alignment_state=alignment_state,
    )
    attempt_question.save(update_fields=["selected_answer", "is_correct", "feedback", "updated_at"])


def _save_official_attempt_answer(
    attempt_question: ValidationAttemptQuestion,
    *,
    selected_answers: list[str],
    answer_text: str,
    is_correct: bool,
    feedback_text: str,
    alignment_score: int | None = None,
    alignment_state: str | None = None,
) -> None:
    attempt_question.selected_answers = selected_answers
    attempt_question.answer_text = answer_text
    attempt_question.is_correct = is_correct
    attempt_question.feedback = _feedback_payload(
        feedback_text,
        correct=is_correct,
        alignment_score=alignment_score,
        alignment_state=alignment_state,
    )
    attempt_question.answered_at = timezone.now()
    attempt_question.save(
        update_fields=["selected_answers", "answer_text", "is_correct", "feedback", "answered_at", "updated_at"]
    )


def draft_validation_practice_answer(request, attempt: PracticeAttempt, question_id: int, answer_text: str) -> dict:
    attempt_question = _practice_current_question(attempt)
    normalized = _normalize_written_answer_text(answer_text)
    if attempt_question is None or attempt_question.question_id != question_id or not attempt_question.question.is_written_answer():
        return {"question_id": question_id, "answer_text": normalized, "alignment_score": 0, "alignment_state": "drafting"}
    draft = _get_draft(request, "practice", attempt.pk, question_id)
    alignment = _draft_written_answer_alignment(attempt_question.question, attempt_question.question.block, normalized, draft or {})
    _set_draft(
        request,
        "practice",
        attempt.pk,
        question_id,
        {
            "answer_text": alignment["answer_text"],
            "alignment_score": alignment["alignment_score"],
            "alignment_state": alignment["alignment_state"],
        },
    )
    return {
        "question_id": question_id,
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }


def draft_official_validation_answer(request, attempt: ValidationAttempt, question_id: int, answer_text: str) -> dict:
    attempt_question = _current_attempt_question(attempt)
    normalized = _normalize_written_answer_text(answer_text)
    if attempt_question is None or attempt_question.question_id != question_id or not attempt_question.question.is_written_answer():
        return {"question_id": question_id, "answer_text": normalized, "alignment_score": 0, "alignment_state": "drafting"}
    draft = _get_draft(request, "official", attempt.pk, question_id)
    alignment = _draft_written_answer_alignment(attempt_question.question, attempt_question.question.block, normalized, draft or {})
    _set_draft(
        request,
        "official",
        attempt.pk,
        question_id,
        {
            "answer_text": alignment["answer_text"],
            "alignment_score": alignment["alignment_score"],
            "alignment_state": alignment["alignment_state"],
        },
    )
    return {
        "question_id": question_id,
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }


def confirm_official_validation_instructions(request, attempt: ValidationAttempt) -> dict:
    _set_ui_attempt_state(request, attempt.pk, instructions_confirmed=True)
    return serialize_official_validation_session(attempt, request=request)


def reveal_official_validation_next(request, attempt: ValidationAttempt) -> dict:
    _clear_next_step(request, attempt)
    return serialize_official_validation_session(attempt, request=request)


def reveal_validation_practice_next(request, attempt: PracticeAttempt) -> dict:
    if not attempt.completed_at and _practice_awaiting_next_step(request, attempt) and _practice_current_question(attempt) is None:
        _mark_practice_complete(attempt)
    _clear_practice_next_step(request, attempt)
    return serialize_validation_practice_session(attempt, request=request)


@transaction.atomic
def skip_validation_practice_question(request, attempt: PracticeAttempt, question_id: int) -> dict:
    if attempt.completed_at:
        return serialize_validation_practice_session(attempt, request=request)
    attempt_question = _practice_current_question(attempt)
    if attempt_question is None or attempt_question.question_id != question_id:
        raise ValidationFlowError("That question is no longer active.")
    _save_practice_attempt_answer(
        attempt_question,
        selected_answers=[],
        answer_text="",
        is_correct=False,
        feedback_text=VALIDATION_SKIPPED_TEXT,
    )
    _clear_draft(request, "practice", attempt.pk, question_id)
    _clear_practice_next_step(request, attempt)
    if _practice_current_question(attempt) is None:
        _mark_practice_complete(attempt)
    return serialize_validation_practice_session(attempt, request=request)


@transaction.atomic
def submit_validation_practice_response(request, attempt: PracticeAttempt, question_id: int, selected_answers=None, *, answer_text: str = "") -> dict:
    if attempt.completed_at:
        return serialize_validation_practice_session(attempt, request=request)
    if _practice_is_expired(attempt):
        _mark_practice_complete(attempt)
        return serialize_validation_practice_session(attempt, request=request)
    attempt_question = _practice_current_question(attempt)
    if attempt_question is None or attempt_question.question_id != question_id:
        latest_answered = _practice_latest_answered_question(attempt) if _practice_awaiting_next_step(request, attempt) else None
        if latest_answered is None or latest_answered.question_id != question_id or not latest_answered.question.is_written_answer():
            raise ValidationFlowError("That question is no longer active.")
        attempt_question = latest_answered
    question = attempt_question.question
    normalized_answers = _normalize_submitted_answers(selected_answers)
    normalized_text = _normalize_written_answer_text(answer_text)
    if question.is_written_answer():
        cumulative_text = _merge_written_answer_text(attempt_question.selected_answer, normalized_text)
        if len(cumulative_text.split()) < PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS:
            raise ValidationFlowError("Please write a little more before submitting.")
        is_correct, alignment, feedback_text = _grade_written_answer_response(question, question.block, cumulative_text)
        _save_practice_attempt_answer(
            attempt_question,
            selected_answers=[],
            answer_text=cumulative_text,
            is_correct=is_correct,
            feedback_text=feedback_text,
            alignment_score=int(alignment["alignment_score"]),
            alignment_state=str(alignment["alignment_state"]),
        )
        _clear_draft(request, "practice", attempt.pk, question_id)
        _append_practice_question_message(
            request,
            attempt,
            question_id,
            {
                "id": f"practice-validation-answer-{attempt.pk}-{question_id}-{timezone.now().timestamp()}",
                "role": "user",
                "kind": "text",
                "question_id": question_id,
                "question_type": question.question_type,
                "text": normalized_text,
            },
        )
    else:
        is_correct, missing_answers, extra_answers = _grade_question_response(question, normalized_answers)
        feedback_text = _feedback_text(question, normalized_answers, is_correct, missing_answers, extra_answers)
        _save_practice_attempt_answer(
            attempt_question,
            selected_answers=normalized_answers,
            answer_text="",
            is_correct=is_correct,
            feedback_text=feedback_text,
        )
    current_after_submit = _practice_current_question(attempt)
    if attempt.completed_at:
        _clear_practice_next_step(request, attempt)
    elif question.is_written_answer():
        _set_practice_next_step(request, attempt, True)
    elif current_after_submit is None:
        _mark_practice_complete(attempt)
    else:
        _clear_practice_next_step(request, attempt)
    return serialize_validation_practice_session(attempt, request=request)


@transaction.atomic
def submit_official_validation_response(
    request,
    attempt: ValidationAttempt,
    *,
    question_id: int | None = None,
    selected_answers=None,
    answer_text: str = "",
    audit_prompt_id: int | None = None,
) -> dict:
    attempt = ValidationAttempt.objects.select_related("event", "enrollment", "enrollment__course").get(pk=attempt.pk)
    _ensure_official_attempt_ready(attempt)
    if attempt.status != ValidationAttempt.Status.IN_PROGRESS:
        return serialize_official_validation_session(attempt, request=request)

    pending_audit = _pending_audit_prompt(attempt)
    normalized_text = _normalize_written_answer_text(answer_text)
    if pending_audit:
        if pending_audit.prompt_index == 0 and not _validation_instructions_confirmed(request, attempt):
            raise ValidationFlowError("Please confirm that you have read the instructions first.")
        if audit_prompt_id and int(audit_prompt_id) != pending_audit.pk:
            raise ValidationFlowError("That audit prompt is no longer active.")
        submitted_code = _normalize_audit_code(normalized_text)
        is_attendance_audit = pending_audit.prompt_index == 0
        expected_code = current_room_code(attempt.event, moment=timezone.now()) if is_attendance_audit else pending_audit.expected_code
        pending_audit.submitted_code = submitted_code
        pending_audit.expected_code = expected_code
        pending_audit.is_correct = submitted_code == _normalize_audit_code(expected_code)
        if pending_audit.is_correct:
            pending_audit.answered_at = timezone.now()
        pending_audit.save(update_fields=["submitted_code", "expected_code", "is_correct", "answered_at", "updated_at"])
        _append_attempt_message(attempt, "user", "text", text=normalized_text or submitted_code, source_blocks=[])
        _append_attempt_message(
            attempt,
            "assistant",
            "feedback",
            text=(
                "Attendance confirmed. Your validation has now started."
                if is_attendance_audit and pending_audit.is_correct
                else (
                    "That code did not match the room display. Your validation has not started yet."
                    if is_attendance_audit
                    else _audit_feedback(bool(pending_audit.is_correct))
                )
            ),
            payload={"correct": bool(pending_audit.is_correct), "audit_prompt_id": pending_audit.pk},
            source_blocks=[],
        )
        if is_attendance_audit and pending_audit.is_correct:
            _start_official_validation_timer(attempt)
            _clear_next_step(request, attempt)
        _ensure_next_attempt_question_message(attempt)
        return serialize_official_validation_session(attempt, request=request)

    attempt_question = _current_attempt_question(attempt)
    if attempt_question is None or attempt_question.question_id != question_id:
        latest_answered = _latest_answered_attempt_question(attempt) if _awaiting_next_step(request, attempt) else None
        if latest_answered is None or latest_answered.question_id != question_id or not latest_answered.question.is_written_answer():
            raise ValidationFlowError("That question is no longer active.")
        attempt_question = latest_answered
    question = attempt_question.question
    normalized_answers = _normalize_submitted_answers(selected_answers)
    if question.is_written_answer():
        cumulative_text = _merge_written_answer_text(attempt_question.answer_text, normalized_text)
        if len(cumulative_text.split()) < PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS:
            raise ValidationFlowError("Please write a little more before submitting.")
        is_correct, alignment, feedback_text = _grade_written_answer_response(question, question.block, cumulative_text)
        _save_official_attempt_answer(
            attempt_question,
            selected_answers=[],
            answer_text=cumulative_text,
            is_correct=is_correct,
            feedback_text=feedback_text,
            alignment_score=int(alignment["alignment_score"]),
            alignment_state=str(alignment["alignment_state"]),
        )
        _clear_draft(request, "official", attempt.pk, question_id)
        _append_attempt_message(
            attempt,
            "user",
            "text",
            question=question,
            attempt_question=attempt_question,
            text=normalized_text,
            payload={"question_type": question.question_type},
        )
    else:
        is_correct, missing_answers, extra_answers = _grade_question_response(question, normalized_answers)
        feedback_text = _feedback_text(question, normalized_answers, is_correct, missing_answers, extra_answers)
        _save_official_attempt_answer(
            attempt_question,
            selected_answers=normalized_answers,
            answer_text="",
            is_correct=is_correct,
            feedback_text=feedback_text,
        )
    if attempt.review_visible and not question.is_written_answer():
        _append_attempt_message(
            attempt,
            "assistant",
            "feedback",
            question=question,
            attempt_question=attempt_question,
            text=feedback_text,
            payload={"correct": bool(is_correct)},
        )
    _ensure_next_attempt_question_message(attempt)
    if question.is_written_answer() and attempt.status == ValidationAttempt.Status.IN_PROGRESS and _current_attempt_question(attempt) is not None:
        _set_next_step(request, attempt, True)
    else:
        _clear_next_step(request, attempt)
    return serialize_official_validation_session(attempt, request=request)


@transaction.atomic
def skip_official_validation_question(request, attempt: ValidationAttempt, *, question_id: int) -> dict:
    attempt = ValidationAttempt.objects.select_related("event", "enrollment", "enrollment__course").get(pk=attempt.pk)
    _ensure_official_attempt_ready(attempt)
    if attempt.status != ValidationAttempt.Status.IN_PROGRESS:
        return serialize_official_validation_session(attempt, request=request)
    if _pending_audit_prompt(attempt):
        raise ValidationFlowError("You need to answer the current attendance audit first.")
    attempt_question = _current_attempt_question(attempt)
    if attempt_question is None or attempt_question.question_id != question_id:
        raise ValidationFlowError("That question is no longer active.")
    question = attempt_question.question
    _save_official_attempt_answer(
        attempt_question,
        selected_answers=[],
        answer_text="",
        is_correct=False,
        feedback_text=VALIDATION_SKIPPED_TEXT,
    )
    _clear_draft(request, "official", attempt.pk, question_id)
    _append_attempt_message(
        attempt,
        "user",
        "text",
        question=question,
        attempt_question=attempt_question,
        text=VALIDATION_SKIPPED_TEXT,
    )
    if attempt.review_visible:
        _append_attempt_message(
            attempt,
            "assistant",
            "feedback",
            question=question,
            attempt_question=attempt_question,
            text=VALIDATION_SKIPPED_TEXT,
            payload={"correct": False},
        )
    _clear_next_step(request, attempt)
    _ensure_next_attempt_question_message(attempt)
    return serialize_official_validation_session(attempt, request=request)


@transaction.atomic
def report_validation_presence(request, attempt: ValidationAttempt, away_seconds: int) -> dict:
    attempt = ValidationAttempt.objects.select_related("event", "enrollment", "enrollment__course").get(pk=attempt.pk)
    if attempt.status != ValidationAttempt.Status.IN_PROGRESS:
        return serialize_official_validation_session(attempt, request=request)
    if int(away_seconds or 0) > VALIDATION_NAVIGATION_GRACE_SECONDS:
        _void_attempt(
            attempt,
            "Validation voided because you navigated away from the validation for too long. You will need to re-validate.",
        )
        return serialize_official_validation_session(attempt, request=request)
    attempt.navigation_warning_count = int(attempt.navigation_warning_count or 0) + 1
    attempt.save(update_fields=["navigation_warning_count", "updated_at"])
    _append_attempt_message(
        attempt,
        "assistant",
        "warning",
        text="Warning: stay inside the validation. Leaving again for longer than 10 seconds will void this attempt.",
        payload={"warning": True, "away_seconds": int(away_seconds or 0)},
        source_blocks=[],
    )
    return serialize_official_validation_session(attempt, request=request)


def release_event_feedback(event: ValidationEvent) -> int:
    attempts = ValidationAttempt.objects.filter(
        event=event,
        completed_at__isnull=False,
        review_released_at__isnull=True,
    )
    released_at = timezone.now()
    return attempts.update(review_released_at=released_at, updated_at=released_at)
