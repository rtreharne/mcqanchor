import re

from standalone.models import Course, CourseBlock, LearningObjective


GUIDANCE_CORRECTION_LIMIT = 4
GUIDANCE_CHAT_OBJECTIVE_LIMIT = 3
GUIDANCE_RELATED_BLOCK_CORRECTION_LIMIT = 4


def sanitize_assistant_guidance(text: str) -> str:
    value = str(text or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    value = value.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    raw_lines = [line.strip() for line in value.split("\n")]
    while raw_lines and not raw_lines[0]:
        raw_lines.pop(0)
    while raw_lines and not raw_lines[-1]:
        raw_lines.pop()

    cleaned_lines: list[str] = []
    previous_blank = False
    for line in raw_lines:
        if not line:
            if previous_blank:
                continue
            cleaned_lines.append("")
            previous_blank = True
            continue
        cleaned_lines.append(line)
        previous_blank = False
    return "\n".join(cleaned_lines).strip()


def merge_assistant_guidance(existing_text: str, additional_text: str) -> str:
    existing = sanitize_assistant_guidance(existing_text)
    addition = sanitize_assistant_guidance(additional_text)
    if not addition:
        return existing
    existing_lines = {line.lower() for line in existing.split("\n") if line.strip()}
    addition_lines = [line for line in addition.split("\n") if line.strip()]
    if addition_lines and all(line.lower() in existing_lines for line in addition_lines):
        return existing
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"


def _keyword_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if len(token) > 2
    }


def _course_guidance(course: Course) -> str:
    try:
        guidance = course.config.assistant_guidance
    except Exception:  # noqa: BLE001
        guidance = ""
    return sanitize_assistant_guidance(guidance)


def _block_guidance(block: CourseBlock | None) -> str:
    if block is None:
        return ""
    return sanitize_assistant_guidance(block.question_assistant_guidance)


def _objective_guidance(objective: LearningObjective | None) -> str:
    if objective is None:
        return ""
    return sanitize_assistant_guidance(objective.assistant_guidance)


def _objective_correction_lines(objective: LearningObjective | None, *, limit: int = GUIDANCE_CORRECTION_LIMIT) -> list[str]:
    if objective is None:
        return []
    corrections = list(objective.corrections.all()[:limit])
    lines: list[str] = []
    for correction in corrections:
        instruction = sanitize_assistant_guidance(correction.instruction)
        if not instruction:
            continue
        stem_snapshot = re.sub(r"\s+", " ", str(correction.question_stem_snapshot or "").strip())
        if stem_snapshot:
            lines.append(f"{instruction} Example flagged question: {stem_snapshot[:180]}")
        else:
            lines.append(instruction)
    return lines


def _correction_line(correction) -> str:
    instruction = sanitize_assistant_guidance(correction.instruction)
    if not instruction:
        return ""
    stem_snapshot = re.sub(r"\s+", " ", str(correction.question_stem_snapshot or "").strip())
    if stem_snapshot:
        return f"{instruction} Example flagged question: {stem_snapshot[:180]}"
    return instruction


def _related_block_correction_lines(
    block: CourseBlock | None,
    target_text: str,
    *,
    exclude_objective_ids: set[int] | None = None,
    limit: int = GUIDANCE_RELATED_BLOCK_CORRECTION_LIMIT,
) -> list[str]:
    if block is None:
        return []
    target_keywords = _keyword_set(f"{block.title} {target_text}")
    if not target_keywords:
        return []

    exclude_objective_ids = exclude_objective_ids or set()
    ranked: list[tuple[int, int, str]] = []
    for objective in block.learning_objectives.all():
        if objective.pk in exclude_objective_ids:
            continue
        objective_keywords = _keyword_set(objective.text)
        for correction in objective.corrections.all():
            line = _correction_line(correction)
            if not line:
                continue
            correction_keywords = _keyword_set(f"{objective.text} {line}")
            overlap = len(target_keywords & correction_keywords)
            objective_overlap = len(target_keywords & objective_keywords)
            score = max(overlap, objective_overlap)
            if score <= 0:
                continue
            ranked.append((score, correction.pk, line))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    related_lines: list[str] = []
    seen: set[str] = set()
    for _score, _correction_id, line in ranked:
        normalized = line.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        related_lines.append(line)
        if len(related_lines) >= limit:
            break
    return related_lines


