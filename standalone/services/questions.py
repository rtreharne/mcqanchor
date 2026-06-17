import hashlib
import json
import random
import re
from collections import defaultdict

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from openai import OpenAI

from standalone.models import ContentChunk, Course, CourseBlock, LearningObjective, QuestionBankItem


OBJECTIVE_MATCH_STOPWORDS = {
    "about",
    "across",
    "between",
    "compare",
    "describe",
    "discuss",
    "explain",
    "identify",
    "into",
    "into",
    "using",
    "understand",
    "with",
    "from",
    "that",
    "this",
    "their",
    "there",
    "which",
}

QUESTION_TYPE_GENERATION_PRIORITY = {
    QuestionBankItem.QuestionType.MAQ: 0,
    QuestionBankItem.QuestionType.WAQ: 1,
}

WAQ_FALLBACK_STEM_TEMPLATES = (
    "How would you explain {topic}?",
    "Why does {topic} matter here?",
    "What is the role of {topic}?",
    "What does {topic} help to explain?",
)

FURTHER_STUDY_QUESTION_COUNT = 3
STUDY_FOCUS_ACTION_VERBS = (
    "explain",
    "describe",
    "identify",
    "discuss",
    "outline",
    "summarise",
    "summarize",
    "compare",
    "show",
    "state",
    "interpret",
    "analyse",
    "analyze",
    "evaluate",
    "explore",
    "define",
    "apply",
    "relate",
    "connect",
    "infer",
)


def _keyword_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) >= 4 and token not in OBJECTIVE_MATCH_STOPWORDS
    }


def normalize_explanation_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""

    replacements = [
        (r"\bapproved course materials\b", "this block"),
        (r"\bapproved course material\b", "this block"),
        (r"\bcourse materials\b", "this block"),
        (r"\bcourse material\b", "this block"),
        (r"\bapproved materials\b", "this block"),
        (r"\bapproved material\b", "this block"),
        (r"\bthe materials\b", "this block"),
        (r"\bthe material\b", "this block"),
        (r"\bthe presented content\b", "this block"),
        (r"\bthe provided content\b", "this block"),
        (r"\bthe content\b", "this block"),
        (r"\bthe text\b", "this block"),
        (r"\bthis item is based directly on this block for the block\b", "This follows directly from this block"),
    ]
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\bfrom this block for the block\b", "from this block", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _normalize_answer_list(items) -> list[str]:
    normalized = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        cleaned = str(item).strip()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _fallback_written_answer_keywords(*texts: str, limit: int = 6) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    ignored_starts = ("explain ", "describe ", "identify ", "discuss ", "outline ", "summarise ", "summarize ", "how ", "why ")

    for text in texts:
        for segment in re.split(r"[.;:!?]+", str(text or "")):
            cleaned = re.sub(r"\s+", " ", segment).strip(" -")
            lowered = cleaned.lower()
            if (
                cleaned
                and 2 <= len(cleaned.split()) <= 7
                and lowered not in seen
                and not lowered.startswith(ignored_starts)
            ):
                keywords.append(cleaned[:90])
                seen.add(lowered)
                if len(keywords) >= limit:
                    return keywords

        for token in sorted(_keyword_set(str(text or ""))):
            if token not in seen:
                keywords.append(token)
                seen.add(token)
                if len(keywords) >= limit:
                    return keywords

    return keywords or ["core idea"]


