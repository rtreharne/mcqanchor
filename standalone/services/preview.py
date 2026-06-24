import json
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from openai import OpenAI

from standalone.models import Course, CourseBlock, LearningObjective, LearningObjectiveCorrection, QuestionBankItem
from standalone.services.guidance import build_chat_guidance_prompt, merge_assistant_guidance, sanitize_assistant_guidance
from standalone.services.questions import (
    QuestionGenerationError,
    coding_question_matches_expected_language,
    coding_question_quality_sort_key,
    further_study_questions_for_chat,
    further_study_questions_for_question,
    generate_question_pair_for_block,
    normalize_explanation_text,
    normalize_numeric_explanation_text,
    preferred_coding_language_for_block,
    question_quality_sort_key,
)


PREVIEW_SESSION_KEY = "standalone_student_preview"
PREVIEW_ENROLLMENT_KEY = "preview"
PREVIEW_RETRY_COMPLETION_GAP = 3
PREVIEW_ENGAGEMENT_WINDOW_DAYS = 7
PREVIEW_CHAT_RETRIEVAL_LIMIT = 6
PREVIEW_CHAT_HISTORY_LIMIT = 6
PREVIEW_INAPPROPRIATE_MESSAGE_WARNING = "Please keep messages respectful and appropriate. All conversations are logged and auditable by teachers."
PREVIEW_KEEP_GOING_LINES = (
    "Hit Quiz to keep going!",
    "Ready for another one? Hit Quiz.",
    "Keep the streak going. Hit Quiz.",
    "Want the next question? Tap Quiz.",
    "On to the next one. Hit Quiz.",
)
PREVIEW_KEEP_GOING_DELAY = timedelta(minutes=5)
WAQ_ALIGNMENT_THRESHOLD = 0.75
PREVIEW_WAQ_CLOSE_THRESHOLD = 0.55
PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS = 3
PREVIEW_WAQ_OPENAI_DRAFT_MIN_CHARS = 24
PREVIEW_WAQ_OPENAI_CHECK_INTERVAL = 12
ADVANCED_QUESTION_TYPES = {QuestionBankItem.QuestionType.MAQ, QuestionBankItem.QuestionType.WAQ}


def _empty_course_state() -> dict:
    return {
        "completion_sequence": 0,
        "message_counter": 0,
        "question_states": {},
        "flagged_question_ids": [],
        "transcripts": {},
        "pending_questions": {},
        "written_answer_drafts": {},
        "completed_events": [],
    }


def _default_question_state(question_id: int) -> dict:
    return {
        "enrollment": PREVIEW_ENROLLMENT_KEY,
        "question": question_id,
        "times_presented": 0,
        "times_correct": 0,
        "times_incorrect": 0,
        "last_presented_sequence": 0,
        "retired_at": None,
    }


def _question_state(course_state: dict, question_id: int) -> dict:
    states = course_state.setdefault("question_states", {})
    return states.setdefault(str(question_id), _default_question_state(question_id))


def _course_state(request, course: Course) -> dict:
    preview_root = request.session.setdefault(PREVIEW_SESSION_KEY, {})
    return preview_root.setdefault(str(course.pk), _empty_course_state())


def _next_message_id(course_state: dict) -> str:
    course_state["message_counter"] = course_state.get("message_counter", 0) + 1
    return f"preview-message-{course_state['message_counter']}"


def _ensure_block_transcript(course_state: dict, block: CourseBlock) -> list[dict]:
    transcripts = course_state.setdefault("transcripts", {})
    transcript = transcripts.setdefault(str(block.pk), [])
    if not transcript:
        transcript.append(
            {
                "id": _next_message_id(course_state),
                "created_at": timezone.now().isoformat(),
                "role": "assistant",
                "kind": "text",
                "text": (
                    f"Welcome to {block.title}. You are in practice mode. "
                    "Tap Quiz to get a question for this block, or ask about anything in the course. "
                    'If you wish to validate your practice averages then please click "Validate" to enter validate mode.'
                ),
                "source_blocks": [block.title],
            }
        )
    return transcript


def _append_message(course_state: dict, block: CourseBlock, role: str, kind: str, **data) -> dict:
    transcript = _ensure_block_transcript(course_state, block)
    message = {
        "id": _next_message_id(course_state),
        "created_at": timezone.now().isoformat(),
        "role": role,
        "kind": kind,
        **data,
    }
    transcript.append(message)
    return message


def _first_active_block(course: Course):
    blocks = list(course.blocks.all())
    if not blocks:
        return None
    for block in blocks:
        if block.is_available():
            return block
    return blocks[0]


def _preview_blocks(course: Course):
    return list(course.blocks.select_related("config").prefetch_related("learning_objectives").order_by("order", "created_at"))


def _flagged_question_ids(course_state: dict) -> set[int]:
    return {int(question_id) for question_id in course_state.get("flagged_question_ids", [])}


def _written_answer_draft(course_state: dict, question_id: int) -> dict:
    drafts = course_state.setdefault("written_answer_drafts", {})
    return drafts.setdefault(
        str(question_id),
        {
            "answer_text": "",
            "alignment_score": 0,
            "alignment_state": "drafting",
            "semantic_answer_text": "",
            "semantic_bucket": -1,
            "semantic_score": None,
            "semantic_aligned": False,
        },
    )


def _clear_written_answer_draft(course_state: dict, question_id: int) -> None:
    course_state.setdefault("written_answer_drafts", {}).pop(str(question_id), None)


def _question_prompt_message(course_state: dict, block: CourseBlock, question: QuestionBankItem) -> dict:
    options = question.all_answer_options()
    random.shuffle(options)
    message = _append_message(
        course_state,
        block,
        "assistant",
        "question",
        question_id=question.pk,
        question_type=question.question_type,
        question_type_label=question.question_type_label(),
        text=question.stem,
        options=options,
        block_label=block.title,
        learning_objective_id=question.learning_objective_id,
        learning_objective=(question.learning_objective.text if question.learning_objective else "General course understanding"),
        further_study_questions=further_study_questions_for_question(question),
        is_numerical=question.is_numeric(),
        is_coding_question=question.is_coding_question,
        coding_language=question.coding_language,
        coding_question_kind=question.coding_question_kind,
        code_snippet=question.code_snippet,
        answered=False,
        flagged=False,
    )
    if question.is_written_answer():
        draft = _written_answer_draft(course_state, question.pk)
        message.update(
            {
                "draft_answer": draft.get("answer_text", ""),
                "alignment_score": draft.get("alignment_score", 0),
                "alignment_state": draft.get("alignment_state", "drafting"),
                "submitted_text": "",
                "model_answer_revealed": False,
                "model_answer": "",
            }
        )
    return message


def _move_pending_question_message_to_bottom(course_state: dict, block: CourseBlock, question: QuestionBankItem) -> bool:
    transcript = _ensure_block_transcript(course_state, block)
    for index in range(len(transcript) - 1, -1, -1):
        message = transcript[index]
        if (
            message.get("kind") == "question"
            and message.get("question_id") == question.pk
            and not message.get("answered")
            and not message.get("flagged")
        ):
            if index != len(transcript) - 1:
                transcript.append(transcript.pop(index))
            return True
    return False


