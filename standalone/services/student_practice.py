import re
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from standalone.models import (
    Enrollment,
    EnrollmentQuestionState,
    PracticeAttempt,
    PracticeAttemptQuestion,
    PracticeMessage,
    QuestionBankItem,
    QuestionFlag,
)
from standalone.services.metrics import refresh_enrollment_metrics
from standalone.services.preview import (
    PREVIEW_SESSION_KEY,
    _empty_course_state,
    draft_preview_written_answer,
    flag_preview_question,
    request_preview_quiz,
    send_preview_chat_message,
    serialize_preview_state,
    submit_preview_answer,
)


class _StateSession(dict):
    modified = False


def _fake_request(course, course_state):
    return SimpleNamespace(session=_StateSession({PREVIEW_SESSION_KEY: {str(course.pk): course_state}}))


def _course_state_from_request(request, course):
    return request.session[PREVIEW_SESSION_KEY][str(course.pk)]


def _message_sequence(message: dict, fallback: int) -> int:
    message_id = str(message.get("id") or "")
    match = re.search(r"(\d+)$", message_id)
    if match:
        return int(match.group(1))
    return fallback


def _aware_datetime(value):
    if not value:
        return None
    if not isinstance(value, str):
        return value
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _student_course_state(enrollment: Enrollment) -> dict:
    course = enrollment.course
    course_state = _empty_course_state()
    transcripts = course_state.setdefault("transcripts", {})
    max_sequence = 0
    for message in enrollment.practice_messages.select_related("block", "question").order_by("sequence", "created_at"):
        payload = dict(message.payload or {})
        payload.setdefault("id", message.message_id)
        payload.setdefault("created_at", message.created_at.isoformat())
        payload.setdefault("role", message.role)
        payload.setdefault("kind", message.kind)
        payload.setdefault("text", message.text)
        if message.question_id:
            payload.setdefault("question_id", message.question_id)
        if message.source_blocks:
            payload.setdefault("source_blocks", message.source_blocks)
        transcripts.setdefault(str(message.block_id), []).append(payload)
        max_sequence = max(max_sequence, message.sequence)
    course_state["message_counter"] = max_sequence

    for state in EnrollmentQuestionState.objects.filter(enrollment=enrollment, question__course=course):
        course_state["question_states"][str(state.question_id)] = {
            "enrollment": enrollment.pk,
            "question": state.question_id,
            "times_presented": state.times_presented,
            "times_correct": state.times_correct,
            "times_incorrect": state.times_incorrect,
            "last_presented_sequence": state.last_presented_sequence,
            "retired_at": state.retired_at.isoformat() if state.retired_at else None,
        }

    flagged_ids = list(
        QuestionFlag.objects.filter(enrollment=enrollment, question__course=course)
        .values_list("question_id", flat=True)
        .distinct()
    )
    course_state["flagged_question_ids"] = [str(question_id) for question_id in flagged_ids]

    completed_events = []
    answers = (
        PracticeAttemptQuestion.objects.filter(
            attempt__enrollment=enrollment,
            attempt__attempt_type=PracticeAttempt.AttemptType.PRACTICE,
        )
        .select_related("attempt", "question")
        .order_by("created_at", "pk")
    )
    for answer in answers:
        question = answer.question
        answered_at = answer.attempt.completed_at or answer.created_at
        completed_events.append(
            {
                "attempt_question_id": answer.pk,
                "block_id": answer.attempt.block_id or question.block_id,
                "question_id": question.pk,
                "correct": answer.is_correct,
                "answered_at": answered_at.isoformat(),
                "learning_objective_id": question.learning_objective_id,
                "source_chunk_id": question.source_chunk_id,
                "question_type": question.question_type,
                "answer_text": answer.selected_answer,
                "feedback": answer.feedback,
            }
        )
    course_state["completed_events"] = completed_events
    course_state["completion_sequence"] = len(completed_events)

    pending_questions = course_state.setdefault("pending_questions", {})
    for block_id, transcript in transcripts.items():
        pending_questions[block_id] = None
        for message in transcript:
            if message.get("kind") == "question":
                if message.get("answered") or message.get("flagged"):
                    if pending_questions.get(block_id) == message.get("question_id"):
                        pending_questions[block_id] = None
                    continue
                pending_questions[block_id] = message.get("question_id")
    return course_state


def _sync_completed_events(enrollment: Enrollment, course_state: dict) -> bool:
    created_answers = False
    for event in course_state.get("completed_events", []):
        if event.get("attempt_question_id"):
            continue
        question = QuestionBankItem.objects.filter(
            pk=event.get("question_id"),
            course=enrollment.course,
            bank_type=QuestionBankItem.BankType.PRACTICE,
        ).first()
        if question is None:
            continue
        is_correct = bool(event.get("correct"))
        answered_at = _aware_datetime(event.get("answered_at")) or timezone.now()
        selected_answer = str(event.get("answer_text") or ", ".join(event.get("selected_answers") or [])).strip()
        attempt = PracticeAttempt.objects.create(
            enrollment=enrollment,
            attempt_type=PracticeAttempt.AttemptType.PRACTICE,
            block=question.block,
            completed_at=answered_at,
            score=100 if is_correct else 0,
        )
        attempt_question = PracticeAttemptQuestion.objects.create(
            attempt=attempt,
            question=question,
            order=1,
            selected_answer=selected_answer,
            is_correct=is_correct,
            feedback=str(event.get("feedback") or ""),
        )
        event["attempt_question_id"] = attempt_question.pk
        created_answers = True
    return created_answers


