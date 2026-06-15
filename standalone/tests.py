from datetime import timedelta
import re

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from standalone.models import (
    ContentAsset,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    Enrollment,
    LearningObjective,
    PracticeAttempt,
    QuestionBankItem,
    StudentInvitation,
    TeacherInvitation,
    ValidationEvent,
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
        self.assertContains(response, 'href="%s"' % reverse("standalone:teacher_dashboard"), html=False)
        self.assertContains(response, ">Dashboard<", html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:regenerate_course_content", args=[course.pk]), html=False)
        self.assertContains(response, "data-async-refresh", html=False)
        self.assertContains(response, "Regenerate descriptions and objectives")
        self.assertContains(response, 'href="%s"' % reverse("standalone:asset_upload", args=[block.pk]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:regenerate_block_content", args=[block.pk]), html=False)
        self.assertContains(response, "Upload content")
        self.assertContains(response, "Re-generate")
        self.assertContains(response, 'data-block-toggle', html=False)
        self.assertContains(response, 'aria-expanded="false"', html=False)
        self.assertContains(response, 'id="block-content-%s"' % block.pk, html=False)
        self.assertContains(response, 'class="block-content-grid"', html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_block_field", args=[block.pk, "title"]), html=False)
        self.assertContains(response, 'data-inline-url="%s"' % reverse("standalone:update_learning_objective", args=[objective.pk]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:move_learning_objective", args=[objective.pk, "up"]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:move_learning_objective", args=[objective.pk, "down"]), html=False)
        self.assertContains(response, 'action="%s"' % reverse("standalone:delete_learning_objective", args=[objective.pk]), html=False)
        self.assertContains(response, "Delete this learning objective? This will re-number the remaining objectives.")
        self.assertLess(response.content.decode("utf-8").find("Learning objectives"), response.content.decode("utf-8").find("Assets"))

    def test_teacher_dashboard_course_card_is_clickable_without_open_course_link(self):
        course = self.create_course()
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("standalone:teacher_dashboard"))
        self.assertContains(response, 'class="card course-card"', html=False)
        self.assertContains(response, 'href="%s"' % reverse("standalone:course_detail", args=[course.pk]), html=False)
        self.assertNotContains(response, "Open course")

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

        self.client.force_login(self.teacher)
        with self.settings(OPENAI_API_KEY=""):
            response = self.client.post(reverse("standalone:regenerate_block_content", args=[block_one.pk]))
        self.assertEqual(response.status_code, 302)

        block_one.refresh_from_db()
        block_two.refresh_from_db()
        self.assertTrue(block_one.summary)
        self.assertGreater(block_one.learning_objectives.count(), 0)
        self.assertEqual(block_two.summary, "Keep me")

    def test_inline_block_title_update_returns_json_and_persists(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", summary="Original summary", order=1)
        self.client.force_login(self.teacher)
        response = self.client.post(reverse("standalone:update_block_field", args=[block.pk, "title"]), {"title": "Foundations"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["display_value"], "Foundations")
        block.refresh_from_db()
        self.assertEqual(block.title, "Foundations")

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

    def test_teacher_can_upload_supported_content_and_generate_chunks(self):
        course = self.create_course()
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        self.client.force_login(self.teacher)
        with self.settings(OPENAI_API_KEY=""):
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
                    "include_in_generation": "on",
                },
            )
        self.assertEqual(response.status_code, 302)
        asset = ContentAsset.objects.get(block=block)
        self.assertEqual(asset.processing_status, ContentAsset.ProcessingStatus.PROCESSED)
        self.assertGreater(asset.chunks.count(), 0)
        self.assertGreater(block.learning_objectives.count(), 0)

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

    def test_practice_quiz_does_not_repeat_question_for_student(self):
        course = self.create_course()
        enrollment = Enrollment.objects.create(course=course, student=self.student)
        block = CourseBlock.objects.create(course=course, title="Week 1", order=1)
        q1 = QuestionBankItem.objects.create(
            course=course,
            block=block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Question one?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="hash-1",
        )
        QuestionBankItem.objects.create(
            course=course,
            block=block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem="Question two?",
            correct_answer="A",
            distractors=["B", "C", "D"],
            question_hash="hash-2",
        )
        self.client.force_login(self.student)
        start = self.client.get(reverse("standalone:practice_quiz", args=[course.pk]))
        attempt_url = start.url
        attempt_page = self.client.get(attempt_url)
        self.assertContains(attempt_page, "Question one?")
        response = self.client.post(attempt_url, {"question_id": q1.pk, "answer": "A"})
        self.assertContains(response, "Question two?")
        self.assertNotContains(response, "Question one?")
        self.assertEqual(PracticeAttempt.objects.filter(enrollment=enrollment).count(), 1)

    def test_validation_booking_enforces_capacity_and_pdf_generation(self):
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

        self.client.force_login(self.teacher)
        pdf_response = self.client.get(reverse("standalone:validation_pack_pdf", args=[event.pk]))
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
