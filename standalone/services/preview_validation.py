from django.utils import timezone

from standalone.models import Course, QuestionBankItem
from standalone.services.preview import PREVIEW_SESSION_KEY, PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS, serialize_preview_state
from standalone.services.questions import generate_question_pair_for_block, question_quality_sort_key
from standalone.services.validation_flow import (
    VALIDATION_SKIPPED_TEXT,
    ValidationFlowError,
    VALIDATION_OFFICIAL_INSTRUCTION_LINES,
    _shuffle_options,
    _draft_written_answer_alignment,
    _feedback_text,
    _grade_question_response,
    _grade_written_answer_response,
    _normalize_audit_code,
    _normalize_submitted_answers,
    _normalize_written_answer_text,
    current_room_code,
    room_code_client_payload,
    select_stratified_validation_questions,
)


PREVIEW_VALIDATION_SESSION_KEY = "standalone_preview_validation"
PREVIEW_STUDENT_VALIDATE_SESSION_KEY = "standalone_preview_student_validate"
PREVIEW_VALIDATION_DEFAULT_QUESTION_COUNT = 10


def _preview_validation_root(request) -> dict:
    return request.session.setdefault(PREVIEW_VALIDATION_SESSION_KEY, {})


def _preview_student_validate_root(request) -> dict:
    return request.session.setdefault(PREVIEW_STUDENT_VALIDATE_SESSION_KEY, {})


def _question_queryset(course: Course):
    return (
        course.question_bank_items.filter(
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            block__available_from__lte=timezone.localdate(),
        )
        .select_related("block", "learning_objective", "source_chunk")
        .order_by("block__order", "learning_objective__position", "created_at", "pk")
    )


def _practice_question_queryset(course: Course):
    return (
        course.question_bank_items.filter(
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            block__available_from__lte=timezone.localdate(),
        )
        .select_related("block", "learning_objective", "source_chunk")
        .order_by("block__order", "learning_objective__position", "created_at", "pk")
    )


def _released_blocks(course: Course):
    return list(course.blocks.filter(available_from__lte=timezone.localdate()).order_by("order", "created_at"))


def _history_entry_question_ids(entry: dict) -> set[int]:
    session = dict(entry.get("session") or {})
    question_ids = {int(question_id) for question_id in session.get("question_ids") or [] if question_id}
    if question_ids:
        return question_ids
    for message in session.get("transcript") or []:
        if message.get("kind") == "question" and message.get("question_id"):
            question_ids.add(int(message["question_id"]))
    return question_ids


def _expand_seen_pair_ids(question_ids: set[int]) -> set[int]:
    expanded = {int(question_id) for question_id in question_ids if question_id}
    if not expanded:
        return expanded
    linked_ids = QuestionBankItem.objects.filter(pk__in=expanded).values_list("linked_question_id", flat=True)
    expanded.update(int(linked_id) for linked_id in linked_ids if linked_id)
    reverse_linked_ids = QuestionBankItem.objects.filter(linked_question_id__in=expanded).values_list("pk", flat=True)
    expanded.update(int(question_id) for question_id in reverse_linked_ids if question_id)
    return expanded


def _preview_seen_question_ids(request, course: Course) -> set[int]:
    seen_ids: set[int] = set()
    preview_root = request.session.get(PREVIEW_SESSION_KEY, {})
    preview_course_state = dict(preview_root.get(str(course.pk)) or {})
    for question_id, state in (preview_course_state.get("question_states") or {}).items():
        if int((state or {}).get("times_presented") or 0) > 0:
            seen_ids.add(int(question_id))
    for event in preview_course_state.get("completed_events") or []:
        if event.get("question_id"):
            seen_ids.add(int(event["question_id"]))

    validation_state = (_preview_validation_root(request).get(str(course.pk)) or {})
    seen_ids.update(int(question_id) for question_id in validation_state.get("question_ids") or [] if question_id)
    for entry in validation_state.get("history") or []:
        seen_ids.update(_history_entry_question_ids(entry))

    preview_validate_state = (_preview_student_validate_root(request).get(str(course.pk)) or {})
    seen_ids.update(int(question_id) for question_id in preview_validate_state.get("question_ids") or [] if question_id)
    return _expand_seen_pair_ids(seen_ids)


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


