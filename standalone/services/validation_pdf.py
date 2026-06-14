from io import BytesIO

import qrcode
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from standalone.models import ValidationBooking, ValidationPack, ValidationSubmission


def build_validation_pack_pdf(pack: ValidationPack, bookings: list[ValidationBooking]) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    for booking in bookings:
        submission, _ = ValidationSubmission.objects.get_or_create(booking=booking)
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
        questions = booking.event.course.question_bank_items.filter(
            bank_type="validation",
            status="approved",
        )[: pack.event.question_count]
        for index, question in enumerate(questions, start=1):
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