def _normalize_study_question(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip(" -")
    if not cleaned:
        return ""
    cleaned = cleaned.rstrip(".!")
    if not cleaned.endswith("?"):
        cleaned = f"{cleaned}?"
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _normalize_further_study_questions(items, *, limit: int = FURTHER_STUDY_QUESTION_COUNT) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return normalized
    for item in items:
        cleaned = _normalize_study_question(item)
        if len(cleaned) > 140:
            cleaned = f"{cleaned[:139].rstrip(' ?.!')}?"
        lowered = cleaned.lower()
        if cleaned and lowered not in seen:
            normalized.append(cleaned)
            seen.add(lowered)
        if len(normalized) >= limit:
            break
    return normalized


def _is_weird_study_question(text: str) -> bool:
    lowered = str(text or "").lower()
    action_verbs = "|".join(STUDY_FOCUS_ACTION_VERBS)
    weird_patterns = (
        rf"\b(?:{action_verbs})\s+(?:{action_verbs})\b",
        rf"\b(?:example of|thinking about|with)\s+(?:{action_verbs})\b",
        r"\bwith\s+it\s+(?:allows|shows|means|helps?)\b",
        r"\bin your own\?$",
        r"\b[a-z]\?$",
    )
    return any(re.search(pattern, lowered) for pattern in weird_patterns)


def _usable_further_study_questions(items, *, limit: int = FURTHER_STUDY_QUESTION_COUNT) -> list[str]:
    normalized = _normalize_further_study_questions(items, limit=limit)
    if len(normalized) < min(limit, FURTHER_STUDY_QUESTION_COUNT):
        return []
    if any(_is_weird_study_question(question) for question in normalized):
        return []
    return normalized


def _focus_phrase_from_text(text: str) -> str:
    cleaned = str(text or "").replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.!:-")
    if not cleaned:
        return ""
    wrapper_patterns = (
        r"^(?:please\s+)?(?:can|could|would)\s+you\s+(?:show|give|provide|share|offer|outline|explain|describe|walk)\s+(?:me\s+)?",
        r"^(?:please\s+)?(?:tell|show|give|provide|share)\s+(?:me\s+)?",
        r"^(?:please\s+)?help\s+me\s+(?:understand|explain)\s+",
        r"^(?:please\s+)?how\s+would\s+you\s+explain\s+",
        r"^(?:please\s+)?what\s+common\s+mistake(?:s)?(?:\s+or\s+misconception(?:s)?)?\s+should\s+i\s+avoid\s+(?:with|when\s+thinking\s+about)\s+",
    )
    for pattern in wrapper_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(?:a\s+simple\s+|an?\s+)?example\s+of\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        rf"^(?:{'|'.join(STUDY_FOCUS_ACTION_VERBS)})\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:what|why|how|which|when|where)\s+(?:is|are|does|do|did|can|could|would|should|might|statements?\s+best\s+explain|statement\s+best\s+explains|best\s+explains?)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(?:in\s+your\s+own\s+words|connect\s+to\s+another\s+idea\s+in\s+this\s+block|connect\s+to\s+the\s+bigger\s+picture|matter(?:\s+here)?)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip(" ?.!:-")
    if not cleaned:
        return ""
    if len(cleaned) > 110:
        truncated = re.split(r"[,;:]\s+", cleaned, maxsplit=1)[0].strip(" ?.!:-")
        if len(truncated) >= 24:
            cleaned = truncated
    return cleaned[0].lower() + cleaned[1:]


def fallback_further_study_questions(
    *,
    stem: str = "",
    objective_text: str = "",
    chunk_text: str = "",
    correct_answer: str = "",
    limit: int = FURTHER_STUDY_QUESTION_COUNT,
) -> list[str]:
    focus = (
        _focus_phrase_from_text(objective_text)
        or _focus_phrase_from_text(stem)
        or _focus_phrase_from_text(correct_answer)
        or _focus_phrase_from_text(chunk_text.split(".")[0] if chunk_text else "")
        or "this idea"
    )
    questions = [
        f"Can you show a simple example of {focus}?",
        f"How would you explain {focus} in your own words?",
        f"What common mistake should I avoid when thinking about {focus}?",
        f"Why does {focus} matter?",
    ]
    return _usable_further_study_questions(questions, limit=limit) or _normalize_further_study_questions(questions, limit=limit)


def further_study_questions_for_question(question: QuestionBankItem) -> list[str]:
    return _usable_further_study_questions(question.further_study_questions) or fallback_further_study_questions(
        stem=question.stem,
        objective_text=(question.learning_objective.text if question.learning_objective else ""),
        chunk_text=(question.source_chunk.text if question.source_chunk else ""),
        correct_answer=question.correct_answer,
    )


def further_study_questions_for_chat(
    *,
    question: str = "",
    answer: str = "",
    block_title: str = "",
    objective_texts: list[str] | tuple[str, ...] | None = None,
    limit: int = FURTHER_STUDY_QUESTION_COUNT,
) -> list[str]:
    objective_focus = " ".join(str(text or "").strip() for text in (objective_texts or []) if str(text or "").strip())
    focus = (
        _focus_phrase_from_text(question)
        or _focus_phrase_from_text(objective_focus)
        or _focus_phrase_from_text(block_title)
        or _focus_phrase_from_text(answer)
        or "this idea"
    )
    questions = [
        f"Can you show a simple example of {focus}?",
        f"How would you explain {focus} in your own words?",
        f"What common mistake should I avoid when thinking about {focus}?",
        f"How does {focus} connect to the bigger picture?",
    ]
    return _usable_further_study_questions(questions, limit=limit) or _normalize_further_study_questions(questions, limit=limit)


def _select_objective_for_chunk(
    chunk: ContentChunk,
    objectives: list[LearningObjective],
    objective_keywords: dict[int, set[str]],
    chunk_index: int,
    total_chunks: int,
) -> LearningObjective | None:
    if not objectives:
        return None
    if len(objectives) == 1:
        return objectives[0]

    chunk_keywords = _keyword_set(chunk.text)
    best_objective = None
    best_score = 0
    for objective in objectives:
        overlap = len(chunk_keywords & objective_keywords.get(objective.pk, set()))
        if overlap > best_score:
            best_score = overlap
            best_objective = objective

    if best_objective is not None and best_score > 0:
        return best_objective

    scaled_index = min(len(objectives) - 1, (chunk_index * len(objectives)) // max(1, total_chunks))
    return objectives[scaled_index]


def _fallback_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    *,
    question_variant_index: int = 0,
) -> dict:
    summary = chunk.text.split(".")[0][:180].strip() or "this topic"
    correct_answer = (objective.text[:90].strip() if objective else summary) or "this topic"
    source_sentences = [
        sentence.strip(" .")
        for sentence in re.split(r"[.!?]+", chunk.text)
        if sentence.strip()
    ]

    if question_type == QuestionBankItem.QuestionType.WAQ:
        canonical_answer = source_sentences[0][:180].strip() if source_sentences else correct_answer
        topic = summary.lower()
        waq_stem = WAQ_FALLBACK_STEM_TEMPLATES[question_variant_index % len(WAQ_FALLBACK_STEM_TEMPLATES)].format(topic=topic)
        return {
            "question_type": question_type,
            "stem": waq_stem,
            "correct_answers": [canonical_answer or correct_answer],
            "written_answer_keywords": _fallback_written_answer_keywords(
                objective.text if objective else "",
                canonical_answer or correct_answer,
                summary,
                chunk.text,
            ),
            "further_study_questions": fallback_further_study_questions(
                stem=waq_stem,
                objective_text=(objective.text if objective else ""),
                chunk_text=chunk.text,
                correct_answer=canonical_answer or correct_answer,
            ),
            "distractors": [],
            "explanation": "This follows directly from this block.",
            "difficulty": "core",
        }

    distractors = [f"Alternative interpretation {index}" for index in range(1, distractor_count + 1)]
    correct_answers = [correct_answer]
    if question_type == QuestionBankItem.QuestionType.MAQ:
        fallback_candidates = [sentence[:90].strip() for sentence in source_sentences if sentence[:90].strip()]
        for candidate in fallback_candidates:
            if candidate not in correct_answers:
                correct_answers.append(candidate)
            if len(correct_answers) >= 2:
                break
        if len(correct_answers) < 2:
            correct_answers.append(f"Another accurate point about {summary.lower()}")
    return {
        "question_type": question_type,
        "stem": (
            f"Which statements best explain {summary.lower()}?"
            if question_type == QuestionBankItem.QuestionType.MAQ
            else f"Which statement best explains {summary.lower()}?"
        ),
        "correct_answers": correct_answers,
        "distractors": distractors,
        "written_answer_keywords": [],
        "further_study_questions": fallback_further_study_questions(
            stem=(
                f"Which statements best explain {summary.lower()}?"
                if question_type == QuestionBankItem.QuestionType.MAQ
                else f"Which statement best explains {summary.lower()}?"
            ),
            objective_text=(objective.text if objective else ""),
            chunk_text=chunk.text,
            correct_answer=correct_answers[0] if correct_answers else correct_answer,
        ),
        "explanation": "This follows directly from this block.",
        "difficulty": "core",
    }


def _normalize_question_stem(stem: str) -> str:
    cleaned = re.sub(r"\s+", " ", stem.strip())
    if not cleaned:
        return "Why is this important?"

    according_why_match = re.match(
        r"according to (?:the )?(?:presented|approved|provided) content,\s*why\s+(.*?)(?:\?)?$",
        cleaned,
        re.IGNORECASE,
    )
    if according_why_match:
        remainder = according_why_match.group(1).strip()
        if remainder:
            return f"Why {remainder.rstrip('?.')}?"

    cleaned = re.sub(
        r"^according to (?:the )?(?:presented|approved|provided) content,\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if cleaned.lower().startswith("why "):
        return f"Why {cleaned[4:].rstrip('?.')}?"
    return cleaned.rstrip("?") + "?"


def _openai_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    *,
    avoid_question_angles: list[str] | None = None,
) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    objective_text = objective.text if objective else "this block"
    is_maq = question_type == QuestionBankItem.QuestionType.MAQ
    is_waq = question_type == QuestionBankItem.QuestionType.WAQ
    avoidance_prompt = ""
    if avoid_question_angles:
        avoidance_prompt = "\nAvoid repeating the wording, answer angle, or explanation focus of these recent questions:\n" + "\n".join(
            avoid_question_angles[:6]
        )
    prompt = f"""
Create one educator-facing {"written-answer" if is_waq else ("multiple-answer" if is_maq else "single-answer")} question in JSON for the source text below.

Rules:
- no numerical calculation questions
- keep it answerable from the source text below
- avoid lead-ins like "According to these notes" or "Based on the passage"
- when the best stem is a why-question, start it directly with "Why ..."
- set question_type to "{question_type}"
- correct_answers must be an array of strings
- {"return strict JSON with keys: question_type, stem, correct_answers, written_answer_keywords, further_study_questions, explanation, difficulty" if is_waq else "return strict JSON with keys: question_type, stem, correct_answers, distractors, further_study_questions, explanation, difficulty"}
- {"correct_answers must contain exactly 1 item" if is_waq or not is_maq else "correct_answers must contain at least 2 items"}
- {"the question must require the student to type an answer in their own words" if is_waq else ("the question must require selecting more than one correct answer" if is_maq else "the question must have only one correct answer")}
- {"written_answer_keywords must be an array of 3 to 6 short concept phrases or key terms needed for a strong answer" if is_waq else f"use exactly {distractor_count} distractors"}
- {"do not return distractors for a written-answer question" if is_waq else "distractors must be plausible and distinct from the correct answer(s)"}
- further_study_questions must be an array of exactly 3 concise student-facing follow-up questions
- further_study_questions should invite deeper understanding, examples, comparison, application, or common mistakes when helpful
- each further_study_questions item must be phrased as a question a student could click to ask next
- prioritise a genuinely different question angle from recent questions when possible{avoidance_prompt}

Learning objective:
{objective_text}

Source text:
{chunk.text}
""".strip()
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Return only valid JSON."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    )
    raw_output = (getattr(response, "output_text", "") or "").strip()
    if not raw_output:
        raise ValueError("OpenAI returned an empty question payload.")

    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    object_match = re.search(r"\{.*\}", raw_output, re.DOTALL)
    if object_match:
        return json.loads(object_match.group(0))

    raise ValueError("OpenAI did not return parseable JSON for question generation.")


def _released_objectives_by_block(course: Course, today=None) -> dict[int, list[LearningObjective]]:
    today = today or timezone.localdate()
    objectives_by_block: dict[int, list[LearningObjective]] = defaultdict(list)

    for objective in LearningObjective.objects.filter(course=course, block__available_from__lte=today).select_related("block").order_by("block__order", "position", "pk"):
        objectives_by_block[objective.block_id].append(objective)

    return objectives_by_block


def _ordered_released_chunks(course: Course, today=None):
    today = today or timezone.localdate()
    return list(
        ContentChunk.objects.filter(course=course, asset__include_in_generation=True, block__available_from__lte=today)
        .select_related("block", "asset")
        .order_by("block__order", "asset__created_at", "ordinal", "pk")
    )


def _ordered_generation_objectives(
    block: CourseBlock,
    objectives_by_block: dict[int, list[LearningObjective]],
    preferred_objective_ids: list[int] | None = None,
) -> list[LearningObjective]:
    ordered_objectives = list(objectives_by_block.get(block.pk, []))
    if not preferred_objective_ids:
        return ordered_objectives

    preferred_lookup = {objective_id: index for index, objective_id in enumerate(preferred_objective_ids)}
    preferred = [objective for objective in ordered_objectives if objective.pk in preferred_lookup]
    preferred.sort(key=lambda objective: preferred_lookup[objective.pk])
    remaining = [objective for objective in ordered_objectives if objective.pk not in preferred_lookup]
    return [*preferred, *remaining]


def _rank_candidate_chunks_for_objective(
    candidate_chunks: list[ContentChunk],
    objective: LearningObjective,
    objective_keywords: dict[int, set[str]],
    *,
    question_type: str,
    question_type_chunk_counts: dict[int, int] | None = None,
    question_type_objective_chunk_counts: dict[tuple[int | None, int | None], int] | None = None,
) -> list[ContentChunk]:
    ranked_chunks = []
    target_keywords = objective_keywords.get(objective.pk, set())
    question_type_chunk_counts = question_type_chunk_counts or {}
    question_type_objective_chunk_counts = question_type_objective_chunk_counts or {}
    for chunk in candidate_chunks:
        overlap = len(_keyword_set(chunk.text) & target_keywords)
        ranked_chunks.append(
            (
                question_type_objective_chunk_counts.get((objective.pk, chunk.pk), 0)
                if question_type == QuestionBankItem.QuestionType.WAQ
                else 0,
                question_type_chunk_counts.get(chunk.pk, 0)
                if question_type == QuestionBankItem.QuestionType.WAQ
                else 0,
                0 if overlap > 0 else 1,
                chunk.practice_question_count,
                -overlap,
                chunk.asset.created_at,
                chunk.ordinal,
                chunk.pk,
                chunk,
            )
        )
    ranked_chunks.sort()
    return [item[-1] for item in ranked_chunks]


def _question_type_distribution_for_block(
    block: CourseBlock,
    question_type: str,
) -> tuple[dict[int, int], dict[int, int], dict[tuple[int | None, int | None], int]]:
    objective_counts: dict[int, int] = defaultdict(int)
    chunk_counts: dict[int, int] = defaultdict(int)
    objective_chunk_counts: dict[tuple[int | None, int | None], int] = defaultdict(int)
    rows = (
        QuestionBankItem.objects.filter(
            course=block.course,
            block=block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=question_type,
        )
        .values("learning_objective_id", "source_chunk_id")
        .annotate(total=Count("id"))
    )
    for row in rows:
        total = int(row["total"] or 0)
        objective_id = row["learning_objective_id"]
        source_chunk_id = row["source_chunk_id"]
        if objective_id is not None:
            objective_counts[int(objective_id)] += total
        if source_chunk_id is not None:
            chunk_counts[int(source_chunk_id)] += total
        objective_chunk_counts[(objective_id, source_chunk_id)] += total
    return objective_counts, chunk_counts, objective_chunk_counts


def _recent_question_avoidance_notes(
    block: CourseBlock,
    question_type: str,
    *,
    objective: LearningObjective | None = None,
    limit: int = 6,
) -> list[str]:
    queryset = QuestionBankItem.objects.filter(
        course=block.course,
        block=block,
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        question_type=question_type,
    ).select_related("learning_objective")
    recent_questions = []
    if objective is not None:
        recent_questions.extend(list(queryset.filter(learning_objective=objective).order_by("-created_at", "-pk")[:limit]))
    if len(recent_questions) < limit:
        recent_ids = {question.pk for question in recent_questions}
        recent_questions.extend(
            list(queryset.exclude(pk__in=recent_ids).order_by("-created_at", "-pk")[: max(0, limit - len(recent_questions))])
        )
    return [
        f'- "{question.stem}" with answer focus "{question.correct_answer[:120]}"'
        for question in recent_questions
    ]


def _preferred_generated_question_type(course: Course) -> str:
    practice_questions = course.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
    )
    practice_total = practice_questions.count()
    candidates = []
    for candidate_type, target_ratio in (
        (QuestionBankItem.QuestionType.MAQ, course.config.maq_ratio_percent),
        (QuestionBankItem.QuestionType.WAQ, course.config.waq_ratio_percent),
    ):
        if target_ratio <= 0:
            continue
        current_total = practice_questions.filter(question_type=candidate_type).count()
        current_ratio = (current_total * 100 / practice_total) if practice_total else 0.0
        gap = target_ratio - current_ratio
        if gap > 0:
            candidates.append((gap, target_ratio, QUESTION_TYPE_GENERATION_PRIORITY[candidate_type], candidate_type))

    if candidates:
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return candidates[0][3]

    return QuestionBankItem.QuestionType.MCQ