def _pick_preview_validation_practice_questions(request, course: Course, question_count: int) -> list[int]:
    released_blocks = _released_blocks(course)
    if not released_blocks:
        return []
    seen_ids = _preview_seen_question_ids(request, course)
    available = [
        question
        for question in _practice_question_queryset(course)
        if question.pk not in seen_ids and int(question.linked_question_id or 0) not in seen_ids
        and not question_quality_sort_key(question)[0]
    ]
    seed_key = f"preview-validation:{course.pk}:pool"

    def generator(block, objective_id, question_type, *, preferred_objective_ids=None, strict_preferred_objectives=False):
        practice_question, _validation_question = generate_question_pair_for_block(
            block,
            question_type=question_type,
            preferred_objective_ids=preferred_objective_ids,
            strict_preferred_objectives=strict_preferred_objectives,
        )
        if practice_question is None:
            return None
        if practice_question.pk in seen_ids or int(practice_question.linked_question_id or 0) in seen_ids:
            return None
        return practice_question

    selected = select_stratified_validation_questions(
        course,
        list(available),
        question_count,
        seed_key=seed_key,
        blocks=released_blocks,
        generate_question=generator,
    )
    return [question.pk for question in selected[:question_count]]


def _initial_preview_state(request, course: Course) -> dict:
    question_ids = _pick_preview_validation_practice_questions(request, course, PREVIEW_VALIDATION_DEFAULT_QUESTION_COUNT)
    if not question_ids:
        raise ValidationFlowError("No fresh practice questions are available yet for this course.")
    return {
        "started_at": timezone.now().isoformat(),
        "time_limit_minutes": 0,
        "question_ids": question_ids,
        "answers": {},
        "drafts": {},
        "messages": {},
        "completed": False,
        "completed_at": "",
        "history": [],
        "history_counter": 0,
        "history_entry_id": 0,
    }


def _initial_preview_student_validate_state(course: Course, event) -> dict:
    question_count = int(getattr(event, "question_count", 0) or PREVIEW_VALIDATION_DEFAULT_QUESTION_COUNT)
    released_blocks = _released_blocks(course)
    available = [question for question in _question_queryset(course) if not question_quality_sort_key(question)[0]]
    seed_key = f"preview-validate:{course.pk}:event:{int(event.pk)}"

    def generator(block, objective_id, question_type, *, preferred_objective_ids=None, strict_preferred_objectives=False):
        _practice_question, validation_question = generate_question_pair_for_block(
            block,
            question_type=question_type,
            preferred_objective_ids=preferred_objective_ids,
            strict_preferred_objectives=strict_preferred_objectives,
        )
        return validation_question

    question_ids = [
        question.pk
        for question in select_stratified_validation_questions(
            course,
            available,
            question_count,
            seed_key=seed_key,
            blocks=released_blocks,
            generate_question=generator,
        )
    ]
    if not question_ids:
        raise ValidationFlowError("No validation questions are available yet for this course.")
    return {
        "event_id": int(event.pk),
        "question_ids": question_ids,
        "answers": {},
        "drafts": {},
        "messages": {},
        "preflight_messages": [],
        "instructions_confirmed": False,
        "attendance_confirmed": False,
        "next_available": False,
        "completed": False,
    }


def _course_state(request, course: Course) -> dict:
    preview_root = _preview_validation_root(request)
    state = preview_root.get(str(course.pk))
    if state is None:
        state = _initial_preview_state(request, course)
        preview_root[str(course.pk)] = state
        request.session.modified = True
    return state


def reset_preview_validation_state(request, course: Course) -> dict:
    preview_root = _preview_validation_root(request)
    existing = preview_root.get(str(course.pk)) or {}
    state = _initial_preview_state(request, course)
    state["history"] = list(existing.get("history") or [])
    state["history_counter"] = int(existing.get("history_counter") or 0)
    preview_root[str(course.pk)] = state
    request.session.modified = True
    return state


def _course_student_validate_state(request, course: Course, event) -> dict:
    preview_root = _preview_student_validate_root(request)
    key = str(course.pk)
    state = preview_root.get(key)
    if state is None or int(state.get("event_id") or 0) != int(event.pk):
        state = _initial_preview_student_validate_state(course, event)
        preview_root[key] = state
        request.session.modified = True
    return state


def _question_map(course: Course, question_ids: list[int]) -> dict[int, QuestionBankItem]:
    return {
        question.pk: question
        for question in QuestionBankItem.objects.filter(pk__in=question_ids).select_related("block", "learning_objective", "source_chunk")
    }


def _option_label_options(question: QuestionBankItem, *, seed_key: str = "") -> list[str]:
    options = question.all_answer_options()
    if not options or not seed_key:
        return options
    return _shuffle_options(options, seed_key, question.pk)


def _preview_practice_seed_key(course: Course, state: dict) -> str:
    return f"preview-validation:{course.pk}:{state.get('started_at') or 'default'}"


