from io import BytesIO
from pathlib import Path
import re

import qrcode
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from standalone.models import PracticeAttempt, QuestionBankItem, ValidationBooking, ValidationPack, ValidationSubmission
from standalone.services.validation_flow import _shuffle_options, get_or_create_official_attempt


PRINT_FONT = "MCQAnchorPrint"
PRINT_FONT_BOLD = "MCQAnchorPrintBold"
PRINT_FONT_MONO = "MCQAnchorPrintMono"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGO_PATH = PROJECT_ROOT / "website" / "static" / "website" / "images" / "mcq-anchor-logo.png"
SUPERSCRIPT_TRANSLATION = str.maketrans({
    "-": "⁻",
    "+": "⁺",
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
})
SCIENTIFIC_NOTATION_RE = re.compile(r"(?<![\w.])([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*[eE]\s*([+-]?\d+)(?![\w])")
CARET_EXPONENT_RE = re.compile(r"\^([+-]?\d+)")
LATEX_FRACTION_RE = re.compile(r"(?:\\?frac|rac)\{([^{}]+)\}\{([^{}]+)\}")
LATEX_INLINE_RE = re.compile(r"\$([^$]+)\$")
SUBSCRIPT_RE = re.compile(r"_([A-Za-z0-9+-])")
SUBSCRIPT_MAP = {
    "0": "₀",
    "1": "₁",
    "2": "₂",
    "3": "₃",
    "4": "₄",
    "5": "₅",
    "6": "₆",
    "7": "₇",
    "8": "₈",
    "9": "₉",
    "+": "₊",
    "-": "₋",
    "a": "ₐ",
    "e": "ₑ",
    "h": "ₕ",
    "i": "ᵢ",
    "j": "ⱼ",
    "k": "ₖ",
    "l": "ₗ",
    "m": "ₘ",
    "n": "ₙ",
    "o": "ₒ",
    "p": "ₚ",
    "r": "ᵣ",
    "s": "ₛ",
    "t": "ₜ",
    "u": "ᵤ",
    "v": "ᵥ",
    "x": "ₓ",
}