def _normalize_requested_question_type(question_type: str | None) -> str | None:
    if question_type in {QuestionBankItem.QuestionType.MCQ, QuestionBankItem.QuestionType.NUM, QuestionBankItem.QuestionType.MAQ, QuestionBankItem.QuestionType.WAQ}:
        return question_type
    return None


def _block_completed_count(course_state: dict, block: CourseBlock) -> int:
    return len([event for event in course_state.get("completed_events", []) if int(event["block_id"]) == block.pk])


def _advanced_question_start_percent(block: CourseBlock) -> int:
    return max(0, min(100, int(block.question_advanced_question_start_percent or 0)))


def _advanced_question_types_unlocked(course: Course, block: CourseBlock, course_state: dict) -> bool:
    threshold_percent = _advanced_question_start_percent(block)
    if threshold_percent <= 0:
        return True
    target_question_count = max(1, block.preview_target_question_count)
    completed_count = _block_completed_count(course_state, block)
    return completed_count * 100 >= threshold_percent * target_question_count


def _effective_preview_question_type(
    course: Course,
    block: CourseBlock,
    course_state: dict,
    requested_question_type: str | None,
    *,
    force_requested_type: bool = False,
) -> str | None:
    normalized_type = _normalize_requested_question_type(requested_question_type)
    if force_requested_type:
        return normalized_type
    if _advanced_question_types_unlocked(course, block, course_state):
        return normalized_type
    if normalized_type in ADVANCED_QUESTION_TYPES or normalized_type is None:
        return QuestionBankItem.QuestionType.MCQ
    return normalized_type


def _course_question_queryset(course: Course, block: CourseBlock, course_state: dict, question_type: str | None = None):
    queryset = course.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        block=block,
        block__available_from__lte=timezone.localdate(),
    ).exclude(pk__in=_flagged_question_ids(course_state))
    preferred_coding_language = preferred_coding_language_for_block(block)
    if preferred_coding_language:
        queryset = queryset.filter(Q(is_coding_question=False) | Q(coding_language=preferred_coding_language))
    normalized_type = _normalize_requested_question_type(question_type)
    if normalized_type:
        queryset = queryset.filter(question_type=normalized_type)
    return queryset.select_related("learning_objective", "block")


def _block_question_history(question_queryset, course_state: dict, block: CourseBlock):
    objective_presented_counts: dict[int, int] = defaultdict(int)
    chunk_presented_counts: dict[int, int] = defaultdict(int)
    questions = list(question_queryset)
    for question in questions:
        state = _question_state(course_state, question.pk)
        times_presented = int(state.get("times_presented", 0) or 0)
        if times_presented <= 0:
            continue
        if question.learning_objective_id is not None:
            objective_presented_counts[int(question.learning_objective_id)] += times_presented
        if question.source_chunk_id is not None:
            chunk_presented_counts[int(question.source_chunk_id)] += times_presented

    recent_events = [
        event
        for event in reversed(course_state.get("completed_events", []))
        if int(event.get("block_id") or 0) == block.pk
    ]
    recent_objective_ids = {
        int(event["learning_objective_id"])
        for event in recent_events[:3]
        if event.get("learning_objective_id") is not None
    }
    recent_question_ids = {
        int(event["question_id"])
        for event in recent_events[:3]
        if event.get("question_id") is not None
    }
    covered_objective_ids = _covered_objective_ids(course_state, block)
    return questions, objective_presented_counts, chunk_presented_counts, recent_objective_ids, recent_question_ids, covered_objective_ids


def _pick_unseen_question(course: Course, block: CourseBlock, course_state: dict, question_type: str | None = None):
    queryset = _course_question_queryset(course, block, course_state, question_type).annotate(cohort_seen_count=Count("attempt_questions"))
    (
        questions,
        objective_presented_counts,
        chunk_presented_counts,
        recent_objective_ids,
        recent_question_ids,
        covered_objective_ids,
    ) = _block_question_history(queryset, course_state, block)
    preferred_languages_by_block: dict[int, str] = {}
    candidates = []
    for question in questions:
        if _question_state(course_state, question.pk)["times_presented"] != 0:
            continue
        preferred_coding_language = ""
        if question.is_coding_question:
            preferred_coding_language = preferred_languages_by_block.get(question.block_id, "")
            if not preferred_coding_language:
                preferred_coding_language = preferred_coding_language_for_block(question.block)
                preferred_languages_by_block[question.block_id] = preferred_coding_language
        if not coding_question_matches_expected_language(question, preferred_coding_language):
            continue
        if question_quality_sort_key(question)[0]:
            continue
        candidates.append(
            (
                0 if question.learning_objective_id not in covered_objective_ids else 1,
                objective_presented_counts.get(int(question.learning_objective_id or 0), 0),
                chunk_presented_counts.get(int(question.source_chunk_id or 0), 0),
                1 if question.learning_objective_id in recent_objective_ids else 0,
                1 if question.pk in recent_question_ids else 0,
                *question_quality_sort_key(question),
                *coding_question_quality_sort_key(question),
                question.cohort_seen_count,
                question.created_at,
                question.pk,
                question,
            )
        )
    candidates.sort()
    return candidates[0][-1] if candidates else None


def _pick_retry_question(course: Course, block: CourseBlock, course_state: dict, question_type: str | None = None):
    completion_sequence = course_state.get("completion_sequence", 0)
    queryset = _course_question_queryset(course, block, course_state, question_type)
    (
        questions,
        objective_presented_counts,
        chunk_presented_counts,
        recent_objective_ids,
        recent_question_ids,
        _covered_objective_ids,
    ) = _block_question_history(queryset, course_state, block)
    preferred_languages_by_block: dict[int, str] = {}
    candidates = []
    for question in questions:
        state = _question_state(course_state, question.pk)
        if state["times_correct"] > 0 or state["retired_at"] or state["times_incorrect"] == 0:
            continue
        preferred_coding_language = ""
        if question.is_coding_question:
            preferred_coding_language = preferred_languages_by_block.get(question.block_id, "")
            if not preferred_coding_language:
                preferred_coding_language = preferred_coding_language_for_block(question.block)
                preferred_languages_by_block[question.block_id] = preferred_coding_language
        if not coding_question_matches_expected_language(question, preferred_coding_language):
            continue
        if question_quality_sort_key(question)[0]:
            continue
        if completion_sequence - state["last_presented_sequence"] < PREVIEW_RETRY_COMPLETION_GAP:
            continue
        candidates.append(
            (
                1 if question.learning_objective_id in recent_objective_ids else 0,
                1 if question.pk in recent_question_ids else 0,
                objective_presented_counts.get(int(question.learning_objective_id or 0), 0),
                chunk_presented_counts.get(int(question.source_chunk_id or 0), 0),
                *question_quality_sort_key(question),
                *coding_question_quality_sort_key(question),
                state["last_presented_sequence"],
                state["times_incorrect"],
                question.pk,
                question,
            )
        )
    candidates.sort()
    return candidates[0][-1] if candidates else None


def _ordered_unmet_objective_ids(course_state: dict, block: CourseBlock) -> list[int]:
    covered_objective_ids = _covered_objective_ids(course_state, block)
    unmet_objective_ids = [
        objective.pk
        for objective in block.learning_objectives.all()
        if objective.pk not in covered_objective_ids
    ]
    return unmet_objective_ids