def _preview_validate_seed_key(course: Course, event) -> str:
    return f"preview-validate:{course.pk}:event:{int(event.pk)}"


def _serialize_question(question: QuestionBankItem, answer_state=None, *, seed_key: str = "") -> dict:
    answer_state = answer_state or {}
    return {
        "question_id": question.pk,
        "question_type": question.question_type,
        "question_type_label": question.question_type_label(),
        "text": question.stem,
        "options": _option_label_options(question, seed_key=seed_key),
        "is_numerical": question.is_numeric(),
        "block_label": question.block.title,
        "learning_objective": question.learning_objective.text if question.learning_objective else "General course understanding",
        "is_coding_question": question.is_coding_question,
        "coding_language": question.coding_language,
        "coding_question_kind": question.coding_question_kind,
        "code_snippet": question.code_snippet,
        "answered": bool(answer_state.get("answered_at")),
        "is_correct": bool(answer_state.get("is_correct")) if answer_state.get("answered_at") else None,
        "selected_answers": list(answer_state.get("selected_answers") or []),
        "correct_answers": question.correct_answers() if answer_state.get("answered_at") else [],
        "submitted_text": answer_state.get("answer_text", ""),
        "alignment_score": int(answer_state.get("alignment_score") or 0),
        "alignment_state": str(answer_state.get("alignment_state") or "drafting"),
        "model_answer": question.correct_answer if question.is_written_answer() and answer_state.get("answered_at") and not answer_state.get("is_correct") else "",
        "model_answer_revealed": bool(question.is_written_answer() and answer_state.get("answered_at") and not answer_state.get("is_correct")),
    }


def _progress(question_ids: list[int], answers: dict) -> dict:
    answered_count = sum(1 for question_id in question_ids if answers.get(str(question_id), {}).get("answered_at"))
    next_index = answered_count + 1 if answered_count < len(question_ids) else len(question_ids)
    return {
        "current_index": next_index,
        "total_questions": len(question_ids),
        "answered_count": answered_count,
        "remaining_count": max(0, len(question_ids) - answered_count),
    }


def _validate_progress(question_ids: list[int], answers: dict) -> dict:
    answered_count = sum(1 for question_id in question_ids if answers.get(str(question_id), {}).get("answered_at"))
    return {
        "current_index": min(answered_count + 1, len(question_ids)) if question_ids else 0,
        "total_questions": len(question_ids),
        "answered_count": answered_count,
        "remaining_count": max(0, len(question_ids) - answered_count),
    }


def _is_complete(state: dict) -> bool:
    return bool(state.get("completed"))


def _pending_question(course: Course, state: dict):
    question_ids = state.get("question_ids", [])
    answers = state.get("answers", {})
    questions = _question_map(course, question_ids)
    for question_id in question_ids:
        if not answers.get(str(question_id), {}).get("answered_at"):
            return questions.get(question_id)
    return None


def _pending_validate_question(course: Course, state: dict):
    question_ids = state.get("question_ids", [])
    answers = state.get("answers", {})
    questions = _question_map(course, question_ids)
    for question_id in question_ids:
        if not answers.get(str(question_id), {}).get("answered_at"):
            return questions.get(question_id)
    return None


def _latest_answered_question(course: Course, state: dict):
    question_ids = state.get("question_ids", [])
    answers = state.get("answers", {})
    questions = _question_map(course, question_ids)
    for question_id in reversed(question_ids):
        if answers.get(str(question_id), {}).get("answered_at"):
            return questions.get(question_id)
    return None


def _merge_written_answer_text(existing_text: str, new_text: str) -> str:
    previous = _normalize_written_answer_text(existing_text)
    latest = _normalize_written_answer_text(new_text)
    if not previous:
        return latest
    if not latest:
        return previous
    return f"{previous}\n\n{latest}"


def _projected_course_score(course: Course, practice_overall: float, validation_score: float) -> tuple[float, int, float, bool]:
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


