import json
import logging

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .chatbot import ChatbotError, get_chatbot_reply
from .forms import PilotEnquiryForm

logger = logging.getLogger(__name__)


def home(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = PilotEnquiryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Thanks for your interest. We will be in touch soon.")
            return redirect("website:home")
    else:
        form = PilotEnquiryForm()

    context = {
        "contact_email": settings.CONTACT_EMAIL,
        "form": form,
    }
    return render(request, "website/home.html", context)


def _get_rate_limit_key(request: HttpRequest) -> str:
    if not request.session.session_key:
        request.session.create()
    identifier = request.session.session_key or request.META.get("REMOTE_ADDR", "anonymous")
    return f"product-chat:{identifier}"


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

    if not settings.OPENAI_API_KEY:
        return JsonResponse(
            {"error": "The chat assistant is not configured yet. Please use the pilot form and we will reply directly."},
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
        return JsonResponse(
            {"error": "The chat assistant is unavailable right now. Please try again shortly or use the pilot form."},
            status=502,
        )

    return JsonResponse({"answer": answer})