def _generation_objective_ids_for_block(course_state: dict, block: CourseBlock) -> list[int]:
    unmet_objective_ids = _ordered_unmet_objective_ids(course_state, block)
    if unmet_objective_ids:
        return unmet_objective_ids

    objective_ids = [objective.pk for objective in block.learning_objectives.all()]
    random.shuffle(objective_ids)
    return objective_ids


def _pending_question(course: Course, block: CourseBlock, course_state: dict):
    pending_question_id = course_state.setdefault("pending_questions", {}).get(str(block.pk))
    if not pending_question_id:
        return None
    return course.question_bank_items.filter(
        pk=pending_question_id,
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
    ).select_related("learning_objective", "block", "source_chunk").first()


def _objective_for_block(block: CourseBlock, learning_objective_id: int | None):
    if not learning_objective_id:
        return None
    return block.learning_objectives.filter(pk=learning_objective_id).first()


def _ensure_question_for_block(
    course: Course,
    block: CourseBlock,
    course_state: dict,
    requested_question_type: str | None = None,
    *,
    preferred_objective_id: int | None = None,
    force_new: bool = False,
):
    effective_type = _effective_preview_question_type(
        course,
        block,
        course_state,
        requested_question_type,
        force_requested_type=force_new and preferred_objective_id is not None,
    )
    preferred_objective = _objective_for_block(block, preferred_objective_id)
    if preferred_objective_id and preferred_objective is None:
        raise ValueError("Choose a learning objective from this block.")
    pending_question_id = course_state.setdefault("pending_questions", {}).get(str(block.pk))
    if force_new and pending_question_id:
        question = course.question_bank_items.filter(
            pk=pending_question_id,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
        ).first()
        if question is not None and question.pk not in _flagged_question_ids(course_state):
            raise ValueError("Answer or flag the current question before generating a fresh question for a learning objective.")
    if pending_question_id:
        question = course.question_bank_items.filter(
            pk=pending_question_id,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
        ).select_related("learning_objective", "block").first()
        if question is not None and question.pk not in _flagged_question_ids(course_state):
            return question, False

    question = None
    if not force_new:
        question = _pick_unseen_question(course, block, course_state, effective_type)
        if question is None:
            question = _pick_retry_question(course, block, course_state, effective_type)
    if question is None:
        question, _ = generate_question_pair_for_block(
            block,
            preferred_objective_ids=[preferred_objective.pk] if preferred_objective is not None else _generation_objective_ids_for_block(course_state, block),
            strict_preferred_objectives=preferred_objective is not None,
            question_type=effective_type,
            raise_generation_errors=True,
        )
    return question, True


def _mark_question_presented(course_state: dict, block: CourseBlock, question: QuestionBankItem):
    state = _question_state(course_state, question.pk)
    state["times_presented"] += 1
    state["last_presented_sequence"] = course_state.get("completion_sequence", 0)
    course_state.setdefault("pending_questions", {})[str(block.pk)] = question.pk
    return state


def request_preview_quiz(
    request,
    course: Course,
    block: CourseBlock,
    requested_question_type: str | None = None,
    *,
    preferred_objective_id: int | None = None,
    force_new: bool = False,
) -> dict:
    course_state = _course_state(request, course)
    _ensure_block_transcript(course_state, block)
    if not block.is_available():
        _append_message(
            course_state,
            block,
            "assistant",
            "text",
            text=f"{block.title} becomes available on {block.available_from:%d %b %Y}.",
            source_blocks=[block.title],
        )
        request.session.modified = True
        return serialize_preview_state(request, course, active_block_id=block.pk)

    try:
        question, is_new_request = _ensure_question_for_block(
            course,
            block,
            course_state,
            requested_question_type,
            preferred_objective_id=preferred_objective_id,
            force_new=force_new,
        )
    except QuestionGenerationError as exc:
        _append_message(
            course_state,
            block,
            "assistant",
            "text",
            text=str(exc),
            source_blocks=[block.title],
        )
        request.session.modified = True
        return serialize_preview_state(request, course, active_block_id=block.pk)
    if question is None:
        _append_message(
            course_state,
            block,
            "assistant",
            "text",
            text="I couldn't build a suitable question for this block yet. Add more notes or learning objectives and try again.",
            source_blocks=[block.title],
        )
        request.session.modified = True
        return serialize_preview_state(request, course, active_block_id=block.pk)

    if is_new_request:
        _mark_question_presented(course_state, block, question)
        _question_prompt_message(course_state, block, question)
    else:
        if not _move_pending_question_message_to_bottom(course_state, block, question):
            _question_prompt_message(course_state, block, question)

    request.session.modified = True
    return serialize_preview_state(request, course, active_block_id=block.pk)


def save_preview_objective_guardrail(
    request,
    course: Course,
    block: CourseBlock,
    learning_objective_id: int,
    instruction: str,
) -> dict:
    objective = _objective_for_block(block, learning_objective_id)
    if objective is None:
        raise ValueError("Choose a learning objective from this block.")

    cleaned_instruction = sanitize_assistant_guidance(instruction)
    if not cleaned_instruction:
        raise ValueError("Enter a guardrail first.")

    updated_guidance = merge_assistant_guidance(objective.assistant_guidance, cleaned_instruction)
    if updated_guidance != objective.assistant_guidance:
        objective.assistant_guidance = updated_guidance
        objective.save(update_fields=["assistant_guidance", "updated_at"])

    course_state = _course_state(request, course)
    _append_message(
        course_state,
        block,
        "assistant",
        "text",
        text=(
            f"Guardrail saved for {objective.code}. "
            "Future questions for this learning objective will follow it here and in the student app."
        ),
        source_blocks=[block.title],
    )
    request.session.modified = True
    return serialize_preview_state(request, course, active_block_id=block.pk)


def draft_preview_written_answer(request, course: Course, block: CourseBlock, question_id: int, answer_text: str) -> dict:
    course_state = _course_state(request, course)
    question = _pending_question(course, block, course_state)
    normalized_answer = _normalize_written_answer_text(answer_text)
    if question is None or question.pk != question_id or not question.is_written_answer():
        return {
            "question_id": question_id,
            "answer_text": normalized_answer,
            "alignment_score": 0,
            "alignment_state": "drafting",
        }

    draft = _written_answer_draft(course_state, question.pk)
    alignment = _draft_written_answer_alignment(question, block, normalized_answer, draft)
    draft.update(
        {
            "answer_text": alignment["answer_text"],
            "alignment_score": alignment["alignment_score"],
            "alignment_state": alignment["alignment_state"],
        }
    )
    request.session.modified = True
    return {
        "question_id": question.pk,
        "answer_text": alignment["answer_text"],
        "alignment_score": alignment["alignment_score"],
        "alignment_state": alignment["alignment_state"],
    }


def _normalize_submitted_answers(selected_answers) -> list[str]:
    if isinstance(selected_answers, str):
        selected_answers = [selected_answers]
    normalized = []
    for answer in selected_answers or []:
        cleaned = str(answer).strip()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _normalize_written_answer_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _written_answer_state(score_ratio: float, answer_text: str) -> str:
    if len(re.findall(r"[a-z0-9]+", answer_text.lower())) < PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS:
        return "drafting"
    if score_ratio >= WAQ_ALIGNMENT_THRESHOLD:
        return "aligned"
    if score_ratio >= PREVIEW_WAQ_CLOSE_THRESHOLD:
        return "close"
    return "drafting"