def _register_print_fonts() -> None:
    if PRINT_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(PRINT_FONT, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    if PRINT_FONT_BOLD not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(PRINT_FONT_BOLD, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    if PRINT_FONT_MONO not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(PRINT_FONT_MONO, "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"))


def _superscript_exponent(exponent: str) -> str:
    cleaned = str(exponent or "").replace(" ", "")
    try:
        cleaned = str(int(cleaned))
    except (TypeError, ValueError):
        pass
    return cleaned.translate(SUPERSCRIPT_TRANSLATION)


def _subscript_token(token: str) -> str:
    cleaned = str(token or "")
    lowered = cleaned.lower()
    if any(char not in SUBSCRIPT_MAP for char in lowered):
        return f"_{cleaned}"
    translated = "".join(SUBSCRIPT_MAP[char] for char in lowered)
    return translated


def _format_latexish_text(text: str) -> str:
    formatted = str(text or "")
    while True:
        updated = LATEX_FRACTION_RE.sub(r"(\1)/(\2)", formatted)
        if updated == formatted:
            break
        formatted = updated
    formatted = LATEX_INLINE_RE.sub(r"\1", formatted)
    replacements = {
        r"\times": "×",
        r"\cdot": "·",
        r"\propto": "∝",
        r"\left": "",
        r"\right": "",
        r"\,": " ",
        r"\ ": " ",
    }
    for source, target in replacements.items():
        formatted = formatted.replace(source, target)
    formatted = formatted.replace("{", "").replace("}", "")
    formatted = SUBSCRIPT_RE.sub(lambda match: _subscript_token(match.group(1)), formatted)
    return formatted


def _format_print_text(text: str) -> str:
    formatted = _format_latexish_text(text)
    formatted = formatted.replace("−", "-").replace("–", "-")
    formatted = SCIENTIFIC_NOTATION_RE.sub(
        lambda match: f"{match.group(1)} × 10{_superscript_exponent(match.group(2))}",
        formatted,
    )
    formatted = CARET_EXPONENT_RE.sub(lambda match: _superscript_exponent(match.group(1)), formatted)
    return formatted


def _draw_print_page_one_header(pdf: canvas.Canvas, *, title: str, left: float, top: float) -> float:
    y = top
    pdf.setFont(PRINT_FONT_BOLD, 18)
    pdf.drawString(left, y, _format_print_text(title))
    y -= 8 * mm
    pdf.setFont(PRINT_FONT_BOLD, 13)
    pdf.drawString(left, y, "Printable practice validation")
    return y - (9 * mm)


def _draw_print_footer_logo(pdf: canvas.Canvas, *, right: float, bottom: float) -> None:
    logo_reader = ImageReader(str(LOGO_PATH))
    logo_width = 24 * mm
    logo_height = logo_width * 203.0 / 789.0
    logo_x = right - logo_width
    logo_y = bottom - (2 * mm)
    pdf.drawImage(logo_reader, logo_x, logo_y, width=logo_width, height=logo_height, mask="auto")


def build_validation_pack_pdf(pack: ValidationPack, bookings: list[ValidationBooking]) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=0)
    width, height = A4

    for booking in bookings:
        attempt = get_or_create_official_attempt(booking.enrollment, booking.event, booking=booking)
        submission, _ = ValidationSubmission.objects.get_or_create(booking=booking, defaults={"attempt": attempt})
        if submission.attempt_id != attempt.pk:
            submission.attempt = attempt
            submission.save(update_fields=["attempt", "updated_at"])
        qr_image = qrcode.make(str(submission.qr_token))
        qr_buffer = BytesIO()
        qr_image.save(qr_buffer, format="PNG")
        qr_buffer.seek(0)
        qr_reader = ImageReader(qr_buffer)

        pdf.setTitle(f"Validation pack - {pack.event.course.title}")
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(20 * mm, height - 25 * mm, pack.event.course.title)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(20 * mm, height - 35 * mm, f"Student: {booking.enrollment.student.get_full_name() or booking.enrollment.student.email}")
        pdf.drawString(20 * mm, height - 43 * mm, f"Session: {pack.event.starts_at:%d %b %Y %H:%M} at {pack.event.location}")
        pdf.drawString(20 * mm, height - 51 * mm, f"QR token: {submission.qr_token}")
        pdf.drawImage(qr_reader, width - 60 * mm, height - 65 * mm, width=35 * mm, height=35 * mm)

        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(20 * mm, height - 72 * mm, "Validation questions")
        pdf.setFont("Helvetica", 11)
        y_position = height - 82 * mm
        questions = (
            attempt.attempt_questions.select_related("question")
            .order_by("order", "created_at")
        )
        for index, attempt_question in enumerate(questions, start=1):
            question = attempt_question.question
            pdf.drawString(20 * mm, y_position, f"{index}. {question.stem[:85]}")
            y_position -= 9 * mm
            if y_position < 55 * mm:
                break

        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(20 * mm, 45 * mm, "Bubble sheet")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(20 * mm, 37 * mm, "Mark one option per question. Teacher scans this sheet after the session.")
        for index in range(1, min(pack.event.question_count, 12) + 1):
            pdf.drawString(20 * mm, (35 - index * 2.2) * mm, f"Q{index}:  A   B   C   D   E")

        pdf.drawString(20 * mm, 8 * mm, f"Generated {timezone.now():%d %b %Y %H:%M}")
        pdf.showPage()

    pdf.save()
    buffer.seek(0)
    return buffer.read()


def _draw_wrapped_lines(
    pdf: canvas.Canvas,
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    font_name: str = PRINT_FONT,
    font_size: int = 11,
    leading: float = 5.5 * mm,
) -> float:
    formatted = _format_print_text(text)
    pdf.setFont(font_name, font_size)
    for line in simpleSplit(formatted, font_name, font_size, width):
        pdf.drawString(x, y, line)
        y -= leading
    return y