def _normalize_generated_payload(payload: dict, question_type: str, distractor_count: int) -> dict:
    normalized_type = question_type

    correct_answers = _normalize_answer_list(payload.get("correct_answers"))
    if not correct_answers and payload.get("correct_answer"):
        correct_answers = [str(payload["correct_answer"]).strip()]
    written_answer_keywords = _normalize_answer_list(payload.get("written_answer_keywords"))
    further_study_questions = _usable_further_study_questions(payload.get("further_study_questions"))

    distractors = [
        distractor
        for distractor in _normalize_answer_list(payload.get("distractors"))
        if distractor not in correct_answers
    ][:distractor_count]
    stem = str(payload.get("stem", "")).strip()
    explanation = str(payload.get("explanation", "")).strip()
    difficulty = str(payload.get("difficulty", "core")).strip() or "core"

    if normalized_type == QuestionBankItem.QuestionType.WAQ:
        if len(correct_answers) != 1:
            raise ValueError("WAQ payload must contain exactly one correct answer.")
        distractors = []
        written_answer_keywords = written_answer_keywords or _fallback_written_answer_keywords(correct_answers[0], stem)
    elif normalized_type == QuestionBankItem.QuestionType.MCQ:
        if len(correct_answers) != 1:
            raise ValueError("MCQ payload must contain exactly one correct answer.")
        written_answer_keywords = []
    else:
        if len(correct_answers) < 2:
            raise ValueError("MAQ payload must contain at least two correct answers.")
        written_answer_keywords = []

    further_study_questions = further_study_questions or fallback_further_study_questions(
        stem=stem,
        objective_text="",
        correct_answer=correct_answers[0] if correct_answers else "",
    )

    return {
        "question_type": normalized_type,
        "stem": stem,
        "correct_answers": correct_answers,
        "distractors": distractors,
        "written_answer_keywords": written_answer_keywords,
        "further_study_questions": further_study_questions,
        "explanation": explanation,
        "difficulty": difficulty,
    }