def _rubric_item_match_ratio(answer_text: str, answer_keywords: set[str], rubric_item: str) -> float:
    normalized_item = _normalize_written_answer_text(rubric_item).lower()
    if not normalized_item:
        return 0.0
    if normalized_item in answer_text:
        return 1.0
    item_keywords = _keyword_set(normalized_item)
    if not item_keywords:
        return 0.0
    matched = len(item_keywords & answer_keywords)
    return matched / len(item_keywords)


def _written_answer_alignment(question: QuestionBankItem, answer_text: str) -> dict:
    normalized_answer = _normalize_written_answer_text(answer_text)
    if not normalized_answer:
        return {
            "answer_text": "",
            "alignment_ratio": 0.0,
            "alignment_score": 0,
            "alignment_state": "drafting",
            "matched_keywords": [],
            "missing_keywords": list(question.written_answer_keywords or []),
        }

    answer_text_lower = normalized_answer.lower()
    answer_keywords = _keyword_set(normalized_answer)
    rubric_items = list(question.written_answer_keywords or [question.correct_answer])
    rubric_scores: list[tuple[str, float]] = []
    for rubric_item in rubric_items:
        rubric_scores.append((rubric_item, _rubric_item_match_ratio(answer_text_lower, answer_keywords, rubric_item)))

    keyword_score = (
        sum(score for _, score in rubric_scores) / len(rubric_scores)
        if rubric_scores
        else 0.0
    )
    correct_answer_keywords = _keyword_set(question.correct_answer)
    correct_answer_score = (
        len(correct_answer_keywords & answer_keywords) / len(correct_answer_keywords)
        if correct_answer_keywords
        else 0.0
    )
    substantive_score = min(1.0, len(answer_keywords) / PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS) if answer_keywords else 0.0
    alignment_ratio = min(1.0, (keyword_score * 0.68) + (correct_answer_score * 0.22) + (substantive_score * 0.10))
    if keyword_score >= 0.66 and correct_answer_score >= 0.6:
        alignment_ratio = max(alignment_ratio, WAQ_ALIGNMENT_THRESHOLD + 0.03)
    matched_keywords = [item for item, score in rubric_scores if score >= 0.74]
    missing_keywords = [item for item, score in rubric_scores if score < 0.74]
    return {
        "answer_text": normalized_answer,
        "alignment_ratio": alignment_ratio,
        "alignment_score": int(round(alignment_ratio * 100)),
        "alignment_state": _written_answer_state(alignment_ratio, normalized_answer),
        "matched_keywords": matched_keywords,
        "missing_keywords": missing_keywords,
    }


def _clear_written_answer_semantic_cache(draft: dict) -> None:
    draft["semantic_answer_text"] = ""
    draft["semantic_bucket"] = -1
    draft["semantic_score"] = None
    draft["semantic_aligned"] = False


def _merged_written_answer_alignment(local_alignment: dict, semantic_score: float | None, semantic_aligned: bool) -> dict:
    if semantic_score is None:
        return local_alignment
    merged_ratio = max(local_alignment["alignment_ratio"], max(0.0, min(1.0, semantic_score)))
    if semantic_aligned:
        merged_ratio = max(merged_ratio, WAQ_ALIGNMENT_THRESHOLD + 0.03)
    return {
        **local_alignment,
        "alignment_ratio": merged_ratio,
        "alignment_score": int(round(merged_ratio * 100)),
        "alignment_state": _written_answer_state(merged_ratio, local_alignment["answer_text"]),
    }


def _parse_json_object(raw_output: str) -> dict:
    normalized_output = (raw_output or "").strip()
    if not normalized_output:
        raise ValueError("OpenAI returned an empty JSON payload.")
    try:
        return json.loads(normalized_output)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", normalized_output, re.DOTALL)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    object_match = re.search(r"\{.*\}", normalized_output, re.DOTALL)
    if object_match:
        return json.loads(object_match.group(0))

    raise ValueError("OpenAI did not return parseable JSON.")


def _openai_written_answer_grade(question: QuestionBankItem, block: CourseBlock, answer_text: str, *, draft_mode: bool = False) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    learning_objective = question.learning_objective.text if question.learning_objective else "General course understanding"
    source_excerpt = (question.source_chunk.text[:700].strip() if question.source_chunk_id and question.source_chunk else "") or block.summary.strip()
    prompt = f"""
{"Assess this student's in-progress written-answer draft" if draft_mode else "Grade this student's written-answer response"} and return strict JSON.

Rules:
- return only valid JSON with keys: aligned, score, feedback
- aligned must be true only if the student's answer captures the essential meaning of the model answer and rubric
- score must be a number between 0 and 1
- feedback must be one short sentence under 18 words
- ignore minor spelling and grammar issues
- do not mention "the content", "materials", "text", or "passage"
- {"treat this as a live draft: score what is written so far, even if incomplete" if draft_mode else "judge the answer as a final submission"}
- {"aligned should mean the student could reasonably submit now" if draft_mode else "aligned should mean the answer is correct overall"}

Question:
{question.stem}

Learning objective:
{learning_objective}

Canonical answer:
{question.correct_answer}

Hidden rubric:
{", ".join(question.written_answer_keywords or [question.correct_answer])}

Block context:
{source_excerpt}

Student answer:
{answer_text}
""".strip()
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Return only valid JSON."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    )
    payload = _parse_json_object(getattr(response, "output_text", ""))
    score = max(0.0, min(1.0, float(payload.get("score", 0.0))))
    feedback = _normalize_written_answer_text(str(payload.get("feedback", "")))
    return {
        "aligned": bool(payload.get("aligned")),
        "score": score,
        "feedback": feedback,
    }


def _draft_written_answer_alignment(question: QuestionBankItem, block: CourseBlock, answer_text: str, draft: dict) -> dict:
    local_alignment = _written_answer_alignment(question, answer_text)
    normalized_answer = local_alignment["answer_text"]
    if (
        not settings.OPENAI_API_KEY
        or len(normalized_answer) < PREVIEW_WAQ_OPENAI_DRAFT_MIN_CHARS
        or len(re.findall(r"[a-z0-9]+", normalized_answer.lower())) < PREVIEW_WAQ_MIN_SUBSTANTIVE_WORDS
    ):
        _clear_written_answer_semantic_cache(draft)
        return local_alignment

    current_bucket = len(normalized_answer) // PREVIEW_WAQ_OPENAI_CHECK_INTERVAL
    cached_answer = str(draft.get("semantic_answer_text", ""))
    cached_bucket = int(draft.get("semantic_bucket", -1))
    cached_score = draft.get("semantic_score")
    cached_aligned = bool(draft.get("semantic_aligned"))

    if (
        cached_score is not None
        and cached_bucket == current_bucket
        and cached_answer
        and normalized_answer.startswith(cached_answer)
    ):
        return _merged_written_answer_alignment(local_alignment, float(cached_score), cached_aligned)

    try:
        judged = _openai_written_answer_grade(question, block, normalized_answer, draft_mode=True)
    except Exception:
        _clear_written_answer_semantic_cache(draft)
        return local_alignment

    draft.update(
        {
            "semantic_answer_text": normalized_answer,
            "semantic_bucket": current_bucket,
            "semantic_score": judged["score"],
            "semantic_aligned": bool(judged["aligned"]),
        }
    )
    return _merged_written_answer_alignment(local_alignment, judged["score"], bool(judged["aligned"]))


