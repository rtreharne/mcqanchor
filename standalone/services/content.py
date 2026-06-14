import hashlib
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.conf import settings
from markdown import markdown
from openai import OpenAI
from openpyxl import load_workbook
from pypdf import PdfReader
from docx import Document
from pptx import Presentation

from standalone.models import ContentAsset, ContentChunk, LearningObjective


SUPPORTED_EXTENSIONS = {
    ".html",
    ".docx",
    ".pdf",
    ".txt",
    ".r",
    ".py",
    ".ipynb",
    ".rmd",
    ".md",
    ".pptx",
    ".xlsx",
}


def normalize_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _plain_text_from_markdown(text: str) -> str:
    return BeautifulSoup(markdown(text), "html.parser").get_text("\n")


def extract_text_from_asset(asset: ContentAsset) -> str:
    ext = asset.extension.lower()
    file_path = Path(asset.file.path)

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported content type: {ext}")

    if ext in {".txt", ".r", ".py"}:
        return normalize_text(file_path.read_text(encoding="utf-8", errors="ignore"))

    if ext in {".md", ".rmd"}:
        return normalize_text(_plain_text_from_markdown(file_path.read_text(encoding="utf-8", errors="ignore")))

    if ext == ".html":
        return normalize_text(BeautifulSoup(file_path.read_text(encoding="utf-8", errors="ignore"), "html.parser").get_text("\n"))

    if ext == ".ipynb":
        notebook = json.loads(file_path.read_text(encoding="utf-8", errors="ignore"))
        cells = []
        for cell in notebook.get("cells", []):
            source = "".join(cell.get("source", []))
            if cell.get("cell_type") == "markdown":
                cells.append(_plain_text_from_markdown(source))
            else:
                cells.append(source)
        return normalize_text("\n\n".join(cells))

    if ext == ".docx":
        document = Document(file_path)
        return normalize_text("\n".join(paragraph.text for paragraph in document.paragraphs))

    if ext == ".pdf":
        reader = PdfReader(str(file_path))
        return normalize_text("\n".join(page.extract_text() or "" for page in reader.pages))

    if ext == ".pptx":
        deck = Presentation(str(file_path))
        slides = []
        for slide in deck.slides:
            slide_lines = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text]
            if slide_lines:
                slides.append("\n".join(slide_lines))
        return normalize_text("\n\n".join(slides))

    if ext == ".xlsx":
        workbook = load_workbook(str(file_path), data_only=True)
        sheets = []
        for sheet in workbook.worksheets:
            rows = []
            for row in sheet.iter_rows(values_only=True):
                values = [str(cell).strip() for cell in row if cell not in (None, "")]
                if values:
                    rows.append(" | ".join(values))
            if rows:
                sheets.append(f"{sheet.title}\n" + "\n".join(rows))
        return normalize_text("\n\n".join(sheets))

    raise ValueError(f"Unsupported content type: {ext}")


def chunk_text(text: str, target_size: int = 1200) -> list[str]:
    paragraphs = [segment.strip() for segment in text.split("\n\n") if segment.strip()]
    if not paragraphs:
        return []

    chunks = []
    current = []
    current_len = 0
    for paragraph in paragraphs:
        if current and current_len + len(paragraph) > target_size:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += len(paragraph)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def derive_learning_objectives(text: str, max_items: int = 8) -> list[str]:
    candidates = []
    for line in text.splitlines():
        stripped = line.strip(" -*\t")
        if 40 <= len(stripped) <= 180 and stripped not in candidates:
            candidates.append(stripped)
        if len(candidates) >= max_items:
            break

    if not candidates:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for sentence in sentences:
            stripped = sentence.strip()
            if 5 <= len(stripped) <= 160:
                candidates.append(stripped)
            if len(candidates) >= max_items:
                break

    return candidates[:max_items]


def generate_embeddings(texts: list[str]) -> list[list[float]]:
    if not settings.OPENAI_API_KEY:
        return [[] for _ in texts]
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.embeddings.create(model=settings.OPENAI_EMBEDDING_MODEL, input=texts)
        return [item.embedding for item in response.data]
    except Exception:  # noqa: BLE001
        return [[] for _ in texts]


def ingest_content_asset(asset: ContentAsset) -> None:
    extracted_text = extract_text_from_asset(asset)
    asset.extracted_text = extracted_text
    asset.processing_status = ContentAsset.ProcessingStatus.PROCESSED
    asset.processing_error = ""
    asset.save(update_fields=["extracted_text", "processing_status", "processing_error", "updated_at"])

    asset.chunks.all().delete()
    asset.learning_objectives.all().delete()

    chunks = chunk_text(extracted_text)
    embeddings = generate_embeddings(chunks) if chunks else []
    for index, chunk in enumerate(chunks, start=1):
        ContentChunk.objects.create(
            asset=asset,
            course=asset.block.course,
            block=asset.block,
            ordinal=index,
            text=chunk,
            token_count=max(1, len(chunk.split())),
            embedding_model=settings.OPENAI_EMBEDDING_MODEL if embeddings and embeddings[index - 1] else "",
            embedding_vector=embeddings[index - 1] if embeddings else [],
            checksum=hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
        )

    objectives = derive_learning_objectives(extracted_text)
    for index, objective in enumerate(objectives, start=1):
        LearningObjective.objects.create(
            course=asset.block.course,
            block=asset.block,
            source_asset=asset,
            code=f"{asset.block.order}.{index}",
            text=objective,
        )
