import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from openai import OpenAI
from pypdf import PdfReader

from standalone.services.content import normalize_text


@dataclass
class PdfChapterCandidate:
    title: str
    start_page: int
    end_page: int
    confidence: int
    extracted_text: str


@dataclass
class _ChapterBoundary:
    title: str
    start_page: int
    confidence: int


_CHAPTER_PATTERNS = [
    re.compile(r"^\s*(chapter|unit|part)\s+([0-9]+|[ivxlcdm]+|[a-z])(?:\s*[:.\-]\s*|\s+)?(?P<title>.*)$", re.IGNORECASE),
    re.compile(r"^\s*([0-9]{1,2})\s*[:.\-]\s+(?P<title>[A-Z][A-Za-z0-9 ,:;'\-/()]{4,120})$"),
]


def extract_pdf_pages(file_path: str | Path) -> list[str]:
    reader = PdfReader(str(file_path))
    return [normalize_text(page.extract_text() or "") for page in reader.pages]


def analyze_pdf_chapters(file_path: str | Path) -> list[PdfChapterCandidate]:
    pages = extract_pdf_pages(file_path)
    if not pages:
        return []

    reader = PdfReader(str(file_path))
    boundaries = _outline_boundaries(reader) or _heading_boundaries(pages)
    if not boundaries:
        boundaries = [_ChapterBoundary("Imported PDF", 1, 35)]

    chapters = _boundaries_to_chapters(boundaries, pages)
    return _cleanup_with_openai(chapters) if settings.OPENAI_API_KEY else chapters


def _outline_boundaries(reader: PdfReader) -> list[_ChapterBoundary]:
    raw_items: list[tuple[str, int, int]] = []

    def collect(items: list[Any], depth: int = 0) -> None:
        for item in items:
            if isinstance(item, list):
                collect(item, depth + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            try:
                page_number = reader.get_destination_page_number(item) + 1
            except Exception:  # noqa: BLE001
                continue
            raw_items.append((title, page_number, depth))

    try:
        collect(list(reader.outline))
    except Exception:  # noqa: BLE001
        return []

    if not raw_items:
        return []

    chapter_like = [item for item in raw_items if _looks_like_chapter_heading(item[0])]
    top_level = [item for item in raw_items if item[2] == 0]
    source_items = chapter_like if len(chapter_like) >= 2 else top_level
    if len(source_items) < 2:
        return []

    return _dedupe_boundaries(
        [
            _ChapterBoundary(_clean_title(title), page_number, 90 if _looks_like_chapter_heading(title) else 75)
            for title, page_number, _depth in sorted(source_items, key=lambda item: item[1])
        ]
    )


def _heading_boundaries(pages: list[str]) -> list[_ChapterBoundary]:
    boundaries: list[_ChapterBoundary] = []
    seen_titles: set[str] = set()

    for page_index, page_text in enumerate(pages, start=1):
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        for line_index, line in enumerate(lines[:16]):
            title = _heading_title_from_line(line)
            if not title:
                continue
            if _is_noise_heading(title):
                continue
            if title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            if re.match(r"^(chapter|unit|part)\s+\S+$", title, flags=re.IGNORECASE):
                next_title = _next_title_line(lines, line_index + 1)
                if next_title:
                    title = f"{title}: {next_title}"
            boundaries.append(_ChapterBoundary(_clean_title(title), page_index, 80))
            break

    return _dedupe_boundaries(boundaries)


def _heading_title_from_line(line: str) -> str:
    compact = re.sub(r"\s+", " ", line).strip(" -:\t")
    if not compact or len(compact) > 140:
        return ""

    for pattern in _CHAPTER_PATTERNS:
        match = pattern.match(compact)
        if not match:
            continue
        title = (match.groupdict().get("title") or "").strip(" -:\t")
        prefix = compact[: match.start("title")].strip(" -:\t") if "title" in match.groupdict() else ""
        if title:
            return f"{prefix}: {title}" if prefix else title
        return compact

    return ""


def _looks_like_chapter_heading(title: str) -> bool:
    return bool(_heading_title_from_line(title))


def _next_title_line(lines: list[str], start_index: int) -> str:
    for line in lines[start_index : start_index + 4]:
        compact = re.sub(r"\s+", " ", line).strip(" -:\t")
        if 4 <= len(compact) <= 120 and not _heading_title_from_line(compact):
            return compact
    return ""


def _is_noise_heading(title: str) -> bool:
    lowered = title.lower()
    noise = {"contents", "table of contents", "index", "references", "bibliography", "glossary"}
    return lowered in noise or lowered.startswith("page ")


def _clean_title(title: str) -> str:
    title = normalize_text(title).replace("\n", " ")
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"^[.:\-\s]+|[.:\-\s]+$", "", title)
    return title[:255] or "Untitled chapter"