def _fallback_written_answer_feedback(question: QuestionBankItem, alignment: dict) -> str:
    explanation = normalize_explanation_text(question.explanation)
    if alignment["alignment_ratio"] >= WAQ_ALIGNMENT_THRESHOLD:
        if explanation:
            return f"Correct. {explanation}"
        return "Correct."

    if alignment["missing_keywords"]:
        focus_points = ", ".join(alignment["missing_keywords"][:2])
        return f"Not aligned yet. Include {focus_points}. Model answer: {question.correct_answer}"
    return f"Not aligned yet. Be more specific. Model answer: {question.correct_answer}"


def _grade_written_answer_response(question: QuestionBankItem, block: CourseBlock, answer_text: str) -> tuple[bool, dict, str]:
    fallback_alignment = _written_answer_alignment(question, answer_text)
    if settings.OPENAI_API_KEY:
        try:
            judged = _openai_written_answer_grade(question, block, answer_text)
            judged_score = judged["score"]
            judged_alignment = {
                **fallback_alignment,
                "alignment_ratio": judged_score,
                "alignment_score": int(round(judged_score * 100)),
                "alignment_state": _written_answer_state(judged_score, fallback_alignment["answer_text"]),
            }
            is_correct = bool(judged["aligned"]) or judged_score >= WAQ_ALIGNMENT_THRESHOLD
            if is_correct:
                explanation = normalize_explanation_text(question.explanation)
                feedback = f"Correct. {explanation}" if explanation else "Correct."
            else:
                reason = judged["feedback"] or "Try to be more specific."
                feedback = f"Not aligned yet. {reason} Model answer: {question.correct_answer}"
            return is_correct, judged_alignment, feedback
        except Exception:
            pass

    is_correct = fallback_alignment["alignment_ratio"] >= WAQ_ALIGNMENT_THRESHOLD
    return is_correct, fallback_alignment, _fallback_written_answer_feedback(question, fallback_alignment)


def _grade_question_response(question: QuestionBankItem, selected_answers) -> tuple[bool, list[str], list[str]]:
    submitted_answers = _normalize_submitted_answers(selected_answers)
    correct_answers = question.correct_answers()
    missing_answers = [answer for answer in correct_answers if answer not in submitted_answers]
    extra_answers = [answer for answer in submitted_answers if answer not in correct_answers]
    return not missing_answers and not extra_answers, missing_answers, extra_answers


def _feedback_text(question: QuestionBankItem, selected_answers, is_correct: bool, missing_answers: list[str], extra_answers: list[str]) -> str:
    explanation = (
        normalize_numeric_explanation_text(question.explanation)
        if question.is_numeric()
        else normalize_explanation_text(question.explanation)
    )
    if question.is_multiple_answer():
        if is_correct:
            return "Correct."
        parts = ["Not quite."]
        if missing_answers:
            parts.append(f"Missed: {', '.join(missing_answers)}.")
        if extra_answers:
            parts.append(f"Extra: {', '.join(extra_answers)}.")
        return " ".join(parts)
    if is_correct:
        if explanation:
            return f"Correct. {explanation}"
        return "Correct. Nice work."
    if explanation:
        return explanation
    return "Not quite."


def _feedback_with_keep_going_line(course_state: dict, feedback_text: str) -> str:
    completion_count = len(course_state.get("completed_events", []))
    keep_going_line = PREVIEW_KEEP_GOING_LINES[completion_count % len(PREVIEW_KEEP_GOING_LINES)]
    return f"{feedback_text}\n\n{keep_going_line}"


def _should_add_keep_going_line(transcript: list[dict]) -> bool:
    now = timezone.now()
    for message in reversed(transcript):
        timestamp = parse_datetime(str(message.get("created_at", "")))
        if timestamp is None:
            continue
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp, timezone.get_current_timezone())
        return now - timestamp > PREVIEW_KEEP_GOING_DELAY
    return False


def submit_preview_answer(request, course: Course, block: CourseBlock, question_id: int, selected_answers=None, *, answer_text: str = "") -> dict:
    course_state = _course_state(request, course)
    pending_question_id = course_state.setdefault("pending_questions", {}).get(str(block.pk))
    question = course.question_bank_items.filter(
        pk=question_id,
        course=course,
        block=block,
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
    ).select_related("learning_objective", "block", "source_chunk").first()
    if question is None or pending_question_id != question_id:
        return serialize_preview_state(request, course, active_block_id=block.pk)

    transcript = _ensure_block_transcript(course_state, block)
    normalized_answers = _normalize_submitted_answers(selected_answers)
    normalized_answer_text = _normalize_written_answer_text(answer_text)
    written_alignment = None
    if question.is_written_answer():
        is_correct, written_alignment, feedback_text = _grade_written_answer_response(question, block, normalized_answer_text)
        answer_display_text = normalized_answer_text
    else:
        is_correct, missing_answers, extra_answers = _grade_question_response(question, normalized_answers)
        feedback_text = _feedback_text(question, normalized_answers, is_correct, missing_answers, extra_answers)
        answer_display_text = ", ".join(normalized_answers)
    include_keep_going_line = _should_add_keep_going_line(transcript)

    for message in reversed(transcript):
        if message.get("kind") == "question" and message.get("question_id") == question_id and not message.get("answered"):
            message["answered"] = True
            if question.is_written_answer():
                message["submitted_text"] = answer_display_text
                message["alignment_score"] = written_alignment["alignment_score"] if written_alignment else 0
                message["alignment_state"] = written_alignment["alignment_state"] if written_alignment else "drafting"
                message["model_answer_revealed"] = not is_correct
                message["model_answer"] = question.correct_answer if not is_correct else ""
                message["draft_answer"] = ""
            else:
                message["selected_answers"] = normalized_answers
                message["selected_answer"] = normalized_answers[0] if len(normalized_answers) == 1 else ""
                message["correct_answers"] = question.correct_answers()
            break

    _append_message(course_state, block, "user", "answer", text=answer_display_text)
    _append_message(
        course_state,
        block,
        "assistant",
        "feedback",
        text=_feedback_with_keep_going_line(course_state, feedback_text) if include_keep_going_line else feedback_text,
        correct=is_correct,
        source_blocks=[block.title],
    )

    course_state["completion_sequence"] = course_state.get("completion_sequence", 0) + 1
    state = _question_state(course_state, question_id)
    if is_correct:
        state["times_correct"] += 1
        state["retired_at"] = timezone.now().isoformat()
    else:
        state["times_incorrect"] += 1
    course_state.setdefault("completed_events", []).append(
        {
            "block_id": block.pk,
            "question_id": question.pk,
            "correct": is_correct,
            "answered_at": timezone.now().isoformat(),
            "learning_objective_id": question.learning_objective_id,
            "source_chunk_id": question.source_chunk_id,
            "question_type": question.question_type,
            "selected_answers": normalized_answers,
            "answer_text": answer_display_text,
            "feedback": feedback_text,
        }
    )
    course_state.setdefault("pending_questions", {})[str(block.pk)] = None
    _clear_written_answer_draft(course_state, question_id)

    request.session.modified = True
    return serialize_preview_state(request, course, active_block_id=block.pk)