def _create_question_pair(
    *,
    course: Course,
    block: CourseBlock,
    chunk: ContentChunk,
    objective: LearningObjective | None,
    question_type: str,
    payload: dict,
    existing_hashes: set[str],
):
    normalized_payload = _normalize_generated_payload(payload, question_type, course.config.distractor_count)
    stem = _normalize_question_stem(normalized_payload["stem"])
    if any(char.isdigit() for char in stem) and any(token in stem.lower() for token in ("calculate", "solve", "compute")):
        return None, None

    item_hash = hashlib.sha256(stem.lower().encode("utf-8")).hexdigest()
    if item_hash in existing_hashes:
        for variant_number in range(2, 6):
            candidate_stem = f"{stem.rstrip('?')} (variant {variant_number})?"
            candidate_hash = hashlib.sha256(candidate_stem.lower().encode("utf-8")).hexdigest()
            if candidate_hash not in existing_hashes:
                stem = candidate_stem
                item_hash = candidate_hash
                break
        else:
            return None, None

    practice = QuestionBankItem.objects.create(
        course=course,
        block=block,
        learning_objective=objective,
        source_chunk=chunk,
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        stem=stem,
        question_type=normalized_payload["question_type"],
        correct_answer=normalized_payload["correct_answers"][0],
        additional_correct_answers=normalized_payload["correct_answers"][1:],
        written_answer_keywords=normalized_payload["written_answer_keywords"],
        further_study_questions=normalized_payload["further_study_questions"],
        distractors=normalized_payload["distractors"],
        explanation=normalize_explanation_text(normalized_payload["explanation"]),
        difficulty=normalized_payload["difficulty"],
        question_hash=item_hash,
    )
    validation = QuestionBankItem.objects.create(
        course=course,
        block=block,
        learning_objective=objective,
        source_chunk=chunk,
        bank_type=QuestionBankItem.BankType.VALIDATION,
        status=QuestionBankItem.Status.APPROVED,
        stem=f"{stem.rstrip('?')} (validation variant {random.randint(1000, 9999)})?",
        question_type=practice.question_type,
        correct_answer=practice.correct_answer,
        additional_correct_answers=practice.additional_correct_answers,
        written_answer_keywords=practice.written_answer_keywords,
        further_study_questions=practice.further_study_questions,
        distractors=practice.distractors,
        explanation=practice.explanation,
        difficulty=practice.difficulty,
        question_hash=hashlib.sha256(f"{item_hash}:validation".encode("utf-8")).hexdigest(),
        linked_question=practice,
    )
    practice.linked_question = validation
    practice.save(update_fields=["linked_question", "updated_at"])
    existing_hashes.add(item_hash)
    return practice, validation


