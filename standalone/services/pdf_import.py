import json
import re
import shutil
import subprocess
import tempfile
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
_NON_CHAPTER_OUTLINE_TITLES = {
    "acknowledgements",
    "appendices",
    "appendix",
    "bibliography",
    "contents",
    "core curriculum",
    "electives",
    "further resources",
    "glossary",
    "index",
    "preface",
    "references",
    "setup",
    "table of contents",
}
_NON_CHAPTER_OUTLINE_PATTERN = re.compile(r"^(part|unit|section|appendix|appendices)\b", re.IGNORECASE)
_TOC_HEADER_PATTERN = re.compile(r"\b(?P<kind>chapter|module)\s*[.:]?\s*(?P<number>\d{1,3})", re.IGNORECASE)
_TOP_LEVEL_SECTION_PATTERN = re.compile(r"^(?P<number>\d{1,2})\s+(?P<title>[A-Z][A-Za-z0-9 ,:;'\-/()&]+)$")
_SUBSECTION_PATTERN = re.compile(r"^\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\s+")
_NUMBERED_STEP_PATTERN = re.compile(r"^\d{1,2}[.:]\s+")
_PAGE_COUNTER_PATTERN = re.compile(r"^\d+\s+of\s+\d+$", re.IGNORECASE)
_TIMESTAMP_PATTERN = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},\s+\d{1,2}:\d{2}$")
_OCR_PROBE_PAGE_COUNT = 40
_END_BOUNDARY_TITLE = "__END_OF_CHAPTERS__"
_ANALYSIS_PAGE_TEXT_CHAR_LIMIT = 2500


class PdfOcrUnavailableError(RuntimeError):
    pass


def _truncate_page_text(text: str, max_chars_per_page: int | None = None) -> str:
    if max_chars_per_page is None or max_chars_per_page <= 0:
        return text
    return text[:max_chars_per_page]