def flag_preview_question(
    request,
    course: Course,
    block: CourseBlock,
    question_id: int,
    *,
    instruction: str = "",
    learning_objective_id: int | None = None,
) -> dict:
    course_state = _course_state(request, course)
    question = course.question_bank_items.filter(
        pk=question_id,
        course=course,
        block=block,
        bank_type=QuestionBankItem.BankType.PRACTICE,
    ).select_related("linked_question", "learning_objective").first()
    if question is None:
        return serialize_preview_state(request, course, active_block_id=block.pk)

    cleaned_instruction = sanitize_assistant_guidance(instruction)
    if cleaned_instruction:
        correction_objective = question.learning_objective
        if correction_objective is None:
            correction_objective = block.learning_objectives.filter(pk=learning_objective_id or 0).first()
        if correction_objective is None:
            raise ValueError("Choose a learning objective before saving a correction note for this question.")
        LearningObjectiveCorrection.objects.create(
            learning_objective=correction_objective,
            question=question,
            created_by=getattr(request, "user", None),
            instruction=cleaned_instruction,
            question_stem_snapshot=question.stem,
        )

    flagged_ids = course_state.setdefault("flagged_question_ids", [])
    for linked_question_id in filter(None, [question.pk, question.linked_question_id]):
        if str(linked_question_id) not in flagged_ids:
            flagged_ids.append(str(linked_question_id))

    transcript = _ensure_block_transcript(course_state, block)
    for message in reversed(transcript):
        if message.get("kind") == "question" and message.get("question_id") == question.pk:
            message["flagged"] = True
            message["answered"] = True
            break

    if course_state.setdefault("pending_questions", {}).get(str(block.pk)) == question.pk:
        course_state["pending_questions"][str(block.pk)] = None
    _clear_written_answer_draft(course_state, question.pk)

    _append_message(
        course_state,
        block,
        "assistant",
        "text",
        text=(
            f"Thanks. I saved that correction note against {question.learning_objective.code if question.learning_objective else correction_objective.code}, "
            "and I won't show this question or its linked validation variant again here."
            if cleaned_instruction
            else "Thanks. I won't show this question again here, and its linked validation question has been removed too."
        ),
        source_blocks=[block.title],
    )
    request.session.modified = True
    return serialize_preview_state(request, course, active_block_id=block.pk)


def _keyword_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 3
    }


def _is_inappropriate_chat_message(text: str) -> bool:
    normalized = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
    flagged_phrases = {
        " fuck ",
        " fucking ",
        " shit ",
        " bullshit ",
        " bitch ",
        " bastard ",
        " cunt ",
        " dick ",
        " prick ",
        " slut ",
        " whore ",
        " retard ",
        " kill yourself ",
        " kys ",
        " nazi ",
        " rape ",
        " raped ",
        " sexually explicit ",
        " porn ",
        " nigger ",
        " nigga ",
        " fag ",
        " faggot ",
        " stupid ",
        " moron ",
        " idiot ",
    }
    if any(phrase in normalized for phrase in flagged_phrases):
        return True

    targeted_harassment = [
        r"\byou(?:'re| are)?\s+(?:an?\s+)?(?:idiot|moron|stupid|pathetic|useless|disgusting)\b",
        r"\bgo\s+kill\s+yourself\b",
        r"\bi\s+hate\s+you\b",
    ]
    return any(re.search(pattern, normalized) for pattern in targeted_harassment)


def _block_source_documents(course: Course):
    documents = []
    for block in course.blocks.prefetch_related("learning_objectives", "assets", "content_chunks").all():
        extracted_parts = [block.summary.strip()] if block.summary.strip() else []
        extracted_parts.extend(objective.text.strip() for objective in block.learning_objectives.all() if objective.text.strip())
        extracted_parts.extend(asset.extracted_text[:240].strip() for asset in block.assets.all() if asset.extracted_text.strip())
        extracted_parts.extend(chunk.text[:240].strip() for chunk in block.content_chunks.all()[:2] if chunk.text.strip())
        combined = " ".join(part for part in extracted_parts if part)
        documents.append((block, combined))
    return documents


def _chat_source_snippets(course: Course):
    snippets = []
    for block in course.blocks.prefetch_related("learning_objectives", "assets", "content_chunks").all():
        if block.summary.strip():
            snippets.append({"block": block, "text": block.summary.strip(), "kind": "summary", "bias": 3})
        for objective in block.learning_objectives.all():
            if objective.text.strip():
                snippets.append({"block": block, "text": f"{objective.code}: {objective.text.strip()}", "kind": "objective", "bias": 2})
        for asset in list(block.assets.all())[:2]:
            excerpt = asset.extracted_text[:420].strip()
            if excerpt:
                snippets.append({"block": block, "text": excerpt, "kind": "notes", "bias": 1})
        for chunk in list(block.content_chunks.all())[:4]:
            excerpt = chunk.text[:420].strip()
            if excerpt:
                snippets.append({"block": block, "text": excerpt, "kind": "notes", "bias": 1})
    return snippets


def _retrieve_chat_snippets(course: Course, block: CourseBlock, question: str):
    question_keywords = _keyword_set(question)
    ranked = []
    for snippet in _chat_source_snippets(course):
        block_title_keywords = _keyword_set(snippet["block"].title)
        snippet_keywords = _keyword_set(f"{snippet['block'].title} {snippet['text']}")
        overlap = len(question_keywords & snippet_keywords)
        title_overlap = len(question_keywords & block_title_keywords)
        active_block_boost = 2 if snippet["block"].pk == block.pk else 0
        score = (overlap * 4) + (title_overlap * 2) + snippet["bias"] + active_block_boost
        ranked.append((score, overlap, snippet))

    ranked.sort(key=lambda item: (item[0], item[1], item[2]["block"].order), reverse=True)
    selected = [item[2] for item in ranked if item[0] > 0][:PREVIEW_CHAT_RETRIEVAL_LIMIT]
    if selected:
        return selected

    return [snippet for snippet in _chat_source_snippets(course) if snippet["block"].pk == block.pk][:PREVIEW_CHAT_RETRIEVAL_LIMIT]