def _practice_validation_review_transcript(request, course: Course, state: dict) -> list[dict]:
    question_ids = list(state.get("question_ids") or [])
    answers = state.get("answers") or {}
    questions = _question_map(course, question_ids)
    seed_key = _preview_practice_seed_key(course, state)
    correct_count = sum(1 for question_id in question_ids if answers.get(str(question_id), {}).get("is_correct"))
    validation_score = round((correct_count * 100) / max(1, len(question_ids)), 2)
    practice_overall = float(serialize_preview_state(request, course).get("course", {}).get("metrics", {}).get("overall", 0) or 0)
    projected_score, combined_weight, raw_projected_score, applied_floor = _projected_course_score(
        course,
        practice_overall,
        validation_score,
    )
    impact_text = (
        "**Projected overall course score** from this **practice validation**: "
        f"**({practice_overall:.1f} x {int(course.config.practice_weight or 0)} + "
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
            "id": f"preview-validation-summary-score-{course.pk}",
            "role": "assistant",
            "kind": "summary",
            "text": f"**Practice validation complete.** You scored **{validation_score:.1f}%** (**{correct_count} of {len(question_ids)}**).",
        },
        {
            "id": f"preview-validation-summary-impact-{course.pk}",
            "role": "assistant",
            "kind": "summary",
            "text": impact_text,
        },
    ]
    for order, question_id in enumerate(question_ids, start=1):
        question = questions.get(question_id)
        if question is None:
            continue
        answer_state = answers.get(str(question_id), {})
        transcript.append(
            {
                "id": f"preview-validation-review-question-{course.pk}-{order}",
                "role": "assistant",
                "kind": "question",
                **(_serialize_question(question, answer_state, seed_key=seed_key) | {"review_visible": True}),
            }
        )
        user_text = answer_state.get("answer_text") or ", ".join(answer_state.get("selected_answers") or [])
        if user_text and not question.is_written_answer():
            transcript.append(
                {
                    "id": f"preview-validation-review-answer-{course.pk}-{order}",
                    "role": "user",
                    "kind": "text",
                    "question_id": question_id,
                    "question_type": question.question_type,
                    "text": user_text,
                }
            )
        if answer_state.get("feedback") and not question.is_written_answer():
            transcript.append(
                {
                    "id": f"preview-validation-review-feedback-{course.pk}-{order}",
                    "role": "assistant",
                    "kind": "feedback",
                    "question_id": question_id,
                    "text": answer_state.get("feedback", ""),
                    "correct": bool(answer_state.get("is_correct")),
                }
            )
    return transcript


def _completed_preview_validation_session(request, course: Course, state: dict) -> dict:
    question_ids = list(state.get("question_ids") or [])
    answers = state.get("answers") or {}
    correct_count = sum(1 for question_id in question_ids if answers.get(str(question_id), {}).get("is_correct"))
    score = round((correct_count * 100) / max(1, len(question_ids)), 2)
    return {
        "mode": "validation_practice",
        "attempt_id": f"preview-{course.pk}",
        "title": f"{course.title} Validation practice",
        "transcript": _practice_validation_review_transcript(request, course, state),
        "pending_question": None,
        "pending_audit": None,
        "completed": True,
        "review_visible": True,
        "score": score,
        "time_limit_minutes": 0,
        "expires_at": "",
        "time_remaining_seconds": 0,
        "timer_running": False,
        "show_timer": False,
        "progress": _progress(question_ids, answers),
        "waq_draft": {},
        "next_available": False,
    }


def _ensure_preview_validation_history_entry(request, course: Course, state: dict, session_state: dict) -> None:
    if int(state.get("history_entry_id") or 0):
        return
    history = list(state.get("history") or [])
    next_id = int(state.get("history_counter") or 0) + 1
    history.append(
        {
            "id": next_id,
            "completed_at": str(state.get("completed_at") or timezone.now().isoformat()),
            "score": float(session_state.get("score") or 0),
            "question_count": int(session_state.get("progress", {}).get("total_questions") or 0),
            "session": session_state,
        }
    )
    state["history"] = history
    state["history_counter"] = next_id
    state["history_entry_id"] = next_id
    request.session.modified = True


def preview_validation_history_items(request, course: Course) -> list[dict]:
    state = (_preview_validation_root(request).get(str(course.pk)) or {})
    items = []
    for entry in reversed(list(state.get("history") or [])):
        items.append(
            {
                "id": int(entry.get("id") or 0),
                "completed_at": str(entry.get("completed_at") or ""),
                "score": float(entry.get("score") or 0),
                "question_count": int(entry.get("question_count") or 0),
            }
        )
    return items


def preview_validation_history_session(request, course: Course, history_id: int) -> dict:
    state = (_preview_validation_root(request).get(str(course.pk)) or {})
    for entry in state.get("history") or []:
        if int(entry.get("id") or 0) == int(history_id):
            return dict(entry.get("session") or {})
    raise ValidationFlowError("That practice validation review is no longer available.")


