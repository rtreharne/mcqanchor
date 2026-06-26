import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from standalone.models import Course, CourseConfig
from standalone.services.demo_mode import ensure_demo_access

from .models import ChatConversation, ChatMessage, PilotEnquiry


class HomePageTests(TestCase):
    def test_home_page_renders_core_content_and_accessibility_hooks(self):
        response = self.client.get(reverse("website:home"))

        self.assertContains(response, "Continuous practice. Anchored assessment.")
        self.assertContains(response, 'aria-label="Primary"', html=False)
        self.assertContains(response, 'alt="MCQ Anchor logo"', html=False)
        self.assertContains(response, "Download the handout")
        self.assertContains(response, "Start a conversation.")
        self.assertContains(response, f'href="{reverse("standalone:login")}"', html=False)
        self.assertContains(response, "Log in")

    def test_authenticated_user_visiting_home_redirects_to_dashboard(self):
        user = get_user_model().objects.create_user(
            username="teacher",
            email="teacher@example.com",
            password="password123",
            role="teacher",
        )
        self.client.force_login(user)

        response = self.client.get(reverse("website:home"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("standalone:dashboard"))

    def test_home_page_shows_featured_public_demos_only(self):
        teacher = get_user_model().objects.create_user(
            username="demo-teacher",
            email="demo-teacher@example.com",
            password="password123",
            role="teacher",
        )
        featured_course = Course.objects.create(
            teacher=teacher,
            title="Featured Demo Course",
            slug="featured-demo-course",
            summary="A public practice demo.",
            is_active=True,
        )
        CourseConfig.objects.create(course=featured_course, demo_enabled=True, homepage_demo_enabled=True)
        featured_access = ensure_demo_access(featured_course)
        featured_access.access_count = 7
        featured_access.save(update_fields=["access_count", "updated_at"])

        hidden_course = Course.objects.create(
            teacher=teacher,
            title="Hidden Demo Course",
            slug="hidden-demo-course",
            summary="Should not appear on the homepage.",
            is_active=True,
        )
        CourseConfig.objects.create(course=hidden_course, demo_enabled=True, homepage_demo_enabled=False)
        ensure_demo_access(hidden_course)

        response = self.client.get(reverse("website:home"))

        self.assertContains(response, "Try MCQ Anchor with real course demos.")
        self.assertContains(response, 'href="#demos"', html=False)
        self.assertContains(response, ">Demo<", html=False)
        self.assertContains(response, "Featured Demo Course")
        self.assertContains(response, "Accessed 7 times")
        self.assertContains(response, reverse("standalone:demo_practice", args=[featured_access.token]), html=False)
        self.assertContains(response, 'class="button button-secondary demo-now-button"', html=False)
        self.assertContains(response, 'href="#demos"', html=False)
        self.assertContains(response, "Demo Now")
        self.assertContains(response, 'target="_blank"', html=False)
        self.assertContains(response, 'rel="noopener noreferrer"', html=False)
        self.assertNotContains(response, "Open practice validation")
        self.assertNotContains(response, "Hidden Demo Course")


class ContactFormTests(TestCase):
    def test_contact_form_success(self):
        response = self.client.post(
            reverse("website:home"),
            {
                "name": "Alex Morgan",
                "email": "alex@example.com",
                "institution": "Northshore University",
                "module_or_subject": "Biochemistry",
                "message": "Interested in a pilot for semester one.",
                "website": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PilotEnquiry.objects.count(), 1)
        self.assertContains(response, "Thanks for your interest. We will be in touch soon.")

    def test_invalid_contact_form_submission(self):
        response = self.client.post(
            reverse("website:home"),
            {
                "name": "",
                "email": "not-an-email",
                "website": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PilotEnquiry.objects.count(), 0)
        self.assertContains(response, "This field is required.")

    def test_honeypot_spam_rejection(self):
        response = self.client.post(
            reverse("website:home"),
            {
                "name": "Spam Bot",
                "email": "spam@example.com",
                "website": "https://spam.example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(PilotEnquiry.objects.count(), 0)
        self.assertContains(response, "Please leave this field empty.")


class AdminBootstrapCommandTests(TestCase):
    @patch.dict(
        os.environ,
        {
            "DJANGO_ADMIN_USERNAME": "renderadmin",
            "DJANGO_ADMIN_PASSWORD": "super-secret-pass",
        },
        clear=False,
    )
    def test_ensure_admin_user_creates_superuser(self):
        call_command("ensure_admin_user")

        user = get_user_model().objects.get(username="renderadmin")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password("super-secret-pass"))

    @patch.dict(
        os.environ,
        {
            "DJANGO_ADMIN_USERNAME": "renderadmin",
            "DJANGO_ADMIN_PASSWORD": "new-secret-pass",
        },
        clear=False,
    )
    def test_ensure_admin_user_updates_existing_superuser_password(self):
        user = get_user_model().objects.create_superuser(
            username="renderadmin",
            email="old@example.com",
            password="old-pass",
        )

        call_command("ensure_admin_user")

        user.refresh_from_db()
        self.assertTrue(user.check_password("new-secret-pass"))


class SettingsConfigurationTests(TestCase):
    def test_media_root_can_be_configured_with_environment_variable(self):
        settings_path = Path(__file__).resolve().parent.parent / "config" / "settings.py"
        spec = importlib.util.spec_from_file_location("config_settings_media_root_test", settings_path)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)

        with patch.dict(os.environ, {"MEDIA_ROOT": "/tmp/mcq-anchor-media-test"}, clear=False):
            spec.loader.exec_module(module)

        self.assertEqual(module.MEDIA_ROOT, "/tmp/mcq-anchor-media-test")


class AdminCsvExportTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="password123",
        )
        self.client.force_login(self.admin_user)

    def test_pilot_enquiries_can_be_exported_as_csv(self):
        enquiry = PilotEnquiry.objects.create(
            name="Alex Morgan",
            email="alex@example.com",
            institution="Northshore University",
            module_or_subject="Biochemistry",
            message="Interested in a pilot.",
        )

        response = self.client.post(
            reverse("admin:website_pilotenquiry_changelist"),
            {
                "action": "export_selected_as_csv",
                "_selected_action": [str(enquiry.pk)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("pilot-enquiries.csv", response["Content-Disposition"])
        content = response.content.decode("utf-8")
        self.assertIn("Alex Morgan", content)
        self.assertIn("alex@example.com", content)
        self.assertIn("Biochemistry", content)

    def test_chat_conversations_can_be_exported_as_csv(self):
        conversation = ChatConversation.objects.create(
            session_key="session-123",
            ip_address="127.0.0.1",
            user_agent="Test Browser",
        )
        ChatMessage.objects.create(
            conversation=conversation,
            role=ChatMessage.Role.USER,
            content="How does validation work?",
        )
        ChatMessage.objects.create(
            conversation=conversation,
            role=ChatMessage.Role.ASSISTANT,
            content="It uses a short controlled digital validation session on a single device.",
        )

        response = self.client.post(
            reverse("admin:website_chatconversation_changelist"),
            {
                "action": "export_selected_as_csv",
                "_selected_action": [str(conversation.pk)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("chat-conversations.csv", response["Content-Disposition"])
        content = response.content.decode("utf-8")
        self.assertIn(str(conversation.public_id), content)
        self.assertIn("session-123", content)
        self.assertIn("How does validation work?", content)
        self.assertIn("assistant: It uses a short controlled digital validation session on a single device.", content)


@override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_MODEL="gpt-4.1-mini",
    CHAT_RATE_LIMIT=8,
    CHAT_RATE_WINDOW=60,
)
class ProductChatTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        cache.clear()
        self.client.get(reverse("website:home"))
        self.csrf_token = self.client.cookies["csrftoken"].value

    def _post_json(self, payload, **extra):
        return self.client.post(
            reverse("website:product_chat"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token,
            **extra,
        )

    @patch("website.chatbot.OpenAI")
    def test_successful_chatbot_response_with_mocked_openai(self, openai_class):
        mock_client = Mock()
        mock_response = Mock(output_text="MCQ Anchor uses a short validation test to anchor online practice.")
        mock_client.responses.create.return_value = mock_response
        openai_class.return_value = mock_client

        response = self._post_json({"question": "How does the validation work?"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["answer"],
            "MCQ Anchor uses a short validation test to anchor online practice.",
        )

    @override_settings(OPENAI_API_KEY="")
    def test_missing_api_key(self):
        response = self._post_json({"question": "How does it work?"})

        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.json()["error"])

    def test_empty_question(self):
        response = self._post_json({"question": "   "})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Please enter a question", response.json()["error"])

    def test_overly_long_question(self):
        response = self._post_json({"question": "a" * 501})

        self.assertEqual(response.status_code, 400)
        self.assertIn("under 500 characters", response.json()["error"])

    def test_malformed_json(self):
        response = self.client.post(
            reverse("website:product_chat"),
            data="{oops",
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Please send valid JSON.")

    @patch("website.chatbot.OpenAI")
    def test_failed_upstream_response(self, openai_class):
        mock_client = Mock()
        mock_client.responses.create.side_effect = RuntimeError("boom")
        openai_class.return_value = mock_client

        response = self._post_json({"question": "How does the digital validation work?"})

        self.assertEqual(response.status_code, 502)
        self.assertIn("unavailable", response.json()["error"])

    @patch("website.chatbot.OpenAI")
    def test_lti_question_uses_enabled_and_standalone_positioning(self, openai_class):
        mock_client = Mock()
        mock_response = Mock(
            output_text="LTI 1.3 enabled. Integrate into your VLE seamlessly or use it as a standalone product."
        )
        mock_client.responses.create.return_value = mock_response
        openai_class.return_value = mock_client

        response = self._post_json({"question": "Will this connect to our VLE?"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("LTI 1.3 enabled", response.json()["answer"])
        self.assertIn("standalone product", response.json()["answer"])

    @patch("website.chatbot.OpenAI")
    def test_rate_limit(self, openai_class):
        mock_client = Mock()
        mock_client.responses.create.return_value = Mock(output_text="Short answer.")
        openai_class.return_value = mock_client

        for _ in range(8):
            response = self._post_json({"question": "How does it work?"})
            self.assertEqual(response.status_code, 200)

        response = self._post_json({"question": "One more question."})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many questions", response.json()["error"])
