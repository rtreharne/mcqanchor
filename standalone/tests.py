from datetime import timedelta
import io
import json
import re
import tempfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openai import OpenAIError
from unittest.mock import patch

from standalone.forms import CourseForm, ValidationEventForm
from standalone.models import (
    BlockConfig,
    ContentAsset,
    ContentChunk,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseImport,
    CourseImportChapter,
    CourseMagicLink,
    Enrollment,
    EnrollmentQuestionState,
    LearningObjective,
    PracticeAttempt,
    PracticeAttemptQuestion,
    PracticeMessage,
    QuestionBankItem,
    QuestionFlag,
    StudentInvitation,
    TeacherInvitation,
    ValidationAttempt,
    ValidationAttemptQuestion,
    ValidationBooking,
    ValidationEvent,
)
from standalone.tasks import run_block_creation_processing, run_block_regeneration, run_course_import_block_creation
from standalone.services.content import chunk_text, summarize_block_content
from standalone.services.pdf_import import analyze_pdf_chapters, _select_outline_items, _toc_boundaries_from_ocr
from standalone.services.preview import PREVIEW_SESSION_KEY
from standalone.services.numeric_questions import NumericQuestionValidationError, _evaluate_expression
from standalone.services.validation_flow import _pick_locked_questions, current_room_code
from standalone.services.questions import (
    QuestionGenerationError,
    _normalize_generated_payload,
    coding_signal_for_text,
    fallback_further_study_questions,
    further_study_questions_for_question,
    generate_question_pair_for_block,
)


