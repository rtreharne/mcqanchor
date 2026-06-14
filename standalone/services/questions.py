import hashlib
import json
import random

from django.conf import settings
from openai import OpenAI

from standalone.models import ContentChunk, Course, LearningObjective, QuestionBankItem


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
    return json.loads(response.output_text)


def generate_question_banks(course: Course, *, approve: bool = False) -> int:
    config = course.config
    question_count = 0
    existing_hashes = set(course.question_bank_items.values_list("question_hash", flat=True))

    for chunk in ContentChunk.objects.filter(course=course, asset__include_in_generation=True).select_related("block"):
        objective = (
            LearningObjective.objects.filter(course=course, block=chunk.block).order_by("created_at").first()
        )
        payload = (
            _openai_question_payload(chunk, objective, config.distractor_count)
            if settings.OPENAI_API_KEY
            else _fallback_question_payload(chunk, objective, config.distractor_count)
        )

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

