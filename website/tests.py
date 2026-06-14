import json
import os
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import PilotEnquiry


class HomePageTests(TestCase):
    def test_home_page_renders_core_content_and_accessibility_hooks(self):
        response = self.client.get(reverse("website:home"))

        self.assertContains(response, "Continuous practice. Anchored assessment.")
        self.assertContains(response, 'aria-label="Primary"', html=False)
        self.assertContains(response, 'alt="MCQ Anchor logo"', html=False)
        self.assertContains(response, "Download the handout")
        self.assertContains(response, "Start a conversation.")


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

        response = self._post_json({"question": "How does the paper validation work?"})

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