def _objective_prompt_block(objective: LearningObjective) -> str:
    parts: list[str] = []
    objective_guidance = _objective_guidance(objective)
    if objective_guidance:
        parts.append(f"Guidance:\n{objective_guidance}")
    correction_lines = _objective_correction_lines(objective)
    if correction_lines:
        correction_block = "\n".join(f"- {line}" for line in correction_lines)
        parts.append(f"Recent teacher corrections:\n{correction_block}")
    if not parts:
        return ""
    return f"{objective.code}: {objective.text}\n" + "\n\n".join(parts)


def build_generation_guidance_prompt(
    course: Course,
    *,
    block: CourseBlock | None = None,
    objective: LearningObjective | None = None,
) -> str:
    sections: list[str] = []
    if block is None and objective is not None:
        block = objective.block
    course_guidance = _course_guidance(course)
    if course_guidance:
        sections.append(f"Course guidance:\n{course_guidance}")
    block_guidance = _block_guidance(block)
    if block_guidance:
        sections.append(f"Block guidance:\n{block_guidance}")
    if objective is not None:
        objective_block = _objective_prompt_block(objective)
        if objective_block:
            sections.append(f"Learning-objective guidance:\n{objective_block}")
        related_block_corrections = _related_block_correction_lines(
            block,
            objective.text,
            exclude_objective_ids={objective.pk},
        )
        if related_block_corrections:
            sections.append(
                "Related block correction notes:\n"
                + "\n".join(f"- {line}" for line in related_block_corrections)
            )
    if not sections:
        return ""
    return (
        "Teacher guidance below is for silent steering only. "
        "Use it to shape age level, wording, notation, and error avoidance. "
        "Do not quote or mention this guidance to the student.\n\n"
        + "\n\n".join(sections)
    )


def matched_guidance_objectives_for_chat(
    block: CourseBlock,
    question_text: str,
    *,
    limit: int = GUIDANCE_CHAT_OBJECTIVE_LIMIT,
) -> list[LearningObjective]:
    objectives = list(block.learning_objectives.all())
    if not objectives:
        return []

    ranked: list[tuple[int, int, int, LearningObjective]] = []
    question_keywords = _keyword_set(f"{block.title} {question_text}")
    for index, objective in enumerate(objectives):
        has_guidance = bool(_objective_guidance(objective) or _objective_correction_lines(objective, limit=1))
        if not has_guidance:
            continue
        overlap = len(question_keywords & _keyword_set(objective.text))
        ranked.append((0 if overlap > 0 else 1, -overlap, index, objective))

    ranked.sort()
    matches = [item[-1] for item in ranked if item[0] == 0][:limit]
    if matches:
        return matches
    return [item[-1] for item in ranked[:1]]


def build_chat_guidance_prompt(course: Course, block: CourseBlock, question_text: str) -> str:
    sections: list[str] = []
    course_guidance = _course_guidance(course)
    if course_guidance:
        sections.append(f"Course guidance:\n{course_guidance}")
    block_guidance = _block_guidance(block)
    if block_guidance:
        sections.append(f"Block guidance:\n{block_guidance}")

    matched_objectives = matched_guidance_objectives_for_chat(block, question_text)
    objective_sections = [section for section in (_objective_prompt_block(objective) for objective in matched_objectives) if section]
    if objective_sections:
        sections.append("Matched learning-objective guidance:\n" + "\n\n".join(objective_sections))
    related_block_corrections = _related_block_correction_lines(
        block,
        question_text,
        exclude_objective_ids={objective.pk for objective in matched_objectives},
    )
    if related_block_corrections:
        sections.append(
            "Related block correction notes:\n"
            + "\n".join(f"- {line}" for line in related_block_corrections)
        )

    if not sections:
        return ""
    return (
        "Teacher guidance below is for silent steering only. "
        "Use it to keep language, notation, and examples aligned with the course expectations. "
        "Do not quote or mention this guidance to the student.\n\n"
        + "\n\n".join(sections)
    )