def _validate_transcript_questions(course: Course, state: dict, *, seed_key: str = "") -> list[dict]:
    question_ids = list(state.get("question_ids") or [])
    answers = state.get("answers") or {}
    questions = _question_map(course, question_ids)
    transcript = []
    for order, question_id in enumerate(question_ids, start=1):
        question = questions.get(question_id)
        if question is None:
            continue
        answer_state = answers.get(str(question_id), {})
        transcript.append(
            {
                "id": f"preview-student-validate-question-{course.pk}-{order}",
                "role": "assistant",
                "kind": "question",
                **_serialize_question(question, answer_state, seed_key=seed_key),
            }
        )
        if answer_state.get("answered_at") and not question.is_written_answer():
            transcript.append(
                {
                    "id": f"preview-student-validate-feedback-{course.pk}-{order}",
                    "role": "assistant",
                    "kind": "feedback",
                    "question_id": question_id,
                    "text": answer_state.get("feedback", ""),
                    "correct": bool(answer_state.get("is_correct")),
                }
            )
    return transcript


def _validate_is_complete(state: dict) -> bool:
    question_ids = list(state.get("question_ids") or [])
    answers = state.get("answers") or {}
    return bool(state.get("completed")) or (bool(question_ids) and all(answers.get(str(question_id), {}).get("answered_at") for question_id in question_ids))


def serialize_preview_validation_state(request, course: Course) -> dict:
    state = _course_state(request, course)
    seed_key = _preview_practice_seed_key(course, state)
    question_ids = list(state.get("question_ids") or [])
    answers = state.get("answers") or {}
    message_state = state.get("messages") or {}
    next_available = bool(state.get("next_available"))
    if _is_complete(state):
        session_state = _completed_preview_validation_session(request, course, state)
        _ensure_preview_validation_history_entry(request, course, state, session_state)
        return session_state
    questions = _question_map(course, question_ids)
    transcript = [
        {
            "id": f"preview-validation-intro-{course.pk}",
            "role": "assistant",
            "kind": "text",
            "text": "Practice validation is untimed. Work through the locked practice set in order. Feedback is shown at the end.",
        }
    ]
    correct_count = 0
    for order, question_id in enumerate(question_ids, start=1):
        question = questions.get(question_id)
        if question is None:
            continue
        answer_state = answers.get(str(question_id), {})
        transcript.append(
            {
                "id": f"preview-validation-question-{course.pk}-{order}",
                "role": "assistant",
                "kind": "question",
                **_serialize_question(question, answer_state, seed_key=seed_key),
            }
        )
        if answer_state.get("answered_at") and answer_state.get("is_correct"):
            correct_count += 1
    progress = _progress(question_ids, answers)
    complete = False
    score = 0.0
    pending_question = None if complete else _pending_question(course, state)
    latest_answered = _latest_answered_question(course, state)
    visible_question = pending_question
    if next_available and latest_answered:
        visible_question = latest_answered
    if visible_question and visible_question.is_written_answer():
        transcript.extend(list(message_state.get(str(visible_question.pk)) or []))
    draft = {}
    if visible_question and visible_question.is_written_answer():
        draft = dict((state.get("drafts") or {}).get(str(visible_question.pk), {}))
    return {
        "mode": "validation_practice",
        "attempt_id": f"preview-{course.pk}",
        "title": f"{course.title} Validation practice",
        "transcript": transcript,
        "pending_question": (
            _serialize_question(visible_question, (answers.get(str(visible_question.pk), {}) | draft), seed_key=seed_key)
            if visible_question and (not next_available or visible_question.is_written_answer())
            else None
        ),
        "pending_audit": None,
        "completed": complete,
        "review_visible": False,
        "score": score,
        "time_limit_minutes": 0,
        "expires_at": "",
        "time_remaining_seconds": 0,
        "timer_running": False,
        "show_timer": False,
        "progress": progress,
        "waq_draft": draft,
        "next_available": bool(next_available and (pending_question or latest_answered) and not complete),
    }


def reveal_preview_validation_next(request, course: Course) -> dict:
    state = _course_state(request, course)
    if state.get("next_available") and _pending_question(course, state) is None:
        state["completed"] = True
        state["completed_at"] = timezone.now().isoformat()
    state["next_available"] = False
    request.session.modified = True
    return serialize_preview_validation_state(request, course)


