import json
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from standalone.models import Course
from standalone.services.demo_mode import ensure_demo_access

from .chatbot import ChatbotError, get_chatbot_reply
from .forms import PilotEnquiryForm
from .models import ChatConversation, ChatMessage

logger = logging.getLogger(__name__)
DEMO_SUMMARY_LIMIT = 200


def _truncate_demo_summary(text: str, limit: int = DEMO_SUMMARY_LIMIT) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized

    excerpt = normalized[:limit]
    last_space = excerpt.rfind(" ")
    minimum_word_boundary = int(limit * 0.72)
    if last_space > minimum_word_boundary:
        excerpt = excerpt[:last_space]
    return excerpt.rstrip()


def home(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("standalone:dashboard")
    if request.method == "POST":
        form = PilotEnquiryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Thanks for your interest. We will be in touch soon.")
            return redirect("website:home")
    else:
        form = PilotEnquiryForm()

    homepage_demos = []
    featured_courses = (
        Course.objects.filter(
            is_active=True,
            config__demo_enabled=True,
            config__homepage_demo_enabled=True,
        )
        .select_related("config")
        .order_by("title")
    )
    for course in featured_courses:
        access = ensure_demo_access(course)
        summary = (course.summary or "").strip() or "Open a live MCQ Anchor demo for this course."
        summary_excerpt = _truncate_demo_summary(summary)
        homepage_demos.append(
            {
                "title": course.title,
                "summary": summary,
                "summary_excerpt": summary_excerpt,
                "summary_is_truncated": len(summary_excerpt) < len(summary),
                "practice_url": reverse("standalone:demo_practice", args=[access.token]),
                "access_count": int(access.access_count or 0),
            }
        )

    context = {
        "contact_email": settings.CONTACT_EMAIL,
        "form": form,
        "homepage_demos": homepage_demos,
    }
    return render(request, "website/home.html", context)


def _get_session_key(request: HttpRequest) -> str:
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key or request.META.get("REMOTE_ADDR", "anonymous")


def _get_rate_limit_key(request: HttpRequest) -> str:
    identifier = _get_session_key(request)
    return f"product-chat:{identifier}"


def _get_client_ip(request: HttpRequest) -> str:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "").strip()


def _rate_limit_exceeded(request: HttpRequest) -> bool:
    key = _get_rate_limit_key(request)
    attempts = cache.get(key, 0)
    if attempts >= settings.CHAT_RATE_LIMIT:
        return True
    cache.set(key, attempts + 1, timeout=settings.CHAT_RATE_WINDOW)
    return False


def _validate_history(history):
    if history is None:
        return []
    if not isinstance(history, list):
        raise ValueError("History must be a list.")
    if len(history) > settings.CHAT_MAX_HISTORY_ITEMS:
        raise ValueError("History is too long.")

    clean_history = []
    for item in history:
        if not isinstance(item, dict):
            raise ValueError("History items must be objects.")
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            raise ValueError("History role is invalid.")
        if not isinstance(content, str):
            raise ValueError("History content must be text.")
        content = content.strip()
        if not content:
            raise ValueError("History content cannot be empty.")
        if len(content) > settings.CHAT_MAX_HISTORY_MESSAGE_LENGTH:
            raise ValueError("History content is too long.")
        clean_history.append({"role": role, "content": content})
    return clean_history


def _get_or_create_chat_conversation(
    request: HttpRequest,
    conversation_id: str | None,
) -> ChatConversation:
    public_id = None
    if conversation_id:
        try:
            public_id = uuid.UUID(str(conversation_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("Conversation ID is invalid.") from exc

    session_key = _get_session_key(request)
    defaults = {
        "session_key": session_key,
        "ip_address": _get_client_ip(request) or None,
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:1000],
    }

    if public_id is None:
        return ChatConversation.objects.create(**defaults)

    conversation, created = ChatConversation.objects.get_or_create(
        public_id=public_id,
        session_key=session_key,
        defaults=defaults,
    )
    if not created:
        conversation.ip_address = defaults["ip_address"]
        conversation.user_agent = defaults["user_agent"]
        conversation.save(update_fields=["ip_address", "user_agent", "last_message_at"])
    return conversation


def _log_chat_message(conversation: ChatConversation, role: str, content: str) -> None:
    ChatMessage.objects.create(conversation=conversation, role=role, content=content)


@require_http_methods(["POST"])
def product_chat(request: HttpRequest) -> JsonResponse:
    if _rate_limit_exceeded(request):
        return JsonResponse(
            {"error": "Too many questions in a short period. Please wait a moment and try again."},
            status=429,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"error": "Please send valid JSON."}, status=400)

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return JsonResponse({"error": "Please enter a question about MCQ Anchor."}, status=400)

    question = question.strip()
    if len(question) > settings.CHAT_MAX_QUESTION_LENGTH:
        return JsonResponse({"error": "Please keep your question under 500 characters."}, status=400)

    try:
        history = _validate_history(payload.get("history"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        conversation = _get_or_create_chat_conversation(request, payload.get("conversation_id"))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    _log_chat_message(conversation, ChatMessage.Role.USER, question)

    if not settings.OPENAI_API_KEY:
        error_message = (
            "The chat assistant is not configured yet. Please use the pilot form and we will reply directly."
        )
        _log_chat_message(conversation, ChatMessage.Role.ERROR, error_message)
        return JsonResponse(
            {"error": error_message, "conversation_id": str(conversation.public_id)},
            status=503,
        )

    try:
        answer = get_chatbot_reply(
            question=question,
            history=history,
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_MODEL,
        )
    except ChatbotError as exc:
        logger.warning("Product chat failed: %s", exc)
        error_message = "The chat assistant is unavailable right now. Please try again shortly or use the pilot form."
        _log_chat_message(conversation, ChatMessage.Role.ERROR, error_message)
        return JsonResponse(
            {"error": error_message, "conversation_id": str(conversation.public_id)},
            status=502,
        )

    _log_chat_message(conversation, ChatMessage.Role.ASSISTANT, answer)
    return JsonResponse({"answer": answer, "conversation_id": str(conversation.public_id)})