def _option_letter(index: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return letters[index] if 0 <= index < len(letters) else f"Option {index + 1}"


def _build_validation_practice_pdf_document(
    *,
    title: str,
    owner_label: str,
    questions: list[QuestionBankItem],
    seed_key: str,
) -> bytes:
    _register_print_fonts()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=0)
    width, height = A4
    left = 18 * mm
    right = width - (18 * mm)
    printable_width = right - left
    top = height - (16 * mm)
    bottom = 18 * mm
    line_gap = 5.5 * mm
    paragraph_gap = 2.5 * mm
    printable_questions = [question for question in questions if question.question_type != QuestionBankItem.QuestionType.WAQ]

    if not printable_questions:
        raise ValueError("No printable questions are available for this validation practice attempt.")

    answer_rows: list[tuple[int, str]] = []
    y = _draw_print_page_one_header(pdf, title=title, left=left, top=top)
    pdf.setTitle(f"Validation practice - {title}")

    for index, question in enumerate(printable_questions, start=1):
        options = _shuffle_options(question.all_answer_options(), seed_key, question.pk)
        option_labels = {_option_letter(option_index): option for option_index, option in enumerate(options)}
        correct_letters = [
            label
            for label, option_text in option_labels.items()
            if option_text in set(question.correct_answers())
        ]
        answer_text = ", ".join(correct_letters) if correct_letters else question.correct_answer
        answer_rows.append((index, answer_text))

        question_text = f"{index}. {question.stem}"
        if question.is_multiple_answer():
            question_text += " (Select all that apply.)"

        estimated_lines = len(simpleSplit(_format_print_text(question_text), PRINT_FONT, 11, printable_width))
        if question.block_id and question.block:
            estimated_lines += len(
                simpleSplit(
                    _format_print_text(f"Block: {question.block.title}"),
                    PRINT_FONT,
                    9,
                    printable_width,
                )
            )
        estimated_lines += sum(
            len(simpleSplit(_format_print_text(f"{_option_letter(option_index)}. {option}"), PRINT_FONT, 10, printable_width - (6 * mm)))
            for option_index, option in enumerate(options)
        )
        estimated_lines += 4
        estimated_height = estimated_lines * line_gap
        if y - estimated_height < bottom:
            _draw_print_footer_logo(pdf, right=right, bottom=bottom)
            pdf.showPage()
            y = top

        pdf.setFont(PRINT_FONT_BOLD, 12)
        y = _draw_wrapped_lines(
            pdf,
            question_text,
            x=left,
            y=y,
            width=printable_width,
            font_name=PRINT_FONT,
            font_size=11,
            leading=line_gap,
        )
        y -= paragraph_gap

        if question.block_id and question.block:
            y = _draw_wrapped_lines(
                pdf,
                f"Block: {question.block.title}",
                x=left,
                y=y,
                width=printable_width,
                font_name=PRINT_FONT,
                font_size=9,
                leading=4.5 * mm,
            )
            y -= paragraph_gap

        if question.code_snippet:
            y = _draw_wrapped_lines(
                pdf,
                question.code_snippet,
                x=left + (4 * mm),
                y=y,
                width=printable_width - (4 * mm),
                font_name=PRINT_FONT_MONO,
                font_size=9,
                leading=4.5 * mm,
            )
            y -= paragraph_gap

        pdf.setFont(PRINT_FONT, 10)
        for option_index, option in enumerate(options):
            y = _draw_wrapped_lines(
                pdf,
                f"{_option_letter(option_index)}. {option}",
                x=left + (4 * mm),
                y=y,
                width=printable_width - (4 * mm),
                font_name=PRINT_FONT,
                font_size=10,
                leading=line_gap,
            )
            y -= 1.5 * mm

        y -= 6 * mm

    _draw_print_footer_logo(pdf, right=right, bottom=bottom)
    pdf.showPage()
    y = top
    pdf.setFont(PRINT_FONT_BOLD, 16)
    pdf.drawString(left, y, "Answer key")
    y -= 9 * mm
    pdf.setFont(PRINT_FONT, 11)
    pdf.drawString(left, y, "Use this final page to mark the printable practice validation.")
    y -= 8 * mm

    for index, answer_text in answer_rows:
        if y < bottom:
            _draw_print_footer_logo(pdf, right=right, bottom=bottom)
            pdf.showPage()
            y = top
            pdf.setFont(PRINT_FONT_BOLD, 16)
            pdf.drawString(left, y, "Answer key")
            y -= 9 * mm
            pdf.setFont(PRINT_FONT, 11)
        y = _draw_wrapped_lines(
            pdf,
            f"{index}. {answer_text}",
            x=left,
            y=y,
            width=printable_width,
            font_name=PRINT_FONT,
            font_size=11,
            leading=line_gap,
        )
        y -= 1.5 * mm

    _draw_print_footer_logo(pdf, right=right, bottom=bottom)
    pdf.save()
    buffer.seek(0)
    return buffer.read()


def build_validation_practice_pdf(attempt: PracticeAttempt) -> bytes:
    questions = [
        attempt_question.question
        for attempt_question in attempt.attempt_questions.select_related(
            "question",
            "question__block",
            "question__learning_objective",
        ).order_by("order", "created_at")
    ]
    return _build_validation_practice_pdf_document(
        title=attempt.enrollment.course.title,
        owner_label=f"Student: {attempt.enrollment.student.get_full_name() or attempt.enrollment.student.email}",
        questions=questions,
        seed_key=f"practice-validation:{attempt.pk}:course:{attempt.enrollment.course_id}",
    )


def build_preview_validation_practice_pdf(course_title: str, questions: list[QuestionBankItem], *, seed_key: str) -> bytes:
    return _build_validation_practice_pdf_document(
        title=course_title,
        owner_label="Student preview",
        questions=questions,
        seed_key=seed_key,
    )