def serialize_preview_student_validate_state(request, course: Course, event) -> dict:
    state = _course_student_validate_state(request, course, event)
    seed_key = _preview_validate_seed_key(course, event)
    question_ids = list(state.get("question_ids") or [])
    answers = state.get("answers") or {}
    message_state = state.get("messages") or {}
    progress = _validate_progress(question_ids, answers)
    complete = _validate_is_complete(state)
    attendance_confirmed = bool(state.get("attendance_confirmed"))
    instructions_confirmed = bool(state.get("instructions_confirmed"))
    next_available = bool(state.get("next_available")) and not complete
    pending_question = _pending_validate_question(course, state)
    latest_answered = _latest_answered_question(course, state)
    visible_question = pending_question
    if next_available and latest_answered and latest_answered.is_written_answer():
        visible_question = latest_answered
    if visible_question and visible_question.is_written_answer():
        draft = dict((state.get("drafts") or {}).get(str(visible_question.pk), {}))
    else:
        draft = {}

    transcript = []
    if not attendance_confirmed:
        transcript.extend(
            [
                {
                    "id": f"preview-student-validate-instruction-{course.pk}-{index}",
                    "role": "assistant",
                    "kind": "text",
                    "text": line,
                }
                for index, line in enumerate(VALIDATION_OFFICIAL_INSTRUCTION_LINES, start=1)
            ]
        )
        transcript.extend(list(state.get("preflight_messages") or []))
        if not instructions_confirmed:
            transcript.append(
                {
                    "id": f"preview-student-validate-confirm-{course.pk}",
                    "role": "assistant",
                    "kind": "confirm",
                    "text": "Please confirm that you have read and understood these instructions.",
                    "button_label": "I have read and understood these instructions",
                }
            )
        else:
            transcript.append(
                {
                    "id": f"preview-student-validate-audit-copy-{course.pk}",
                    "role": "assistant",
                    "kind": "text",
                    "text": "When you are ready to begin please select the matching session code from the list below.",
                }
            )
    else:
        transcript.extend(list(state.get("preflight_messages") or []))
        transcript.extend(_validate_transcript_questions(course, state, seed_key=seed_key))
        if visible_question and visible_question.is_written_answer():
            transcript.extend(list(message_state.get(str(visible_question.pk)) or []))

    if complete:
        correct_count = sum(1 for question_id in question_ids if answers.get(str(question_id), {}).get("is_correct"))
        score = round((correct_count * 100) / max(1, len(question_ids)), 2)
    else:
        score = 0.0

    return {
        "mode": "preview_validate",
        "attempt_id": f"preview-validate-{course.pk}-{event.pk}",
        "event_id": event.pk,
        "title": "Validation session",
        "course_title": course.title,
        "eyebrow": "Validate",
        "transcript": transcript,
        "pending_question": (
            _serialize_question(visible_question, (answers.get(str(visible_question.pk), {}) | draft), seed_key=seed_key)
            if visible_question and attendance_confirmed and (not next_available or visible_question.is_written_answer()) and not complete
            else None
        ),
        "pending_audit": (
            {
                "id": int(event.pk),
                "text": "When you are ready to begin please select the matching session code from the list below.",
                "options_mode": "select",
                "option_count": 4,
                "attendance_audit": True,
                "code_bucket": None,
            }
            if instructions_confirmed and not attendance_confirmed
            else None
        ),
        "completed": complete,
        "review_visible": True,
        "score": score,
        "feedback_release_mode": "immediate",
        "time_limit_minutes": 0,
        "expires_at": "",
        "time_remaining_seconds": 0,
        "timer_running": attendance_confirmed and not complete,
        "show_timer": False,
        "progress": progress,
        "waq_draft": draft,
        "room_code": None,
        "room_code_client": room_code_client_payload(event),
        "selected_blocks": [block.title for block in _released_blocks(course)],
        "navigation_grace_seconds": 10,
        "navigation_warning_count": 0,
        "invalidated_reason": "",
        "awaiting_attendance_audit": not attendance_confirmed,
        "instructions_confirmed": instructions_confirmed,
        "next_available": next_available,
        "show_block_switcher": False,
    }


def draft_preview_validation_answer(request, course: Course, question_id: int, answer_text: str) -> dict:
    state = _course_state(request, course)
    question = _pending_question(course, state)
    normalized_text = _normalize_written_answer_text(answer_text)
    if question is None or question.pk != question_id or not question.is_written_answer():
        return {
            "question_id": question_id,
            "answer_text": normalized_text,
            "alignment_score": 0,
            "alignment_state": "drafting",
        }
    draft_state = dict((state.get("drafts") or {}).get(str(question_id), {}))
    alignment = _draft_written_answer_alignment(question, question.block, normalized_text, draft_state)
    state.setdefault("drafts", {})[str(question_id)] = {
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }
    request.session.modified = True
    return {
        "question_id": question_id,
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }


def confirm_preview_student_validate(request, course: Course, event) -> dict:
    state = _course_student_validate_state(request, course, event)
    state["instructions_confirmed"] = True
    request.session.modified = True
    return serialize_preview_student_validate_state(request, course, event)


