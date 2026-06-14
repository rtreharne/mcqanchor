from datetime import timedelta

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
        response = self.client.post(
            reverse("standalone:asset_upload", args=[block.pk]),
            {
                "file": SimpleUploadedFile("notes.txt", b"Topic one.\n\nTopic two.\n\nTopic three.", content_type="text/plain"),
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