def _dedupe_boundaries(boundaries: list[_ChapterBoundary]) -> list[_ChapterBoundary]:
    deduped: list[_ChapterBoundary] = []
    seen_pages: set[int] = set()
    for boundary in sorted(boundaries, key=lambda item: item.start_page):
        if boundary.start_page in seen_pages:
            continue
        seen_pages.add(boundary.start_page)
        deduped.append(boundary)
    return deduped


def _boundaries_to_chapters(boundaries: list[_ChapterBoundary], pages: list[str]) -> list[PdfChapterCandidate]:
    page_count = len(pages)
    valid_boundaries = [boundary for boundary in boundaries if 1 <= boundary.start_page <= page_count]
    if not valid_boundaries:
        valid_boundaries = [_ChapterBoundary("Imported PDF", 1, 35)]

    chapters: list[PdfChapterCandidate] = []
    for index, boundary in enumerate(valid_boundaries):
        next_start = valid_boundaries[index + 1].start_page if index + 1 < len(valid_boundaries) else page_count + 1
        end_page = max(boundary.start_page, min(page_count, next_start - 1))
        text = normalize_text("\n\n".join(pages[boundary.start_page - 1 : end_page]))
        if not text:
            continue
        chapters.append(
            PdfChapterCandidate(
                title=boundary.title,
                start_page=boundary.start_page,
                end_page=end_page,
                confidence=boundary.confidence,
                extracted_text=text,
            )
        )

    if chapters:
        return chapters

    return [
        PdfChapterCandidate(
            title="Imported PDF",
            start_page=1,
            end_page=page_count,
            confidence=30,
            extracted_text=normalize_text("\n\n".join(pages)),
        )
    ]


def _cleanup_with_openai(chapters: list[PdfChapterCandidate]) -> list[PdfChapterCandidate]:
    if not chapters:
        return chapters

    prompt_chapters = [
        {
            "index": index,
            "title": chapter.title,
            "start_page": chapter.start_page,
            "end_page": chapter.end_page,
            "preview": chapter.extracted_text[:700],
        }
        for index, chapter in enumerate(chapters)
    ]
    prompt = f"""
Clean up proposed textbook chapter titles for a teacher review screen.

Return strict JSON with exactly this shape:
{{"chapters":[{{"index":0,"title":"Clean chapter title","confidence":80}}]}}

Rules:
- Preserve the supplied indexes.
- Do not invent, merge, split, or remove chapters.
- Keep titles concise and specific.
- Confidence must be an integer from 1 to 100.

Proposed chapters:
{json.dumps(prompt_chapters)}
""".strip()

    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": "Return only valid JSON."}]},
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            ],
        )
        payload = _parse_json_object((getattr(response, "output_text", "") or "").strip())
    except Exception:  # noqa: BLE001
        return chapters

    updates = payload.get("chapters", [])
    if not isinstance(updates, list):
        return chapters

    by_index = {index: chapter for index, chapter in enumerate(chapters)}
    for item in updates:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or index not in by_index:
            continue
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            by_index[index].title = _clean_title(title)
        confidence = item.get("confidence")
        if isinstance(confidence, int):
            by_index[index].confidence = max(1, min(100, confidence))

    return chapters


def _parse_json_object(raw_output: str) -> dict[str, Any]:
    if not raw_output:
        raise ValueError("OpenAI returned an empty payload.")
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

    raise ValueError("OpenAI did not return parseable JSON.")