def _sync_question_states(enrollment: Enrollment, course_state: dict) -> None:
    for question_id, state in course_state.get("question_states", {}).items():
        question = QuestionBankItem.objects.filter(pk=question_id, course=enrollment.course).first()
        if question is None:
            continue
        EnrollmentQuestionState.objects.update_or_create(
            enrollment=enrollment,
            question=question,
            defaults={
                "times_presented": int(state.get("times_presented", 0) or 0),
                "times_correct": int(state.get("times_correct", 0) or 0),
                "times_incorrect": int(state.get("times_incorrect", 0) or 0),
                "last_presented_sequence": int(state.get("last_presented_sequence", 0) or 0),
                "retired_at": _aware_datetime(state.get("retired_at")),
            },
        )


def _sync_flags(enrollment: Enrollment, course_state: dict) -> None:
    for question_id in course_state.get("flagged_question_ids", []):
        question = QuestionBankItem.objects.filter(pk=question_id, course=enrollment.course).first()
        if question is None:
            continue
        QuestionFlag.objects.get_or_create(
            question=question,
            flagged_by=enrollment.student,
            enrollment=enrollment,
            defaults={"reason": "Flagged during student practice."},
        )


def _sync_messages(enrollment: Enrollment, course_state: dict) -> None:
    fallback_sequence = 0
    for block_id, transcript in course_state.get("transcripts", {}).items():
        for message in transcript:
            fallback_sequence += 1
            sequence = _message_sequence(message, fallback_sequence)
            message_id = str(message.get("id") or f"student-practice-message-{sequence}")
            message["id"] = message_id
            question_id = message.get("question_id") or None
            question = None
            if question_id:
                question = QuestionBankItem.objects.filter(pk=question_id, course=enrollment.course).first()
            PracticeMessage.objects.update_or_create(
                enrollment=enrollment,
                message_id=message_id,
                defaults={
                    "block_id": int(block_id),
                    "question": question,
                    "sequence": sequence,
                    "role": str(message.get("role") or "assistant")[:20],
                    "kind": str(message.get("kind") or "text")[:30],
                    "text": str(message.get("text") or ""),
                    "payload": message,
                    "source_blocks": list(message.get("source_blocks") or []),
                },
            )


def _sync_state_to_enrollment(enrollment: Enrollment, course_state: dict, *, refresh_metrics: bool = True) -> None:
    with transaction.atomic():
        created_answers = _sync_completed_events(enrollment, course_state)
        _sync_question_states(enrollment, course_state)
        _sync_flags(enrollment, course_state)
        _sync_messages(enrollment, course_state)
        if refresh_metrics and created_answers:
            refresh_enrollment_metrics(enrollment)


def serialize_student_practice_state(enrollment: Enrollment, *, active_block_id=None) -> dict:
    course_state = _student_course_state(enrollment)
    request = _fake_request(enrollment.course, course_state)
    payload = serialize_preview_state(request, enrollment.course, active_block_id=active_block_id)
    _sync_state_to_enrollment(enrollment, _course_state_from_request(request, enrollment.course), refresh_metrics=False)
    return payload


def request_student_practice_quiz(enrollment: Enrollment, block, requested_question_type: str | None = None) -> dict:
    course_state = _student_course_state(enrollment)
    request = _fake_request(enrollment.course, course_state)
    payload = request_preview_quiz(request, enrollment.course, block, requested_question_type=requested_question_type)
    _sync_state_to_enrollment(enrollment, _course_state_from_request(request, enrollment.course), refresh_metrics=False)
    return payload


def draft_student_practice_written_answer(enrollment: Enrollment, block, question_id: int, answer_text: str) -> dict:
    course_state = _student_course_state(enrollment)
    request = _fake_request(enrollment.course, course_state)
    return draft_preview_written_answer(request, enrollment.course, block, question_id, answer_text)


def submit_student_practice_answer(enrollment: Enrollment, block, question_id: int, selected_answers=None, *, answer_text: str = "") -> dict:
    course_state = _student_course_state(enrollment)
    request = _fake_request(enrollment.course, course_state)
    payload = submit_preview_answer(
        request,
        enrollment.course,
        block,
        question_id,
        selected_answers,
        answer_text=answer_text,
    )
    _sync_state_to_enrollment(enrollment, _course_state_from_request(request, enrollment.course))
    return payload


def send_student_practice_chat_message(enrollment: Enrollment, block, question: str) -> dict:
    course_state = _student_course_state(enrollment)
    request = _fake_request(enrollment.course, course_state)
    payload = send_preview_chat_message(request, enrollment.course, block, question)
    _sync_state_to_enrollment(enrollment, _course_state_from_request(request, enrollment.course), refresh_metrics=False)
    return payload


def flag_student_practice_question(enrollment: Enrollment, block, question_id: int) -> dict:
    course_state = _student_course_state(enrollment)
    request = _fake_request(enrollment.course, course_state)
    payload = flag_preview_question(request, enrollment.course, block, question_id)
    _sync_state_to_enrollment(enrollment, _course_state_from_request(request, enrollment.course), refresh_metrics=False)
    return payload
