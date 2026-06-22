import copy
import re
import threading
import time
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.db import OperationalError, transaction

from standalone.models import Course, CourseDemoAccess, CourseDemoValidationSession
from standalone.services.preview import (
    PREVIEW_SESSION_KEY,
    _empty_course_state,
    draft_preview_written_answer,
    request_preview_quiz,
    send_preview_chat_message,
    serialize_preview_state,
    submit_preview_answer,
)
from standalone.services.preview_validation import (
    PREVIEW_VALIDATION_SESSION_KEY,
    draft_preview_validation_answer,
    reset_preview_validation_state,
    reveal_preview_validation_next,
    serialize_preview_validation_state,
    skip_preview_validation_question,
    submit_preview_validation_answer,
)


class _StateSession(dict):
    modified = False


_DEMO_ACCESS_LOCKS: dict[int, threading.RLock] = {}
_DEMO_ACCESS_LOCKS_GUARD = threading.Lock()


@contextmanager
def _demo_access_lock(access_id: int):
    with _DEMO_ACCESS_LOCKS_GUARD:
        lock = _DEMO_ACCESS_LOCKS.setdefault(int(access_id), threading.RLock())
    with lock:
        yield


def _save_with_locked_retry(instance, *, update_fields: list[str], attempts: int = 4) -> None:
    for attempt in range(1, attempts + 1):
        try:
            instance.save(update_fields=update_fields)
            return
        except OperationalError as error:
            if "locked" not in str(error).lower() or attempt >= attempts:
                raise
            time.sleep(0.05 * attempt)


def ensure_demo_access(course: Course) -> CourseDemoAccess:
    access, _created = CourseDemoAccess.objects.get_or_create(course=course)
    return access


def rotate_demo_access_token(access: CourseDemoAccess) -> CourseDemoAccess:
    access.token = uuid.uuid4()
    access.save(update_fields=["token", "updated_at"])
    return access


def normalize_demo_iframe_origins(value: str) -> str:
    normalized = []
    for raw_item in re.split(r"[\n,]+", str(value or "")):
        candidate = raw_item.strip()
        if not candidate:
            continue
        parts = urlsplit(candidate)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            continue
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        if origin not in normalized:
            normalized.append(origin)
    return "\n".join(normalized)


def demo_iframe_origin_list(value: str) -> list[str]:
    normalized = normalize_demo_iframe_origins(value)
    return [item for item in normalized.splitlines() if item]


def demo_iframe_origin_allowed(value: str, origin: str | None) -> bool:
    normalized_origin = ""
    if origin:
        parts = urlsplit(origin.strip())
        if parts.scheme and parts.netloc:
            normalized_origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    if not normalized_origin:
        return False
    return normalized_origin in demo_iframe_origin_list(value)


def new_demo_visitor_key() -> str:
    return uuid.uuid4().hex


def _shared_practice_state(access: CourseDemoAccess) -> dict:
    state = copy.deepcopy(access.shared_practice_state or {})
    if not state:
        state = _empty_course_state()
    return state


def _validation_state(session: CourseDemoValidationSession | None) -> dict:
    return copy.deepcopy((session.validation_state if session is not None else None) or {})


def _demo_request(
    access: CourseDemoAccess,
    *,
    shared_practice_state: dict | None = None,
    validation_state: dict | None = None,
):
    payload = {
        PREVIEW_SESSION_KEY: {
            str(access.course_id): copy.deepcopy(shared_practice_state if shared_practice_state is not None else _shared_practice_state(access))
        }
    }
    if validation_state is not None:
        payload[PREVIEW_VALIDATION_SESSION_KEY] = {}
        if validation_state:
            payload[PREVIEW_VALIDATION_SESSION_KEY][str(access.course_id)] = copy.deepcopy(validation_state)
    return SimpleNamespace(session=_StateSession(payload))


def _persist_demo_states(
    access: CourseDemoAccess,
    request,
    *,
    validation_session: CourseDemoValidationSession | None = None,
) -> None:
    next_shared_state = copy.deepcopy(
        request.session.get(PREVIEW_SESSION_KEY, {}).get(str(access.course_id)) or _empty_course_state()
    )
    current_shared_state = copy.deepcopy(access.shared_practice_state or _empty_course_state())
    if current_shared_state != next_shared_state:
        access.shared_practice_state = next_shared_state
        _save_with_locked_retry(access, update_fields=["shared_practice_state", "updated_at"])
    if validation_session is not None:
        next_validation_state = copy.deepcopy(
            request.session.get(PREVIEW_VALIDATION_SESSION_KEY, {}).get(str(access.course_id)) or {}
        )
        current_validation_state = copy.deepcopy(validation_session.validation_state or {})
        if current_validation_state != next_validation_state:
            validation_session.validation_state = next_validation_state
            _save_with_locked_retry(validation_session, update_fields=["validation_state", "updated_at"])


def serialize_demo_preview_state(access: CourseDemoAccess, *, active_block_id=None) -> dict:
    with _demo_access_lock(access.pk):
        locked_access = CourseDemoAccess.objects.select_related("course").get(pk=access.pk)
        request = _demo_request(locked_access)
        payload = serialize_preview_state(request, locked_access.course, active_block_id=active_block_id)
        _persist_demo_states(locked_access, request)
    return payload