def generate_question_pair_for_block(
    block: CourseBlock,
    *,
    existing_hashes: set[str] | None = None,
    preferred_objective_ids: list[int] | None = None,
    question_type: str | None = None,
):
    course = block.course
    existing_hashes = existing_hashes or set(course.question_bank_items.values_list("question_hash", flat=True))
    objectives_by_block = _released_objectives_by_block(course)
    objective_keywords = {
        objective.pk: _keyword_set(objective.text)
        for objectives in objectives_by_block.values()
        for objective in objectives
    }
    candidate_chunks = list(
        ContentChunk.objects.filter(block=block, asset__include_in_generation=True)
        .select_related("course", "block", "asset")
        .annotate(
            practice_question_count=Count(
                "question_bank_items",
                filter=Q(question_bank_items__bank_type=QuestionBankItem.BankType.PRACTICE),
            )
        )
        .order_by("practice_question_count", "asset__created_at", "ordinal", "pk")
    )
    if not candidate_chunks:
        return None, None

    total_chunks_by_block: dict[int, int] = defaultdict(int)
    chunk_index_by_block: dict[int, int] = defaultdict(int)
    question_type = (
        question_type
        if question_type in {QuestionBankItem.QuestionType.MCQ, QuestionBankItem.QuestionType.MAQ, QuestionBankItem.QuestionType.WAQ}
        else _preferred_generated_question_type(course)
    )
    question_type_objective_counts, question_type_chunk_counts, question_type_objective_chunk_counts = _question_type_distribution_for_block(
        block,
        question_type,
    )
    for chunk in candidate_chunks:
        total_chunks_by_block[chunk.block_id] += 1

    ordered_objectives = _ordered_generation_objectives(block, objectives_by_block, preferred_objective_ids)
    if question_type == QuestionBankItem.QuestionType.WAQ:
        ordered_objectives = sorted(
            ordered_objectives,
            key=lambda objective: question_type_objective_counts.get(objective.pk, 0),
        )
    attempted_chunk_ids: set[int] = set()

    for objective in ordered_objectives:
        avoid_question_angles = _recent_question_avoidance_notes(block, question_type, objective=objective)
        for chunk in _rank_candidate_chunks_for_objective(
            candidate_chunks,
            objective,
            objective_keywords,
            question_type=question_type,
            question_type_chunk_counts=question_type_chunk_counts,
            question_type_objective_chunk_counts=question_type_objective_chunk_counts,
        ):
            attempted_chunk_ids.add(chunk.pk)
            question_variant_index = question_type_objective_counts.get(objective.pk, 0) + question_type_objective_chunk_counts.get(
                (objective.pk, chunk.pk),
                0,
            )
            payload = _fallback_question_payload(
                chunk,
                objective,
                course.config.distractor_count,
                question_type,
                question_variant_index=question_variant_index,
            )
            if settings.OPENAI_API_KEY:
                try:
                    payload = _openai_question_payload(
                        chunk,
                        objective,
                        course.config.distractor_count,
                        question_type,
                        avoid_question_angles=avoid_question_angles,
                    )
                except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                    payload = _fallback_question_payload(
                        chunk,
                        objective,
                        course.config.distractor_count,
                        question_type,
                        question_variant_index=question_variant_index,
                    )
            practice, validation = _create_question_pair(
                course=course,
                block=block,
                chunk=chunk,
                objective=objective,
                question_type=question_type,
                payload=payload,
                existing_hashes=existing_hashes,
            )
            if practice is not None and validation is not None:
                return practice, validation

    for chunk in candidate_chunks:
        if chunk.pk in attempted_chunk_ids:
            continue
        chunk_index_by_block[chunk.block_id] += 1
        objective = _select_objective_for_chunk(
            chunk,
            objectives_by_block.get(chunk.block_id, []),
            objective_keywords,
            chunk_index_by_block[chunk.block_id] - 1,
            total_chunks_by_block[chunk.block_id],
        )
        question_variant_index = question_type_objective_counts.get(int(objective.pk) if objective else 0, 0) + question_type_objective_chunk_counts.get(
            ((objective.pk if objective else None), chunk.pk),
            0,
        )
        payload = _fallback_question_payload(
            chunk,
            objective,
            course.config.distractor_count,
            question_type,
            question_variant_index=question_variant_index,
        )
        if settings.OPENAI_API_KEY:
            try:
                payload = _openai_question_payload(
                    chunk,
                    objective,
                    course.config.distractor_count,
                    question_type,
                    avoid_question_angles=_recent_question_avoidance_notes(block, question_type, objective=objective),
                )
            except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                payload = _fallback_question_payload(
                    chunk,
                    objective,
                    course.config.distractor_count,
                    question_type,
                    question_variant_index=question_variant_index,
                )
        practice, validation = _create_question_pair(
            course=course,
            block=block,
            chunk=chunk,
            objective=objective,
            question_type=question_type,
            payload=payload,
            existing_hashes=existing_hashes,
        )
        if practice is not None and validation is not None:
            return practice, validation

    return None, None