User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class StandaloneFlowTests(TestCase):
    def setUp(self):
        self.internal = User.objects.create_user(
            username="internal",
            email="internal@example.com",
            password="password123",
            role=User.Role.INTERNAL,
            is_staff=True,
        )
        self.teacher = User.objects.create_user(
            username="teacher",
            email="teacher@example.com",
            password="password123",
            role=User.Role.TEACHER,
        )
        self.student = User.objects.create_user(
            username="student",
            email="student@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )

    def create_course(self):
        course = Course.objects.create(teacher=self.teacher, title="Cell Biology", slug="cell-biology", summary="Cells.")
        CourseConfig.objects.create(course=course)
        return course

    def create_preview_content_block(self, course, *, title="Week 1", order=1):
        block = CourseBlock.objects.create(course=course, title=title, summary=f"{title} summary", order=order)
        BlockConfig.objects.create(block=block)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile(f"{title.lower().replace(' ', '-')}.txt", b"Block notes", content_type="text/plain"),
            original_filename=f"{title.lower().replace(' ', '-')}.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=f"{title} focuses on membranes, transport, and signalling.",
        )
        objective = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code=f"{order}.1",
            text=f"Explain the key ideas in {title}",
        )
        chunk = ContentChunk.objects.create(
            asset=asset,
            course=course,
            block=block,
            ordinal=1,
            text=f"{title} explores membranes, transport, and signalling in detail.",
            token_count=10,
            checksum=f"{title.lower().replace(' ', '-')}-chunk",
        )
        return block, asset, objective, chunk

    def create_coding_content_block(self, course, *, extension=".py", text=None):
        code_text = text or "```python\ndef double(value):\n    return value * 2\n\nresult = double(4)\n```"
        block = CourseBlock.objects.create(course=course, title="Coding", summary="Coding summary", order=1)
        BlockConfig.objects.create(block=block)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile(f"code{extension}", code_text.encode("utf-8"), content_type="text/plain"),
            original_filename=f"code{extension}",
            extension=extension,
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=code_text,
        )
        objective = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Explain how code structure affects program behavior",
        )
        chunk = ContentChunk.objects.create(
            asset=asset,
            course=course,
            block=block,
            ordinal=1,
            text=code_text,
            token_count=max(1, len(code_text.split())),
            checksum="coding-chunk",
        )
        return block, asset, objective, chunk

    def build_pdf_upload(self, pages, filename="book.pdf"):
        from reportlab.pdfgen import canvas

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer)
        for page_lines in pages:
            y = 790
            for line in page_lines:
                pdf.drawString(72, y, line)
                y -= 18
            pdf.showPage()
        pdf.save()
        return SimpleUploadedFile(filename, buffer.getvalue(), content_type="application/pdf")

    def test_chunk_text_splits_single_long_paragraph_to_target_size(self):
        text = " ".join(["word"] * 700)
        chunks = chunk_text(text, target_size=1200)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1200 for chunk in chunks))

    def test_chunk_text_splits_oversized_paragraphs_in_mixed_content(self):
        text = "Short intro.\n\n" + (" ".join(["alpha"] * 260)) + "\n\n" + (" ".join(["beta"] * 260))
        chunks = chunk_text(text, target_size=1200)

        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 1200 for chunk in chunks))

    @override_settings(OPENAI_API_KEY="test-key")
    def test_summarize_block_content_uses_chunked_openai_pipeline_for_large_uploads(self):
        long_text = "\n\n".join(
            [
                "Alpha topic sentence. " * 180,
                "Beta topic sentence. " * 180,
                "Gamma topic sentence. " * 180,
            ]
        )
        prompts = []

        class DummyResponse:
            def __init__(self, output_text):
                self.output_text = output_text

        def fake_create(*, model, input):
            prompt = input[1]["content"][0]["text"]
            prompts.append(prompt)
            if "Section summaries:" in prompt:
                return DummyResponse(
                    '{"summary":"Combined uploaded material summary.","learning_objectives":["Explain alpha topic","Explain beta topic","Explain gamma topic"]}'
                )
            if "Alpha topic sentence" in prompt:
                return DummyResponse('{"summary":"Alpha summary.","learning_objectives":["Explain alpha topic"]}')
            if "Beta topic sentence" in prompt:
                return DummyResponse('{"summary":"Beta summary.","learning_objectives":["Explain beta topic"]}')
            return DummyResponse('{"summary":"Gamma summary.","learning_objectives":["Explain gamma topic"]}')

        with patch("standalone.services.content.OpenAI") as mock_client:
            mock_client.return_value.responses.create.side_effect = fake_create
            summary, objectives = summarize_block_content(long_text, max_items=6)

        self.assertEqual(summary, "Combined uploaded material summary.")
        self.assertEqual(objectives, ["Explain alpha topic", "Explain beta topic", "Explain gamma topic"])
        self.assertGreaterEqual(len(prompts), 4)
        self.assertTrue(any("Section summaries:" in prompt for prompt in prompts))
        self.assertFalse(any("Block title:" in prompt for prompt in prompts))

    @override_settings(OPENAI_API_KEY="test-key")
    def test_summarize_block_content_removes_content_meta_prefixes(self):
        class DummyResponse:
            output_text = (
                '{"summary":"The teaching content provides an overview of membrane transport.",'
                '"learning_objectives":["Explain membrane transport"]}'
            )

        with patch("standalone.services.content.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            summary, _objectives = summarize_block_content("Membrane transport regulates cell exchange.", max_items=3)

        self.assertEqual(summary, "Membrane transport.")

    @override_settings(OPENAI_API_KEY="")
    def test_pdf_import_detects_chapters_from_page_headings(self):
        upload = self.build_pdf_upload(
            [
                ["Chapter 1 Foundations", "Cells are the basic unit of life."],
                ["Chapter 2 Membranes", "Membranes regulate transport and signalling."],
            ]
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf_file:
            pdf_file.write(upload.read())
            pdf_file.flush()

            chapters = analyze_pdf_chapters(pdf_file.name)

        self.assertEqual([chapter.title for chapter in chapters], ["Chapter 1: Foundations", "Chapter 2: Membranes"])
        self.assertEqual(chapters[0].start_page, 1)
        self.assertEqual(chapters[0].end_page, 1)
        self.assertIn("Cells are the basic unit of life", chapters[0].extracted_text)

    @override_settings(OPENAI_API_KEY="")
    def test_pdf_import_falls_back_to_single_chapter_without_headings(self):
        upload = self.build_pdf_upload([["Foundations of cell biology", "No chapter heading is present."]])
        with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf_file:
            pdf_file.write(upload.read())
            pdf_file.flush()

            chapters = analyze_pdf_chapters(pdf_file.name)

        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0].title, "Imported PDF")
        self.assertEqual(chapters[0].start_page, 1)
        self.assertEqual(chapters[0].end_page, 1)

    def test_course_form_auto_generates_slug_and_defaults(self):
        form = CourseForm(data={"title": "  Cell Biology 101  "})

        self.assertTrue(form.is_valid(), form.errors)
        course = form.save(commit=False)

        self.assertEqual(course.title, "Cell Biology 101")
        self.assertEqual(course.slug, "cell-biology-101")
        self.assertEqual(course.summary, "")
        self.assertTrue(course.is_active)

    def test_course_form_generates_unique_slug(self):
        Course.objects.create(
            teacher=self.teacher,
            title="Cell Biology 101",
            slug="cell-biology-101",
            summary="Existing summary",
            is_active=False,
        )
        form = CourseForm(data={"title": "Cell Biology 101"})

        self.assertTrue(form.is_valid(), form.errors)
        course = form.save(commit=False)

        self.assertEqual(course.slug, "cell-biology-101-2")
        self.assertEqual(course.summary, "")
        self.assertTrue(course.is_active)

    def test_validation_event_form_requires_minimum_fifty_minute_session(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        starts_at = timezone.now() + timedelta(days=1)
        ends_at = starts_at + timedelta(minutes=49)

        form = ValidationEventForm(
            data={
                "title": "Too short validation",
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M"),
                "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M"),
                "location": "Validation Centre",
                "capacity": 30,
                "late_booking_cutoff_minutes": 20,
                "question_count": 10,
                "time_limit_minutes": 20,
                "audit_prompt_count": 2,
                "feedback_release_mode": ValidationEvent.FeedbackReleaseMode.IMMEDIATE,
            },
            course=course,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("at least 50 minutes", form.non_field_errors()[0])

    def test_validation_event_form_hides_title_and_generates_internal_session_title(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        starts_at = timezone.now() + timedelta(days=1)
        ends_at = starts_at + timedelta(hours=1)

        form = ValidationEventForm(
            data={
                "starts_at": starts_at.strftime("%Y-%m-%dT%H:%M"),
                "ends_at": ends_at.strftime("%Y-%m-%dT%H:%M"),
                "location": "Validation Centre",
                "capacity": 30,
                "late_booking_cutoff_minutes": 20,
                "question_count": 10,
                "time_limit_minutes": 20,
                "audit_prompt_count": 2,
                "feedback_release_mode": ValidationEvent.FeedbackReleaseMode.IMMEDIATE,
            },
            course=course,
        )

        self.assertNotIn("title", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        event = form.save(commit=False)
        self.assertTrue(event.title.startswith("Validation session "))

    def test_coding_question_payload_removes_fenced_code_from_stem(self):
        payload = _normalize_generated_payload(
            {
                "question_type": "mcq",
                "stem": 'Consider this R snippet: ```r\nlibrary(tibble)\ndf <- data.frame(x = 1:5)\n``` What does it print?',
                "correct_answers": ["A tibble-style print output."],
                "distractors": ["It opens a file.", "It performs a network call.", "It deletes a column."],
                "further_study_questions": ["Why does tibble printing differ?"],
                "explanation": "Tibbles print differently.",
                "difficulty": "core",
                "is_coding_question": True,
                "coding_language": "r",
                "coding_question_kind": "comprehension",
                "code_snippet": "library(tibble)\ndf <- data.frame(x = 1:5)",
            },
            QuestionBankItem.QuestionType.MCQ,
            distractor_count=3,
        )

        self.assertNotIn("```", payload["stem"])
        self.assertEqual(payload["stem"], "Consider this R snippet: What does it print?")

    def test_pdf_import_outline_selection_prefers_chapter_depth_below_part_headings(self):
        items = _select_outline_items(
            [
                ("Preface", 8, 0),
                ("Acknowledgements", 10, 0),
                ("Core Curriculum", 11, 0),
                ("Electives", 168, 0),
                ("References", 379, 0),
                ("Appendices", 380, 0),
                ("Basics", 12, 1),
                ("Tibbles", 18, 1),
                ("Data Manipulation with dplyr", 28, 1),
                ("Data Visualization with ggplot2", 72, 1),
                ("Refresher: Tidy Exploratory Data Analysis", 114, 1),
                ("Reproducible Reporting with RMarkdown", 156, 1),
                ("RStudio", 12, 2),
                ("Basic operations", 13, 2),
                ("Functions", 15, 2),
                ("Tibbles (data frames)", 17, 2),
            ]
        )

        self.assertEqual(
            [title for title, _page_number, _depth in items],
            [
                "Basics",
                "Tibbles",
                "Data Manipulation with dplyr",
                "Data Visualization with ggplot2",
                "Refresher: Tidy Exploratory Data Analysis",
                "Reproducible Reporting with RMarkdown",
            ],
        )

    def test_pdf_import_detects_scanned_book_chapters_from_ocr_contents(self):
        page_map = {
            6: "Contents\nModule 1 Development of practical skills in physics 2\nModule 2 Foundations of physics 6\nChapter 2 8\nChapter 3 Motion 22\nChapter 4 Forces in action 46",
            15: "MODULE 1\nDevelopment of practical skills in physics",
            19: "MODULE 2\nFoundations of physics",
            34: "3 3.1 Distance and speed\nSpecification reference: 3.1.1",
        }

        boundaries = _toc_boundaries_from_ocr(page_map, page_count=660)

        self.assertEqual(
            [(boundary.title, boundary.start_page) for boundary in boundaries],
            [("Chapter 2", 20), ("Chapter 3: Motion", 34), ("Chapter 4: Forces in action", 58)],
        )

    def test_coding_signal_detects_common_languages(self):
        cases = [
            ("python", "```python\ndef total(values):\n    return sum(values)\n```", ".txt"),
            ("r", "x <- c(1, 2, 3)\nmean(x)", ".r"),
            ("java", "public class Demo {\n  public static void main(String[] args) {\n    System.out.println(\"Hi\");\n  }\n}", ".txt"),
            ("matlab", "function y = squareValue(x)\ny = x.^2;\nend", ".m"),
        ]

        for expected_language, text, extension in cases:
            with self.subTest(expected_language):
                signal = coding_signal_for_text(text, extension=extension)
                self.assertEqual(signal["language"], expected_language)
                self.assertTrue(signal["snippet"])

        self.assertEqual(coding_signal_for_text("Cell membranes regulate transport.")["language"], "")

    def test_teacher_invitation_activation_creates_teacher_account(self):
        self.client.force_login(self.internal)
        response = self.client.post(reverse("standalone:teacher_invite"), {"email": "newteacher@example.com"})
        self.assertEqual(response.status_code, 302)
        invitation = TeacherInvitation.objects.get(email="newteacher@example.com")
        self.assertEqual(len(mail.outbox), 1)

        activation_response = self.client.post(
            reverse("standalone:teacher_activate", args=[invitation.token]),
            {
                "full_name": "New Teacher",
                "password1": "safe-pass-123",
                "password2": "safe-pass-123",
                "institution": "Anchor University",
            },
        )
        self.assertEqual(activation_response.status_code, 302)
        created_user = User.objects.get(email="newteacher@example.com")
        self.assertEqual(created_user.role, User.Role.TEACHER)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

    def test_teacher_can_log_in_with_email_address(self):
        response = self.client.post(
            reverse("standalone:login"),
            {
                "username": "teacher@example.com",
                "password": "password123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("standalone:dashboard"))

    def test_course_config_edit_updates_question_type_ratios(self):
        course = self.create_course()
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:course_config", args=[course.pk]),
            {
                "self_enrol_enabled": "on",
                "self_enrol_domain": "",
                "practice_weight": 80,
                "validation_weight": 20,
                "mastery_weight": 40,
                "coverage_weight": 30,
                "engagement_weight": 20,
                "target_weight": 10,
                "distractor_count": 3,
                "numeric_ratio_percent": 25,
                "maq_ratio_percent": 35,
                "waq_ratio_percent": 15,
                "coding_question_ratio_percent": 50,
                "advanced_question_start_percent": 40,
                "revalidation_attempts": 0,
                "show_validation_feedback_immediately": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        course.config.refresh_from_db()
        self.assertEqual(course.config.numeric_ratio_percent, 25)
        self.assertEqual(course.config.maq_ratio_percent, 35)
        self.assertEqual(course.config.waq_ratio_percent, 15)
        self.assertEqual(course.config.coding_question_ratio_percent, 50)
        self.assertEqual(course.config.advanced_question_start_percent, 40)

    def test_course_config_field_autosave_updates_and_normalises_values(self):
        course = self.create_course()
        self.client.force_login(self.teacher)

        domain_response = self.client.post(
            reverse("standalone:update_course_config_field", args=[course.pk, "self_enrol_domain"]),
            {"self_enrol_domain": "@Example.AC.UK"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(domain_response.status_code, 200)
        course.config.refresh_from_db()
        self.assertEqual(course.config.self_enrol_domain, "example.ac.uk")

        numeric_response = self.client.post(
            reverse("standalone:update_course_config_field", args=[course.pk, "numeric_ratio_percent"]),
            {"numeric_ratio_percent": "30"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(numeric_response.status_code, 200)
        course.config.refresh_from_db()
        self.assertEqual(course.config.numeric_ratio_percent, 30)

        checkbox_response = self.client.post(
            reverse("standalone:update_course_config_field", args=[course.pk, "show_validation_feedback_immediately"]),
            {},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(checkbox_response.status_code, 200)
        course.config.refresh_from_db()
        self.assertFalse(course.config.show_validation_feedback_immediately)

    def test_question_bank_item_numeric_type_enforces_is_numerical(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)

        numeric_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Calculate the speed for a body travelling 20 m in 4 s.",
            question_type=QuestionBankItem.QuestionType.NUM,
            correct_answer="5 m/s",
            distractors=["4 m/s", "16 m/s", "10 m/s"],
            explanation="Use \\(v = d/t\\).",
            question_hash="numeric-enforces-flag",
            is_numerical=False,
            numeric_metadata={"script_version": "v1"},
        )

        self.assertTrue(numeric_question.is_numerical)

        numeric_question.question_type = QuestionBankItem.QuestionType.MCQ
        numeric_question.save()
        numeric_question.refresh_from_db()

        self.assertFalse(numeric_question.is_numerical)
        self.assertEqual(numeric_question.numeric_metadata, {})

    def test_teacher_can_delete_course_from_detail_page(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("delete-me.txt", b"Delete this file", content_type="text/plain"),
            original_filename="delete-me.txt",
            extension=".txt",
        )
        course_import = CourseImport.objects.create(
            course=course,
            uploaded_by=self.teacher,
            source_file=SimpleUploadedFile("delete-me.pdf", b"%PDF-1.4", content_type="application/pdf"),
            original_filename="delete-me.pdf",
        )
        asset_name = asset.file.name
        import_name = course_import.source_file.name
        self.client.force_login(self.teacher)

        detail_response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))
        self.assertContains(detail_response, reverse("standalone:course_delete", args=[course.pk]))
        delete_response = self.client.post(reverse("standalone:course_delete", args=[course.pk]))

        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(delete_response.url, reverse("standalone:teacher_dashboard"))
        self.assertFalse(Course.objects.filter(pk=course.pk).exists())
        self.assertFalse(asset.file.storage.exists(asset_name))
        self.assertFalse(course_import.source_file.storage.exists(import_name))

    def test_teacher_can_delete_validation_event_from_course_detail(self):
        course = self.create_course()
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Delete me",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Centre",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            late_booking_cutoff_minutes=20,
            question_count=10,
            time_limit_minutes=20,
        )
        self.client.force_login(self.teacher)

        detail_response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))
        self.assertContains(detail_response, reverse("standalone:validation_event_delete", args=[event.pk]), html=False)

        delete_response = self.client.post(reverse("standalone:validation_event_delete", args=[event.pk]))

        self.assertEqual(delete_response.status_code, 302)
        self.assertEqual(delete_response.url, reverse("standalone:course_detail", args=[course.pk]))
        self.assertFalse(ValidationEvent.objects.filter(pk=event.pk).exists())

    def test_teacher_dashboard_shows_validation_event_delete_action(self):
        course = self.create_course()
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Dashboard delete me",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Centre",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            late_booking_cutoff_minutes=20,
            question_count=10,
            time_limit_minutes=20,
        )
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:teacher_dashboard"))

        self.assertContains(response, "Validation sessions")
        self.assertContains(response, "Dashboard delete me")
        self.assertContains(response, reverse("standalone:validation_event_delete", args=[event.pk]), html=False)

    def test_teacher_cannot_delete_another_teachers_validation_event(self):
        other_teacher = User.objects.create_user(
            username="otherteacher-validation",
            email="otherteacher-validation@example.com",
            password="password123",
            role=User.Role.TEACHER,
        )
        course = Course.objects.create(teacher=other_teacher, title="Other Course", slug="other-course-validation")
        CourseConfig.objects.create(course=course)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=other_teacher,
            title="Protected event",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Centre",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            late_booking_cutoff_minutes=20,
            question_count=10,
            time_limit_minutes=20,
        )
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:validation_event_delete", args=[event.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ValidationEvent.objects.filter(pk=event.pk).exists())

    def test_validation_event_delete_requires_post(self):
        course = self.create_course()
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Delete me later",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Centre",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            late_booking_cutoff_minutes=20,
            question_count=10,
            time_limit_minutes=20,
        )
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:validation_event_delete", args=[event.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(ValidationEvent.objects.filter(pk=event.pk).exists())

    def test_validation_event_delete_is_blocked_after_student_submission(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Submitted validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="submitted-validation-delete-lock",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Locked validation event",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Centre",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            late_booking_cutoff_minutes=20,
            question_count=10,
            time_limit_minutes=20,
        )
        attempt = ValidationAttempt.objects.create(
            enrollment=enrollment,
            event=event,
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            expires_at=timezone.now() + timedelta(days=1, hours=2),
            feedback_release_mode=ValidationEvent.FeedbackReleaseMode.IMMEDIATE,
        )
        ValidationAttemptQuestion.objects.create(
            attempt=attempt,
            question=question,
            order=1,
            question_type=question.question_type,
            selected_answers=["A"],
            is_correct=True,
            answered_at=timezone.now(),
        )
        self.client.force_login(self.teacher)

        detail_response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))
        self.assertContains(detail_response, "Delete locked")
        self.assertNotContains(detail_response, reverse("standalone:validation_event_delete", args=[event.pk]), html=False)

        delete_response = self.client.post(reverse("standalone:validation_event_delete", args=[event.pk]), follow=True)

        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(ValidationEvent.objects.filter(pk=event.pk).exists())
        self.assertContains(delete_response, "cannot be deleted because a student has already submitted validation")

    def test_teacher_cannot_delete_another_teachers_course(self):
        other_teacher = User.objects.create_user(
            username="otherteacher",
            email="otherteacher@example.com",
            password="password123",
            role=User.Role.TEACHER,
        )
        course = Course.objects.create(teacher=other_teacher, title="Other Course", slug="other-course")
        CourseConfig.objects.create(course=course)
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:course_delete", args=[course.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_course_delete_requires_post(self):
        course = self.create_course()
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:course_delete", args=[course.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Course.objects.filter(pk=course.pk).exists())

    def test_coding_question_ratio_defaults_to_zero(self):
        course = self.create_course()

        self.assertEqual(course.config.coding_question_ratio_percent, 0)

    def test_question_coding_metadata_defaults_to_non_coding(self):
        course = self.create_course()
        block, _asset, objective, chunk = self.create_preview_content_block(course)

        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="What is membrane transport?",
            correct_answer="Movement across membranes.",
            distractors=["Unrelated option", "Another unrelated option", "A third unrelated option"],
            question_hash="non-coding-defaults",
        )

        self.assertFalse(question.is_coding_question)
        self.assertEqual(question.coding_language, "")
        self.assertEqual(question.coding_question_kind, "")
        self.assertEqual(question.code_snippet, "")

    def test_teacher_can_upload_pdf_for_course_import(self):
        course = self.create_course()
        self.client.force_login(self.teacher)
        upload = self.build_pdf_upload([["Chapter 1 Foundations", "Cells are the basic unit of life."]])

        with patch("standalone.views._queue_course_import_analysis") as mock_queue:
            response = self.client.post(reverse("standalone:course_import_upload", args=[course.pk]), {"source_file": upload})

        self.assertEqual(response.status_code, 302)
        course_import = CourseImport.objects.get(course=course)
        self.assertEqual(course_import.original_filename, "book.pdf")
        self.assertEqual(course_import.status, CourseImport.Status.UPLOADED)
        mock_queue.assert_called_once_with(course_import.pk)

    def test_course_import_upload_rejects_non_pdf(self):
        course = self.create_course()
        self.client.force_login(self.teacher)
        upload = SimpleUploadedFile("notes.txt", b"Chapter 1", content_type="text/plain")

        response = self.client.post(reverse("standalone:course_import_upload", args=[course.pk]), {"source_file": upload})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(CourseImport.objects.count(), 0)
        self.assertContains(response, "Please upload a PDF file.")

    def test_teacher_can_review_and_submit_selected_import_chapters(self):
        course = self.create_course()
        course_import = CourseImport.objects.create(
            course=course,
            uploaded_by=self.teacher,
            source_file=SimpleUploadedFile("book.pdf", b"PDF", content_type="application/pdf"),
            original_filename="book.pdf",
            status=CourseImport.Status.READY,
            progress=100,
        )
        chapter_one = CourseImportChapter.objects.create(
            course_import=course_import,
            title="Chapter 1: Foundations",
            order=1,
            start_page=1,
            end_page=2,
            extracted_text="Cells are the basic unit of life.",
        )
        CourseImportChapter.objects.create(
            course_import=course_import,
            title="Chapter 2: Membranes",
            order=2,
            start_page=3,
            end_page=4,
            extracted_text="Membranes regulate transport.",
        )
        self.client.force_login(self.teacher)

        get_response = self.client.get(reverse("standalone:course_import_review", args=[course_import.pk]))
        self.assertContains(get_response, "Chapter 1: Foundations")

        with patch("standalone.views._queue_course_import_block_creation") as mock_queue:
            post_response = self.client.post(
                reverse("standalone:course_import_review", args=[course_import.pk]),
                {"selected_chapters": [str(chapter_one.pk)]},
            )

        self.assertEqual(post_response.status_code, 302)
        mock_queue.assert_called_once_with(course_import.pk, [chapter_one.pk])

    @override_settings(OPENAI_API_KEY="")
    def test_course_import_block_creation_creates_blocks_for_selected_chapters_only(self):
        course = self.create_course()
        course_import = CourseImport.objects.create(
            course=course,
            uploaded_by=self.teacher,
            source_file=SimpleUploadedFile("book.pdf", b"PDF", content_type="application/pdf"),
            original_filename="book.pdf",
            status=CourseImport.Status.READY,
            progress=100,
        )
        chapter_one = CourseImportChapter.objects.create(
            course_import=course_import,
            title="Chapter 1: Foundations",
            order=1,
            start_page=1,
            end_page=2,
            extracted_text="Cells are the basic unit of life. Cells contain organelles and membranes.",
        )
        chapter_two = CourseImportChapter.objects.create(
            course_import=course_import,
            title="Chapter 2: Membranes",
            order=2,
            start_page=3,
            end_page=4,
            extracted_text="Membranes regulate transport.",
        )

        run_course_import_block_creation(course_import.pk, [chapter_one.pk])

        course_import.refresh_from_db()
        chapter_one.refresh_from_db()
        chapter_two.refresh_from_db()
        self.assertEqual(course_import.status, CourseImport.Status.COMPLETED)
        self.assertIsNotNone(chapter_one.created_block)
        self.assertIsNone(chapter_two.created_block)
        self.assertEqual(course.blocks.count(), 1)
        block = course.blocks.get()
        self.assertEqual(block.title, "Chapter 1: Foundations")
        asset = block.assets.get()
        self.assertEqual(asset.extension, ".txt")
        self.assertEqual(asset.processing_status, ContentAsset.ProcessingStatus.PROCESSED)
        self.assertIn("Cells contain organelles", asset.extracted_text)
        course.refresh_from_db()
        self.assertTrue(course.summary)

    def test_authenticated_user_visiting_login_redirects_to_dashboard(self):
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:login"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("standalone:dashboard"))

    def test_logout_requires_post_and_nav_uses_post_flow(self):
        self.client.force_login(self.teacher)
        get_response = self.client.get(reverse("standalone:logout"))
        self.assertEqual(get_response.status_code, 405)

        post_response = self.client.post(reverse("standalone:logout"))
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.url, reverse("website:home"))

    def test_app_nav_uses_mcq_anchor_logo(self):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:teacher_dashboard"))
        self.assertContains(response, 'alt="MCQ Anchor logo"', html=False)
        self.assertContains(response, "website/images/mcq-anchor-logo.png", html=False)
        self.assertContains(response, "Logged in as: teacher@example.com")
        self.assertContains(response, "Log out")
        self.assertNotContains(response, ">Dashboard<", html=False)

    def test_student_invitation_acceptance_creates_enrollment(self):
        course = self.create_course()
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_invite", args=[course.pk]), {"email": "invitee@example.com"})
        invitation = StudentInvitation.objects.get(email="invitee@example.com")

        response = self.client.post(
            reverse("standalone:student_activate", args=[invitation.token]),
            {
                "full_name": "Invited Student",
                "email": "invitee@example.com",
                "password1": "safe-pass-123",
                "password2": "safe-pass-123",
                "institution": "Anchor University",
            },
        )
        self.assertEqual(response.status_code, 302)
        enrolled_user = User.objects.get(email="invitee@example.com")
        self.assertTrue(Enrollment.objects.filter(course=course, student=enrolled_user).exists())

    def test_course_detail_has_dashboard_button(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Block notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Describe membrane structure.",
        )
        LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Describe membrane structure",
        )
        objective = block.learning_objectives.first()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))
        self.assertNotContains(response, 'class="eyebrow"', html=False)
        self.assertNotContains(response, 'class="course-management-links"', html=False)
        self.assertContains(response, 'class="block-chip-row"', html=False)
        self.assertContains(response, 'class="section-head compact course-section-head course-blocks-head course-section-toggle"', html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:teacher_dashboard"), html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:student_preview", args=[course.pk]), html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:block_create", args=[course.pk]), html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:course_import_upload", args=[course.pk]), html=False)
        self.assertContains(response, 'href="#course-settings-content"', html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:student_invite", args=[course.pk]), html=False)
        self.assertContains(response, ">Dashboard<", html=False)
        self.assertContains(response, ">Student preview<", html=False)
        self.assertContains(response, ">Settings<", html=False)
        self.assertContains(response, ">Course settings<", html=False)
        self.assertContains(response, "Auto-save")
        self.assertContains(response, "Multiple-answer question ratio (%)")
        self.assertContains(response, "Written-answer question ratio (%)")
        self.assertContains(response, 'data-settings-toast', html=False)
        self.assertContains(
            response,
            'data-course-config-url="%s"' % reverse("standalone:update_course_config_field", args=[course.pk, "maq_ratio_percent"]),
            html=False,
        )
        self.assertContains(response, ">Import PDF textbook<", html=False)
        self.assertContains(response, ">Add new block<", html=False)
        self.assertContains(response, ">Invite student<", html=False)
        self.assertContains(response, reverse("standalone:asset_upload", args=[block.pk]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:block_delete", args=[block.pk]), html=False)
        self.assertContains(response, 'data-block-menu-trigger', html=False)
        self.assertContains(response, "Block actions")
        self.assertContains(response, 'action="%s"' % reverse("standalone:delete_asset", args=[asset.pk]), html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_course_field", args=[course.pk, "title"]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:regenerate_block_content", args=[block.pk]), html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_block_field", args=[block.pk, "available_from"]), html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_block_config_field", args=[block.pk, "target_question_count"]), html=False)
        self.assertContains(response, "Available from")
        self.assertContains(response, "Target MCQs")
        self.assertContains(response, "Enrolment routes")
        self.assertContains(response, "Course blocks")
        self.assertContains(response, "Course settings")
        self.assertContains(response, "PDF imports")
        self.assertContains(response, "Danger zone")
        self.assertContains(response, "Delete course")
        self.assertContains(response, 'action="%s"' % reverse("standalone:course_delete", args=[course.pk]), html=False)
        self.assertContains(response, "Self-enrol allowlist")
        self.assertContains(response, "Magic links")
        page_html = response.content.decode("utf-8")
        self.assertEqual(page_html.count("course-section-head"), 6)
        self.assertIn('id="course-settings-content"', page_html)
        self.assertIn('id="course-imports-content"', page_html)
        self.assertIn('id="course-validation-content"', page_html)
        self.assertIn('id="course-blocks-content"', page_html)
        self.assertIn('id="student-access-content"', page_html)
        self.assertIn('id="course-danger-content"', page_html)
        self.assertLess(page_html.find("Course blocks"), page_html.find("Enrolment routes"))
        self.assertContains(response, "Students can join from the self-enrol URL only when their exact email address is on this course", html=False)
        self.assertContains(response, "Magic links do not require an exact allowlist email.")
        self.assertContains(response, "This will replace the current description and learning objectives using every file in this block.")
        self.assertContains(response, "Delete this content block? This will remove its uploads, learning objectives, and generated questions. Remaining blocks will be re-numbered.")
        self.assertContains(response, "Upload files")
        self.assertContains(response, "Re-generate")
        self.assertNotContains(response, "Draft questions")
        self.assertNotContains(response, "Approved questions")
        self.assertNotContains(response, "Allowed emails")
        self.assertContains(response, "Validation")
        self.assertContains(response, "Create validation session")
        self.assertNotContains(response, "Regenerate descriptions and objectives")
        self.assertNotContains(response, "Generate question bank")
        self.assertNotContains(response, "Approve all draft questions")
        self.assertContains(response, 'data-block-toggle', html=False)
        self.assertContains(response, 'aria-expanded="false"', html=False)
        self.assertContains(response, 'id="block-content-%s"' % block.pk, html=False)
        self.assertContains(response, 'id="objectives-content-%s"' % block.pk, html=False)
        self.assertContains(response, 'id="assets-content-%s"' % block.pk, html=False)
        self.assertContains(response, 'class="child-block-list course-block-subsections"', html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_block_field", args=[block.pk, "title"]), html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_learning_objective", args=[objective.pk]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:move_learning_objective", args=[objective.pk, "up"]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:move_learning_objective", args=[objective.pk, "down"]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:delete_learning_objective", args=[objective.pk]), html=False)
        self.assertContains(response, "Delete this learning objective? This will re-number the remaining objectives.")
        self.assertLess(response.content.decode("utf-8").find("Learning objectives"), response.content.decode("utf-8").find("Uploads"))

    def test_teacher_dashboard_course_card_is_clickable_with_mobile_summary(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Quick explanation.",
            question_hash="dashboard-summary-question",
        )
        Enrollment.objects.create(
            course=course,
            student=self.student,
            mastery_score=50,
            coverage_score=80,
            engagement_score=20,
            target_score=100,
        )
        second_student = User.objects.create_user(
            username="student-two",
            email="student-two@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )
        Enrollment.objects.create(course=course, student=second_student)
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:teacher_dashboard"))

        self.assertContains(response, "Teacher dashboard")
        self.assertContains(response, "Course workspace")
        self.assertNotContains(response, 'class="dashboard-stat-grid"', html=False)
        self.assertContains(response, 'class="course-card dashboard-course-card"', html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:course_detail", args=[course.pk]), html=False)
        self.assertContains(response, "Open course")
        self.assertContains(response, "2 students")
        self.assertContains(response, "1 questions")
        self.assertContains(response, "Practice averages")
        self.assertContains(response, "Overall practice")
        self.assertContains(response, "<strong>29.0%</strong>", html=False)
        self.assertContains(response, "Mastery")
        self.assertContains(response, "<strong>25.0%</strong>", html=False)
        self.assertContains(response, "Coverage")
        self.assertContains(response, "<strong>40.0%</strong>", html=False)
        self.assertContains(response, "Engagement")
        self.assertContains(response, "<strong>10.0%</strong>", html=False)
        self.assertContains(response, "Target")
        self.assertContains(response, "<strong>50.0%</strong>", html=False)
        self.assertContains(response, "Continuous practice. Anchored assessment.")

    def test_course_detail_shows_course_practice_averages_across_students(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        Enrollment.objects.create(
            course=course,
            student=self.student,
            mastery_score=80,
            coverage_score=60,
            engagement_score=40,
            target_score=20,
        )
        second_student = User.objects.create_user(
            username="student-three",
            email="student-three@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )
        Enrollment.objects.create(
            course=course,
            student=second_student,
            mastery_score=20,
            coverage_score=40,
            engagement_score=60,
            target_score=80,
        )

        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))

        self.assertContains(response, 'class="practice-average-panel course-practice-average-panel"', html=False)
        self.assertContains(response, 'class="practice-average-label"', html=False)
        self.assertContains(response, "Practice averages")
        self.assertContains(response, "Overall practice")
        self.assertContains(response, "Mastery")
        self.assertContains(response, "Coverage")
        self.assertContains(response, "Engagement")
        self.assertContains(response, "Target")
        self.assertContains(response, "<strong>50.0%</strong>", html=False, count=5)

    def test_standalone_footer_is_omitted_from_student_preview(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        dashboard_response = self.client.get(reverse("standalone:teacher_dashboard"))
        preview_response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))

        self.assertContains(dashboard_response, 'data-app-footer', html=False)
        self.assertNotContains(preview_response, 'data-app-footer', html=False)
        self.assertNotContains(preview_response, "Continuous practice. Anchored assessment.")

    def test_block_create_form_includes_upload_picker(self):
        course = self.create_course()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:block_create", args=[course.pk]))
        self.assertContains(response, 'data-upload-form', html=False)
        self.assertContains(response, 'data-upload-input="true"', html=False)
        self.assertContains(response, 'name="available_from"', html=False)
        self.assertContains(response, 'type="date"', html=False)
        self.assertContains(response, "Choose files")
        self.assertContains(response, "Create block")
        self.assertContains(response, "MCQs for this block will only be generated and shown to students from this date.")
        self.assertNotContains(response, 'name="summary"', html=False)
        self.assertNotContains(response, 'name="order"', html=False)

    def test_regenerate_course_content_refreshes_summaries_and_sanitized_objectives(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Cell membrane notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=(
                "1. Describe the structure of the cell membrane.\n"
                "\u2022 Explain how transport proteins regulate movement across the membrane.\n"
                "LO3: Compare diffusion with active transport.\n\n"
                "Cell membranes control exchange between the cell and its environment. "
                "Transport proteins regulate what crosses the membrane."
            ),
        )

        self.client.force_login(self.teacher)
        with self.settings(OPENAI_API_KEY=""):
            response = self.client.post(reverse("standalone:regenerate_course_content", args=[course.pk]))
        self.assertEqual(response.status_code, 302)

        course.refresh_from_db()
        block.refresh_from_db()
        objectives = list(block.learning_objectives.order_by("position", "pk").values_list("text", flat=True))

        self.assertTrue(course.summary)
        self.assertTrue(block.summary)
        self.assertGreaterEqual(len(objectives), 3)
        self.assertIn("Describe the structure of the cell membrane", objectives)
        self.assertIn("Explain how transport proteins regulate movement across the membrane", objectives)
        self.assertIn("Compare diffusion with active transport", objectives)
        self.assertFalse(any(re.match(r"^\s*(?:\d+|LO\d+|[\u2022\u25cf\u25e6\u25aa*-])", text, re.IGNORECASE) for text in objectives))

    def test_regenerate_block_content_refreshes_only_target_block(self):
        course = self.create_course()
        block_one = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        block_two = CourseBlock.objects.create(course=course, title="Week 2", summary="Keep me", order=2)
        ContentAsset.objects.create(
            block=block_one,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Cell membrane notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=(
                "Describe the structure of the cell membrane.\n"
                "Explain how transport proteins regulate movement across the membrane.\n"
                "Compare diffusion with active transport."
            ),
        )

        with self.settings(OPENAI_API_KEY=""):
            run_block_regeneration(block_one.pk)

        block_one.refresh_from_db()
        block_two.refresh_from_db()
        self.assertTrue(block_one.summary)
        self.assertGreater(block_one.learning_objectives.count(), 0)
        self.assertEqual(block_two.summary, "Keep me")

    def test_regenerate_block_content_queues_background_task_and_sets_status(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Old summary", order=1)
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Cell membrane notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=(
                "Describe the structure of the cell membrane.\n"
                "Explain how transport proteins regulate movement across the membrane.\n"
                "Compare diffusion with active transport."
            ),
        )

        self.client.force_login(self.teacher)
        with patch("standalone.views._queue_block_regeneration") as queue_regeneration:
            response = self.client.post(reverse("standalone:regenerate_block_content", args=[block.pk]))
        self.assertEqual(response.status_code, 302)
        block.refresh_from_db()
        queue_regeneration.assert_called_once_with(block.pk)
        self.assertEqual(block.regeneration_status, CourseBlock.RegenerationStatus.QUEUED)
        self.assertEqual(block.regeneration_progress, 5)
        self.assertEqual(block.regeneration_error, "")

    def test_block_delete_allowed_while_regeneration_is_running(self):
        course = self.create_course()
        block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            summary="Old summary",
            order=1,
            regeneration_status=CourseBlock.RegenerationStatus.RUNNING,
            regeneration_progress=55,
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:block_delete", args=[block.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(CourseBlock.objects.filter(pk=block.pk).exists())

    def test_run_block_regeneration_updates_summary_objectives_and_completion_status(self):
        course = self.create_course()
        block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            summary="Old summary",
            order=1,
            regeneration_status=CourseBlock.RegenerationStatus.QUEUED,
            regeneration_progress=5,
        )
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Cell membrane notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=(
                "Describe the structure of the cell membrane.\n"
                "Explain how transport proteins regulate movement across the membrane.\n"
                "Compare diffusion with active transport."
            ),
        )

        with self.settings(OPENAI_API_KEY=""):
            run_block_regeneration(block.pk)

        block.refresh_from_db()
        objectives = list(block.learning_objectives.order_by("position", "pk").values_list("text", flat=True))
        self.assertTrue(block.summary)
        self.assertNotEqual(block.summary, "Old summary")
        self.assertGreaterEqual(len(objectives), 3)
        self.assertIn("Describe the structure of the cell membrane", objectives)
        self.assertEqual(block.regeneration_status, CourseBlock.RegenerationStatus.IDLE)
        self.assertEqual(block.regeneration_progress, 0)
        self.assertEqual(block.regeneration_error, "")

    def test_run_block_regeneration_preserves_coverage_from_later_uploaded_material(self):
        course = self.create_course()
        block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            order=1,
            regeneration_status=CourseBlock.RegenerationStatus.QUEUED,
            regeneration_progress=5,
        )
        late_topic_lines = "\n".join(
            [
                "Explain the structure and function of the cell membrane.",
                "Describe how transport proteins regulate movement across the membrane.",
                "Compare diffusion with active transport.",
                "Analyse how enzymes lower activation energy in metabolic pathways.",
                "Evaluate how ATP couples energy release to cellular work.",
                "Explain how mitochondrial electron transport drives oxidative phosphorylation.",
                "Assess the role of feedback inhibition in metabolic control.",
                "Interpret how glycogen metabolism responds to hormonal signalling.",
            ]
        )
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", late_topic_lines.encode("utf-8"), content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=late_topic_lines,
        )

        with self.settings(OPENAI_API_KEY=""):
            run_block_regeneration(block.pk)

        objectives = list(block.learning_objectives.order_by("position", "pk").values_list("text", flat=True))
        self.assertGreaterEqual(len(objectives), 8)
        self.assertIn("Explain how mitochondrial electron transport drives oxidative phosphorylation", objectives)
        self.assertIn("Interpret how glycogen metabolism responds to hormonal signalling", objectives)

    def test_inline_block_title_update_returns_json_and_persists(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:update_block_field", args=[block.pk, "title"]), {"title": "Foundations"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_value"], "Foundations")
        block.refresh_from_db()
        self.assertEqual(block.title, "Foundations")

    def test_inline_course_title_update_returns_json_and_persists(self):
        course = self.create_course()
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:update_course_field", args=[course.pk, "title"]), {"title": "Advanced Cell Biology"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_value"], "Advanced Cell Biology")
        course.refresh_from_db()
        self.assertEqual(course.title, "Advanced Cell Biology")

    def test_inline_block_available_from_update_returns_json_and_persists(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:update_block_field", args=[block.pk, "available_from"]),
            {"available_from": "2026-07-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_value"], "1 Jul 2026")
        self.assertEqual(response.json()["raw_value"], "2026-07-01")
        block.refresh_from_db()
        self.assertEqual(str(block.available_from), "2026-07-01")

    def test_inline_block_target_update_returns_json_and_persists(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        config = BlockConfig.objects.create(block=block)
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:update_block_config_field", args=[block.pk, "target_question_count"]),
            {"target_question_count": "32"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_value"], 32)
        config.refresh_from_db()
        self.assertEqual(config.target_question_count, 32)

    def test_inline_learning_objective_update_sanitizes_text(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Block notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Describe membrane structure.",
        )
        objective = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Describe membrane structure",
        )
        self.client.force_login(self.teacher)
        response = self.client.post(
            reverse("standalone:update_learning_objective", args=[objective.pk]),
            {"text": "1. Explain how membrane proteins regulate transport."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_value"], "Explain how membrane proteins regulate transport")
        objective.refresh_from_db()
        self.assertEqual(objective.text, "Explain how membrane proteins regulate transport")

    def test_learning_objective_can_move_down_and_resequence(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Block notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Describe membrane structure.",
        )
        first = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Describe membrane structure",
        )
        second = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=2,
            code="1.2",
            text="Explain membrane transport",
        )
        third = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=3,
            code="1.3",
            text="Compare diffusion and active transport",
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:move_learning_objective", args=[first.pk, "down"]))
        self.assertEqual(response.status_code, 302)

        reordered = list(block.learning_objectives.order_by("position", "pk"))
        self.assertEqual([objective.pk for objective in reordered], [second.pk, first.pk, third.pk])
        self.assertEqual([objective.code for objective in reordered], ["1.1", "1.2", "1.3"])

    def test_learning_objective_delete_resequences_positions_and_codes(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Block notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Describe membrane structure.",
        )
        first = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Describe membrane structure",
        )
        second = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=2,
            code="1.2",
            text="Explain membrane transport",
        )
        third = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=3,
            code="1.3",
            text="Compare diffusion and active transport",
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:delete_learning_objective", args=[second.pk]))
        self.assertEqual(response.status_code, 302)

        remaining = list(block.learning_objectives.order_by("position", "pk"))
        self.assertEqual([objective.pk for objective in remaining], [first.pk, third.pk])
        self.assertEqual([objective.position for objective in remaining], [1, 2])
        self.assertEqual([objective.code for objective in remaining], ["1.1", "1.2"])

    def test_asset_delete_removes_uploaded_file_and_generated_objectives(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Block notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Describe membrane structure.",
        )
        LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Describe membrane structure",
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:delete_asset", args=[asset.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContentAsset.objects.filter(pk=asset.pk).exists())
        self.assertEqual(block.learning_objectives.count(), 0)

    def test_block_delete_removes_block_and_resequences_remaining_blocks(self):
        course = self.create_course()
        CourseBlock.objects.create(course=course, title="Week 1", summary="Membranes.", order=1)
        deleted_block = CourseBlock.objects.create(course=course, title="Week 2", summary="Transport.", order=2)
        trailing_block = CourseBlock.objects.create(course=course, title="Week 3", summary="Metabolism.", order=3)
        ContentAsset.objects.create(
            block=deleted_block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Transport notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Explain membrane transport.",
        )
        trailing_asset = ContentAsset.objects.create(
            block=trailing_block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("metabolism.txt", b"Metabolism notes", content_type="text/plain"),
            original_filename="metabolism.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Explain cellular metabolism.",
        )
        LearningObjective.objects.create(
            course=course,
            block=trailing_block,
            source_asset=trailing_asset,
            position=1,
            code="3.1",
            text="Explain cellular metabolism",
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:block_delete", args=[deleted_block.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CourseBlock.objects.filter(pk=deleted_block.pk).exists())
        remaining_blocks = list(course.blocks.order_by("order", "pk"))
        self.assertEqual([block.title for block in remaining_blocks], ["Week 1", "Week 3"])
        self.assertEqual([block.order for block in remaining_blocks], [1, 2])
        trailing_block.refresh_from_db()
        self.assertEqual(trailing_block.learning_objectives.get().code, "2.1")
        course.refresh_from_db()
        self.assertTrue(course.summary)

    def test_self_enrol_requires_allowlist_and_domain(self):
        course = self.create_course()
        course.config.self_enrol_domain = "example.com"
        course.config.save()
        CourseAllowedEmail.objects.create(course=course, email="allowed@example.com")

        bad_response = self.client.post(
            reverse("standalone:self_enrol", args=[course.slug]),
            {
                "full_name": "Blocked Student",
                "email": "blocked@other.com",
                "password1": "safe-pass-123",
                "password2": "safe-pass-123",
                "institution": "",
            },
        )
        self.assertEqual(bad_response.status_code, 200)
        self.assertFalse(User.objects.filter(email="blocked@other.com").exists())

        good_response = self.client.post(
            reverse("standalone:self_enrol", args=[course.slug]),
            {
                "full_name": "Allowed Student",
                "email": "allowed@example.com",
                "password1": "safe-pass-123",
                "password2": "safe-pass-123",
                "institution": "",
            },
        )
        self.assertEqual(good_response.status_code, 302)
        enrolled_user = User.objects.get(email="allowed@example.com")
        self.assertTrue(Enrollment.objects.filter(course=course, student=enrolled_user, source="self_enrol").exists())

    def test_self_enrol_rejects_staff_accounts_and_requires_existing_student_password(self):
        course = self.create_course()
        CourseAllowedEmail.objects.create(course=course, email=self.teacher.email)
        CourseAllowedEmail.objects.create(course=course, email=self.student.email)

        staff_response = self.client.post(
            reverse("standalone:self_enrol", args=[course.slug]),
            {
                "full_name": "Teacher User",
                "email": self.teacher.email,
                "password1": "password123",
                "password2": "password123",
                "institution": "",
            },
        )
        bad_password_response = self.client.post(
            reverse("standalone:self_enrol", args=[course.slug]),
            {
                "full_name": "Student User",
                "email": self.student.email,
                "password1": "wrong-password",
                "password2": "wrong-password",
                "institution": "",
            },
        )
        self.assertFalse(Enrollment.objects.filter(course=course, student=self.student).exists())
        good_response = self.client.post(
            reverse("standalone:self_enrol", args=[course.slug]),
            {
                "full_name": "Student User",
                "email": self.student.email,
                "password1": "password123",
                "password2": "password123",
                "institution": "",
            },
        )

        self.assertEqual(staff_response.status_code, 200)
        self.assertContains(staff_response, "Use a student email address")
        self.assertFalse(Enrollment.objects.filter(course=course, student=self.teacher).exists())
        self.assertEqual(bad_password_response.status_code, 200)
        self.assertContains(bad_password_response, "Enter the password for this existing student account.")
        self.assertEqual(good_response.status_code, 302)
        self.assertTrue(Enrollment.objects.filter(course=course, student=self.student, source="self_enrol").exists())

    def test_add_allowed_email_normalises_and_deduplicates(self):
        course = self.create_course()
        self.client.force_login(self.teacher)

        first_response = self.client.post(
            reverse("standalone:allowed_email_add", args=[course.pk]),
            {"email": " Allowed@Example.COM "},
        )
        second_response = self.client.post(
            reverse("standalone:allowed_email_add", args=[course.pk]),
            {"email": "allowed@example.com"},
        )

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(list(course.allowed_emails.values_list("email", flat=True)), ["allowed@example.com"])

    def test_magic_link_enrols_without_allowlist_but_checks_existing_accounts_and_uses(self):
        course = self.create_course()
        link = CourseMagicLink.objects.create(
            course=course,
            created_by=self.teacher,
            expires_at=timezone.now() + timedelta(hours=2),
            max_uses=2,
        )
        existing_link = CourseMagicLink.objects.create(
            course=course,
            created_by=self.teacher,
            expires_at=timezone.now() + timedelta(hours=2),
            max_uses=1,
        )
        Enrollment.objects.create(course=course, student=self.student, source="invite")

        new_student_response = self.client.post(
            reverse("standalone:magic_enrol", args=[link.token]),
            {
                "full_name": "Magic Student",
                "email": "magic@example.com",
                "password1": "safe-pass-123",
                "password2": "safe-pass-123",
                "institution": "",
            },
        )
        link.refresh_from_db()

        self.assertEqual(new_student_response.status_code, 302)
        magic_student = User.objects.get(email="magic@example.com")
        self.assertTrue(Enrollment.objects.filter(course=course, student=magic_student, source="magic_link").exists())
        self.assertEqual(link.use_count, 1)
        self.assertTrue(link.is_active)

        existing_student_response = self.client.post(
            reverse("standalone:magic_enrol", args=[existing_link.token]),
            {
                "full_name": "Existing Student",
                "email": self.student.email,
                "password1": "password123",
                "password2": "password123",
                "institution": "",
            },
        )
        existing_link.refresh_from_db()

        self.assertEqual(existing_student_response.status_code, 302)
        self.assertEqual(existing_link.use_count, 0)
        self.assertTrue(existing_link.is_active)

        staff_response = self.client.post(
            reverse("standalone:magic_enrol", args=[existing_link.token]),
            {
                "full_name": "Teacher User",
                "email": self.teacher.email,
                "password1": "password123",
                "password2": "password123",
                "institution": "",
            },
        )
        existing_link.refresh_from_db()

        self.assertEqual(staff_response.status_code, 200)
        self.assertContains(staff_response, "Use a student email address")
        self.assertEqual(existing_link.use_count, 0)

    def test_teacher_can_upload_supported_content_and_generate_chunks(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        self.client.force_login(self.teacher)
        with self.settings(OPENAI_API_KEY="", CELERY_TASK_ALWAYS_EAGER=True):
            response = self.client.post(
                reverse("standalone:asset_upload", args=[block.pk]),
                {
                    "file": SimpleUploadedFile(
                        "notes.txt",
                        (
                            b"Explain how membrane structure controls permeability.\n\n"
                            b"Compare passive diffusion with active transport across the cell membrane.\n\n"
                            b"Describe how transport proteins regulate movement into and out of the cell."
                        ),
                        content_type="text/plain",
                    ),
                },
            )
        self.assertEqual(response.status_code, 302)
        asset = ContentAsset.objects.get(block=block)
        self.assertEqual(asset.processing_status, ContentAsset.ProcessingStatus.PROCESSED)
        self.assertGreater(asset.chunks.count(), 0)
        self.assertGreater(block.learning_objectives.count(), 0)

    def test_teacher_can_create_block_with_uploaded_content(self):
        course = self.create_course()
        CourseBlock.objects.create(course=course, title="Existing block", order=1)
        self.client.force_login(self.teacher)
        with patch("standalone.views._queue_block_creation_processing") as queue_processing:
            response = self.client.post(
                reverse("standalone:block_create", args=[course.pk]),
                {
                    "title": "Week 1",
                    "available_from": "2026-07-04",
                    "file": [
                        SimpleUploadedFile(
                            "notes-1.txt",
                            b"Describe membrane structure and function.",
                            content_type="text/plain",
                        ),
                        SimpleUploadedFile(
                            "notes-2.txt",
                            b"Explain how transport proteins regulate movement across the membrane.",
                            content_type="text/plain",
                        ),
                    ],
                },
            )
        self.assertEqual(response.status_code, 302)
        block = CourseBlock.objects.get(course=course, title="Week 1")
        self.assertEqual(response.url, f"{reverse('standalone:course_detail', args=[course.pk])}#block-content-{block.pk}")
        assets = list(ContentAsset.objects.filter(block=block).order_by("original_filename"))
        self.assertEqual(len(assets), 2)
        self.assertEqual([asset.original_filename for asset in assets], ["notes-1.txt", "notes-2.txt"])
        block.refresh_from_db()
        queue_processing.assert_called_once_with(block.pk)
        self.assertEqual(block.order, 2)
        self.assertEqual(str(block.available_from), "2026-07-04")
        self.assertEqual(block.regeneration_status, CourseBlock.RegenerationStatus.QUEUED)
        self.assertEqual(block.regeneration_progress, 5)
        self.assertEqual(block.regeneration_error, "")
        self.assertTrue(all(asset.processing_status == ContentAsset.ProcessingStatus.PENDING for asset in assets))

    def test_teacher_can_create_block_without_explicit_available_from_and_it_defaults_to_today(self):
        course = self.create_course()
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:block_create", args=[course.pk]), {"title": "Week 1"})

        self.assertEqual(response.status_code, 302)
        block = CourseBlock.objects.get(course=course, title="Week 1")
        self.assertEqual(block.available_from, timezone.localdate())

    def test_course_detail_hides_regenerate_button_while_new_block_is_initially_processing(self):
        course = self.create_course()
        block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            order=1,
            regeneration_status=CourseBlock.RegenerationStatus.QUEUED,
            regeneration_progress=5,
        )
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Fresh notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            processing_status=ContentAsset.ProcessingStatus.PENDING,
        )

        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))

        self.assertNotContains(response, 'action="%s"' % reverse("standalone:regenerate_block_content", args=[block.pk]), html=False)
        self.assertNotContains(response, "Re-generating...")

    def test_course_detail_keeps_regenerate_button_visible_during_rerun_for_existing_block(self):
        course = self.create_course()
        block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            summary="Existing summary",
            order=1,
            regeneration_status=CourseBlock.RegenerationStatus.RUNNING,
            regeneration_progress=50,
        )
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Existing notes", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
        )
        LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Existing objective",
        )

        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:course_detail", args=[course.pk]))

        self.assertContains(response, 'action="%s"' % reverse("standalone:regenerate_block_content", args=[block.pk]), html=False)
        self.assertContains(response, "Re-generating...")

    def test_run_block_creation_processing_processes_assets_and_generates_block_content(self):
        course = self.create_course()
        block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            order=1,
            regeneration_status=CourseBlock.RegenerationStatus.QUEUED,
            regeneration_progress=5,
        )
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile(
                "notes-1.txt",
                b"Describe membrane structure and function.",
                content_type="text/plain",
            ),
            original_filename="notes-1.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PENDING,
        )
        ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile(
                "notes-2.txt",
                b"Explain how transport proteins regulate movement across the membrane.",
                content_type="text/plain",
            ),
            original_filename="notes-2.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PENDING,
        )

        with self.settings(OPENAI_API_KEY=""):
            run_block_creation_processing(block.pk)

        block.refresh_from_db()
        assets = list(ContentAsset.objects.filter(block=block).order_by("original_filename"))
        self.assertTrue(all(asset.processing_status == ContentAsset.ProcessingStatus.PROCESSED for asset in assets))
        self.assertTrue(all(asset.chunks.exists() for asset in assets))
        self.assertTrue(block.summary)
        self.assertGreater(block.learning_objectives.count(), 0)
        self.assertEqual(block.regeneration_status, CourseBlock.RegenerationStatus.IDLE)
        self.assertEqual(block.regeneration_progress, 0)
        self.assertEqual(block.regeneration_error, "")

    def test_teacher_can_upload_multiple_files_in_one_submit(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        self.client.force_login(self.teacher)
        with self.settings(OPENAI_API_KEY="", CELERY_TASK_ALWAYS_EAGER=True):
            response = self.client.post(
                reverse("standalone:asset_upload", args=[block.pk]),
                {
                    "file": [
                        SimpleUploadedFile(
                            "notes-1.txt",
                            b"Describe membrane structure and function.",
                            content_type="text/plain",
                        ),
                        SimpleUploadedFile(
                            "notes-2.txt",
                            b"Explain how transport proteins regulate movement across the membrane.",
                            content_type="text/plain",
                        ),
                    ],
                },
            )
        self.assertEqual(response.status_code, 302)
        assets = list(ContentAsset.objects.filter(block=block).order_by("original_filename"))
        self.assertEqual(len(assets), 2)
        self.assertEqual([asset.original_filename for asset in assets], ["notes-1.txt", "notes-2.txt"])
        self.assertTrue(all(asset.processing_status == ContentAsset.ProcessingStatus.PROCESSED for asset in assets))
        self.assertTrue(all(asset.chunks.exists() for asset in assets))

    def test_asset_upload_form_cancel_and_submit_return_to_same_course_section(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        self.client.force_login(self.teacher)
        return_to = f"{reverse('standalone:course_detail', args=[course.pk])}#assets-content-{block.pk}"

        get_response = self.client.get(reverse("standalone:asset_upload", args=[block.pk]), {"next": return_to})
        self.assertContains(get_response, 'href="%s"' % return_to, html=False)
        self.assertContains(get_response, 'name="next"', html=False)
        self.assertContains(get_response, 'value="%s"' % return_to, html=False)

        with self.settings(OPENAI_API_KEY="", CELERY_TASK_ALWAYS_EAGER=True):
            post_response = self.client.post(
                reverse("standalone:asset_upload", args=[block.pk]),
                {
                    "next": return_to,
                    "file": SimpleUploadedFile("notes.txt", b"Cell notes", content_type="text/plain"),
                },
            )
        self.assertEqual(post_response.status_code, 302)
        self.assertEqual(post_response.url, return_to)

    def test_question_bank_generation_creates_practice_and_validation_items(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Approved content for question generation.", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PENDING,
        )
        from standalone.services.content import ingest_content_asset

        with self.settings(OPENAI_API_KEY=""):
            ingest_content_asset(asset)
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:generate_course_bank", args=[course.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(course.question_bank_items.filter(bank_type=QuestionBankItem.BankType.PRACTICE).exists())
        self.assertTrue(course.question_bank_items.filter(bank_type=QuestionBankItem.BankType.VALIDATION).exists())

    def test_question_bank_generation_skips_future_blocks(self):
        course = self.create_course()
        released_block = CourseBlock.objects.create(course=course, title="Released block", order=1)
        future_block = CourseBlock.objects.create(
            course=course,
            title="Future block",
            order=2,
            available_from=timezone.localdate() + timedelta(days=7),
        )
        released_asset = ContentAsset.objects.create(
            block=released_block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("released.txt", b"Released content.", content_type="text/plain"),
            original_filename="released.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Released content for available questions.",
        )
        future_asset = ContentAsset.objects.create(
            block=future_block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("future.txt", b"Future content.", content_type="text/plain"),
            original_filename="future.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text="Future content that should not generate questions yet.",
        )
        LearningObjective.objects.create(
            course=course,
            block=released_block,
            source_asset=released_asset,
            position=1,
            code="1.1",
            text="Explain released content",
        )
        LearningObjective.objects.create(
            course=course,
            block=future_block,
            source_asset=future_asset,
            position=1,
            code="2.1",
            text="Explain future content",
        )
        from standalone.models import ContentChunk

        ContentChunk.objects.create(
            asset=released_asset,
            course=course,
            block=released_block,
            ordinal=1,
            text="Released content for available questions.",
            token_count=5,
            checksum="released-chunk",
        )
        ContentChunk.objects.create(
            asset=future_asset,
            course=course,
            block=future_block,
            ordinal=1,
            text="Future content that should not generate questions yet.",
            token_count=6,
            checksum="future-chunk",
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:generate_course_bank", args=[course.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(course.question_bank_items.filter(block=released_block).exists())
        self.assertFalse(course.question_bank_items.filter(block=future_block).exists())

    def test_question_bank_generation_uses_multiple_objectives_across_chunks(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Approved content for chunked question generation.", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PROCESSED,
            extracted_text=(
                "Membrane transport relies on channels and carriers.\n\n"
                "ATP hydrolysis powers pumps against concentration gradients.\n\n"
                "Mitochondria generate ATP through oxidative phosphorylation."
            ),
        )
        from standalone.models import ContentChunk

        first_objective = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=1,
            code="1.1",
            text="Explain membrane transport through channels and carriers",
        )
        second_objective = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=2,
            code="1.2",
            text="Explain how ATP powers membrane pumps and oxidative phosphorylation",
        )
        ContentChunk.objects.create(
            asset=asset,
            course=course,
            block=block,
            ordinal=1,
            text="Membrane transport relies on channels and carriers for selective movement.",
            token_count=10,
            checksum="chunk-1",
        )
        ContentChunk.objects.create(
            asset=asset,
            course=course,
            block=block,
            ordinal=2,
            text="ATP hydrolysis powers pumps and oxidative phosphorylation in mitochondria.",
            token_count=11,
            checksum="chunk-2",
        )

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:generate_course_bank", args=[course.pk]))
        self.assertEqual(response.status_code, 302)
        practice_items = list(course.question_bank_items.filter(bank_type=QuestionBankItem.BankType.PRACTICE).order_by("source_chunk__ordinal", "pk"))
        self.assertEqual(len(practice_items), 2)
        self.assertEqual(practice_items[0].learning_objective_id, first_objective.pk)
        self.assertEqual(practice_items[1].learning_objective_id, second_objective.pk)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_question_bank_generation_falls_back_when_openai_output_is_not_json(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        asset = ContentAsset.objects.create(
            block=block,
            uploaded_by=self.teacher,
            file=SimpleUploadedFile("notes.txt", b"Approved content for fallback generation.", content_type="text/plain"),
            original_filename="notes.txt",
            extension=".txt",
            include_in_generation=True,
            processing_status=ContentAsset.ProcessingStatus.PENDING,
        )
        from standalone.services.content import ingest_content_asset

        with self.settings(OPENAI_API_KEY=""):
            ingest_content_asset(asset)
        self.client.force_login(self.teacher)

        class DummyResponse:
            output_text = "Here is your question as requested."

        with self.settings(OPENAI_API_KEY="test-key"):
            with self.subTest("fallback generation"):
                from unittest.mock import patch

                with patch("standalone.services.questions.OpenAI") as mock_client:
                    mock_client.return_value.responses.create.return_value = DummyResponse()
                    response = self.client.post(reverse("standalone:generate_course_bank", args=[course.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(course.question_bank_items.filter(bank_type=QuestionBankItem.BankType.PRACTICE).exists())

    @override_settings(OPENAI_API_KEY="test-key")
    def test_question_generation_rejects_textbook_meta_stems(self):
        course = self.create_course()
        block, _asset, _objective, _chunk = self.create_preview_content_block(course)

        class DummyResponse:
            output_text = json.dumps(
                {
                    "question_type": QuestionBankItem.QuestionType.MAQ,
                    "stem": "What is one of the main topics covered in the source text?",
                    "correct_answers": ["Membrane transport", "Cell signalling"],
                    "distractors": ["Unrelated astronomy", "Medieval history", "Poetry analysis"],
                    "further_study_questions": [
                        "Why does membrane transport matter?",
                        "How would you explain cell signalling?",
                        "What common mistake should I avoid with membranes?",
                    ],
                    "explanation": "The source text covers membrane transport and cell signalling.",
                    "difficulty": "core",
                }
            )

        with patch("standalone.services.questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            practice, validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.MAQ)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertNotIn("main topics covered", practice.stem.lower())
        self.assertNotIn("source text", practice.stem.lower())
        self.assertNotIn("textbook", practice.stem.lower())
        self.assertNotIn("content covers", practice.explanation.lower())

    @override_settings(OPENAI_API_KEY="")
    def test_coding_question_generation_can_use_existing_answer_types(self):
        for index, question_type in enumerate((
            QuestionBankItem.QuestionType.MCQ,
            QuestionBankItem.QuestionType.MAQ,
            QuestionBankItem.QuestionType.WAQ,
        ), start=1):
            with self.subTest(question_type):
                course = Course.objects.create(teacher=self.teacher, title=f"Coding {index}", slug=f"coding-{index}", summary="Code.")
                CourseConfig.objects.create(course=course)
                course.config.coding_question_ratio_percent = 100
                course.config.waq_ratio_percent = 0
                course.config.maq_ratio_percent = 0
                course.config.save(update_fields=["coding_question_ratio_percent", "waq_ratio_percent", "maq_ratio_percent", "updated_at"])
                block, _asset, _objective, _chunk = self.create_coding_content_block(course)

                practice, validation = generate_question_pair_for_block(block, question_type=question_type)

                self.assertIsNotNone(practice)
                self.assertIsNotNone(validation)
                self.assertEqual(practice.question_type, question_type)
                self.assertTrue(practice.is_coding_question)
                self.assertEqual(practice.coding_language, "python")
                self.assertIn("double", practice.code_snippet)
                self.assertIn(practice.coding_question_kind, {"comprehension", "debug"})
                self.assertEqual(validation.code_snippet, practice.code_snippet)

    @override_settings(OPENAI_API_KEY="")
    def test_coding_ratio_is_ignored_without_coding_chunks(self):
        course = self.create_course()
        course.config.coding_question_ratio_percent = 100
        course.config.save(update_fields=["coding_question_ratio_percent", "updated_at"])
        block, _asset, _objective, _chunk = self.create_preview_content_block(course)

        practice, _validation = generate_question_pair_for_block(block)

        self.assertIsNotNone(practice)
        self.assertFalse(practice.is_coding_question)
        self.assertEqual(practice.code_snippet, "")

    @override_settings(OPENAI_API_KEY="test-key")
    def test_bad_ai_coding_payload_falls_back_to_language_aligned_coding_question(self):
        course = self.create_course()
        course.config.coding_question_ratio_percent = 100
        course.config.save(update_fields=["coding_question_ratio_percent", "updated_at"])
        block, _asset, _objective, _chunk = self.create_coding_content_block(
            course,
            extension=".r",
            text="""```r
summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)
```""",
        )

        class DummyResponse:
            output_text = json.dumps(
                {
                    "question_type": QuestionBankItem.QuestionType.MCQ,
                    "stem": "After the code runs, what is the value of result in this MATLAB example?",
                    "correct_answers": ["7.333333"],
                    "distractors": ["5", "8", "It depends on an unseen file."],
                    "further_study_questions": [
                        "How does the function affect the result?",
                        "What would happen with different inputs?",
                        "Why does filtering matter here?",
                    ],
                    "explanation": "This MATLAB code calculates a mean after filtering.",
                    "difficulty": "core",
                    "is_coding_question": True,
                    "coding_language": "matlab",
                    "coding_question_kind": "comprehension",
                    "code_snippet": """summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)""",
                }
            )

        with patch("standalone.services.questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            practice, _validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.MCQ)

        self.assertIsNotNone(practice)
        self.assertTrue(practice.is_coding_question)
        self.assertEqual(practice.coding_language, "r")
        self.assertIn("summarise_values", practice.code_snippet)
        self.assertNotIn("matlab", practice.stem.lower())
        self.assertNotRegex(practice.stem.lower(), r"what is the value of|after the code runs")

    @override_settings(OPENAI_API_KEY="test-key")
    def test_ai_coding_payload_language_is_forced_to_detected_block_language(self):
        course = self.create_course()
        course.config.coding_question_ratio_percent = 100
        course.config.save(update_fields=["coding_question_ratio_percent", "updated_at"])
        block, _asset, _objective, _chunk = self.create_coding_content_block(
            course,
            extension=".r",
            text='library(tibble)\ndf <- data.frame(x = 1:5, y = letters[1:5])\nprint(df[, "x"])',
        )

        class DummyResponse:
            output_text = json.dumps(
                {
                    "question_type": QuestionBankItem.QuestionType.MCQ,
                    "stem": "What does this code do?",
                    "correct_answers": ["It prints the x column."],
                    "distractors": ["It writes a file.", "It opens a socket.", "It trains a model."],
                    "further_study_questions": [
                        "Why does subsetting matter here?",
                        "How does tibble printing differ?",
                        "What mistake should I avoid with column extraction?",
                    ],
                    "explanation": "The snippet prints a selected column.",
                    "difficulty": "core",
                    "is_coding_question": True,
                    "coding_language": "matlab",
                    "coding_question_kind": "comprehension",
                    "code_snippet": 'library(tibble)\ndf <- data.frame(x = 1:5, y = letters[1:5])\nprint(df[, "x"])',
                }
            )

        with patch("standalone.services.questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            practice, _validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.MCQ)

        self.assertIsNotNone(practice)
        self.assertTrue(practice.is_coding_question)
        self.assertEqual(practice.coding_language, "r")

    @override_settings(OPENAI_API_KEY="test-key")
    def test_openai_coding_prompt_requests_longer_interpretive_examples(self):
        course = self.create_course()
        course.config.coding_question_ratio_percent = 100
        course.config.save(update_fields=["coding_question_ratio_percent", "updated_at"])
        block, _asset, _objective, _chunk = self.create_coding_content_block(
            course,
            extension=".r",
            text="""```r
summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)
```""",
        )
        prompts = []

        class DummyResponse:
            output_text = json.dumps(
                {
                    "question_type": QuestionBankItem.QuestionType.MCQ,
                    "stem": "Which statement best explains how the R function logic and call site work together?",
                    "correct_answers": ["The function filters values before taking the mean, and the printed result depends on that return value."],
                    "distractors": ["The code loads an external file.", "The result is unrelated to the function body.", "The function is never used."],
                    "further_study_questions": [
                        "How would you adapt the function for missing values?",
                        "Why does the call site matter here?",
                        "What mistake should I avoid when tracing returned values?",
                    ],
                    "explanation": "The function returns the mean of positive values and the later lines depend on that return value.",
                    "difficulty": "core",
                    "is_coding_question": True,
                    "coding_language": "r",
                    "coding_question_kind": "comprehension",
                    "code_snippet": """summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)""",
                }
            )

        def capture_create(*args, **kwargs):
            prompts.append(kwargs["input"][1]["content"][0]["text"])
            return DummyResponse()

        with patch("standalone.services.questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.side_effect = capture_create
            practice, _validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.MCQ)

        self.assertIsNotNone(practice)
        self.assertTrue(prompts)
        prompt = prompts[0]
        self.assertIn("6 to 16 meaningful lines", prompt)
        self.assertIn("prefer a named function or helper plus a call site", prompt)
        self.assertIn("do not ask students to manually compute the value of a single variable", prompt)
        self.assertIn("keep the question entirely in R", prompt)

    def test_preview_filters_out_mismatched_coding_language_questions(self):
        course = self.create_course()
        block, _asset, objective, chunk = self.create_coding_content_block(
            course,
            extension=".r",
            text='library(tibble)\ndf <- data.frame(x = 1:5, y = letters[1:5])\nprint(df[, "x"])',
        )
        matlab_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="What does this MATLAB snippet return?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="It returns a column vector.",
            distractors=["It plots a graph.", "It opens a file.", "It sends a request."],
            explanation="This MATLAB snippet returns a vector.",
            question_hash="matlab-mismatch-preview",
            is_coding_question=True,
            coding_language="matlab",
            coding_question_kind=QuestionBankItem.CodingQuestionKind.COMPREHENSION,
            code_snippet="x = (1:5)';\ndisp(x)",
        )
        r_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem='What does this R snippet print?',
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="It prints the x column.",
            distractors=["It deletes x.", "It opens a file.", "It creates a plot."],
            explanation="The R snippet prints a selected column.",
            question_hash="r-preview-question",
            is_coding_question=True,
            coding_language="r",
            coding_question_kind=QuestionBankItem.CodingQuestionKind.COMPREHENSION,
            code_snippet='library(tibble)\ndf <- data.frame(x = 1:5, y = letters[1:5])\nprint(df[, "x"])',
        )
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        question_payload = [message for message in block_payload["transcript"] if message["kind"] == "question"][-1]
        self.assertEqual(question_payload["question_id"], r_question.pk)
        self.assertEqual(question_payload["coding_language"], "r")
        self.assertNotEqual(question_payload["question_id"], matlab_question.pk)

    def test_official_validation_filters_out_coding_questions_with_wrong_language_references(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _asset, objective, chunk = self.create_coding_content_block(
            course,
            extension=".r",
            text="""```r
summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)
```""",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Which statement best explains what this MATLAB function returns?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="It returns a filtered mean.",
            distractors=["It opens a file.", "It calls Simulink.", "It plots a figure."],
            explanation="This MATLAB function returns a filtered mean.",
            question_hash="validation-wrong-language-reference",
            is_coding_question=True,
            coding_language="r",
            coding_question_kind=QuestionBankItem.CodingQuestionKind.COMPREHENSION,
            code_snippet="""summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)""",
        )
        correct_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Which statement best explains how this R function and call site work together?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="The function filters positive values before returning the mean used later in the script.",
            distractors=["The function is never called.", "The code reads from a hidden file.", "The result ignores the function body."],
            explanation="The call site uses the function return value to produce the printed result.",
            question_hash="validation-right-language-reference",
            is_coding_question=True,
            coding_language="r",
            coding_question_kind=QuestionBankItem.CodingQuestionKind.COMPREHENSION,
            code_snippet="""summarise_values <- function(values) {
  cleaned <- values[values > 0]
  mean(cleaned)
}

numbers <- c(4, -2, 8, 10)
result <- summarise_values(numbers)
print(result)""",
        )

        selected = _pick_locked_questions(course, enrollment, 1, include_written=True, blocks=[block])

        self.assertEqual([question.pk for question in selected], [correct_question.pk])

    def test_preview_payload_includes_coding_question_metadata(self):
        course = self.create_course()
        block, _asset, objective, chunk = self.create_coding_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="What does this Python snippet return?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="It returns twice the input value.",
            distractors=["It reads from disk.", "It opens a socket.", "It mutates a global variable."],
            explanation="The function multiplies the argument by two.",
            question_hash="coding-preview-question",
            is_coding_question=True,
            coding_language="python",
            coding_question_kind=QuestionBankItem.CodingQuestionKind.COMPREHENSION,
            code_snippet="def double(value):\n    return value * 2",
        )
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        question_payload = [message for message in block_payload["transcript"] if message["kind"] == "question"][-1]
        self.assertEqual(question_payload["question_id"], question.pk)
        self.assertTrue(question_payload["is_coding_question"])
        self.assertEqual(question_payload["coding_language"], "python")
        self.assertEqual(question_payload["coding_question_kind"], "comprehension")
        self.assertIn("return value * 2", question_payload["code_snippet"])

    def test_generate_question_pair_creates_maq_when_course_is_below_target_ratio(self):
        course = self.create_course()
        course.config.maq_ratio_percent = 100
        course.config.save(update_fields=["maq_ratio_percent", "updated_at"])
        block, _, _, _ = self.create_preview_content_block(course)

        practice, validation = generate_question_pair_for_block(block)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.MAQ)
        self.assertGreaterEqual(len(practice.additional_correct_answers), 1)
        self.assertEqual(validation.question_type, QuestionBankItem.QuestionType.MAQ)
        self.assertEqual(validation.additional_correct_answers, practice.additional_correct_answers)

    def test_generate_question_pair_creates_mcq_when_maq_ratio_is_already_met(self):
        course = self.create_course()
        course.config.maq_ratio_percent = 50
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Existing MAQ question?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="A",
            additional_correct_answers=["B"],
            distractors=["C", "D", "E"],
            explanation="This follows directly from this block.",
            question_hash="existing-maq-question",
        )

        practice, validation = generate_question_pair_for_block(block)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.MCQ)
        self.assertEqual(practice.additional_correct_answers, [])
        self.assertEqual(validation.question_type, QuestionBankItem.QuestionType.MCQ)

    def test_generate_question_pair_creates_waq_when_waq_gap_is_largest(self):
        course = self.create_course()
        course.config.maq_ratio_percent = 20
        course.config.waq_ratio_percent = 100
        course.config.save(update_fields=["maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, _, _, _ = self.create_preview_content_block(course)

        practice, validation = generate_question_pair_for_block(block)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.WAQ)
        self.assertTrue(practice.written_answer_keywords)
        self.assertEqual(practice.distractors, [])
        self.assertEqual(validation.question_type, QuestionBankItem.QuestionType.WAQ)
        self.assertEqual(validation.written_answer_keywords, practice.written_answer_keywords)

    def test_numeric_expression_rejects_executable_python(self):
        with self.assertRaises(NumericQuestionValidationError):
            _evaluate_expression("__import__('os').system('id')", {"charge": 1.0})

    def test_numeric_expression_records_surplus_variables_without_rejecting_question(self):
        value, _tree, used_variables = _evaluate_expression(
            "force / charge",
            {"force": 8.6e-7, "charge": 5e-4, "velocity": 2.0, "viscosity": 1.8e-5},
        )

        self.assertAlmostEqual(value, 0.00172)
        self.assertEqual(used_variables, {"force", "charge"})

    @override_settings(OPENAI_API_KEY="test-key")
    def test_generate_question_pair_creates_numeric_when_numeric_gap_is_largest(self):
        course = self.create_course()
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, asset, _, chunk = self.create_preview_content_block(course)
        chunk.text = "Calculate the speed when a body travels 20 m in 4 s."
        chunk.save(update_fields=["text"])

        openai_payload = {
            "question_type": "num",
            "stem_template": "An object travels {distance} m in {time} s. Calculate its speed.",
            "variables": [
                {"name": "distance", "value": 20, "unit": "m"},
                {"name": "time", "value": 4, "unit": "s"},
            ],
            "calculation_expression": "distance / time",
            "answer_unit": "m/s",
            "significant_figures": 2,
            "explanation": "Speed is distance divided by elapsed time.",
            "difficulty": "core",
            "further_study_questions": [
                "How does changing time affect speed?",
                "When should average speed be used?",
                "How can speed be represented graphically?",
            ],
        }

        class DummyResponse:
            output_text = json.dumps(openai_payload)

        with patch("standalone.services.numeric_questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            practice, validation = generate_question_pair_for_block(block)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.NUM)
        self.assertTrue(practice.is_numerical)
        self.assertIn("Worked solution:", practice.explanation)
        self.assertIn("\\[", practice.explanation)
        self.assertEqual(validation.question_type, QuestionBankItem.QuestionType.NUM)
        self.assertEqual(validation.numeric_metadata["script_version"], "expression-v2")
        self.assertTrue(validation.numeric_metadata["validation"]["expression_evaluated_locally"])
        self.assertTrue(validation.is_numerical)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_numeric_openai_request_uses_structured_json_schema(self):
        course = self.create_course()
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, _asset, _objective, chunk = self.create_preview_content_block(course)
        chunk.text = "Calculate the speed when a body travels 20 m in 4 s."
        chunk.save(update_fields=["text"])

        captured_kwargs = {}

        class DummyResponse:
            output_text = json.dumps(
                {
                    "question_type": "num",
                    "stem_template": "An object travels {distance} m in {time} s. Calculate its speed.",
                    "variables": [
                        {"name": "distance", "value": 20, "unit": "m"},
                        {"name": "time", "value": 4, "unit": "s"},
                    ],
                    "calculation_expression": "distance / time",
                    "answer_unit": "m/s",
                    "significant_figures": 2,
                    "explanation": "Speed is distance divided by elapsed time.",
                    "difficulty": "core",
                    "further_study_questions": [
                        "How does changing time affect speed?",
                        "When should average speed be used?",
                        "How can speed be represented graphically?",
                    ],
                }
            )

        def fake_create(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return DummyResponse()

        with patch("standalone.services.numeric_questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.side_effect = fake_create
            practice, validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.NUM)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(captured_kwargs["text"]["format"]["type"], "json_schema")
        self.assertTrue(captured_kwargs["text"]["format"]["strict"])
        self.assertNotIn("verbosity", captured_kwargs["text"])
        self.assertEqual(
            captured_kwargs["text"]["format"]["schema"]["required"],
            [
                "question_type",
                "stem_template",
                "variables",
                "calculation_expression",
                "answer_unit",
                "significant_figures",
                "explanation",
                "difficulty",
                "further_study_questions",
            ],
        )

    @override_settings(OPENAI_API_KEY="test-key")
    def test_generate_question_pair_rejects_repeated_numeric_angle(self):
        course = self.create_course()
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        objective.text = "Calculate speed from distance and time in motion problems"
        objective.save(update_fields=["text"])
        chunk.text = "Use distance and time to calculate speed in motion problems."
        chunk.save(update_fields=["text"])
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="An object travels 20 m in 4 s. Calculate its speed.",
            question_type=QuestionBankItem.QuestionType.NUM,
            correct_answer="5 m/s",
            distractors=["4 m/s", "16 m/s", "10 m/s"],
            explanation="Use \\(v = d/t\\).",
            question_hash="existing-numeric-speed-angle",
            is_numerical=True,
            numeric_metadata={
                "output_snapshot": {
                    "formula_tex": r"v = \frac{d}{t}",
                }
            },
        )

        class DummyResponse:
            output_text = json.dumps(
                {
                    "question_type": "num",
                    "stem_template": "A cyclist travels {distance} m in {time} s. Calculate the average speed.",
                    "variables": [
                        {"name": "distance", "value": 24, "unit": "m"},
                        {"name": "time", "value": 4, "unit": "s"},
                    ],
                    "calculation_expression": "distance / time",
                    "answer_unit": "m/s",
                    "significant_figures": 2,
                    "explanation": "Average speed is distance divided by elapsed time.",
                    "difficulty": "core",
                    "further_study_questions": [
                        "How does changing time affect speed?",
                        "When should average speed be used?",
                        "How can speed be represented graphically?",
                    ],
                }
            )

        with patch("standalone.services.numeric_questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            practice, validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.NUM)

        self.assertIsNone(practice)
        self.assertIsNone(validation)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_generate_question_pair_returns_none_when_numeric_openai_json_is_malformed(self):
        course = self.create_course()
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, asset, _, chunk = self.create_preview_content_block(course)
        chunk.text = "Calculate the speed when a body travels 20 m in 4 s."
        chunk.save(update_fields=["text"])

        class DummyResponse:
            output_text = """```json
{"question_type":"num","generator_script":"def build_question(seed, inputs):\n    return {\"worked_solution_tex\": \"\\invalid\"}"}
```"""

        with patch("standalone.services.numeric_questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            practice, validation = generate_question_pair_for_block(block)

        self.assertIsNone(practice)
        self.assertIsNone(validation)
        self.assertEqual(mock_client.return_value.responses.create.call_count, 1)

    @override_settings(OPENAI_API_KEY="")
    def test_generate_question_pair_returns_none_without_openai_for_numeric(self):
        course = self.create_course()
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, asset, _, chunk = self.create_preview_content_block(course)
        chunk.text = "Calculate the speed when a body travels 20 m in 4 s."
        chunk.save(update_fields=["text"])

        practice, validation = generate_question_pair_for_block(block)

        self.assertIsNone(practice)
        self.assertIsNone(validation)

    @override_settings(OPENAI_API_KEY="")
    def test_generate_question_pair_raises_generation_error_without_openai_when_requested(self):
        course = self.create_course()
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        block, asset, objective, chunk = self.create_preview_content_block(course, title="Oscillations")
        objective.text = "Calculate the maximum speed and acceleration of oscillators and evaluate conditions causing loss of contact in vibrating systems"
        objective.save(update_fields=["text"])
        chunk.text = "Oscillations involve amplitude, frequency, resonance, and loss of contact in vibrating systems."
        chunk.save(update_fields=["text"])

        with self.assertRaises(QuestionGenerationError):
            generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.NUM, raise_generation_errors=True)

    @override_settings(OPENAI_API_KEY="")
    def test_generated_question_stem_is_independent_of_stored_text(self):
        course = self.create_course()
        block, _, _, chunk = self.create_preview_content_block(course)
        chunk.text = "Worked example: figure 3 in chapter 2 shows an arrangement used to accelerate electrons."
        chunk.save(update_fields=["text"])

        practice, validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.MCQ)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.MCQ)
        self.assertNotIn("worked example", practice.stem.lower())
        self.assertNotIn("figure", practice.stem.lower())
        self.assertNotIn("chapter", practice.stem.lower())

    def test_generate_question_pair_includes_further_study_questions(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)

        practice, validation = generate_question_pair_for_block(block)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(len(practice.further_study_questions), 3)
        self.assertTrue(all(question.endswith("?") for question in practice.further_study_questions))
        self.assertEqual(validation.further_study_questions, practice.further_study_questions)

    def test_fallback_further_study_questions_strip_objective_command_language(self):
        questions = fallback_further_study_questions(
            objective_text="Interpret the interconnectedness of earth sciences and life sciences in understanding biological history",
            correct_answer="It allows scientists to infer that studying other organisms, like yeast or mice, can clarify human biology.",
        )

        self.assertEqual(len(questions), 3)
        self.assertTrue(all(question.endswith("?") for question in questions))
        self.assertTrue(all(" interpret " not in f" {question.lower()} " for question in questions))
        self.assertTrue(all("with it allows" not in question.lower() for question in questions))

    def test_further_study_questions_for_question_falls_back_when_stored_prompts_are_weird(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        objective.text = "Interpret the interconnectedness of earth sciences and life sciences in understanding biological history"
        objective.save(update_fields=["text"])
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Why does comparative biology matter?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="It allows scientists to infer that studying other organisms, like yeast or mice, can clarify human biology.",
            distractors=["It removes the need for experiments", "It only applies to plants", "It prevents evolution"],
            explanation="This follows directly from this block.",
            question_hash="weird-further-study-prompts",
            further_study_questions=[
                "Can you show a simple example of interpret the interconnectedness of earth sciences and life sciences in understanding biological history?",
                "How would you explain interpret the interconnectedness of earth sciences and life sciences in understanding biological history in your own?",
                "What common mistake or misconception should I avoid with it allows scientists to infer that studying other organisms, like yeast or mice, c?",
            ],
        )

        cleaned_questions = further_study_questions_for_question(question)

        self.assertEqual(len(cleaned_questions), 3)
        self.assertTrue(all(question.endswith("?") for question in cleaned_questions))
        self.assertTrue(all(" interpret " not in f" {question.lower()} " for question in cleaned_questions))
        self.assertTrue(all("with it allows" not in question.lower() for question in cleaned_questions))

    def test_generate_question_pair_spreads_waq_generation_across_objectives_before_repeating(self):
        course = self.create_course()
        block, asset, objective_a, chunk = self.create_preview_content_block(course)
        objective_b = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=2,
            code="1.2",
            text="Explain signalling in cells",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective_a,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="existing-preview-waq-objective-a",
        )

        practice, validation = generate_question_pair_for_block(block, question_type=QuestionBankItem.QuestionType.WAQ)

        self.assertIsNotNone(practice)
        self.assertIsNotNone(validation)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.WAQ)
        self.assertEqual(practice.learning_objective_id, objective_b.pk)
        self.assertEqual(validation.learning_objective_id, objective_b.pk)

    def test_student_practice_quiz_answer_persists_and_reload_reconstructs_transcript(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _, objective, chunk = self.create_preview_content_block(course)
        q1 = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Question one?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="hash-1",
        )
        self.client.force_login(self.student)
        page = self.client.get(reverse("standalone:practice_quiz", args=[course.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "preview-chat-shell", html=False)
        self.assertContains(page, "student-preview-data", html=False)
        self.assertContains(page, reverse("standalone:student_practice_action", args=[course.pk, 0, "ACTION"]), html=False)
        self.assertTrue(PracticeMessage.objects.filter(enrollment=enrollment, kind="text").exists())
        self.assertEqual(PracticeAttempt.objects.count(), 0)

        quiz_response = self.client.post(reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]))
        self.assertEqual(quiz_response.status_code, 200)
        block_payload = next(item for item in quiz_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_id"], q1.pk)
        self.assertEqual(enrollment.question_states.get(question=q1).times_presented, 1)
        self.assertTrue(PracticeMessage.objects.filter(enrollment=enrollment, kind="question", question=q1).exists())

        answer_response = self.client.post(
            reverse("standalone:student_practice_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": q1.pk, "answer": "A"}),
            content_type="application/json",
        )

        self.assertEqual(answer_response.status_code, 200)
        self.assertEqual(PracticeAttempt.objects.filter(enrollment=enrollment).count(), 1)
        self.assertEqual(PracticeAttemptQuestion.objects.filter(attempt__enrollment=enrollment, question=q1, is_correct=True).count(), 1)
        self.assertTrue(PracticeMessage.objects.filter(enrollment=enrollment, kind="feedback").exists())
        enrollment.refresh_from_db()
        self.assertEqual(float(enrollment.mastery_score), 100.0)
        self.assertEqual(float(enrollment.coverage_score), 100.0)
        self.assertEqual(float(enrollment.engagement_score), 5.0)
        self.assertEqual(float(enrollment.target_score), 5.0)

        reload_response = self.client.get(reverse("standalone:practice_quiz", args=[course.pk]))
        self.assertContains(reload_response, "Question one?")
        self.assertContains(reload_response, "Correct.")

    def test_student_practice_skips_future_block_questions(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        released_block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        future_block = CourseBlock.objects.create(
            course=course,
            title="Week 2",
            order=2,
            available_from=timezone.localdate() + timedelta(days=5),
        )
        QuestionBankItem.objects.create(
            course=course,
            block=released_block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Released question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="released-hash",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=future_block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Future question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="future-hash",
        )

        self.client.force_login(self.student)
        released_response = self.client.post(reverse("standalone:student_practice_action", args=[course.pk, released_block.pk, "quiz"]))
        future_response = self.client.post(reverse("standalone:student_practice_action", args=[course.pk, future_block.pk, "quiz"]))

        released_block_payload = next(item for item in released_response.json()["preview"]["blocks"] if item["id"] == released_block.pk)
        released_questions = [message for message in released_block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(released_questions[-1]["text"], "Released question?")
        future_block_payload = next(item for item in future_response.json()["preview"]["blocks"] if item["id"] == future_block.pk)
        self.assertTrue(any("becomes available" in message["text"] for message in future_block_payload["transcript"] if message["kind"] == "text"))
        self.assertFalse(any(message.get("text") == "Future question?" for message in future_block_payload["transcript"]))

    def test_student_practice_flag_persists_and_skips_question(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, _, objective, chunk = self.create_preview_content_block(course)
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Flag me?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="First explanation.",
            question_hash="student-practice-flag-a",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Use me next?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Second explanation.",
            question_hash="student-practice-flag-b",
        )
        self.client.force_login(self.student)

        self.client.post(reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]))
        flag_response = self.client.post(
            reverse("standalone:student_practice_action", args=[course.pk, block.pk, "flag"]),
            data=json.dumps({"question_id": first_question.pk}),
            content_type="application/json",
        )
        next_response = self.client.post(reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(flag_response.status_code, 200)
        self.assertTrue(QuestionFlag.objects.filter(enrollment__student=self.student, question=first_question).exists())
        block_payload = next(item for item in next_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_id"], second_question.pk)

    def test_student_practice_supports_maq_and_waq_with_draft_alignment(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _, objective, chunk = self.create_preview_content_block(course)
        maq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Select all correct ideas?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["Gravity drives diffusion", "DNA replication happens in the nucleus"],
            explanation="This follows directly from this block.",
            question_hash="student-practice-maq",
        )
        first_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="student-practice-waq",
        )
        self.client.force_login(self.student)

        with self.settings(OPENAI_API_KEY=""):
            maq_quiz = self.client.post(
                reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]),
                data=json.dumps({"question_type": QuestionBankItem.QuestionType.MAQ}),
                content_type="application/json",
            ).json()["preview"]
            maq_message = [message for message in maq_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]
            self.assertEqual(maq_message["question_id"], maq.pk)
            self.client.post(
                reverse("standalone:student_practice_action", args=[course.pk, block.pk, "answer"]),
                data=json.dumps(
                    {
                        "question_id": maq.pk,
                        "answers": ["Membranes regulate transport", "Signalling coordinates responses"],
                    }
                ),
                content_type="application/json",
            )
            draft_response = self.client.post(
                reverse("standalone:student_practice_action", args=[course.pk, block.pk, "draft_answer"]),
                data=json.dumps({"question_id": waq.pk, "answer_text": "Membranes regulate transport into and out of cells."}),
                content_type="application/json",
            )
            self.assertEqual(draft_response.json()["alignment"]["alignment_score"], 0)
            waq_quiz = self.client.post(
                reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]),
                data=json.dumps({"question_type": QuestionBankItem.QuestionType.WAQ}),
                content_type="application/json",
            ).json()["preview"]
            waq_message = [message for message in waq_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]
            self.assertEqual(waq_message["question_id"], waq.pk)
            draft_response = self.client.post(
                reverse("standalone:student_practice_action", args=[course.pk, block.pk, "draft_answer"]),
                data=json.dumps({"question_id": waq.pk, "answer_text": "Membranes regulate what enters and leaves cells."}),
                content_type="application/json",
            )
            self.assertGreater(draft_response.json()["alignment"]["alignment_score"], 0)
            self.client.post(
                reverse("standalone:student_practice_action", args=[course.pk, block.pk, "answer"]),
                data=json.dumps({"question_id": waq.pk, "answer_text": "Membranes regulate what enters and leaves cells."}),
                content_type="application/json",
            )

        self.assertEqual(PracticeAttemptQuestion.objects.filter(attempt__enrollment=enrollment, question=maq, is_correct=True).count(), 1)
        self.assertEqual(PracticeAttemptQuestion.objects.filter(attempt__enrollment=enrollment, question=waq, is_correct=True).count(), 1)

    def test_student_practice_advanced_question_types_unlock_at_configured_threshold(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        block.config.target_question_count = 2
        block.config.save(update_fields=["target_question_count", "updated_at"])
        Enrollment.objects.create(course=course, student=self.student)
        mcq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer first?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="student-practice-threshold-mcq",
        )
        maq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Select all after threshold?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["Gravity drives diffusion", "DNA replication happens in the nucleus"],
            explanation="This follows directly from this block.",
            question_hash="student-practice-threshold-maq",
        )
        self.client.force_login(self.student)

        first_response = self.client.post(
            reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.MAQ}),
            content_type="application/json",
        ).json()["preview"]
        first_message = [message for message in first_response["blocks"][0]["transcript"] if message["kind"] == "question"][-1]
        self.assertEqual(first_message["question_id"], mcq.pk)
        self.client.post(
            reverse("standalone:student_practice_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": mcq.pk, "answer": "A"}),
            content_type="application/json",
        )
        unlocked_response = self.client.post(
            reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.MAQ}),
            content_type="application/json",
        ).json()["preview"]
        unlocked_message = [message for message in unlocked_response["blocks"][0]["transcript"] if message["kind"] == "question"][-1]
        self.assertEqual(unlocked_message["question_id"], maq.pk)
        self.assertTrue(unlocked_response["blocks"][0]["metrics"]["advanced_question_types_unlocked"])

    def test_student_practice_action_rejects_teacher_and_other_student(self):
        course = self.create_course()
        other_student = User.objects.create_user(
            username="other-student",
            email="other-student@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )
        Enrollment.objects.create(course=course, student=other_student)
        block, _, _, _ = self.create_preview_content_block(course)

        self.client.force_login(self.teacher)
        teacher_response = self.client.post(reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]))
        self.client.force_login(self.student)
        other_student_response = self.client.post(reverse("standalone:student_practice_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(teacher_response.status_code, 404)
        self.assertEqual(other_student_response.status_code, 404)

    def test_validation_practice_redirects_when_no_released_questions_are_available(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        future_block = CourseBlock.objects.create(
            course=course,
            title="Week 1",
            order=1,
            available_from=timezone.localdate() + timedelta(days=3),
        )
        QuestionBankItem.objects.create(
            course=course,
            block=future_block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Future question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="future-only-hash",
        )

        self.client.force_login(self.student)
        response = self.client.get(f"{reverse('standalone:practice_quiz', args=[course.pk])}?mode=validation_practice", follow=True)

        self.assertRedirects(response, reverse("standalone:student_dashboard"))
        self.assertContains(response, "No validation questions are available yet for this course.")
        self.assertEqual(PracticeAttempt.objects.count(), 0)

    def test_student_preview_page_renders_launches_full_screen_chat_shell(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exit student view")
        self.assertContains(response, "preview-chat-shell", html=False)
        self.assertContains(response, "preview-block-switcher", html=False)
        self.assertContains(response, "student-preview-data", html=False)
        self.assertContains(response, "data-waq-alignment-loader", html=False)
        self.assertContains(response, "data-preview-sidebar-summary-toggle", html=False)
        self.assertContains(response, "data-preview-course-metrics", html=False)
        self.assertContains(response, block.title)
        self.assertContains(response, "Quiz")

    def test_student_preview_course_metrics_average_available_block_metrics(self):
        course = self.create_course()
        first_block, _, first_objective, _ = self.create_preview_content_block(course, title="Week 1", order=1)
        second_block, _, second_objective, _ = self.create_preview_content_block(course, title="Week 2", order=2)
        future_block, _, _, _ = self.create_preview_content_block(course, title="Week 3", order=3)
        future_block.available_from = timezone.localdate() + timedelta(days=14)
        future_block.save(update_fields=["available_from"])
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=first_block,
            learning_objective=first_objective,
            source_chunk=first_block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="First preview question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="First explanation.",
            question_hash="course-metrics-preview-1",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=second_block,
            learning_objective=second_objective,
            source_chunk=second_block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Second preview question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Second explanation.",
            question_hash="course-metrics-preview-2",
        )
        self.client.force_login(self.teacher)

        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, first_block.pk, "quiz"]))
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, first_block.pk, "answer"]),
            data=json.dumps({"question_id": first_question.pk, "answer": "A"}),
            content_type="application/json",
        )

        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, second_block.pk, "quiz"]))
        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, second_block.pk, "answer"]),
            data=json.dumps({"question_id": second_question.pk, "answer": "B"}),
            content_type="application/json",
        )

        self.assertEqual(answer_response.status_code, 200)
        preview = answer_response.json()["preview"]
        course_metrics = preview["course"]["metrics"]
        self.assertEqual(course_metrics["mastery"], 50.0)
        self.assertEqual(course_metrics["coverage"], 50.0)
        self.assertEqual(course_metrics["engagement"], 5.0)
        self.assertEqual(course_metrics["target"], 5.0)
        self.assertEqual(course_metrics["overall"], 36.5)
        self.assertEqual(course_metrics["correct_count"], 1)
        self.assertEqual(course_metrics["incorrect_count"], 1)
        self.assertEqual(course_metrics["completed_count"], 2)
        self.assertEqual(course_metrics["covered_objective_count"], 1)
        self.assertEqual(course_metrics["total_objective_count"], 3)
        self.assertEqual(course_metrics["on_time_count"], 2)
        self.assertEqual(course_metrics["combined_target_question_count"], 40)
        self.assertEqual(course_metrics["engagement_window_days"], 7)
        self.assertEqual(
            course_metrics["weights"],
            {
                "mastery": 40,
                "coverage": 30,
                "engagement": 20,
                "target": 10,
                "total": 100,
            },
        )

        first_block_metrics = next(block["metrics"] for block in preview["blocks"] if block["id"] == first_block.pk)
        self.assertEqual(first_block_metrics["overall"], 71.5)
        self.assertEqual(first_block_metrics["correct_count"], 1)
        self.assertEqual(first_block_metrics["incorrect_count"], 0)
        self.assertEqual(first_block_metrics["completed_count"], 1)
        self.assertEqual(first_block_metrics["covered_objective_count"], 1)
        self.assertEqual(first_block_metrics["total_objective_count"], 1)
        self.assertEqual(first_block_metrics["on_time_count"], 1)
        self.assertEqual(first_block_metrics["engagement_window_days"], 7)

        second_block_metrics = next(block["metrics"] for block in preview["blocks"] if block["id"] == second_block.pk)
        self.assertEqual(second_block_metrics["overall"], 1.5)

    def test_student_preview_quiz_uses_existing_bank_before_generating_new_pair(self):
        course = self.create_course()
        block, asset, objective, _ = self.create_preview_content_block(course)
        validation = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Bank question? (validation)",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="bank-validation-hash",
        )
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Bank question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Because it matches what this block covers.",
            question_hash="bank-practice-hash",
            linked_question=validation,
        )
        validation.linked_question = practice
        validation.save(update_fields=["linked_question", "updated_at"])

        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(response.status_code, 200)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_id"], practice.pk)
        self.assertEqual(course.question_bank_items.filter(block=block).count(), 2)

    def test_student_preview_quiz_generates_question_pair_when_bank_is_exhausted(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(response.status_code, 200)
        practice_items = list(course.question_bank_items.filter(block=block, bank_type=QuestionBankItem.BankType.PRACTICE))
        validation_items = list(course.question_bank_items.filter(block=block, bank_type=QuestionBankItem.BankType.VALIDATION))
        self.assertEqual(len(practice_items), 1)
        self.assertEqual(len(validation_items), 1)
        self.assertEqual(practice_items[0].linked_question_id, validation_items[0].pk)
        self.assertEqual(validation_items[0].stem, practice_items[0].stem)
        self.assertNotIn("validation variant", validation_items[0].stem.lower())

    def test_student_preview_forced_question_type_prefers_matching_bank_item(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer question?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="forced-mcq-preview",
        )
        forced_maq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Select all correct ideas?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["Gravity drives diffusion", "DNA replication happens in the nucleus"],
            explanation="This follows directly from this block.",
            question_hash="forced-maq-preview",
        )
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.MAQ}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_id"], forced_maq.pk)
        self.assertEqual(question_messages[-1]["question_type"], QuestionBankItem.QuestionType.MAQ)

    def test_student_preview_forced_numeric_question_type_prefers_matching_bank_item(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer question?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="forced-mcq-preview-num",
        )
        forced_num = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Calculate the speed for a body travelling 20 m in 4 s.",
            question_type=QuestionBankItem.QuestionType.NUM,
            correct_answer="5 m/s",
            distractors=["4 m/s", "16 m/s", "10 m/s"],
            explanation="Use \\(v = d/t\\).",
            question_hash="forced-num-preview",
            is_numerical=True,
            numeric_metadata={"script_version": "v1"},
        )
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.NUM}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_id"], forced_num.pk)
        self.assertEqual(question_messages[-1]["question_type"], QuestionBankItem.QuestionType.NUM)
        self.assertTrue(question_messages[-1]["is_numerical"])
        self.assertEqual(question_messages[-1]["question_type_label"], "Numerical MCQ")

    @override_settings(OPENAI_API_KEY="")
    def test_student_preview_forced_numeric_question_type_shows_error_without_openai(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course, title="Oscillations")
        objective.text = "Calculate the maximum speed and acceleration of oscillators and evaluate conditions causing loss of contact in vibrating systems"
        objective.save(update_fields=["text"])
        chunk.text = "Oscillations involve amplitude, frequency, resonance, and loss of contact in vibrating systems."
        chunk.save(update_fields=["text"])
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.NUM}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        text_messages = [message for message in block_payload["transcript"] if message["kind"] == "text"]
        self.assertIn("Could not generate a numerical MCQ", text_messages[-1]["text"])

    @override_settings(OPENAI_API_KEY="test-key")
    def test_student_preview_forced_numeric_question_type_shows_error_when_openai_request_fails(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(
            update_fields=[
                "advanced_question_start_percent",
                "numeric_ratio_percent",
                "maq_ratio_percent",
                "waq_ratio_percent",
                "updated_at",
            ]
        )
        block, _, objective, chunk = self.create_preview_content_block(course, title="Electric Fields")
        objective.text = "Calculate electric field strength and force for charges in uniform electric fields"
        objective.save(update_fields=["text"])
        chunk.text = "Electric field strength, force, charge, and potential difference are related quantitatively."
        chunk.save(update_fields=["text"])
        self.client.force_login(self.teacher)

        with patch("standalone.services.numeric_questions.OpenAI") as mock_client:
            mock_client.return_value.responses.create.side_effect = OpenAIError("invalid_json_schema")
            response = self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
                data=json.dumps({"question_type": QuestionBankItem.QuestionType.NUM}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_client.return_value.responses.create.call_count, 1)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        text_messages = [message for message in block_payload["transcript"] if message["kind"] == "text"]
        self.assertIn("Could not generate a numerical MCQ", text_messages[-1]["text"])

    def test_student_preview_forced_advanced_types_use_mcq_until_threshold_met(self):
        course = self.create_course()
        maq_block, _, maq_objective, maq_chunk = self.create_preview_content_block(course, title="Week 1", order=1)
        waq_block, _, waq_objective, waq_chunk = self.create_preview_content_block(course, title="Week 2", order=2)
        for block in (maq_block, waq_block):
            block.config.target_question_count = 2
            block.config.save(update_fields=["target_question_count", "updated_at"])

        maq_mcq = QuestionBankItem.objects.create(
            course=course,
            block=maq_block,
            learning_objective=maq_objective,
            source_chunk=maq_chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer MAQ block question?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="locked-maq-mcq-preview",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=maq_block,
            learning_objective=maq_objective,
            source_chunk=maq_chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Select all correct ideas?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["Gravity drives diffusion", "DNA replication happens in the nucleus"],
            explanation="This follows directly from this block.",
            question_hash="locked-maq-preview",
        )
        waq_mcq = QuestionBankItem.objects.create(
            course=course,
            block=waq_block,
            learning_objective=waq_objective,
            source_chunk=waq_chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer WAQ block question?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="locked-waq-mcq-preview",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=waq_block,
            learning_objective=waq_objective,
            source_chunk=waq_chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="locked-waq-preview",
        )
        self.client.force_login(self.teacher)

        cases = [
            (maq_block, QuestionBankItem.QuestionType.MAQ, maq_mcq.pk),
            (waq_block, QuestionBankItem.QuestionType.WAQ, waq_mcq.pk),
        ]
        for block, requested_type, expected_question_id in cases:
            with self.subTest(requested_type=requested_type):
                response = self.client.post(
                    reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
                    data=json.dumps({"question_type": requested_type}),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 200)
                block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
                question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
                self.assertEqual(question_messages[-1]["question_id"], expected_question_id)
                self.assertEqual(question_messages[-1]["question_type"], QuestionBankItem.QuestionType.MCQ)
                self.assertFalse(block_payload["metrics"]["advanced_question_types_unlocked"])
                self.assertEqual(block_payload["metrics"]["advanced_question_start_percent"], 50)

    def test_student_preview_forced_numeric_type_is_not_locked_by_advanced_threshold(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        block.config.target_question_count = 2
        block.config.save(update_fields=["target_question_count", "updated_at"])
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Calculate the speed for a body travelling 20 m in 4 s.",
            question_type=QuestionBankItem.QuestionType.NUM,
            correct_answer="5 m/s",
            distractors=["4 m/s", "16 m/s", "10 m/s"],
            explanation="Use \\(v = d/t\\).",
            question_hash="threshold-unlock-num-preview",
            is_numerical=True,
            numeric_metadata={"script_version": "v1"},
        )
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.NUM}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_type"], QuestionBankItem.QuestionType.NUM)

    def test_student_preview_forced_advanced_type_unlocks_at_configured_target_progress(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        block.config.target_question_count = 2
        block.config.save(update_fields=["target_question_count", "updated_at"])
        first_mcq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer question?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="threshold-unlock-mcq-preview",
        )
        unlocked_maq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Select all correct ideas?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["Gravity drives diffusion", "DNA replication happens in the nucleus"],
            explanation="This follows directly from this block.",
            question_hash="threshold-unlock-maq-preview",
        )
        self.client.force_login(self.teacher)

        first_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        first_block_payload = next(item for item in first_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        first_question_messages = [message for message in first_block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(first_question_messages[-1]["question_id"], first_mcq.pk)
        self.assertFalse(first_block_payload["metrics"]["advanced_question_types_unlocked"])

        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": first_mcq.pk, "answer": "A"}),
            content_type="application/json",
        )

        unlocked_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.MAQ}),
            content_type="application/json",
        )

        self.assertEqual(unlocked_response.status_code, 200)
        unlocked_block_payload = next(item for item in unlocked_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        unlocked_question_messages = [message for message in unlocked_block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(unlocked_question_messages[-1]["question_id"], unlocked_maq.pk)
        self.assertEqual(unlocked_question_messages[-1]["question_type"], QuestionBankItem.QuestionType.MAQ)
        self.assertTrue(unlocked_block_payload["metrics"]["advanced_question_types_unlocked"])

    def test_student_preview_question_payload_includes_further_study_questions(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Which statement best explains membrane transport?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="Membranes control what enters and leaves the cell",
            distractors=["Mitochondria store DNA", "Gravity drives transport", "Ribosomes digest proteins"],
            explanation="This follows directly from this block.",
            question_hash="preview-further-study-fallback",
        )
        self.client.force_login(self.teacher)

        response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(response.status_code, 200)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(len(question_messages[-1]["further_study_questions"]), 3)
        self.assertTrue(all(question.endswith("?") for question in question_messages[-1]["further_study_questions"]))

    def test_student_preview_forced_question_type_generates_requested_type(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)
        course.config.maq_ratio_percent = 0
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["maq_ratio_percent", "advanced_question_start_percent", "updated_at"])
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.MAQ}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        practice = course.question_bank_items.get(block=block, bank_type=QuestionBankItem.BankType.PRACTICE)
        validation = course.question_bank_items.get(block=block, bank_type=QuestionBankItem.BankType.VALIDATION)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.MAQ)
        self.assertEqual(validation.question_type, QuestionBankItem.QuestionType.MAQ)

    def test_student_preview_forced_waq_prefers_matching_bank_item(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Single answer question?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="forced-mcq-preview-waq",
        )
        forced_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="forced-waq-preview",
        )
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.WAQ}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()["preview"]
        block_payload = next(item for item in preview["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(question_messages[-1]["question_id"], forced_waq.pk)
        self.assertEqual(question_messages[-1]["question_type"], QuestionBankItem.QuestionType.WAQ)
        self.assertEqual(question_messages[-1]["options"], [])

    def test_student_preview_forced_waq_generates_requested_type(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)
        course.config.waq_ratio_percent = 0
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["waq_ratio_percent", "advanced_question_start_percent", "updated_at"])
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.WAQ}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        practice = course.question_bank_items.get(block=block, bank_type=QuestionBankItem.BankType.PRACTICE)
        validation = course.question_bank_items.get(block=block, bank_type=QuestionBankItem.BankType.VALIDATION)
        self.assertEqual(practice.question_type, QuestionBankItem.QuestionType.WAQ)
        self.assertEqual(validation.question_type, QuestionBankItem.QuestionType.WAQ)
        self.assertTrue(practice.written_answer_keywords)

    def test_student_preview_forced_waq_prefers_fresher_objective_before_repeating_same_one(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, asset, objective_a, chunk = self.create_preview_content_block(course)
        objective_b = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=2,
            code="1.2",
            text="Explain signalling in cells",
        )
        first_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective_a,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-repeat-a-1",
        )
        repeat_waq_same_objective = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective_a,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Why does membrane transport matter here?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-repeat-a-2",
        )
        fresher_objective_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective_b,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain signalling in cells?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Cell signalling coordinates responses to internal and external cues.",
            written_answer_keywords=["signalling", "coordinates responses", "external cues"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-fresher-b",
        )
        self.client.force_login(self.teacher)

        first_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.WAQ}),
            content_type="application/json",
        )
        first_block_payload = next(item for item in first_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        first_question_messages = [message for message in first_block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(first_question_messages[-1]["question_id"], first_waq.pk)

        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps(
                {
                    "question_id": first_waq.pk,
                    "answer_text": "Membranes regulate what enters and leaves the cell.",
                }
            ),
            content_type="application/json",
        )

        second_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]),
            data=json.dumps({"question_type": QuestionBankItem.QuestionType.WAQ}),
            content_type="application/json",
        )
        second_block_payload = next(item for item in second_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        second_question_messages = [message for message in second_block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(second_question_messages[-1]["question_id"], fresher_objective_waq.pk)
        self.assertNotEqual(second_question_messages[-1]["question_id"], repeat_waq_same_objective.pk)

    def test_student_preview_generation_prioritises_first_unmet_objective(self):
        course = self.create_course()
        block, asset, first_objective, first_chunk = self.create_preview_content_block(course)
        second_objective = LearningObjective.objects.create(
            course=course,
            block=block,
            source_asset=asset,
            position=2,
            code="1.2",
            text="Describe signalling pathways",
        )
        ContentChunk.objects.create(
            asset=asset,
            course=course,
            block=block,
            ordinal=2,
            text="Signalling pathways coordinate receptor activity and cellular responses.",
            token_count=10,
            checksum="week-1-signalling-chunk",
        )
        existing_practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=first_objective,
            source_chunk=first_chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Objective one question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows directly from this block.",
            question_hash="objective-one-existing",
        )

        self.client.force_login(self.teacher)

        first_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        first_question = [message for message in first_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(first_question, existing_practice.pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data='{"question_id": %s, "answer": "A"}' % existing_practice.pk,
            content_type="application/json",
        )

        second_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        generated_question_id = [message for message in second_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        generated_question = QuestionBankItem.objects.get(pk=generated_question_id)

        self.assertNotEqual(generated_question.pk, existing_practice.pk)
        self.assertEqual(generated_question.learning_objective_id, second_objective.pk)

    def test_student_preview_answer_updates_feedback_without_creating_real_attempts(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="This follows from the approved notes.",
            question_hash="preview-question-hash",
        )
        self.client.force_login(self.teacher)

        quiz_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        question_id = [message for message in quiz_response.json()["preview"]["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data='{"question_id": %s, "answer": "A"}' % question_id,
            content_type="application/json",
        )

        self.assertEqual(answer_response.status_code, 200)
        block_payload = next(item for item in answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        feedback_messages = [message for message in block_payload["transcript"] if message["kind"] == "feedback"]
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertTrue(feedback_messages)
        self.assertIn("Correct.", feedback_messages[-1]["text"])
        self.assertEqual(question_messages[-1]["correct_answers"], ["A"])
        self.assertEqual(PracticeAttempt.objects.count(), 0)
        self.assertEqual(EnrollmentQuestionState.objects.count(), 0)
        self.assertEqual(PracticeMessage.objects.count(), 0)

    def test_student_preview_feedback_includes_varying_keep_going_note(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview question one?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="First explanation.",
            question_hash="preview-feedback-keep-going-1",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview question two?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Second explanation.",
            question_hash="preview-feedback-keep-going-2",
        )
        self.client.force_login(self.teacher)

        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        session = self.client.session
        transcript = session[PREVIEW_SESSION_KEY][str(course.pk)]["transcripts"][str(block.pk)]
        transcript[-1]["created_at"] = (timezone.now() - timedelta(minutes=6)).isoformat()
        session.modified = True
        session.save()
        first_answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": first_question.pk, "answer": "A"}),
            content_type="application/json",
        )
        first_block_payload = next(item for item in first_answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        first_feedback_messages = [message for message in first_block_payload["transcript"] if message["kind"] == "feedback"]

        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        session = self.client.session
        transcript = session[PREVIEW_SESSION_KEY][str(course.pk)]["transcripts"][str(block.pk)]
        transcript[-1]["created_at"] = (timezone.now() - timedelta(minutes=6)).isoformat()
        session.modified = True
        session.save()
        second_answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": second_question.pk, "answer": "A"}),
            content_type="application/json",
        )
        second_block_payload = next(item for item in second_answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        second_feedback_messages = [message for message in second_block_payload["transcript"] if message["kind"] == "feedback"]

        self.assertIn("Quiz", first_feedback_messages[-1]["text"])
        self.assertIn("Quiz", second_feedback_messages[-1]["text"])
        self.assertNotEqual(first_feedback_messages[-1]["text"], second_feedback_messages[-1]["text"])

    def test_student_preview_feedback_omits_keep_going_note_when_answer_is_immediate(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Quick explanation.",
            question_hash="preview-feedback-no-delay",
        )
        self.client.force_login(self.teacher)

        quiz_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        question_id = [message for message in quiz_response.json()["preview"]["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": question_id, "answer": "A"}),
            content_type="application/json",
        )

        block_payload = next(item for item in answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        feedback_messages = [message for message in block_payload["transcript"] if message["kind"] == "feedback"]
        self.assertNotIn("Hit Quiz", feedback_messages[-1]["text"])
        self.assertNotIn("Tap Quiz", feedback_messages[-1]["text"])

    def test_student_preview_waq_draft_answer_updates_alignment(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-draft",
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "draft_answer"]),
            data=json.dumps(
                {
                    "question_id": practice.pk,
                    "answer_text": "Membranes regulate what enters and leaves the cell.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        alignment = response.json()["alignment"]
        self.assertEqual(alignment["question_id"], practice.pk)
        self.assertEqual(alignment["alignment_state"], "aligned")
        self.assertGreaterEqual(alignment["alignment_score"], 75)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_student_preview_waq_draft_answer_uses_semantic_check_by_character_bucket(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-draft-semantic",
        )
        first_answer = "The cell boundary controls what moves in and out."
        third_answer = f"{first_answer} It manages exchange."
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        class DummyResponse:
            def __init__(self, score):
                self.output_text = json.dumps(
                    {
                        "aligned": True,
                        "score": score,
                        "feedback": "Good answer.",
                    }
                )

        with patch("standalone.services.preview.OpenAI") as mock_client:
            mock_client.return_value.responses.create.side_effect = [DummyResponse(0.84), DummyResponse(0.88)]
            first_response = self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "draft_answer"]),
                data=json.dumps(
                    {
                        "question_id": practice.pk,
                        "answer_text": first_answer,
                    }
                ),
                content_type="application/json",
            )
            second_response = self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "draft_answer"]),
                data=json.dumps(
                    {
                        "question_id": practice.pk,
                        "answer_text": first_answer,
                    }
                ),
                content_type="application/json",
            )
            third_response = self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "draft_answer"]),
                data=json.dumps(
                    {
                        "question_id": practice.pk,
                        "answer_text": third_answer,
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(third_response.status_code, 200)
        self.assertEqual(first_response.json()["alignment"]["alignment_state"], "aligned")
        self.assertEqual(second_response.json()["alignment"]["alignment_state"], "aligned")
        self.assertEqual(third_response.json()["alignment"]["alignment_state"], "aligned")
        self.assertEqual(mock_client.return_value.responses.create.call_count, 2)
        self.assertEqual(second_response.json()["alignment"]["alignment_score"], 84)
        self.assertEqual(third_response.json()["alignment"]["alignment_score"], 88)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_student_preview_chat_uses_openai_for_course_questions(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course, title="Origins of Life")
        self.client.force_login(self.teacher)

        class DummyResponse:
            output_text = "A eukaryotic cell has DNA enclosed in a nucleus and contains specialised structures such as mitochondria."

        with patch("standalone.services.preview.OpenAI") as mock_client:
            mock_client.return_value.responses.create.return_value = DummyResponse()
            response = self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "chat"]),
                data='{"question": "what is a eukaryotic cell?"}',
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        assistant_messages = [message for message in block_payload["transcript"] if message["role"] == "assistant" and message["kind"] == "text"]
        self.assertEqual(
            assistant_messages[-1]["text"],
            "A eukaryotic cell has DNA enclosed in a nucleus and contains specialised structures such as mitochondria.",
        )
        self.assertEqual(len(assistant_messages[-1]["further_study_questions"]), 3)
        self.assertTrue(all(question.endswith("?") for question in assistant_messages[-1]["further_study_questions"]))

    def test_student_preview_chat_warns_on_inappropriate_message(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "chat"]),
            data='{"question": "you are stupid"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        assistant_messages = [message for message in block_payload["transcript"] if message["role"] == "assistant" and message["kind"] == "text"]
        self.assertEqual(
            assistant_messages[-1]["text"],
            "Please keep messages respectful and appropriate. All conversations are logged and auditable by teachers.",
        )

    def test_student_preview_quiz_re_surfaces_pending_question_after_chat_message(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="What does the membrane do?",
            question_type=QuestionBankItem.QuestionType.MCQ,
            correct_answer="It regulates transport.",
            distractors=["It stores DNA.", "It produces ATP.", "It breaks down glucose."],
            explanation="Membranes regulate movement into and out of cells.",
            question_hash="preview-pending-question-resurface",
        )
        self.client.force_login(self.teacher)

        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "chat"]),
            data='{"question": "can you explain that a bit more?"}',
            content_type="application/json",
        )
        response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(len(question_messages), 1)
        self.assertEqual(question_messages[-1]["question_id"], practice.pk)
        self.assertEqual(block_payload["transcript"][-1]["kind"], "question")
        self.assertEqual(block_payload["transcript"][-1]["question_id"], practice.pk)

    def test_student_preview_maq_exact_match_is_required_for_correctness(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Which ideas fit this block?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["DNA replication happens in the nucleus", "Gravity drives diffusion"],
            explanation="This follows directly from this block.",
            question_hash="preview-maq-exact-match",
        )
        self.client.force_login(self.teacher)

        quiz_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        question_payload = [message for message in quiz_response.json()["preview"]["blocks"][0]["transcript"] if message["kind"] == "question"][-1]
        self.assertEqual(question_payload["question_type"], QuestionBankItem.QuestionType.MAQ)

        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps(
                {
                    "question_id": practice.pk,
                    "answers": ["Membranes regulate transport", "Signalling coordinates responses"],
                }
            ),
            content_type="application/json",
        )

        block_payload = next(item for item in answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        feedback_messages = [message for message in block_payload["transcript"] if message["kind"] == "feedback"]
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(feedback_messages[-1]["text"], "Correct.")
        self.assertEqual(
            question_messages[-1]["selected_answers"],
            ["Membranes regulate transport", "Signalling coordinates responses"],
        )

    def test_student_preview_maq_feedback_reports_missing_and_extra_answers(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Which ideas fit this block?",
            question_type=QuestionBankItem.QuestionType.MAQ,
            correct_answer="Membranes regulate transport",
            additional_correct_answers=["Signalling coordinates responses"],
            distractors=["DNA replication happens in the nucleus", "Gravity drives diffusion"],
            explanation="This follows directly from this block.",
            question_hash="preview-maq-missing-extra",
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps(
                {
                    "question_id": practice.pk,
                    "answers": ["Membranes regulate transport", "Gravity drives diffusion"],
                }
            ),
            content_type="application/json",
        )

        block_payload = next(item for item in answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        feedback_messages = [message for message in block_payload["transcript"] if message["kind"] == "feedback"]
        self.assertIn("Missed: Signalling coordinates responses.", feedback_messages[-1]["text"])
        self.assertIn("Extra: Gravity drives diffusion.", feedback_messages[-1]["text"])

    def test_student_preview_waq_correct_submission_updates_question_and_feedback(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-correct",
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps(
                {
                    "question_id": practice.pk,
                    "answer_text": "Membranes regulate transport by controlling what enters and leaves the cell.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(answer_response.status_code, 200)
        block_payload = next(item for item in answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        feedback_messages = [message for message in block_payload["transcript"] if message["kind"] == "feedback"]
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertEqual(feedback_messages[-1]["text"], "Correct. The correct answer reflects the key relationship being tested.")
        self.assertEqual(question_messages[-1]["submitted_text"], "Membranes regulate transport by controlling what enters and leaves the cell.")
        self.assertEqual(question_messages[-1]["alignment_state"], "aligned")
        self.assertFalse(question_messages[-1]["model_answer_revealed"])

    def test_student_preview_waq_incorrect_submission_reveals_model_answer(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-incorrect",
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        answer_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps(
                {
                    "question_id": practice.pk,
                    "answer_text": "It stores the genetic code for the cell.",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(answer_response.status_code, 200)
        block_payload = next(item for item in answer_response.json()["preview"]["blocks"] if item["id"] == block.pk)
        feedback_messages = [message for message in block_payload["transcript"] if message["kind"] == "feedback"]
        question_messages = [message for message in block_payload["transcript"] if message["kind"] == "question"]
        self.assertIn("Not aligned yet.", feedback_messages[-1]["text"])
        self.assertIn(practice.correct_answer, feedback_messages[-1]["text"])
        self.assertTrue(question_messages[-1]["model_answer_revealed"])
        self.assertEqual(question_messages[-1]["model_answer"], practice.correct_answer)

    def test_student_preview_chat_is_suspended_while_waq_is_pending(self):
        course = self.create_course()
        course.config.advanced_question_start_percent = 0
        course.config.save(update_fields=["advanced_question_start_percent", "updated_at"])
        block, _, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="How would you explain membrane transport?",
            question_type=QuestionBankItem.QuestionType.WAQ,
            correct_answer="Membranes regulate what enters and leaves the cell.",
            written_answer_keywords=["membranes", "regulate transport", "enters and leaves"],
            explanation="This follows directly from this block.",
            question_hash="preview-waq-chat-blocked",
        )
        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))

        response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "chat"]),
            data='{"question": "can you help me?"}',
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.json()["preview"]["blocks"] if item["id"] == block.pk)
        assistant_messages = [message for message in block_payload["transcript"] if message["role"] == "assistant" and message["kind"] == "text"]
        self.assertEqual(
            assistant_messages[-1]["text"],
            "Finish the written answer before asking a related question.",
        )

    def test_student_preview_question_retires_after_first_correct_answer(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Retire me?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Correct answer explanation.",
            question_hash="retire-preview-hash",
        )
        self.client.force_login(self.teacher)

        first_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        first_question = [message for message in first_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(first_question, practice.pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data='{"question_id": %s, "answer": "A"}' % practice.pk,
            content_type="application/json",
        )

        second_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        second_question = [message for message in second_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertNotEqual(second_question, practice.pk)

    def test_student_preview_incorrect_question_retires_once_answered_correctly(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        questions = []
        for index in range(1, 5):
            questions.append(
                QuestionBankItem.objects.create(
                    course=course,
                    block=block,
                    learning_objective=objective,
                    source_chunk=block.content_chunks.first(),
                    bank_type=QuestionBankItem.BankType.PRACTICE,
                    status=QuestionBankItem.Status.APPROVED,
                    stem=f"Retire after correction question {index}?",
                    correct_answer="A",
                    distractors=["B", "C", "D"],
                    explanation="Preview explanation.",
                    question_hash=f"retire-after-correction-{index}",
                )
            )

        self.client.force_login(self.teacher)

        first_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        first_question = [message for message in first_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(first_question, questions[0].pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data='{"question_id": %s, "answer": "B"}' % questions[0].pk,
            content_type="application/json",
        )

        for question in questions[1:]:
            next_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
            next_question = [message for message in next_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
            self.assertEqual(next_question, question.pk)
            self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
                data='{"question_id": %s, "answer": "A"}' % question.pk,
                content_type="application/json",
            )

        retry_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        retry_question = [message for message in retry_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(retry_question, questions[0].pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data='{"question_id": %s, "answer": "A"}' % questions[0].pk,
            content_type="application/json",
        )

        next_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        next_question = [message for message in next_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertNotEqual(next_question, questions[0].pk)

    def test_student_preview_incorrect_question_reappears_after_three_other_completed_questions(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        questions = []
        for index in range(1, 5):
            questions.append(
                QuestionBankItem.objects.create(
                    course=course,
                    block=block,
                    learning_objective=objective,
                    source_chunk=block.content_chunks.first(),
                    bank_type=QuestionBankItem.BankType.PRACTICE,
                    status=QuestionBankItem.Status.APPROVED,
                    stem=f"Preview bank question {index}?",
                    correct_answer="A",
                    distractors=["B", "C", "D"],
                    explanation="Preview explanation.",
                    question_hash=f"preview-bank-hash-{index}",
                )
            )

        self.client.force_login(self.teacher)

        first_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        first_question = [message for message in first_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(first_question, questions[0].pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data='{"question_id": %s, "answer": "B"}' % questions[0].pk,
            content_type="application/json",
        )

        for question in questions[1:]:
            next_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
            next_question = [message for message in next_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
            self.assertEqual(next_question, question.pk)
            self.client.post(
                reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
                data='{"question_id": %s, "answer": "A"}' % question.pk,
                content_type="application/json",
            )

        revisit_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        revisit_question = [message for message in revisit_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(revisit_question, questions[0].pk)

    def test_student_preview_flag_removes_practice_and_validation_pair_without_persisting_flag(self):
        course = self.create_course()
        block, _, objective, _ = self.create_preview_content_block(course)
        validation = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Flag me? (validation)",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="flag-validation-hash",
        )
        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Flag me?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Flag explanation.",
            question_hash="flag-practice-hash",
            linked_question=validation,
        )
        validation.linked_question = practice
        validation.save(update_fields=["linked_question", "updated_at"])
        follow_on = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=block.content_chunks.first(),
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Follow on question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Follow on explanation.",
            question_hash="follow-on-practice-hash",
        )

        self.client.force_login(self.teacher)
        self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        flag_response = self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "flag"]),
            data='{"question_id": %s}' % practice.pk,
            content_type="application/json",
        )

        self.assertEqual(flag_response.status_code, 200)
        practice.refresh_from_db()
        validation.refresh_from_db()
        self.assertEqual(practice.status, QuestionBankItem.Status.APPROVED)
        self.assertEqual(validation.status, QuestionBankItem.Status.APPROVED)
        self.assertEqual(QuestionFlag.objects.count(), 0)
        next_quiz = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"])).json()["preview"]
        next_question = [message for message in next_quiz["blocks"][0]["transcript"] if message["kind"] == "question"][-1]["question_id"]
        self.assertEqual(next_question, follow_on.pk)

    def test_validation_booking_enforces_capacity_for_digital_session(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="validation-hash",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Morning session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=2),
            location="Room 101",
            capacity=1,
            freeze_at=timezone.now() + timedelta(days=1),
            question_count=1,
        )
        self.client.force_login(self.student)
        book_response = self.client.get(reverse("standalone:validation_book", args=[event.pk]))
        self.assertEqual(book_response.status_code, 302)

        other_student = User.objects.create_user(
            username="otherstudent",
            email="otherstudent@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )
        Enrollment.objects.create(course=course, student=other_student)
        self.client.force_login(other_student)
        full_response = self.client.get(reverse("standalone:validation_book", args=[event.pk]), follow=True)
        self.assertContains(full_response, "already full")

    def test_validation_booking_stays_open_while_session_is_running_before_cutoff(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Running-session validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="running-session-validation-question",
        )
        ends_at = timezone.now() + timedelta(minutes=30)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Running session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=30),
            ends_at=ends_at,
            location="Room 101",
            capacity=2,
            freeze_at=ends_at - timedelta(minutes=10),
            late_booking_cutoff_minutes=10,
            question_count=1,
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:validation_book", args=[event.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(ValidationBooking.objects.filter(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED).exists())
        self.assertContains(response, "Validation booked.")

    def test_validation_booking_closes_within_configured_cutoff_before_session_end(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Late booking validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="late-booking-validation-question",
        )
        ends_at = timezone.now() + timedelta(minutes=5)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Closing soon session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=20),
            ends_at=ends_at,
            location="Room 102",
            capacity=2,
            freeze_at=ends_at - timedelta(minutes=10),
            late_booking_cutoff_minutes=10,
            question_count=1,
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:validation_book", args=[event.pk]), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(ValidationBooking.objects.filter(event=event, enrollment__student=self.student).exists())
        self.assertContains(response, "Booking has closed for this validation session.")

    def test_student_dashboard_shows_spaces_left_and_recent_bookings_for_bookable_sessions(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        other_student_recent = User.objects.create_user(
            username="recentstudent",
            email="recentstudent@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )
        other_student_old = User.objects.create_user(
            username="oldstudent",
            email="oldstudent@example.com",
            password="password123",
            role=User.Role.STUDENT,
        )
        recent_enrollment = Enrollment.objects.create(course=course, student=other_student_recent)
        old_enrollment = Enrollment.objects.create(course=course, student=other_student_old)
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Dashboard validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="dashboard-validation-question",
        )
        ends_at = timezone.now() + timedelta(hours=4)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Visible booking stats session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(hours=1),
            ends_at=ends_at,
            location="Validation Centre",
            capacity=3,
            freeze_at=ends_at - timedelta(minutes=15),
            late_booking_cutoff_minutes=15,
            question_count=1,
        )
        ValidationBooking.objects.create(event=event, enrollment=recent_enrollment, status=ValidationBooking.Status.BOOKED)
        old_booking = ValidationBooking.objects.create(event=event, enrollment=old_enrollment, status=ValidationBooking.Status.BOOKED)
        ValidationBooking.objects.filter(pk=old_booking.pk).update(updated_at=timezone.now() - timedelta(days=2))

        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:student_dashboard"))

        self.assertContains(response, "Bookable invigilated sessions")
        self.assertContains(response, "Spaces left 1")
        self.assertContains(response, "Bookings in last 24 hours 1")

    def test_digital_validation_start_samples_all_released_blocks_only(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        released_block, asset, objective, chunk = self.create_preview_content_block(course, title="Released block", order=1)
        second_released_block, second_asset, second_objective, second_chunk = self.create_preview_content_block(
            course,
            title="Second released block",
            order=2,
        )
        future_block, future_asset, future_objective, future_chunk = self.create_preview_content_block(course, title="Future block", order=3)
        future_block.available_from = timezone.localdate() + timedelta(days=5)
        future_block.save(update_fields=["available_from", "updated_at"])
        first_validation_question = QuestionBankItem.objects.create(
            course=course,
            block=released_block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Why do membranes matter?",
            correct_answer="They control transport.",
            distractors=["They stop metabolism.", "They only store DNA.", "They replace ribosomes."],
            explanation="Membranes regulate exchange.",
            question_hash="self-validation-question",
        )
        second_validation_question = QuestionBankItem.objects.create(
            course=course,
            block=second_released_block,
            learning_objective=second_objective,
            source_chunk=second_chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Why do signals matter?",
            correct_answer="They coordinate responses.",
            distractors=["They stop transcription.", "They remove ATP.", "They erase organelles."],
            explanation="Signals coordinate activity.",
            question_hash="second-self-validation-question",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=future_block,
            learning_objective=future_objective,
            source_chunk=future_chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Future validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Future.",
            question_hash="future-validation-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Invigilated validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            location="Centre",
            capacity=30,
            freeze_at=timezone.now() + timedelta(minutes=30),
            question_count=2,
            time_limit_minutes=15,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        start_response = self.client.get(reverse("standalone:validation_start", args=[event.pk]))

        self.assertEqual(start_response.status_code, 302)
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)
        self.assertEqual(attempt.mode, ValidationEvent.Mode.DIGITAL_INVIGILATION)
        locked_questions = list(attempt.attempt_questions.order_by("order"))
        self.assertEqual(len(locked_questions), 2)
        self.assertEqual({item.question for item in locked_questions}, {first_validation_question, second_validation_question})

        page = self.client.get(reverse("standalone:validation_attempt", args=[attempt.pk]))
        self.assertContains(page, "Validation session")
        self.assertContains(page, "I have read and understood these instructions")
        self.assertContains(page, "validation-session-data")

    def test_digital_validation_locked_set_respects_course_question_type_mix(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.MCQ,
            stem="Official MCQ validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="official-validation-mcq-mix",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.NUM,
            stem="Official NUM validation question?",
            correct_answer="5 m/s",
            distractors=["4 m/s", "16 m/s", "10 m/s"],
            explanation="Use \\(v = d/t\\).",
            question_hash="official-validation-num-mix",
            is_numerical=True,
            numeric_metadata={"script_version": "v1"},
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.MAQ,
            stem="Official MAQ validation question?",
            correct_answer="A",
            additional_correct_answers=["B"],
            distractors=["C", "D"],
            explanation="A and B.",
            question_hash="official-validation-maq-mix",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="Official WAQ validation question?",
            correct_answer="A written answer.",
            written_answer_keywords=["written", "answer"],
            explanation="A written answer.",
            question_hash="official-validation-waq-mix",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Official mix validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            location="Centre",
            capacity=30,
            freeze_at=timezone.now() + timedelta(minutes=30),
            question_count=3,
            time_limit_minutes=15,
        )
        course.config.numeric_ratio_percent = 100
        course.config.maq_ratio_percent = 0
        course.config.waq_ratio_percent = 0
        course.config.save(update_fields=["numeric_ratio_percent", "maq_ratio_percent", "waq_ratio_percent", "updated_at"])
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        start_response = self.client.get(reverse("standalone:validation_start", args=[event.pk]))

        self.assertEqual(start_response.status_code, 302)
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)
        question_types = set(attempt.attempt_questions.values_list("question__question_type", flat=True))
        self.assertIn(QuestionBankItem.QuestionType.NUM, question_types)

    def test_validation_practice_route_uses_chat_session(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Because A is right.",
            question_hash="validation-practice-chat",
        )

        self.client.force_login(self.student)
        with patch("standalone.services.validation_flow.generate_question_pair_for_block", return_value=(None, None)):
            response = self.client.get(reverse("standalone:validation_practice_session", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Practice validation")
        self.assertContains(response, "untimed")
        self.assertNotContains(response, "Time left")
        self.assertContains(response, "data-validation-launch-loader")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        self.assertEqual(attempt.attempt_questions.count(), 1)
        self.assertFalse(attempt.feedback_visible_immediately)
        self.assertIsNone(attempt.time_limit_minutes)

    def test_validation_practice_restart_creates_fresh_attempt(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="Because A is right.",
            question_hash="validation-practice-restart-chat",
        )
        stale_attempt = PracticeAttempt.objects.create(
            enrollment=enrollment,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
            feedback_visible_immediately=False,
        )
        PracticeAttemptQuestion.objects.create(
            attempt=stale_attempt,
            question=question,
            order=1,
            selected_answer="B",
            is_correct=False,
            feedback="Not quite.",
        )

        self.client.force_login(self.student)
        response = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PracticeAttempt.objects.filter(pk=stale_attempt.pk).exists())
        fresh_attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        self.assertNotEqual(fresh_attempt.pk, stale_attempt.pk)
        self.assertEqual(fresh_attempt.attempt_questions.count(), 1)
        self.assertEqual(fresh_attempt.attempt_questions.first().selected_answer, "")

    def test_validation_practice_submission_advances_to_following_question(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="First practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="first-practice-validation-question",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Second practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="second-practice-validation-question",
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        session_state = page.context["session_state"]
        first_question_id = session_state["pending_question"]["question_id"]
        self.assertIn(first_question_id, {first_question.pk, second_question.pk})

        submit_response = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "submit"]),
            data=json.dumps({"question_id": first_question_id, "answer": "A"}),
            content_type="application/json",
        )

        self.assertEqual(submit_response.status_code, 200)
        submit_session = submit_response.json()["session"]
        self.assertFalse(submit_session["next_available"])
        self.assertIsNotNone(submit_session["pending_question"])
        self.assertNotEqual(submit_session["pending_question"]["question_id"], first_question_id)

    def test_validation_practice_skips_practice_questions_already_seen_by_student(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        seen_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Seen practice question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="seen-practice-validation-question",
        )
        fresh_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Fresh practice question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="fresh-practice-validation-question",
        )
        EnrollmentQuestionState.objects.create(
            enrollment=enrollment,
            question=seen_question,
            times_presented=1,
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")

        self.assertEqual(page.status_code, 200)
        served_question_id = page.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, fresh_question.pk)

    def test_validation_practice_selection_keeps_mcqs_in_mix(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.MCQ,
            stem="MCQ practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="validation-practice-mcq-mix",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.MAQ,
            stem="MAQ practice validation question?",
            correct_answer="A",
            additional_correct_answers=["B"],
            distractors=["C", "D"],
            explanation="A and B.",
            question_hash="validation-practice-maq-mix",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="WAQ practice validation question?",
            correct_answer="A written answer.",
            written_answer_keywords=["written", "answer"],
            explanation="A written answer.",
            question_hash="validation-practice-waq-mix",
        )
        ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Mix event",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            location="Validation Centre",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1),
            question_count=3,
            time_limit_minutes=20,
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )

        self.assertEqual(page.status_code, 200)
        question_types = set(attempt.attempt_questions.values_list("question__question_type", flat=True))
        self.assertIn(QuestionBankItem.QuestionType.MCQ, question_types)

    def test_validation_practice_skip_advances_to_following_question(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="First skip practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="first-skip-practice-validation-question",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Second skip practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="second-skip-practice-validation-question",
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        first_question_id = page.context["session_state"]["pending_question"]["question_id"]
        self.assertIn(first_question_id, {first_question.pk, second_question.pk})

        skip_response = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "skip"]),
            data=json.dumps({"question_id": first_question_id}),
            content_type="application/json",
        )

        self.assertEqual(skip_response.status_code, 200)
        skip_session = skip_response.json()["session"]
        self.assertFalse(skip_session["next_available"])
        self.assertIsNotNone(skip_session["pending_question"])
        self.assertNotEqual(skip_session["pending_question"]["question_id"], first_question_id)

    def test_validation_practice_waq_can_send_multiple_messages_before_next(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        first_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="Why does nutrient restriction help researchers study growth-rate dependent gene expression in yeast?",
            correct_answer="Restricting one nutrient controls growth rate and reveals how gene expression changes with growth rate.",
            written_answer_keywords=["nutrient restriction", "growth rate", "gene expression"],
            explanation="Restricting one nutrient controls growth rate and reveals how gene expression changes with growth rate.",
            question_hash="validation-practice-waq-repeat-send",
        )
        second_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="How does restricting one nutrient help reveal growth-rate linked gene expression in yeast?",
            correct_answer="Restricting one nutrient sets growth rate and lets researchers track gene expression changes as growth rate changes.",
            written_answer_keywords=["nutrient restriction", "growth rate", "gene expression"],
            explanation="Restricting one nutrient sets growth rate and lets researchers track gene expression changes as growth rate changes.",
            question_hash="validation-practice-waq-follow-up",
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        session_state = page.context["session_state"]
        served_question_id = session_state["pending_question"]["question_id"]
        self.assertIn(served_question_id, {first_waq.pk, second_waq.pk})

        first_send = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer_text": "Nutrient restriction sets the growth rate."}),
            content_type="application/json",
        )

        self.assertEqual(first_send.status_code, 200)
        first_session = first_send.json()["session"]
        self.assertTrue(first_session["next_available"])
        self.assertEqual(first_session["pending_question"]["question_id"], served_question_id)
        self.assertTrue(first_session["pending_question"]["answered"])
        self.assertFalse(any(message["kind"] == "feedback" for message in first_session["transcript"]))
        self.assertTrue(any(message["role"] == "user" and message["text"] == "Nutrient restriction sets the growth rate." for message in first_session["transcript"]))

        second_send = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer_text": "It reveals changes in gene expression."}),
            content_type="application/json",
        )

        self.assertEqual(second_send.status_code, 200)
        second_session = second_send.json()["session"]
        self.assertTrue(second_session["next_available"])
        self.assertEqual(second_session["pending_question"]["question_id"], served_question_id)
        self.assertFalse(any(message["kind"] == "feedback" for message in second_session["transcript"]))
        self.assertTrue(any(message["role"] == "user" and message["text"] == "It reveals changes in gene expression." for message in second_session["transcript"]))
        attempt.refresh_from_db()
        answered_question = attempt.attempt_questions.get(question_id=served_question_id)
        self.assertTrue(answered_question.is_correct)
        self.assertIn("growth rate", answered_question.selected_answer)
        self.assertIn("gene expression", answered_question.selected_answer)

    def test_validation_practice_final_submission_returns_score_projection_and_review(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(
            course=course,
            student=self.student,
            mastery_score=50,
            coverage_score=50,
            engagement_score=5,
            target_score=5,
        )
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Final practice validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="final-practice-validation-question",
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        served_question_id = page.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, question.pk)

        submit_response = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer": "A"}),
            content_type="application/json",
        )

        self.assertEqual(submit_response.status_code, 200)
        session = submit_response.json()["session"]
        self.assertTrue(session["completed"])
        self.assertIsNone(session["pending_question"])
        self.assertEqual(session["score"], 100.0)
        self.assertIn("Practice validation complete.", session["transcript"][0]["text"])
        self.assertIn("100.0%", session["transcript"][0]["text"])
        self.assertIn("(36.5 x 80 + 100.0 x 20) / 100 = 49.2%", session["transcript"][1]["text"])
        self.assertIn("practice validation", session["transcript"][1]["text"].lower())
        self.assertTrue(any(message["kind"] == "question" and message.get("question_id") == question.pk for message in session["transcript"]))
        self.assertTrue(any(message["kind"] == "feedback" and message.get("question_id") == question.pk for message in session["transcript"]))

    def test_validation_practice_projection_applies_40_percent_floor(self):
        course = self.create_course()
        Enrollment.objects.create(
            course=course,
            student=self.student,
            mastery_score=17.9,
            coverage_score=17.9,
            engagement_score=17.9,
            target_score=17.9,
        )
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Validation floor question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="practice-validation-floor-question",
        )

        self.client.force_login(self.student)
        page = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        served_question_id = page.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, question.pk)

        submit_response = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer": "A"}),
            content_type="application/json",
        )

        session = submit_response.json()["session"]
        impact_text = session["transcript"][1]["text"]
        self.assertIn("(17.9 x 80 + 100.0 x 20) / 100 = 34.3%", impact_text)
        self.assertIn("projected overall score is lifted to **40.0%**", impact_text)
        self.assertIn("practice validation", impact_text.lower())

    def test_validation_practice_sidebar_history_links_to_completed_review(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Student history question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="student-validation-history-question",
        )

        self.client.force_login(self.student)
        start_response = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")
        attempt = PracticeAttempt.objects.get(
            enrollment__student=self.student,
            enrollment__course=course,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
        )
        served_question_id = start_response.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, question.pk)

        submit_response = self.client.post(
            reverse("standalone:validation_practice_action", args=[course.pk, attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer": "A"}),
            content_type="application/json",
        )
        self.assertTrue(submit_response.json()["session"]["completed"])

        review_response = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?review={attempt.pk}")

        self.assertEqual(review_response.status_code, 200)
        self.assertTrue(review_response.context["session_state"]["completed"])
        history = review_response.context["sidebar_state"]["practice_validation_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["id"], attempt.pk)
        self.assertTrue(history[0]["is_active"])
        self.assertIn(f"review={attempt.pk}", history[0]["url"])
        self.assertEqual(
            PracticeAttempt.objects.filter(
                enrollment__student=self.student,
                enrollment__course=course,
                attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
                completed_at__isnull=True,
            ).count(),
            0,
        )

    def test_student_practice_view_shows_validation_practice_link(self):
        course = self.create_course()
        Enrollment.objects.create(course=course, student=self.student)
        self.create_preview_content_block(course)

        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:practice_quiz", args=[course.pk]))

        self.assertContains(response, reverse("standalone:student_validate", args=[course.pk]), html=False)
        self.assertContains(response, "Validate")
        self.assertNotContains(response, "Book validation")

    def test_student_validate_route_shows_bookable_sidebar_state(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        self.create_preview_content_block(course)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="June validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=2),
            ends_at=timezone.now() + timedelta(days=2, hours=2),
            location="Validation Centre",
            capacity=30,
            freeze_at=timezone.now() + timedelta(days=1),
            question_count=10,
            time_limit_minutes=25,
            late_booking_cutoff_minutes=20,
        )

        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:student_validate", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_state"]["title"], "Book validation")
        self.assertEqual(
            response.context["sidebar_state"]["primary_action"]["url"],
            reverse("standalone:validation_book", args=[event.pk]),
        )
        self.assertFalse(response.context["session_state"]["show_block_switcher"])
        self.assertContains(response, "PRACTICE AVERAGES")
        self.assertContains(response, "Book validation")
        self.assertContains(response, "Start practice validation")

    def test_student_preview_welcome_message_mentions_practice_mode_and_validate(self):
        course = self.create_course()
        block, _, _, _ = self.create_preview_content_block(course, title="Origins of Life")
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        block_payload = next(item for item in response.context["preview_state"]["blocks"] if item["id"] == block.pk)
        assistant_messages = [message for message in block_payload["transcript"] if message["role"] == "assistant" and message["kind"] == "text"]
        self.assertEqual(
            assistant_messages[0]["text"],
            'Welcome to Origins of Life. You are in practice mode. Tap Quiz to get a question for this block, or ask about anything in the course. If you wish to validate your practice averages then please click "Validate" to enter validate mode.',
        )

    def test_student_practice_state_includes_validation_reminder_when_digital_event_exists(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="June validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=3),
            location="Validation Centre",
            capacity=30,
            freeze_at=timezone.now() + timedelta(days=2),
            question_count=10,
            time_limit_minutes=25,
            audit_prompt_count=2,
            room_code_secret="fizzy-newt-seed",
        )
        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:practice_quiz", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        preview_state = response.context["preview_state"]
        reminder_messages = [
            message
            for block_payload in preview_state["blocks"]
            for message in block_payload["transcript"]
            if message.get("kind") == "validation_reminder"
        ]
        self.assertEqual(len(reminder_messages), len(preview_state["blocks"]))
        self.assertTrue(all("digital validation has been created" in message["text"] for message in reminder_messages))
        self.assertTrue(all(message["cta_label"] == "Book validation" for message in reminder_messages))
        self.assertTrue(
            all(message["cta_url"] == reverse("standalone:validation_book", args=[event.pk]) for message in reminder_messages)
        )

    def test_student_practice_validation_reminder_repeats_every_ten_completed_questions(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, _asset, objective, chunk = self.create_preview_content_block(course)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="July validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=5),
            location="Validation Centre",
            capacity=30,
            freeze_at=timezone.now() + timedelta(days=4),
            question_count=10,
            time_limit_minutes=25,
            audit_prompt_count=2,
            room_code_secret="calm-otter-seed",
        )
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="What does this block cover?",
            correct_answer="Transport and signalling.",
            distractors=["Metals", "Planets", "Volcanoes"],
            explanation="Because that is the focus.",
            question_hash="validation-reminder-practice-question",
        )
        for attempt_index in range(10):
            attempt = PracticeAttempt.objects.create(
                enrollment=enrollment,
                attempt_type=PracticeAttempt.AttemptType.PRACTICE,
                block=block,
                completed_at=timezone.now() - timedelta(minutes=attempt_index),
                score=100,
            )
            PracticeAttemptQuestion.objects.create(
                attempt=attempt,
                question=question,
                order=1,
                selected_answer=question.correct_answer,
                is_correct=True,
                feedback="Correct.",
            )

        self.client.force_login(self.student)
        response = self.client.get(reverse("standalone:practice_quiz", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        preview_state = response.context["preview_state"]
        first_block = preview_state["blocks"][0]
        reminder_messages = [message for message in first_block["transcript"] if message.get("kind") == "validation_reminder"]
        self.assertEqual(len(reminder_messages), 2)
        self.assertIn("digital validation has been created", reminder_messages[0]["text"])
        self.assertIn("You've completed 10 practice questions", reminder_messages[1]["text"])

    def test_teacher_preview_can_launch_validation_practice(self):
        course = self.create_course()
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-validation-question",
        )

        self.client.force_login(self.teacher)
        preview_response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))
        self.assertContains(preview_response, reverse("standalone:preview_student_validate", args=[course.pk]), html=False)
        self.assertNotContains(preview_response, "Book validation")
        self.assertContains(preview_response, "Validate")

        validation_response = self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]))
        self.assertEqual(validation_response.status_code, 200)
        self.assertContains(validation_response, "Validation unavailable")
        self.assertContains(validation_response, "Start practice validation")

        practice_response = self.client.get(reverse("standalone:preview_validation_practice", args=[course.pk]))
        self.assertEqual(practice_response.status_code, 200)
        self.assertContains(practice_response, "Practice validation")

    def test_teacher_preview_validate_sidebar_uses_live_preview_practice_metrics(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview metrics question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-validate-live-metrics-question",
        )

        self.client.force_login(self.teacher)
        preview_quiz_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        served_question_id = preview_quiz_response.json()["preview"]["blocks"][0]["transcript"][-1]["question_id"]
        self.assertEqual(served_question_id, practice_question.pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": served_question_id, "answer": "A"}),
            content_type="application/json",
        )

        response = self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.context["sidebar_state"]["course_metrics"]["overall"], 0)

    def test_teacher_preview_validate_uses_chat_booking_picker_for_multiple_sessions(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        event_one = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Session one",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Room A",
            capacity=30,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            question_count=10,
            time_limit_minutes=25,
            late_booking_cutoff_minutes=20,
        )
        event_two = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Session two",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=2),
            ends_at=timezone.now() + timedelta(days=2, hours=2),
            location="Room B",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=2, hours=1, minutes=40),
            question_count=12,
            time_limit_minutes=25,
            late_booking_cutoff_minutes=20,
        )

        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_state"]["title"], "Book validation")
        self.assertEqual(response.context["sidebar_state"]["primary_action"]["kind"], "booking_options")
        self.assertIsNone(response.context["sidebar_state"]["secondary_action"])
        self.assertEqual(len(response.context["sidebar_state"]["booking_sessions"]), 2)
        self.assertContains(response, "data-preview-booking-options-trigger", html=False)
        self.assertContains(response, f"?book_event={event_one.pk}", html=False)
        self.assertContains(response, f"?book_event={event_two.pk}", html=False)

        booked_response = self.client.get(
            reverse("standalone:preview_student_validate", args=[course.pk]),
            {"book_event": event_two.pk},
            follow=True,
        )

        self.assertEqual(booked_response.status_code, 200)
        self.assertEqual(booked_response.context["sidebar_state"]["title"], "Validation booked")
        self.assertContains(booked_response, "Your validation session is booked and ready for you at the scheduled time.")

    def test_teacher_student_preview_sidebar_shows_book_validation_button_only_when_sessions_are_bookable(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        no_session_response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))
        self.assertContains(no_session_response, "Practice Validation")
        self.assertContains(no_session_response, reverse("standalone:preview_validation_practice", args=[course.pk]), html=False)
        self.assertContains(no_session_response, "Book Validation")
        self.assertContains(no_session_response, 'disabled aria-disabled="true"', html=False)
        self.assertContains(no_session_response, "No validation sessions available right now")

        ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Preview booking session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Room A",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            question_count=10,
            time_limit_minutes=25,
            late_booking_cutoff_minutes=20,
        )

        bookable_response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))
        self.assertContains(bookable_response, "Book Validation")
        self.assertContains(bookable_response, "Practice Validation")
        self.assertContains(bookable_response, reverse("standalone:preview_student_validate", args=[course.pk]), html=False)
        self.assertContains(bookable_response, "to")

    def test_teacher_student_preview_sidebar_shows_practice_validation_when_preview_booking_exists_out_of_session(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Preview booked session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() + timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=1, hours=2),
            location="Room B",
            capacity=20,
            freeze_at=timezone.now() + timedelta(days=1, hours=1, minutes=40),
            question_count=10,
            time_limit_minutes=25,
            late_booking_cutoff_minutes=20,
        )

        self.client.force_login(self.teacher)
        self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]), {"book_event": event.pk}, follow=True)
        response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))

        self.assertContains(response, "Practice Validation")
        self.assertContains(response, reverse("standalone:preview_validation_practice", args=[course.pk]), html=False)
        self.assertContains(response, event.starts_at.strftime("%d %b %Y, %H:%M"))

    def test_teacher_student_preview_sidebar_shows_validate_when_booked_preview_session_is_live(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Live preview session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            ends_at=timezone.now() + timedelta(hours=1),
            location="Room C",
            capacity=20,
            freeze_at=timezone.now() + timedelta(minutes=40),
            question_count=10,
            time_limit_minutes=25,
            late_booking_cutoff_minutes=20,
        )

        self.client.force_login(self.teacher)
        self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]), {"book_event": event.pk}, follow=True)
        response = self.client.get(reverse("standalone:student_preview", args=[course.pk]))

        self.assertContains(response, "Validate")
        self.assertContains(response, reverse("standalone:preview_student_validate", args=[course.pk]), html=False)
        self.assertContains(response, "Session live now")

    def test_teacher_preview_validate_hides_validation_panel_when_no_session_is_available(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        response = self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["sidebar_state"]["hide_validation_panel"])
        self.assertNotContains(response, 'class="preview-validation-status-panel"', html=False)
        self.assertNotContains(response, "data-preview-booking-options-trigger", html=False)

    def test_student_practice_validation_shows_back_to_practice_nav(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        self.client.force_login(self.student)

        response = self.client.get(f"{reverse('standalone:validation_practice_session', args=[course.pk])}?restart=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Back to practice")
        self.assertContains(response, reverse("standalone:practice_quiz", args=[course.pk]), html=False)

    def test_preview_practice_validation_shows_back_to_practice_nav(self):
        course = self.create_course()
        self.create_preview_content_block(course)
        self.client.force_login(self.teacher)

        response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Back to practice")
        self.assertContains(response, reverse("standalone:student_preview", args=[course.pk]), html=False)

    def test_preview_validation_practice_final_submission_returns_score_projection_and_review(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview practice question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-practice-question-for-validation-feedback",
        )
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview final validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-final-practice-validation-question",
        )
        self.client.force_login(self.teacher)

        preview_quiz_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        question_id = preview_quiz_response.json()["preview"]["blocks"][0]["transcript"][-1]["question_id"]
        self.assertEqual(question_id, practice_question.pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": question_id, "answer": "A"}),
            content_type="application/json",
        )

        with patch("standalone.services.preview_validation.generate_question_pair_for_block", return_value=(None, None)):
            page = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")
        session_state = page.context["session_state"]
        served_question_id = session_state["pending_question"]["question_id"]
        self.assertEqual(served_question_id, question.pk)

        with patch("standalone.services.preview_validation.generate_question_pair_for_block", return_value=(None, None)):
            submit_response = self.client.post(
                reverse("standalone:preview_validation_practice_action", args=[course.pk, "submit"]),
                data=json.dumps({"question_id": served_question_id, "answer": "A"}),
                content_type="application/json",
            )

        self.assertEqual(submit_response.status_code, 200)
        session = submit_response.json()["session"]
        self.assertTrue(session["completed"])
        self.assertEqual(session["score"], 100.0)
        self.assertIn("Practice validation complete.", session["transcript"][0]["text"])
        self.assertIn("100.0%", session["transcript"][0]["text"])
        self.assertIn("practice validation", session["transcript"][1]["text"].lower())
        self.assertTrue(any(message["kind"] == "question" and message.get("question_id") == question.pk for message in session["transcript"]))
        self.assertTrue(any(message["kind"] == "feedback" and message.get("question_id") == question.pk for message in session["transcript"]))
        self.assertContains(page, "untimed")

    def test_preview_validation_practice_projection_applies_40_percent_floor(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        practice_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview practice floor question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-practice-floor-question",
        )
        validation_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview validation floor question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-validation-floor-question",
        )
        self.client.force_login(self.teacher)

        preview_quiz_response = self.client.post(reverse("standalone:student_preview_action", args=[course.pk, block.pk, "quiz"]))
        practice_question_id = preview_quiz_response.json()["preview"]["blocks"][0]["transcript"][-1]["question_id"]
        self.assertEqual(practice_question_id, practice_question.pk)
        self.client.post(
            reverse("standalone:student_preview_action", args=[course.pk, block.pk, "answer"]),
            data=json.dumps({"question_id": practice_question_id, "answer": "B"}),
            content_type="application/json",
        )

        with patch("standalone.services.preview_validation.generate_question_pair_for_block", return_value=(None, None)):
            page = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")
        served_question_id = page.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, validation_question.pk)

        with patch("standalone.services.preview_validation.generate_question_pair_for_block", return_value=(None, None)):
            submit_response = self.client.post(
                reverse("standalone:preview_validation_practice_action", args=[course.pk, "submit"]),
                data=json.dumps({"question_id": served_question_id, "answer": "A"}),
                content_type="application/json",
            )

        session = submit_response.json()["session"]
        impact_text = session["transcript"][1]["text"]
        self.assertIn("(1.5 x 80 + 100.0 x 20) / 100 = 21.2%", impact_text)
        self.assertIn("projected overall score is lifted to **40.0%**", impact_text)
        self.assertIn("practice validation", impact_text.lower())

    def test_preview_validation_practice_waq_hides_in_progress_feedback_and_final_user_bubble(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="Why does nutrient restriction help reveal growth-rate linked gene expression in yeast?",
            correct_answer="Restricting one nutrient sets the growth rate and reveals how gene expression changes with growth rate.",
            written_answer_keywords=["nutrient restriction", "growth rate", "gene expression"],
            explanation="Restricting one nutrient sets the growth rate and reveals how gene expression changes with growth rate.",
            question_hash="preview-validation-waq-hidden-feedback",
        )
        self.client.force_login(self.teacher)

        start_response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")
        served_question_id = start_response.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, question.pk)

        submit_response = self.client.post(
            reverse("standalone:preview_validation_practice_action", args=[course.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer_text": "Nutrient restriction sets the growth rate and changes gene expression."}),
            content_type="application/json",
        )

        session = submit_response.json()["session"]
        self.assertTrue(session["next_available"])
        self.assertFalse(any(message["kind"] == "feedback" for message in session["transcript"]))
        self.assertTrue(any(message["role"] == "user" and "Nutrient restriction sets the growth rate" in message["text"] for message in session["transcript"]))

        final_response = self.client.post(
            reverse("standalone:preview_validation_practice_action", args=[course.pk, "next"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        final_session = final_response.json()["session"]
        self.assertTrue(final_session["completed"])
        self.assertFalse(any(message["role"] == "user" and message.get("question_type") == QuestionBankItem.QuestionType.WAQ for message in final_session["transcript"]))
        review_question = next(
            message for message in final_session["transcript"]
            if message["kind"] == "question" and message.get("question_id") == question.pk
        )
        self.assertTrue(review_question["is_correct"])

    def test_preview_validation_practice_randomizes_mcq_option_order(self):
        from standalone.services.validation_flow import _shuffle_options

        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Which option should not always appear first?",
            correct_answer="Correct option",
            distractors=["Distractor alpha", "Distractor beta", "Distractor gamma"],
            explanation="Correct option.",
            question_hash="preview-validation-option-shuffle",
        )
        self.client.force_login(self.teacher)

        response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")

        self.assertEqual(response.status_code, 200)
        pending_question = response.context["session_state"]["pending_question"]
        preview_state = self.client.session["standalone_preview_validation"][str(course.pk)]
        expected_options = _shuffle_options(
            question.all_answer_options(),
            f"preview-validation:{course.pk}:{preview_state['started_at']}",
            question.pk,
        )
        self.assertEqual(pending_question["options"], expected_options)
        self.assertNotEqual(pending_question["options"], question.all_answer_options())

    def test_preview_validation_practice_sidebar_history_reopens_completed_review(self):
        course = self.create_course()
        block, _, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview history question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-validation-history-question",
        )
        self.client.force_login(self.teacher)

        start_response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")
        served_question_id = start_response.context["session_state"]["pending_question"]["question_id"]
        self.assertEqual(served_question_id, question.pk)

        submit_response = self.client.post(
            reverse("standalone:preview_validation_practice_action", args=[course.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer": "A"}),
            content_type="application/json",
        )
        self.assertTrue(submit_response.json()["session"]["completed"])

        restarted_response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")
        history = restarted_response.context["sidebar_state"]["practice_validation_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["score"], 100.0)
        self.assertIn("review=1", history[0]["url"])

        review_response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?review=1")

        self.assertEqual(review_response.status_code, 200)
        self.assertTrue(review_response.context["session_state"]["completed"])
        self.assertTrue(review_response.context["sidebar_state"]["practice_validation_history"][0]["is_active"])
        self.assertIn(
            "Practice validation complete.",
            review_response.context["session_state"]["transcript"][0]["text"],
        )

    def test_teacher_preview_validation_practice_restart_resets_session_state(self):
        course = self.create_course()
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-validation-restart-question",
        )

        self.client.force_login(self.teacher)
        self.client.get(reverse("standalone:preview_validation_practice", args=[course.pk]))
        session = self.client.session
        preview_root = session["standalone_preview_validation"]
        course_state = dict(preview_root[str(course.pk)])
        question_id = course_state["question_ids"][0]
        course_state["answers"] = {
            str(question_id): {
                "selected_answers": ["B"],
                "is_correct": False,
                "feedback": "Not quite.",
                "answered_at": timezone.now().isoformat(),
            }
        }
        preview_root[str(course.pk)] = course_state
        session["standalone_preview_validation"] = preview_root
        session.save()

        response = self.client.get(f"{reverse('standalone:preview_validation_practice', args=[course.pk])}?restart=1")

        self.assertEqual(response.status_code, 200)
        session_state = response.context["session_state"]
        self.assertEqual(session_state["progress"]["answered_count"], 0)
        self.assertIsNotNone(session_state["pending_question"])

    def test_teacher_preview_validate_live_session_uses_shared_shell(self):
        course = self.create_course()
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Preview live validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="preview-live-validation-question",
        )
        ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Preview live session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=2),
            ends_at=timezone.now() + timedelta(minutes=58),
            location="Preview Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=10),
            question_count=1,
            time_limit_minutes=18,
            audit_prompt_count=2,
        )

        self.client.force_login(self.teacher)
        validation_response = self.client.get(reverse("standalone:preview_student_validate", args=[course.pk]))

        self.assertEqual(validation_response.status_code, 200)
        self.assertContains(validation_response, "Validation session")
        self.assertContains(validation_response, "Continue practice")
        self.assertContains(validation_response, "I have read and understood these instructions")
        self.assertContains(validation_response, "validation-session-data")
        self.assertNotContains(validation_response, "Time left")

    def test_digital_validation_schedules_audits_and_room_display(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Digital validation question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="digital-validation-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Invigilated session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=2),
            location="Lab 2",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=10),
            question_count=1,
            time_limit_minutes=18,
            audit_prompt_count=2,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        start_response = self.client.get(reverse("standalone:validation_start", args=[event.pk]))

        self.assertEqual(start_response.status_code, 302)
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)
        self.assertEqual(attempt.audit_prompts.count(), 1)
        attendance_prompt = attempt.audit_prompts.get(prompt_index=0)
        self.assertIsNone(attendance_prompt.answered_at)

        self.client.force_login(self.teacher)
        room_display = self.client.get(reverse("standalone:validation_room_display", args=[event.pk]))
        self.assertEqual(room_display.status_code, 200)
        room_json = self.client.get(reverse("standalone:validation_room_display_data", args=[event.pk]))
        self.assertEqual(room_json.status_code, 200)
        self.assertIn("code", room_json.json()["room_code"])

        self.client.force_login(self.student)
        confirm_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "confirm"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(confirm_response.status_code, 200)
        audit_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attendance_prompt.pk, "answer_text": current_room_code(event)}),
            content_type="application/json",
        )
        self.assertEqual(audit_response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.audit_prompts.count(), 3)
        self.assertTrue(attempt.audit_prompts.filter(prompt_index=0, is_correct=True, answered_at__isnull=False).exists())

    def test_manual_feedback_release_updates_attempt_review_visibility(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Manual release question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="manual-release-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Manual release session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            location="Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=30),
            question_count=1,
            time_limit_minutes=12,
            feedback_release_mode=ValidationEvent.FeedbackReleaseMode.MANUAL,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        self.client.get(reverse("standalone:validation_start", args=[event.pk]))
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)
        attendance_prompt = attempt.audit_prompts.get(prompt_index=0)
        self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "confirm"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attendance_prompt.pk, "answer_text": current_room_code(event)}),
            content_type="application/json",
        )
        response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"question_id": question.pk, "answer": "A"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.review_released_at)

        self.client.force_login(self.teacher)
        release_response = self.client.post(reverse("standalone:validation_feedback_release", args=[event.pk]))
        self.assertEqual(release_response.status_code, 302)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.review_released_at)

    def test_official_validation_advances_to_next_question_without_manual_next_when_review_hidden(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="First hidden-review question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="hidden-review-first-question",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Second hidden-review question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="hidden-review-second-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Progressing validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            location="Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=30),
            question_count=2,
            time_limit_minutes=12,
            feedback_release_mode=ValidationEvent.FeedbackReleaseMode.MANUAL,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        self.client.get(reverse("standalone:validation_start", args=[event.pk]))
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)

        self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "confirm"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        audit_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attempt.audit_prompts.get(prompt_index=0).pk, "answer_text": current_room_code(event)}),
            content_type="application/json",
        )
        served_question_id = audit_response.json()["session"]["pending_question"]["question_id"]

        response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer": "A"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        session = response.json()["session"]
        self.assertFalse(session["next_available"])
        self.assertIsNotNone(session["pending_question"])
        self.assertNotEqual(session["pending_question"]["question_id"], served_question_id)

    def test_official_validation_skip_advances_to_following_question(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        first_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="First official skip question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="first-official-skip-question",
        )
        second_question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Second official skip question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="second-official-skip-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Skip validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            location="Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=30),
            question_count=2,
            time_limit_minutes=12,
            feedback_release_mode=ValidationEvent.FeedbackReleaseMode.MANUAL,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        self.client.get(reverse("standalone:validation_start", args=[event.pk]))
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)
        self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "confirm"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        audit_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attempt.audit_prompts.get(prompt_index=0).pk, "answer_text": current_room_code(event)}),
            content_type="application/json",
        )
        served_question_id = audit_response.json()["session"]["pending_question"]["question_id"]
        self.assertIn(served_question_id, {first_question.pk, second_question.pk})

        skip_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "skip"]),
            data=json.dumps({"question_id": served_question_id}),
            content_type="application/json",
        )

        self.assertEqual(skip_response.status_code, 200)
        skip_session = skip_response.json()["session"]
        self.assertFalse(skip_session["next_available"])
        self.assertIsNotNone(skip_session["pending_question"])
        self.assertNotEqual(skip_session["pending_question"]["question_id"], served_question_id)

    def test_official_validation_waq_can_send_multiple_messages_before_next(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        first_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="Why does nutrient restriction help researchers study growth-rate dependent gene expression in yeast?",
            correct_answer="Restricting one nutrient controls growth rate and reveals how gene expression changes with growth rate.",
            written_answer_keywords=["nutrient restriction", "growth rate", "gene expression"],
            explanation="Restricting one nutrient controls growth rate and reveals how gene expression changes with growth rate.",
            question_hash="official-validation-waq-repeat-send",
        )
        second_waq = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            question_type=QuestionBankItem.QuestionType.WAQ,
            stem="How does restricting one nutrient help reveal growth-rate linked gene expression in yeast?",
            correct_answer="Restricting one nutrient sets growth rate and lets researchers track gene expression changes as growth rate changes.",
            written_answer_keywords=["nutrient restriction", "growth rate", "gene expression"],
            explanation="Restricting one nutrient sets growth rate and lets researchers track gene expression changes as growth rate changes.",
            question_hash="official-validation-waq-follow-up",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="WAQ validation",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=5),
            location="Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=30),
            question_count=2,
            time_limit_minutes=12,
            feedback_release_mode=ValidationEvent.FeedbackReleaseMode.MANUAL,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        self.client.get(reverse("standalone:validation_start", args=[event.pk]))
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)
        self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "confirm"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        audit_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attempt.audit_prompts.get(prompt_index=0).pk, "answer_text": current_room_code(event)}),
            content_type="application/json",
        )
        served_question_id = audit_response.json()["session"]["pending_question"]["question_id"]
        self.assertIn(served_question_id, {first_waq.pk, second_waq.pk})

        first_send = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer_text": "Nutrient restriction sets the growth rate."}),
            content_type="application/json",
        )

        self.assertEqual(first_send.status_code, 200)
        first_session = first_send.json()["session"]
        self.assertTrue(first_session["next_available"])
        self.assertEqual(first_session["pending_question"]["question_id"], served_question_id)
        self.assertTrue(first_session["pending_question"]["answered"])

        second_send = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"question_id": served_question_id, "answer_text": "It reveals changes in gene expression."}),
            content_type="application/json",
        )

        self.assertEqual(second_send.status_code, 200)
        second_session = second_send.json()["session"]
        self.assertTrue(second_session["next_available"])
        self.assertEqual(second_session["pending_question"]["question_id"], served_question_id)
        attempt.refresh_from_db()
        answered_question = attempt.attempt_questions.get(question_id=served_question_id)
        self.assertTrue(answered_question.is_correct)
        self.assertIn("growth rate", answered_question.answer_text)
        self.assertIn("gene expression", answered_question.answer_text)

    def test_validation_presence_warning_and_void(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Presence question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="presence-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Presence session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=2),
            location="Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=20),
            question_count=1,
            time_limit_minutes=12,
            audit_prompt_count=2,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        self.client.get(reverse("standalone:validation_start", args=[event.pk]))
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)

        warning_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "presence"]),
            data=json.dumps({"away_seconds": 5}),
            content_type="application/json",
        )
        self.assertEqual(warning_response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ValidationAttempt.Status.IN_PROGRESS)
        self.assertEqual(attempt.navigation_warning_count, 1)

        void_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "presence"]),
            data=json.dumps({"away_seconds": 11}),
            content_type="application/json",
        )
        self.assertEqual(void_response.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, ValidationAttempt.Status.VOIDED)
        self.assertIn("voided", attempt.invalidated_reason.lower())

    def test_digital_validation_timer_waits_for_attendance_audit(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block, asset, objective, chunk = self.create_preview_content_block(course)
        question = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem="Attendance gate question?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            explanation="A.",
            question_hash="attendance-gate-question",
        )
        event = ValidationEvent.objects.create(
            course=course,
            created_by=self.teacher,
            title="Attendance gate session",
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
            starts_at=timezone.now() - timedelta(minutes=3),
            location="Centre",
            capacity=10,
            freeze_at=timezone.now() + timedelta(minutes=20),
            question_count=1,
            time_limit_minutes=12,
            audit_prompt_count=2,
        )
        ValidationBooking.objects.create(event=event, enrollment=enrollment, status=ValidationBooking.Status.BOOKED)

        self.client.force_login(self.student)
        self.client.get(reverse("standalone:validation_start", args=[event.pk]))
        attempt = ValidationAttempt.objects.get(event=event, enrollment=enrollment)

        session_before = self.client.get(reverse("standalone:validation_attempt", args=[attempt.pk])).context["session_state"]
        self.assertFalse(session_before["timer_running"])
        self.assertTrue(session_before["awaiting_attendance_audit"])
        self.assertIsNone(session_before["pending_question"])
        self.assertIsNone(session_before["pending_audit"])
        self.assertFalse(session_before["instructions_confirmed"])
        self.assertEqual(session_before["time_remaining_seconds"], 0)

        blocked_code_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attempt.audit_prompts.get(prompt_index=0).pk, "answer_text": "wrong-code"}),
            content_type="application/json",
        )
        self.assertEqual(blocked_code_response.status_code, 400)
        self.assertIn("instructions", blocked_code_response.json()["error"].lower())

        confirm_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "confirm"]),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(confirm_response.status_code, 200)
        confirmed_session = confirm_response.json()["session"]
        self.assertTrue(confirmed_session["instructions_confirmed"])
        self.assertIsNotNone(confirmed_session["pending_audit"])

        wrong_code_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attempt.audit_prompts.get(prompt_index=0).pk, "answer_text": "wrong-code"}),
            content_type="application/json",
        )
        self.assertEqual(wrong_code_response.status_code, 200)
        wrong_session = wrong_code_response.json()["session"]
        self.assertFalse(wrong_session["timer_running"])
        self.assertIsNone(wrong_session["pending_question"])
        self.assertIsNotNone(wrong_session["pending_audit"])

        correct_code_response = self.client.post(
            reverse("standalone:validation_attempt_action", args=[attempt.pk, "submit"]),
            data=json.dumps({"audit_prompt_id": attempt.audit_prompts.get(prompt_index=0).pk, "answer_text": current_room_code(event)}),
            content_type="application/json",
        )
        self.assertEqual(correct_code_response.status_code, 200)
        started_session = correct_code_response.json()["session"]
        self.assertTrue(started_session["timer_running"])
        self.assertFalse(started_session["awaiting_attendance_audit"])
        self.assertIsNotNone(started_session["pending_question"])
        self.assertEqual(started_session["pending_question"]["question_id"], question.pk)
        self.assertEqual(started_session["time_remaining_seconds"], 0)