def request_demo_preview_quiz(
    access: CourseDemoAccess,
    block,
    *,
    requested_question_type: str | None = None,
    preferred_objective_id: int | None = None,
    force_new: bool = False,
) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            request = _demo_request(locked_access)
            payload = request_preview_quiz(
                request,
                locked_access.course,
                block,
                requested_question_type=requested_question_type,
                preferred_objective_id=preferred_objective_id,
                force_new=force_new,
            )
            _persist_demo_states(locked_access, request)
    return payload


def submit_demo_preview_answer(access: CourseDemoAccess, block, question_id: int, selected_answers=None, *, answer_text: str = "") -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            request = _demo_request(locked_access)
            payload = submit_preview_answer(
                request,
                locked_access.course,
                block,
                question_id,
                selected_answers or [],
                answer_text=answer_text,
            )
            _persist_demo_states(locked_access, request)
    return payload


def draft_demo_preview_written_answer(access: CourseDemoAccess, block, question_id: int, answer_text: str) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            request = _demo_request(locked_access)
            payload = draft_preview_written_answer(request, locked_access.course, block, question_id, answer_text)
            _persist_demo_states(locked_access, request)
    return payload


def send_demo_preview_chat_message(access: CourseDemoAccess, block, question: str) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            request = _demo_request(locked_access)
            payload = send_preview_chat_message(request, locked_access.course, block, question)
            _persist_demo_states(locked_access, request)
    return payload


def get_or_create_demo_validation_session(access: CourseDemoAccess, visitor_key: str) -> CourseDemoValidationSession:
    session, _created = CourseDemoValidationSession.objects.get_or_create(
        demo_access=access,
        visitor_key=visitor_key,
    )
    return session


def serialize_demo_validation_practice_state(
    access: CourseDemoAccess,
    validation_session: CourseDemoValidationSession,
    *,
    restart: bool = False,
) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            locked_session = CourseDemoValidationSession.objects.select_for_update().get(pk=validation_session.pk)
            request = _demo_request(
                locked_access,
                shared_practice_state=_shared_practice_state(locked_access),
                validation_state=_validation_state(locked_session),
            )
            if restart:
                reset_preview_validation_state(request, locked_access.course)
            payload = serialize_preview_validation_state(request, locked_access.course)
            _persist_demo_states(locked_access, request, validation_session=locked_session)
    return payload


def reveal_demo_validation_practice_next(access: CourseDemoAccess, validation_session: CourseDemoValidationSession) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            locked_session = CourseDemoValidationSession.objects.select_for_update().get(pk=validation_session.pk)
            request = _demo_request(
                locked_access,
                shared_practice_state=_shared_practice_state(locked_access),
                validation_state=_validation_state(locked_session),
            )
            payload = reveal_preview_validation_next(request, locked_access.course)
            _persist_demo_states(locked_access, request, validation_session=locked_session)
    return payload


def draft_demo_validation_practice_answer(
    access: CourseDemoAccess,
    validation_session: CourseDemoValidationSession,
    question_id: int,
    answer_text: str,
) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            locked_session = CourseDemoValidationSession.objects.select_for_update().get(pk=validation_session.pk)
            request = _demo_request(
                locked_access,
                shared_practice_state=_shared_practice_state(locked_access),
                validation_state=_validation_state(locked_session),
            )
            payload = draft_preview_validation_answer(request, locked_access.course, question_id, answer_text)
            _persist_demo_states(locked_access, request, validation_session=locked_session)
    return payload


def submit_demo_validation_practice_answer(
    access: CourseDemoAccess,
    validation_session: CourseDemoValidationSession,
    question_id: int,
    selected_answers=None,
    *,
    answer_text: str = "",
) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            locked_session = CourseDemoValidationSession.objects.select_for_update().get(pk=validation_session.pk)
            request = _demo_request(
                locked_access,
                shared_practice_state=_shared_practice_state(locked_access),
                validation_state=_validation_state(locked_session),
            )
            payload = submit_preview_validation_answer(
                request,
                locked_access.course,
                question_id,
                selected_answers or [],
                answer_text=answer_text,
            )
            _persist_demo_states(locked_access, request, validation_session=locked_session)
    return payload


def skip_demo_validation_practice_question(
    access: CourseDemoAccess,
    validation_session: CourseDemoValidationSession,
    question_id: int,
) -> dict:
    with _demo_access_lock(access.pk):
        with transaction.atomic():
            locked_access = CourseDemoAccess.objects.select_for_update().select_related("course").get(pk=access.pk)
            locked_session = CourseDemoValidationSession.objects.select_for_update().get(pk=validation_session.pk)
            request = _demo_request(
                locked_access,
                shared_practice_state=_shared_practice_state(locked_access),
                validation_state=_validation_state(locked_session),
            )
            payload = skip_preview_validation_question(request, locked_access.course, question_id)
            _persist_demo_states(locked_access, request, validation_session=locked_session)
    return payload