def _extract_page_text_with_poppler(
    file_path: str | Path,
    page_number: int,
    *,
    max_chars_per_page: int | None = None,
) -> str:
    extraction = subprocess.run(
        [
            "pdftotext",
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-layout",
            str(file_path),
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if extraction.returncode != 0:
        raise RuntimeError(f"Could not extract PDF page {page_number}: {extraction.stderr.strip()}")
    return _truncate_page_text(normalize_text(extraction.stdout or ""), max_chars_per_page=max_chars_per_page)


def _extract_page_texts(
    reader: PdfReader,
    file_path: str | Path,
    page_numbers: range | list[int],
    *,
    max_chars_per_page: int | None = None,
) -> list[str]:
    if shutil.which("pdftotext"):
        return [
            _extract_page_text_with_poppler(file_path, page_number, max_chars_per_page=max_chars_per_page)
            for page_number in page_numbers
        ]
    return [
        _truncate_page_text(normalize_text(reader.pages[page_number - 1].extract_text() or ""), max_chars_per_page=max_chars_per_page)
        for page_number in page_numbers
    ]


def _extract_reader_pages(reader: PdfReader, *, max_chars_per_page: int | None = None) -> list[str]:
    pages: list[str] = []
    for page in reader.pages:
        text = normalize_text(page.extract_text() or "")
        if max_chars_per_page is not None and max_chars_per_page > 0:
            text = text[:max_chars_per_page]
        pages.append(text)
    return pages


def extract_pdf_pages(file_path: str | Path, *, max_chars_per_page: int | None = None) -> list[str]:
    reader = PdfReader(str(file_path))
    return _extract_page_texts(
        reader,
        file_path,
        range(1, len(reader.pages) + 1),
        max_chars_per_page=max_chars_per_page,
    )


def extract_pdf_page_range(file_path: str | Path, start_page: int, end_page: int) -> str:
    """Extract a page range, using OCR only when the PDF has no usable text layer."""
    reader = PdfReader(str(file_path))
    page_count = len(reader.pages)
    start_page = max(1, start_page)
    end_page = min(page_count, end_page)
    if start_page > end_page:
        return ""

    pages = _extract_page_texts(reader, file_path, range(start_page, end_page + 1))
    if _has_readable_text(pages):
        return normalize_text("\n\n".join(pages))

    ocr_pages = _ocr_pdf_pages(file_path, range(start_page, end_page + 1))
    return normalize_text("\n\n".join(ocr_pages.get(page_number, "") for page_number in range(start_page, end_page + 1)))


def analyze_pdf_chapters(file_path: str | Path) -> list[PdfChapterCandidate]:
    reader = PdfReader(str(file_path))
    page_count = len(reader.pages)
    if page_count <= 0:
        return []

    boundaries = _outline_boundaries(reader)
    if boundaries:
        chapters = _empty_chapters_from_boundaries(boundaries, page_count)
        return _cleanup_with_openai(chapters) if settings.OPENAI_API_KEY else chapters

    pages = _extract_page_texts(
        reader,
        file_path,
        range(1, page_count + 1),
        max_chars_per_page=_ANALYSIS_PAGE_TEXT_CHAR_LIMIT,
    )
    if not _has_readable_text(pages):
        probe_end = min(len(pages), _OCR_PROBE_PAGE_COUNT)
        probe_pages = _ocr_pdf_pages(file_path, range(1, probe_end + 1), dpi=135)
        boundaries = _toc_boundaries_from_ocr(probe_pages, page_count) or _heading_boundaries_from_page_map(probe_pages)
        if not boundaries:
            return []
        chapters = _empty_chapters_from_boundaries(boundaries, page_count)
        return _cleanup_with_openai(chapters) if settings.OPENAI_API_KEY else chapters

    boundaries = _top_level_numbered_section_boundaries(pages) or _heading_boundaries(pages)
    if not boundaries:
        boundaries = [_ChapterBoundary("Imported PDF", 1, 35)]

    chapters = _empty_chapters_from_boundaries(boundaries, page_count)
    return _cleanup_with_openai(chapters) if settings.OPENAI_API_KEY else chapters


def _has_readable_text(pages: list[str]) -> bool:
    sample = "".join(page for page in pages[:50] if page)
    return len(re.sub(r"\s+", "", sample)) >= 20


def _ocr_pdf_pages(
    file_path: str | Path,
    page_numbers: range | list[int],
    *,
    dpi: int = 160,
) -> dict[int, str]:
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        raise PdfOcrUnavailableError(
            "This is a scanned PDF and requires the Poppler and Tesseract OCR system packages."
        )

    extracted: dict[int, str] = {}
    with tempfile.TemporaryDirectory(prefix="mcqanchor-ocr-") as temp_dir:
        image_path = Path(temp_dir) / "page.jpg"
        output_prefix = str(image_path.with_suffix(""))
        for page_number in page_numbers:
            render = subprocess.run(
                [
                    "pdftoppm",
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-r",
                    str(dpi),
                    "-jpeg",
                    "-jpegopt",
                    "quality=82",
                    str(file_path),
                    output_prefix,
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if render.returncode != 0:
                raise RuntimeError(f"Could not render PDF page {page_number} for OCR: {render.stderr.strip()}")
            ocr = subprocess.run(
                ["tesseract", str(image_path), "stdout", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            image_path.unlink(missing_ok=True)
            if ocr.returncode != 0:
                raise RuntimeError(f"Could not OCR PDF page {page_number}: {ocr.stderr.strip()}")
            extracted[page_number] = normalize_text(ocr.stdout)
    return extracted


def _toc_boundaries_from_ocr(page_map: dict[int, str], page_count: int) -> list[_ChapterBoundary]:
    contents_pages = {
        page_number
        for page_number, text in page_map.items()
        if re.search(r"\bcontents\b", text[:800], flags=re.IGNORECASE)
        or len(_TOC_HEADER_PATTERN.findall(text)) >= 2
        or re.search(r"^\s*(?:unifying concepts|appendices)\b", text, flags=re.IGNORECASE | re.MULTILINE)
    }
    if not contents_pages:
        return []

    entries: list[tuple[str, int, str, int]] = []
    for page_number in sorted(contents_pages):
        entries.extend(_parse_toc_entries(page_map[page_number]))

    chapter_entries = [entry for entry in entries if entry[0] == "chapter"]
    if len(chapter_entries) < 2:
        return []

    chapter_entries = sorted(chapter_entries, key=lambda entry: entry[3])
    deduped_entries: list[tuple[str, int, str, int]] = []
    seen_printed_pages: set[int] = set()
    for entry in chapter_entries:
        if entry[3] in seen_printed_pages:
            continue
        seen_printed_pages.add(entry[3])
        deduped_entries.append(entry)
    chapter_entries = deduped_entries

    # A long textbook contents list is strongly sequential. This repairs common
    # OCR substitutions such as "Chapter 38" for "Chapter 16" without guessing
    # titles or page positions.
    if len(chapter_entries) >= 10:
        first_number = chapter_entries[0][1]
        chapter_entries = [
            (kind, first_number + index, title, printed_page)
            for index, (kind, _number, title, printed_page) in enumerate(chapter_entries)
        ]

    offset = _infer_printed_page_offset(chapter_entries, page_map, contents_pages)
    if offset is None:
        return []

    boundaries = []
    for _kind, number, title, printed_page in chapter_entries:
        pdf_page = printed_page + offset
        if not 1 <= pdf_page <= page_count:
            continue
        display_title = f"Chapter {number}"
        if title:
            display_title = f"{display_title}: {title}"
        boundaries.append(_ChapterBoundary(display_title, pdf_page, 82))

    terminal_pages: list[int] = []
    for page_number in contents_pages:
        for match in re.finditer(
            r"^\s*(?:unifying concepts|appendices)\b.*?\s(?P<page>\d{2,4})\s*$",
            page_map[page_number],
            flags=re.IGNORECASE | re.MULTILINE,
        ):
            terminal_pages.append(int(match.group("page")))
    if terminal_pages:
        terminal_pdf_page = min(terminal_pages) + offset
        if boundaries and boundaries[-1].start_page < terminal_pdf_page <= page_count:
            boundaries.append(_ChapterBoundary(_END_BOUNDARY_TITLE, terminal_pdf_page, 0))
    return _dedupe_boundaries(boundaries)


def _parse_toc_entries(text: str) -> list[tuple[str, int, str, int]]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    entries: list[tuple[str, int, str, int]] = []
    for line_index, line in enumerate(lines):
        headers = list(_TOC_HEADER_PATTERN.finditer(line))
        for header_index, header in enumerate(headers):
            segment_end = headers[header_index + 1].start() if header_index + 1 < len(headers) else len(line)
            rest = line[header.end() : segment_end].strip(" .:-?")
            kind = header.group("kind").lower()
            number = int(header.group("number"))

            page_match = None
            for candidate in re.finditer(r"(?<![.\d])(?P<page>\d{1,4})(?![.\d])", rest):
                if int(candidate.group("page")) >= number:
                    page_match = candidate
                    break

            if page_match and int(page_match.group("page")) > number:
                title = rest[: page_match.start()].strip(" .:-")
                entries.append((kind, number, title, int(page_match.group("page"))))
                continue

            if kind != "chapter":
                continue

            # If the chapter header's page number was unreadable, use the first
            # numbered subsection immediately below it (for example 16.1 ... 302).
            following = " ".join(lines[line_index + 1 : line_index + 4])
            section_matches = list(
                re.finditer(
                    r"(?P<section>\d{1,3})[.]\d{1,2}\b.*?\s(?P<page>\d{2,4})(?=\s|$)",
                    following,
                )
            )
            if section_matches:
                section_match = min(
                    section_matches,
                    key=lambda match: abs(int(match.group("section")) - number),
                )
                section_number = int(section_match.group("section"))
                title = rest[: page_match.start()].strip(" .:-") if page_match else rest
                entries.append((kind, section_number, title, int(section_match.group("page"))))
    return entries


def _infer_printed_page_offset(
    entries: list[tuple[str, int, str, int]],
    page_map: dict[int, str],
    contents_pages: set[int],
) -> int | None:
    scores: dict[int, int] = {}
    for kind, number, title, printed_page in entries:
        search_terms = [term.lower() for term in re.findall(r"[A-Za-z]{4,}", title) if term.lower() not in {"chapter", "module"}]
        for pdf_page, text in page_map.items():
            if pdf_page in contents_pages:
                continue
            lowered = text.lower()
            offset = pdf_page - printed_page
            if offset < 0:
                continue
            heading_area = lowered[:300]
            if kind == "chapter" and re.search(rf"\b{number}[.]1\b", heading_area):
                scores[offset] = scores.get(offset, 0) + 5
            if not search_terms:
                continue
            required = min(2, len(search_terms))
            if sum(term in lowered for term in search_terms) < required:
                continue
            if kind == "module" and not re.search(rf"\bmodule\s*{number}\b", lowered):
                continue
            if kind == "chapter":
                if sum(term in heading_area for term in search_terms) < required:
                    continue
                if not re.search(rf"(?:\bchapter\s*)?\b{number}\b", heading_area):
                    continue
            scores[offset] = scores.get(offset, 0) + (3 if kind == "module" else 1)

    if not scores:
        return None
    return max(scores, key=lambda candidate: (scores[candidate], -abs(candidate)))


def _heading_boundaries_from_page_map(page_map: dict[int, str]) -> list[_ChapterBoundary]:
    if not page_map:
        return []
    last_page = max(page_map)
    pages = [page_map.get(page_number, "") for page_number in range(1, last_page + 1)]
    return _heading_boundaries(pages)


def _top_level_numbered_section_boundaries(pages: list[str]) -> list[_ChapterBoundary]:
    candidates: list[tuple[int, int, str]] = []

    for page_index, page_text in enumerate(pages, start=1):
        title = _top_level_numbered_title_from_page(page_text)
        if not title:
            continue
        number, display_title = title
        candidates.append((page_index, number, display_title))

    return _validated_top_level_boundaries(candidates)


def _empty_chapters_from_boundaries(
    boundaries: list[_ChapterBoundary], page_count: int
) -> list[PdfChapterCandidate]:
    chapters: list[PdfChapterCandidate] = []
    valid = [boundary for boundary in boundaries if 1 <= boundary.start_page <= page_count]
    for index, boundary in enumerate(valid):
        next_start = valid[index + 1].start_page if index + 1 < len(valid) else page_count + 1
        if boundary.title == _END_BOUNDARY_TITLE:
            continue
        chapters.append(
            PdfChapterCandidate(
                title=boundary.title,
                start_page=boundary.start_page,
                end_page=max(boundary.start_page, min(page_count, next_start - 1)),
                confidence=boundary.confidence,
                extracted_text="",
            )
        )
    return chapters


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

    source_items = _select_outline_items(raw_items)
    if len(source_items) < 2:
        return []

    return _dedupe_boundaries(
        [
            _ChapterBoundary(_clean_title(title), page_number, 90 if _looks_like_chapter_heading(title) else 75)
            for title, page_number, _depth in source_items
        ]
    )


def _select_outline_items(raw_items: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    by_depth: dict[int, list[tuple[str, int, int]]] = {}
    for title, page_number, depth in sorted(raw_items, key=lambda item: (item[2], item[1], item[0].lower())):
        by_depth.setdefault(depth, []).append((title, page_number, depth))

    best_score = float("-inf")
    best_items: list[tuple[str, int, int]] = []

    for depth in sorted(by_depth):
        cleaned_items: list[tuple[str, int, int]] = []
        seen_pages: set[int] = set()
        for title, page_number, _depth in sorted(by_depth[depth], key=lambda item: item[1]):
            cleaned_title = _clean_title(title)
            if not cleaned_title or _is_noise_heading(cleaned_title):
                continue
            if page_number in seen_pages:
                continue
            seen_pages.add(page_number)
            cleaned_items.append((cleaned_title, page_number, depth))

        if len(cleaned_items) < 2:
            continue

        gaps = [
            cleaned_items[index + 1][1] - cleaned_items[index][1]
            for index in range(len(cleaned_items) - 1)
        ]
        average_gap = (sum(gaps) / len(gaps)) if gaps else 0
        non_chapter_count = sum(1 for title, _page_number, _depth in cleaned_items if _is_non_chapter_outline_title(title))
        non_chapter_ratio = non_chapter_count / len(cleaned_items)
        overcrowding_penalty = max(0, len(cleaned_items) - 40)
        score = (
            min(len(cleaned_items), 24)
            + min(average_gap, 20)
            - (depth * 4)
            - (non_chapter_ratio * 30)
            - overcrowding_penalty
        )
        if score > best_score:
            best_score = score
            best_items = cleaned_items

    return best_items


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


def _top_level_numbered_title_from_page(page_text: str) -> tuple[int, str] | None:
    lines = _normalized_page_lines(page_text)
    if not lines:
        return None

    for line_index, line in enumerate(lines[:8]):
        match = _TOP_LEVEL_SECTION_PATTERN.match(line)
        if not match:
            continue
        if _SUBSECTION_PATTERN.match(line) or _NUMBERED_STEP_PATTERN.match(line):
            continue
        if _looks_like_instruction_line(line):
            continue

        number = int(match.group("number"))
        title = line
        next_line = _joined_wrapped_title_line(lines, line_index)
        if next_line:
            title = next_line
        return number, _clean_title(title)

    return None


def _heading_title_from_line(line: str) -> str:
    compact = re.sub(r"\s+", " ", line).strip(" -:\t")
    if not compact or len(compact) > 140:
        return ""
    if _looks_like_instruction_line(compact):
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


def _normalized_page_lines(page_text: str) -> list[str]:
    lines = [line.strip() for line in page_text.splitlines() if line.strip()]
    normalized: list[str] = []
    for line in lines:
        compact = re.sub(r"\s+", " ", line).strip()
        if not compact or _is_print_pdf_noise_line(compact):
            continue
        normalized.append(compact)
    return normalized


def _is_print_pdf_noise_line(line: str) -> bool:
    lowered = line.lower()
    if _PAGE_COUNTER_PATTERN.match(line) or _TIMESTAMP_PATTERN.match(line):
        return True
    if "http://" in lowered or "https://" in lowered or " | " in line:
        return True
    if lowered in {"watch on", "ninepointeightone"}:
        return True
    return False


def _joined_wrapped_title_line(lines: list[str], line_index: int) -> str:
    current = lines[line_index]
    if not _TOP_LEVEL_SECTION_PATTERN.match(current):
        return current
    if not _looks_like_wrapped_title_fragment(current):
        return current

    for next_line in lines[line_index + 1 : line_index + 3]:
        if _is_print_pdf_noise_line(next_line):
            continue
        if _TOP_LEVEL_SECTION_PATTERN.match(next_line) or _SUBSECTION_PATTERN.match(next_line) or _NUMBERED_STEP_PATTERN.match(next_line):
            break
        if _looks_like_continuation_line(next_line):
            return f"{current} {next_line}"
        break
    return current


def _looks_like_wrapped_title_fragment(line: str) -> bool:
    return bool(re.search(r"\b(in|of|for|and|to|part|linear|anova|statistics|functions):?$", line, flags=re.IGNORECASE))


def _looks_like_continuation_line(line: str) -> bool:
    return bool(line) and len(line) <= 60 and bool(re.match(r"^[A-Z][A-Za-z0-9 ,:;'\-/()&]*$", line))


def _looks_like_instruction_line(line: str) -> bool:
    lowered = line.lower()
    if lowered.endswith(":"):
        return True
    instruction_verbs = {
        "add",
        "apply",
        "calculate",
        "choose",
        "click",
        "create",
        "download",
        "drag",
        "generate",
        "identify",
        "import",
        "insert",
        "interpret",
        "load",
        "open",
        "perform",
        "select",
        "set",
        "start",
        "transpose",
        "use",
        "write",
    }
    words = re.findall(r"[a-z]+", lowered)
    if not words:
        return False
    return words[0] in instruction_verbs


def _validated_top_level_boundaries(candidates: list[tuple[int, int, str]]) -> list[_ChapterBoundary]:
    if len(candidates) < 2:
        return []

    filtered: list[tuple[int, int, str]] = []
    seen_numbers: set[int] = set()
    last_number = 0
    for page_number, section_number, title in candidates:
        if section_number in seen_numbers or section_number < last_number:
            continue
        seen_numbers.add(section_number)
        last_number = section_number
        filtered.append((page_number, section_number, title))

    if len(filtered) < 2:
        return []

    sequential_pairs = sum(
        1
        for index in range(len(filtered) - 1)
        if filtered[index + 1][1] == filtered[index][1] + 1
    )
    if sequential_pairs < 1:
        return []

    confidence = 86 if sequential_pairs >= 2 else 83
    return [
        _ChapterBoundary(title=title, start_page=page_number, confidence=confidence)
        for page_number, _section_number, title in filtered
    ]


def _is_noise_heading(title: str) -> bool:
    lowered = title.lower()
    noise = {"contents", "table of contents", "index", "references", "bibliography", "glossary"}
    return lowered in noise or lowered.startswith("page ")


def _is_non_chapter_outline_title(title: str) -> bool:
    lowered = _clean_title(title).lower()
    return lowered in _NON_CHAPTER_OUTLINE_TITLES or bool(_NON_CHAPTER_OUTLINE_PATTERN.match(lowered))


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