def reveal_preview_student_validate_next(request, course: Course, event) -> dict:
    state = _course_student_validate_state(request, course, event)
    state["next_available"] = False
    request.session.modified = True
    return serialize_preview_student_validate_state(request, course, event)


def draft_preview_student_validate_answer(request, course: Course, event, question_id: int, answer_text: str) -> dict:
    state = _course_student_validate_state(request, course, event)
    question = _pending_validate_question(course, state)
    normalized_text = _normalize_written_answer_text(answer_text)
    if question is None or question.pk != question_id or not question.is_written_answer():
        return {
            "question_id": question_id,
            "answer_text": normalized_text,
            "alignment_score": 0,
            "alignment_state": "drafting",
        }
    draft_state = dict((state.get("drafts") or {}).get(str(question_id), {}))
    alignment = _draft_written_answer_alignment(question, question.block, normalized_text, draft_state)
    state.setdefault("drafts", {})[str(question_id)] = {
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }
    request.session.modified = True
    return {
        "question_id": question_id,
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }


def submit_preview_validation_answer(request, course: Course, question_id: int, selected_answers=None, *, answer_text: str = "") -> dict:
    state = _course_state(request, course)
    question = _pending_question(course, state)
    if question is None or question.pk != question_id:
        latest_answered = _latest_answered_question(course, state) if state.get("next_available") else None
        if latest_answered is None or latest_answered.pk != question_id or not latest_answered.is_written_answer():
            raise ValidationFlowError("That question is no longer active.")
        question = latest_answered

    if question.is_written_answer():
        normalized_text = _normalize_written_answer_text(answer_text)
        existing_answer_state = dict((state.get("answers") or {}).get(str(question_id), {}))
        cumulative_text = _merge_written_answer_text(existing_answer_state.get("answer_text", ""), normalized_text)
        if len(cumulative_text.split()) < PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS:
            raise ValidationFlowError("Please write a little more before submitting.")
        is_correct, alignment, feedback_text = _grade_written_answer_response(question, question.block, cumulative_text)
        answer_state = {
            "answer_text": cumulative_text,
            "selected_answers": [],
            "is_correct": is_correct,
            "feedback": feedback_text,
            "alignment_score": int(alignment["alignment_score"]),
            "alignment_state": str(alignment["alignment_state"]),
            "answered_at": timezone.now().isoformat(),
        }
        state.setdefault("drafts", {}).pop(str(question_id), None)
        state.setdefault("messages", {}).setdefault(str(question_id), []).append(
            {
                "id": f"preview-validation-answer-{course.pk}-{question_id}-{len(state.get('messages', {}).get(str(question_id), [])) + 1}",
                "role": "user",
                "kind": "text",
                "question_id": question_id,
                "question_type": question.question_type,
                "text": normalized_text,
            }
        )
    else:
        normalized_answers = _normalize_submitted_answers(selected_answers)
        is_correct, missing_answers, extra_answers = _grade_question_response(question, normalized_answers)
        feedback_text = _feedback_text(question, normalized_answers, is_correct, missing_answers, extra_answers)
        answer_state = {
            "answer_text": "",
            "selected_answers": normalized_answers,
            "is_correct": is_correct,
            "feedback": feedback_text,
            "answered_at": timezone.now().isoformat(),
        }

    state.setdefault("answers", {})[str(question_id)] = answer_state
    remaining_question = _pending_question(course, state)
    if question.is_written_answer():
        state["next_available"] = True
        state["completed"] = False
    else:
        state["next_available"] = False
        state["completed"] = remaining_question is None
        if state["completed"]:
            state["completed_at"] = timezone.now().isoformat()
    request.session.modified = True
    return serialize_preview_validation_state(request, course)


def skip_preview_validation_question(request, course: Course, question_id: int) -> dict:
    state = _course_state(request, course)
    question = _pending_question(course, state)
    if question is None or question.pk != question_id:
        raise ValidationFlowError("That question is no longer active.")
    state.setdefault("answers", {})[str(question_id)] = {
        "answer_text": "",
        "selected_answers": [],
        "is_correct": False,
        "feedback": VALIDATION_SKIPPED_TEXT,
        "answered_at": timezone.now().isoformat(),
    }
    state["next_available"] = False
    state["completed"] = _pending_question(course, state) is None
    if state["completed"]:
        state["completed_at"] = timezone.now().isoformat()
    request.session.modified = True
    return serialize_preview_validation_state(request, course)


