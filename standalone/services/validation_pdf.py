from io import BytesIO

import qrcode
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

from standalone.models import PracticeAttempt, QuestionBankItem, ValidationBooking, ValidationPack, ValidationSubmission
from standalone.services.validation_flow import _shuffle_options, get_or_create_official_attempt


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
    font_name: str = "Helvetica",
    font_size: int = 11,
    leading: float = 5.5 * mm,
) -> float:
    pdf.setFont(font_name, font_size)
    for line in simpleSplit(str(text or ""), font_name, font_size, width):
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
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=0)
    width, height = A4
    left = 18 * mm
    right = width - (18 * mm)
    printable_width = right - left
    top = height - (20 * mm)
    bottom = 18 * mm
    line_gap = 5.5 * mm
    paragraph_gap = 2.5 * mm
    printable_questions = [question for question in questions if question.question_type != QuestionBankItem.QuestionType.WAQ]

    if not printable_questions:
        raise ValueError("No printable questions are available for this validation practice attempt.")

    answer_rows: list[tuple[int, str]] = []
    y = top

    pdf.setTitle(f"Validation practice - {title}")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(left, y, title)
    y -= 9 * mm
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(left, y, "Printable practice validation")
    y -= 7 * mm
    pdf.setFont("Helvetica", 11)
    pdf.drawString(left, y, owner_label)
    y -= 6 * mm
    pdf.drawString(left, y, f"Generated: {timezone.localtime(timezone.now()):%d %b %Y %H:%M}")
    y -= 6 * mm
    pdf.drawString(left, y, "Written-answer questions are excluded from this printable version.")
    y -= 10 * mm

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

        estimated_lines = len(simpleSplit(question.stem, "Helvetica", 11, printable_width))
        estimated_lines += sum(len(simpleSplit(f"{_option_letter(option_index)}. {option}", "Helvetica", 10, printable_width - (6 * mm))) for option_index, option in enumerate(options))
        estimated_lines += 5
        estimated_height = estimated_lines * line_gap
        if y - estimated_height < bottom:
            pdf.showPage()
            y = top

        pdf.setFont("Helvetica-Bold", 12)
        header = f"{index}. {question.question_type_label()}"
        if question.is_multiple_answer():
            header += " (Select all that apply)"
        pdf.drawString(left, y, header)
        y -= 6 * mm

        if question.block_id and question.block:
            pdf.setFont("Helvetica", 9)
            pdf.drawString(left, y, f"Block: {question.block.title}")
            y -= 4.5 * mm
        if question.learning_objective_id and question.learning_objective:
            y = _draw_wrapped_lines(
                pdf,
                f"Objective: {question.learning_objective.text}",
                x=left,
                y=y,
                width=printable_width,
                font_name="Helvetica",
                font_size=9,
                leading=4.5 * mm,
            )
            y -= paragraph_gap

        y = _draw_wrapped_lines(
            pdf,
            question.stem,
            x=left,
            y=y,
            width=printable_width,
            font_name="Helvetica",
            font_size=11,
            leading=line_gap,
        )
        y -= paragraph_gap

        if question.code_snippet:
            pdf.setFont("Courier", 9)
            y = _draw_wrapped_lines(
                pdf,
                question.code_snippet,
                x=left + (4 * mm),
                y=y,
                width=printable_width - (4 * mm),
                font_name="Courier",
                font_size=9,
                leading=4.5 * mm,
            )
            y -= paragraph_gap

        pdf.setFont("Helvetica", 10)
        for option_index, option in enumerate(options):
            y = _draw_wrapped_lines(
                pdf,
                f"{_option_letter(option_index)}. {option}",
                x=left + (4 * mm),
                y=y,
                width=printable_width - (4 * mm),
                font_name="Helvetica",
                font_size=10,
                leading=line_gap,
            )
            y -= 1.5 * mm

        y -= 6 * mm

    pdf.showPage()
    y = top
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(left, y, "Answer key")
    y -= 9 * mm
    pdf.setFont("Helvetica", 11)
    pdf.drawString(left, y, "Use this final page to mark the printable practice validation.")
    y -= 8 * mm

    for index, answer_text in answer_rows:
        if y < bottom:
            pdf.showPage()
            y = top
            pdf.setFont("Helvetica-Bold", 16)
            pdf.drawString(left, y, "Answer key")
            y -= 9 * mm
            pdf.setFont("Helvetica", 11)
        y = _draw_wrapped_lines(
            pdf,
            f"{index}. {answer_text}",
            x=left,
            y=y,
            width=printable_width,
            font_name="Helvetica",
            font_size=11,
            leading=line_gap,
        )
        y -= 1.5 * mm

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