def generate_question_banks(course: Course, *, approve: bool = False) -> int:
    today = timezone.localdate()
    question_count = 0
    existing_hashes = set(course.question_bank_items.values_list("question_hash", flat=True))
    objectives_by_block = _released_objectives_by_block(course, today=today)
    objective_keywords = {
        objective.pk: _keyword_set(objective.text)
        for objectives in objectives_by_block.values()
        for objective in objectives
    }
    chunks = _ordered_released_chunks(course, today=today)
    total_chunks_by_block: dict[int, int] = defaultdict(int)
    chunk_index_by_block: dict[int, int] = defaultdict(int)
    for chunk in chunks:
        total_chunks_by_block[chunk.block_id] += 1

    for chunk in chunks:
        chunk_index_by_block[chunk.block_id] += 1
        question_type = _preferred_generated_question_type(course)
        objective = _select_objective_for_chunk(
            chunk,
            objectives_by_block.get(chunk.block_id, []),
            objective_keywords,
            chunk_index_by_block[chunk.block_id] - 1,
            total_chunks_by_block[chunk.block_id],
        )
        payload = _fallback_question_payload(chunk, objective, course.config.distractor_count, question_type)
        if settings.OPENAI_API_KEY:
            try:
                payload = _openai_question_payload(chunk, objective, course.config.distractor_count, question_type)
            except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                payload = _fallback_question_payload(chunk, objective, course.config.distractor_count, question_type)
        practice, validation = _create_question_pair(
            course=course,
            block=chunk.block,
            chunk=chunk,
            objective=objective,
            question_type=question_type,
            payload=payload,
            existing_hashes=existing_hashes,
        )
        if practice is not None and validation is not None:
            question_count += 2

    return question_count