def submit_preview_student_validate_response(
    request,
    course: Course,
    event,
    *,
    question_id: int | None = None,
    selected_answers=None,
    answer_text: str = "",
    audit_prompt_id=None,
) -> dict:
    state = _course_student_validate_state(request, course, event)
    normalized_text = _normalize_written_answer_text(answer_text)
    if not state.get("attendance_confirmed"):
        if not state.get("instructions_confirmed"):
            raise ValidationFlowError("Please confirm that you have read the instructions first.")
        submitted_code = _normalize_audit_code(normalized_text)
        expected_code = current_room_code(event)
        state.setdefault("preflight_messages", []).extend(
            [
                {
                    "id": f"preview-student-validate-audit-answer-{course.pk}-{len(state.get('preflight_messages', [])) + 1}",
                    "role": "user",
                    "kind": "text",
                    "text": normalized_text or submitted_code,
                },
                {
                    "id": f"preview-student-validate-audit-feedback-{course.pk}-{len(state.get('preflight_messages', [])) + 2}",
                    "role": "assistant",
                    "kind": "feedback",
                    "text": (
                        "Attendance confirmed. Your validation preview has now started."
                        if submitted_code == _normalize_audit_code(expected_code)
                        else "That code did not match the room display. The validation preview has not started yet."
                    ),
                    "correct": submitted_code == _normalize_audit_code(expected_code),
                },
            ]
        )
        if submitted_code == _normalize_audit_code(expected_code):
            state["attendance_confirmed"] = True
        request.session.modified = True
        return serialize_preview_student_validate_state(request, course, event)

    question = _pending_validate_question(course, state)
    if question is None or question.pk != question_id:
        latest_answered = _latest_answered_question(course, state) if state.get("next_available") else None
        if latest_answered is None or latest_answered.pk != question_id or not latest_answered.is_written_answer():
            raise ValidationFlowError("That question is no longer active.")
        question = latest_answered

    if question.is_written_answer():
        existing_answer_state = dict((state.get("answers") or {}).get(str(question_id), {}))
        cumulative_text = _merge_written_answer_text(existing_answer_state.get("answer_text", ""), normalized_text)
        if len(cumulative_text.split()) < PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS:
            raise ValidationFlowError("Please write a little more before submitting.")
        is_correct, alignment, feedback_text = _grade_written_answer_response(question, question.block, cumulative_text)
        answer_state = {
            "answer_text": cumulative_text,
            "selected_answers": [],
            "is_correct": is_correct,
            "feedback": feedback_text,
            "alignment_score": int(alignment["alignment_score"]),
            "alignment_state": str(alignment["alignment_state"]),
            "answered_at": timezone.now().isoformat(),
        }
        state.setdefault("drafts", {}).pop(str(question_id), None)
        state.setdefault("messages", {}).setdefault(str(question_id), []).append(
            {
                "id": f"preview-student-validate-answer-{course.pk}-{question_id}-{len(state.get('messages', {}).get(str(question_id), [])) + 1}",
                "role": "user",
                "kind": "text",
                "question_id": question_id,
                "question_type": question.question_type,
                "text": normalized_text,
            }
        )
    else:
        normalized_answers = _normalize_submitted_answers(selected_answers)
        is_correct, missing_answers, extra_answers = _grade_question_response(question, normalized_answers)
        feedback_text = _feedback_text(question, normalized_answers, is_correct, missing_answers, extra_answers)
        answer_state = {
            "answer_text": "",
            "selected_answers": normalized_answers,
            "is_correct": is_correct,
            "feedback": feedback_text,
            "answered_at": timezone.now().isoformat(),
        }

    state.setdefault("answers", {})[str(question_id)] = answer_state
    remaining_question = _pending_validate_question(course, state)
    state["next_available"] = bool(question.is_written_answer() and remaining_question is not None)
    request.session.modified = True
    return serialize_preview_student_validate_state(request, course, event)


def skip_preview_student_validate_question(request, course: Course, event, *, question_id: int) -> dict:
    state = _course_student_validate_state(request, course, event)
    if not state.get("attendance_confirmed"):
        raise ValidationFlowError("The validation preview has not started yet.")
    question = _pending_validate_question(course, state)
    if question is None or question.pk != question_id:
        raise ValidationFlowError("That question is no longer active.")
    state.setdefault("answers", {})[str(question_id)] = {
        "answer_text": "",
        "selected_answers": [],
        "is_correct": False,
        "feedback": VALIDATION_SKIPPED_TEXT,
        "answered_at": timezone.now().isoformat(),
    }
    state["next_available"] = False
    request.session.modified = True
    return serialize_preview_student_validate_state(request, course, event)
