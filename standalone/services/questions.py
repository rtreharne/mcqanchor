import hashlib
import json
import random
import re
from collections import defaultdict

from django.conf import settings
from openai import OpenAI

from standalone.models import ContentChunk, Course, LearningObjective, QuestionBankItem


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


def _keyword_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) >= 4 and token not in OBJECTIVE_MATCH_STOPWORDS
    }


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


def _fallback_question_payload(chunk: ContentChunk, objective: LearningObjective | None, distractor_count: int) -> dict:
    summary = chunk.text.split(".")[0][:180].strip() or "the course material"
    correct_answer = (objective.text[:90].strip() if objective else summary) or "the approved material"
    distractors = [f"Alternative interpretation {index}" for index in range(1, distractor_count + 1)]
    return {
        "stem": f"Which statement best reflects the approved material about {summary.lower()}?",
        "correct_answer": correct_answer,
        "distractors": distractors,
        "explanation": "This item is based directly on the approved course material for the block.",
        "difficulty": "core",
    }


def _openai_question_payload(chunk: ContentChunk, objective: LearningObjective | None, distractor_count: int) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    objective_text = objective.text if objective else "the approved course material"
    prompt = f"""
Create one educator-facing MCQ in JSON for the supplied course content.

Rules:
- no numerical calculation questions
- use exactly {distractor_count} distractors
- keep it answerable from the supplied material
- return strict JSON with keys: stem, correct_answer, distractors, explanation, difficulty

Learning objective:
{objective_text}

Content:
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


def generate_question_banks(course: Course, *, approve: bool = False) -> int:
    config = course.config
    question_count = 0
    existing_hashes = set(course.question_bank_items.values_list("question_hash", flat=True))
    objectives_by_block: dict[int, list[LearningObjective]] = defaultdict(list)

    for objective in LearningObjective.objects.filter(course=course).select_related("block").order_by("block__order", "position", "pk"):
        objectives_by_block[objective.block_id].append(objective)

    objective_keywords = {
        objective.pk: _keyword_set(objective.text)
        for objectives in objectives_by_block.values()
        for objective in objectives
    }

    chunks = list(
        ContentChunk.objects.filter(course=course, asset__include_in_generation=True)
        .select_related("block", "asset")
        .order_by("block__order", "asset__created_at", "ordinal", "pk")
    )
    total_chunks_by_block: dict[int, int] = defaultdict(int)
    chunk_index_by_block: dict[int, int] = defaultdict(int)
    for chunk in chunks:
        total_chunks_by_block[chunk.block_id] += 1

    for chunk in chunks:
        chunk_index_by_block[chunk.block_id] += 1
        objective = _select_objective_for_chunk(
            chunk,
            objectives_by_block.get(chunk.block_id, []),
            objective_keywords,
            chunk_index_by_block[chunk.block_id] - 1,
            total_chunks_by_block[chunk.block_id],
        )
        payload = _fallback_question_payload(chunk, objective, config.distractor_count)
        if settings.OPENAI_API_KEY:
            try:
                payload = _openai_question_payload(chunk, objective, config.distractor_count)
            except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                payload = _fallback_question_payload(chunk, objective, config.distractor_count)

        stem = payload["stem"].strip()
        if any(char.isdigit() for char in stem) and any(token in stem.lower() for token in ("calculate", "solve", "compute")):
            continue

        item_hash = hashlib.sha256(stem.lower().encode("utf-8")).hexdigest()
        if item_hash in existing_hashes:
            continue

        status = QuestionBankItem.Status.APPROVED if approve else QuestionBankItem.Status.DRAFT
        practice = QuestionBankItem.objects.create(
            course=course,
            block=chunk.block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=status,
            stem=stem,
            correct_answer=payload["correct_answer"].strip(),
            distractors=payload["distractors"][: config.distractor_count],
            explanation=payload.get("explanation", "").strip(),
            difficulty=payload.get("difficulty", "core"),
            question_hash=item_hash,
        )
        validation = QuestionBankItem.objects.create(
            course=course,
            block=chunk.block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=status,
            stem=f"{stem} (validation variant {random.randint(1000, 9999)})",
            correct_answer=practice.correct_answer,
            distractors=practice.distractors,
            explanation=practice.explanation,
            difficulty=practice.difficulty,
            question_hash=hashlib.sha256(f"{item_hash}:validation".encode("utf-8")).hexdigest(),
            linked_question=practice,
        )
        practice.linked_question = validation
        practice.save(update_fields=["linked_question", "updated_at"])
        existing_hashes.add(item_hash)
        question_count += 2

    return question_count