def _recent_chat_context(course_state: dict, block: CourseBlock) -> str:
    transcript = _ensure_block_transcript(course_state, block)
    lines = []
    for message in transcript[-PREVIEW_CHAT_HISTORY_LIMIT:]:
        if message.get("kind") == "loading":
            continue
        if message.get("kind") == "question":
            lines.append(f"assistant: {message.get('text', '')}")
            if message.get("selected_answer"):
                lines.append(f"user: {message['selected_answer']}")
            continue
        if message.get("kind") == "answer":
            lines.append(f"user: {message.get('text', '')}")
            continue
        role = message.get("role", "assistant")
        text = (message.get("text") or "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines[-PREVIEW_CHAT_HISTORY_LIMIT:])


def _openai_chat_reply(course_state: dict, course: Course, block: CourseBlock, question: str) -> tuple[str, list[str]]:
    snippets = _retrieve_chat_snippets(course, block, question)
    if not snippets:
        return _fallback_chat_reply(course, block, question)

    recent_chat = _recent_chat_context(course_state, block)
    source_block_titles = []
    for snippet in snippets:
        if snippet["block"].title not in source_block_titles:
            source_block_titles.append(snippet["block"].title)

    sources_text = "\n\n".join(
        f"[Block: {snippet['block'].title} | Type: {snippet['kind']}]\n{snippet['text']}"
        for snippet in snippets
    )
    teacher_guidance = build_chat_guidance_prompt(course, block, question)
    prompt = f"""
You are the student chat tutor for the course "{course.title}".

Answer the student's question clearly and directly.

Rules:
- use the supplied course notes as your primary grounding
- answer naturally, not like a search result
- do not say "the content", "the materials", "the text", or "the passage"
- if the notes are thin, you may add a brief standard definition to clarify a term, but keep it aligned with the notes
- if the answer is not supported well enough by the notes, say so plainly and be brief
- do not mention source block names in the body unless they help the explanation
- keep the answer concise and useful for a student
- use clean markdown when it helps readability, especially simple bullet lists or numbered steps
- when naming key ideas in a list, prefer markdown like **Idea:** short explanation
- wrap short code snippets, commands, literals, filenames, or syntax examples in single backticks
- use fenced code blocks for multi-line code examples

Current block:
{block.title}

Recent chat:
{recent_chat or "No recent chat."}

Relevant course notes:
{sources_text}

{teacher_guidance}

Student question:
{question}
""".strip()
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Answer clearly using the supplied notes."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    )
    answer = (getattr(response, "output_text", "") or "").strip()
    if not answer:
        return _fallback_chat_reply(course, block, question)

    source_blocks = source_block_titles if any(title != block.title for title in source_block_titles) else []
    return answer, source_blocks


def _fallback_chat_reply(course: Course, block: CourseBlock, question: str) -> tuple[str, list[str]]:
    question_keywords = _keyword_set(question)
    ranked = []
    for candidate_block, document in _block_source_documents(course):
        score = len(question_keywords & _keyword_set(f"{candidate_block.title} {document}"))
        ranked.append((score, candidate_block, document))
    ranked.sort(key=lambda item: (item[0], item[1].order), reverse=True)

    top_ranked = [item for item in ranked if item[0] > 0][:2]
    if not top_ranked:
        return (
            f"I can answer questions about what's covered in {course.title}. Try naming a topic, learning objective, or block.",
            [block.title],
        )

    primary_block = top_ranked[0][1]
    primary_document = top_ranked[0][2]
    primary_summary = primary_block.summary.strip() or primary_document[:220].strip()
    primary_objectives = list(primary_block.learning_objectives.values_list("text", flat=True)[:2])

    answer_parts = [f"{primary_block.title} is the most relevant block here."]
    if primary_summary:
        answer_parts.append(primary_summary)
    if primary_objectives:
        answer_parts.append(f"Key focus: {'; '.join(primary_objectives)}.")
    if len(top_ranked) > 1:
        answer_parts.append(f"This also connects to {top_ranked[1][1].title}.")

    source_blocks = [item[1].title for item in top_ranked]
    return " ".join(answer_parts), source_blocks


def send_preview_chat_message(request, course: Course, block: CourseBlock, question: str) -> dict:
    course_state = _course_state(request, course)
    pending_question = _pending_question(course, block, course_state)
    if pending_question is not None and pending_question.is_written_answer():
        _append_message(
            course_state,
            block,
            "assistant",
            "text",
            text="Finish the written answer before asking a related question.",
            source_blocks=[block.title],
        )
        request.session.modified = True
        return serialize_preview_state(request, course, active_block_id=block.pk)

    if len(question) > settings.CHAT_MAX_QUESTION_LENGTH:
        _append_message(
            course_state,
            block,
            "assistant",
            "text",
            text=f"Please keep your message under {settings.CHAT_MAX_QUESTION_LENGTH} characters.",
            source_blocks=[block.title],
        )
        request.session.modified = True
        return serialize_preview_state(request, course, active_block_id=block.pk)

    _append_message(course_state, block, "user", "text", text=question)
    if _is_inappropriate_chat_message(question):
        _append_message(
            course_state,
            block,
            "assistant",
            "text",
            text=PREVIEW_INAPPROPRIATE_MESSAGE_WARNING,
            source_blocks=[block.title],
        )
        request.session.modified = True
        return serialize_preview_state(request, course, active_block_id=block.pk)

    answer, source_blocks = _fallback_chat_reply(course, block, question)
    if settings.OPENAI_API_KEY:
        try:
            answer, source_blocks = _openai_chat_reply(course_state, course, block, question)
        except Exception:
            answer, source_blocks = _fallback_chat_reply(course, block, question)
    _append_message(
        course_state,
        block,
        "assistant",
        "text",
        text=answer,
        source_blocks=source_blocks,
        further_study_questions=further_study_questions_for_chat(
            question=question,
            answer=answer,
            block_title=block.title,
            objective_texts=list(block.learning_objectives.values_list("text", flat=True)[:3]),
        ),
    )
    request.session.modified = True
    return serialize_preview_state(request, course, active_block_id=block.pk)


def _block_metrics(course_state: dict, block: CourseBlock) -> dict:
    completed_events = [event for event in course_state.get("completed_events", []) if int(event["block_id"]) == block.pk]
    completed_count = _block_completed_count(course_state, block)
    correct_count = sum(1 for event in completed_events if event["correct"])
    incorrect_count = max(0, completed_count - correct_count)
    objective_ids = {
        int(event["learning_objective_id"])
        for event in completed_events
        if event["correct"] and event.get("learning_objective_id") is not None
    }
    total_objectives = block.learning_objectives.count()
    covered_objective_count = len(objective_ids)
    today = timezone.localdate()
    engagement_deadline = block.available_from + timedelta(days=PREVIEW_ENGAGEMENT_WINDOW_DAYS)
    on_time_count = sum(
        1
        for event in completed_events
        if block.available_from <= datetime.fromisoformat(event["answered_at"]).date() <= engagement_deadline
    )
    target_question_count = max(1, block.preview_target_question_count)

    mastery = round((correct_count * 100 / completed_count), 2) if completed_count else 0.0
    coverage = round((covered_objective_count * 100 / total_objectives), 2) if total_objectives else 0.0
    engagement = round(min(100, on_time_count * 100 / target_question_count), 2) if today >= block.available_from else 0.0
    target = round(min(100, completed_count * 100 / target_question_count), 2)
    overall = _weighted_practice_score(
        block.course,
        {
            "mastery": mastery,
            "coverage": coverage,
            "engagement": engagement,
            "target": target,
        },
    )
    advanced_question_start_percent = _advanced_question_start_percent(block)
    advanced_question_types_unlocked = _advanced_question_types_unlocked(block.course, block, course_state)
    return {
        "overall": overall,
        "mastery": mastery,
        "coverage": coverage,
        "engagement": engagement,
        "target": target,
        "completed_count": completed_count,
        "correct_count": correct_count,
        "incorrect_count": incorrect_count,
        "covered_objective_count": covered_objective_count,
        "total_objective_count": total_objectives,
        "on_time_count": on_time_count,
        "engagement_window_days": PREVIEW_ENGAGEMENT_WINDOW_DAYS,
        "target_question_count": target_question_count,
        "advanced_question_start_percent": advanced_question_start_percent,
        "advanced_question_types_unlocked": advanced_question_types_unlocked,
    }


def _practice_score_weights(course: Course) -> dict:
    total_weight = (
        course.config.mastery_weight
        + course.config.coverage_weight
        + course.config.engagement_weight
        + course.config.target_weight
    )
    return {
        "mastery": course.config.mastery_weight,
        "coverage": course.config.coverage_weight,
        "engagement": course.config.engagement_weight,
        "target": course.config.target_weight,
        "total": total_weight,
    }


def _weighted_practice_score(course: Course, metrics: dict) -> float:
    total_weight = _practice_score_weights(course)["total"]
    if total_weight <= 0:
        return 0.0
    weighted_total = (
        metrics["mastery"] * course.config.mastery_weight
        + metrics["coverage"] * course.config.coverage_weight
        + metrics["engagement"] * course.config.engagement_weight
        + metrics["target"] * course.config.target_weight
    )
    return round(weighted_total / total_weight, 2)


def _course_metrics(course: Course, serialized_blocks: list[dict]) -> dict:
    metric_blocks = [block for block in serialized_blocks if block.get("is_available")] or serialized_blocks
    weights = _practice_score_weights(course)
    if not metric_blocks:
        return {
            "mastery": 0.0,
            "coverage": 0.0,
            "engagement": 0.0,
            "target": 0.0,
            "overall": 0.0,
            "block_count": 0,
            "completed_count": 0,
            "correct_count": 0,
            "incorrect_count": 0,
            "covered_objective_count": 0,
            "total_objective_count": sum(block.get("learning_objective_count", 0) for block in serialized_blocks),
            "on_time_count": 0,
            "combined_target_question_count": 0,
            "engagement_window_days": PREVIEW_ENGAGEMENT_WINDOW_DAYS,
            "weights": weights,
        }

    block_count = len(metric_blocks)
    metrics = {
        "mastery": round(sum(block["metrics"]["mastery"] for block in metric_blocks) / block_count, 2),
        "coverage": round(sum(block["metrics"]["coverage"] for block in metric_blocks) / block_count, 2),
        "engagement": round(sum(block["metrics"]["engagement"] for block in metric_blocks) / block_count, 2),
        "target": round(sum(block["metrics"]["target"] for block in metric_blocks) / block_count, 2),
    }
    return {
        **metrics,
        "overall": _weighted_practice_score(course, metrics),
        "block_count": block_count,
        "completed_count": sum(block["metrics"]["completed_count"] for block in metric_blocks),
        "correct_count": sum(block["metrics"]["correct_count"] for block in metric_blocks),
        "incorrect_count": sum(block["metrics"]["incorrect_count"] for block in metric_blocks),
        "covered_objective_count": sum(block["metrics"]["covered_objective_count"] for block in serialized_blocks),
        "total_objective_count": sum(block.get("learning_objective_count", 0) for block in serialized_blocks),
        "on_time_count": sum(block["metrics"]["on_time_count"] for block in metric_blocks),
        "combined_target_question_count": sum(block["metrics"]["target_question_count"] for block in metric_blocks),
        "engagement_window_days": PREVIEW_ENGAGEMENT_WINDOW_DAYS,
        "weights": weights,
    }


def _covered_objective_ids(course_state: dict, block: CourseBlock) -> set[int]:
    return {
        int(event["learning_objective_id"])
        for event in course_state.get("completed_events", [])
        if int(event["block_id"]) == block.pk and event["correct"] and event.get("learning_objective_id") is not None
    }


def _serialized_transcript(course_state: dict, transcript: list[dict]) -> list[dict]:
    question_ids = {
        int(message["question_id"])
        for message in transcript
        if message.get("kind") == "question" and message.get("question_id")
    }
    questions_by_id = {
        question.pk: question
        for question in QuestionBankItem.objects.filter(pk__in=question_ids).select_related("learning_objective")
    }
    serialized_messages = []
    for message in transcript:
        message_payload = dict(message)
        if message_payload.get("kind") == "question":
            question = questions_by_id.get(int(message_payload.get("question_id") or 0))
            if question is not None:
                message_payload.setdefault("further_study_questions", further_study_questions_for_question(question))
                message_payload.setdefault("is_coding_question", question.is_coding_question)
                message_payload.setdefault("coding_language", question.coding_language)
                message_payload.setdefault("coding_question_kind", question.coding_question_kind)
                message_payload.setdefault("code_snippet", question.code_snippet)
        if message_payload.get("kind") == "question" and message_payload.get("question_type") == QuestionBankItem.QuestionType.WAQ:
            if not message_payload.get("answered"):
                draft = _written_answer_draft(course_state, int(message_payload["question_id"]))
                message_payload.setdefault("draft_answer", draft.get("answer_text", ""))
                message_payload.setdefault("alignment_score", draft.get("alignment_score", 0))
                message_payload.setdefault("alignment_state", draft.get("alignment_state", "drafting"))
            message_payload.setdefault("submitted_text", "")
            message_payload.setdefault("model_answer_revealed", False)
            message_payload.setdefault("model_answer", "")
        serialized_messages.append(message_payload)
    return serialized_messages


def serialize_preview_state(request, course: Course, *, active_block_id=None) -> dict:
    course_state = _course_state(request, course)
    blocks = _preview_blocks(course)
    active_block_id = active_block_id or (_first_active_block(course).pk if blocks else None)
    serialized_blocks = []
    pending_questions = course_state.get("pending_questions", {})
    for block in blocks:
        transcript = _ensure_block_transcript(course_state, block)
        objectives = list(block.learning_objectives.all())
        covered_objective_ids = _covered_objective_ids(course_state, block)
        serialized_blocks.append(
            {
                "id": block.pk,
                "title": block.title,
                "summary": block.summary or "No summary yet.",
                "learning_objectives": [
                    {
                        "id": objective.pk,
                        "code": objective.code,
                        "text": objective.text,
                        "covered": objective.pk in covered_objective_ids,
                        "assistant_guidance": sanitize_assistant_guidance(objective.assistant_guidance),
                        "has_guardrail": bool(sanitize_assistant_guidance(objective.assistant_guidance)),
                    }
                    for objective in objectives
                ],
                "available_from": block.available_from.isoformat(),
                "available_from_label": f"{block.available_from.day} {block.available_from:%b %Y}",
                "is_available": block.is_available(),
                "learning_objective_count": len(objectives),
                "target_question_count": block.preview_target_question_count,
                "has_pending_question": bool(pending_questions.get(str(block.pk))),
                "transcript": _serialized_transcript(course_state, transcript),
                "metrics": _block_metrics(course_state, block),
            }
        )
    request.session.modified = True
    return {
        "course": {
            "id": course.pk,
            "title": course.title,
            "summary": course.summary,
            "metrics": _course_metrics(course, serialized_blocks),
        },
        "active_block_id": active_block_id,
        "blocks": serialized_blocks,
    }
