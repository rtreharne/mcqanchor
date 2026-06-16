import json
from datetime import timedelta
from threading import Thread
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LogoutView
from django.db import close_old_connections
from django.db import transaction
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from standalone.forms import (
    BlockAvailableFromInlineForm,
    BlockConfigTargetQuestionCountInlineForm,
    BlockSummaryInlineForm,
    BlockTitleInlineForm,
    ContentAssetForm,
    CourseAllowedEmailForm,
    CourseBlockForm,
    CourseConfigForm,
    CourseForm,
    CourseTitleInlineForm,
    EmailOrUsernameAuthenticationForm,
    LearningObjectiveInlineForm,
    MagicLinkCreateForm,
    MagicLinkEmailForm,
    SelfEnrolForm,
    StudentActivationForm,
    StudentInvitationForm,
    TeacherActivationForm,
    TeacherInvitationForm,
    UserCreationFromInviteMixin,
    ValidationEventForm,
)
from standalone.models import (
    BlockConfig,
    ContentAsset,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseMagicLink,
    Enrollment,
    LearningObjective,
    PracticeAttempt,
    PracticeAttemptQuestion,
    QuestionBankItem,
    StudentInvitation,
    StudentProfile,
    TeacherInvitation,
    TeacherProfile,
    User,
    ValidationBooking,
    ValidationEvent,
    ValidationPack,
)
from standalone.services.content import (
    delete_block_and_resequence,
    delete_learning_objective_and_resequence,
    move_learning_objective,
    regenerate_block_descriptions_and_objectives,
    regenerate_course_descriptions_and_objectives,
    summarize_block_content,
)
from standalone.services.metrics import refresh_enrollment_metrics
from standalone.services.notifications import send_logged_email
from standalone.services.preview import (
    draft_preview_written_answer,
    flag_preview_question,
    request_preview_quiz,
    send_preview_chat_message,
    serialize_preview_state,
    submit_preview_answer,
)
from standalone.services.questions import generate_question_banks
from standalone.services.validation_pdf import build_validation_pack_pdf
from standalone.tasks import process_content_asset_task, run_block_creation_processing, run_content_asset_processing


def _is_teacher(user: User) -> bool:
    return user.is_authenticated and user.role in {User.Role.TEACHER, User.Role.INTERNAL}


def _is_student(user: User) -> bool:
    return user.is_authenticated and user.role == User.Role.STUDENT


def _celery_is_enabled() -> bool:
    return bool(settings.CELERY_BROKER_URL or settings.CELERY_TASK_ALWAYS_EAGER)


def _queue_content_asset_processing(asset_id: int) -> None:
    if _celery_is_enabled():
        process_content_asset_task.delay(asset_id)
        return

    def runner() -> None:
        close_old_connections()
        try:
            run_content_asset_processing(asset_id)
        finally:
            close_old_connections()

    Thread(target=runner, daemon=True).start()


def _queue_block_regeneration(block_id: int) -> None:
    if _celery_is_enabled():
        from standalone.tasks import regenerate_block_content_task

        regenerate_block_content_task.delay(block_id)
        return

    from standalone.tasks import run_block_regeneration

    def runner() -> None:
        close_old_connections()
        try:
            run_block_regeneration(block_id)
        finally:
            close_old_connections()

    Thread(target=runner, daemon=True).start()


def _queue_block_creation_processing(block_id: int) -> None:
    if _celery_is_enabled():
        from standalone.tasks import process_block_creation_task

        process_block_creation_task.delay(block_id)
        return

    def runner() -> None:
        close_old_connections()
        try:
            run_block_creation_processing(block_id)
        finally:
            close_old_connections()

    Thread(target=runner, daemon=True).start()


def _teacher_course_or_404(user: User, course_id: int) -> Course:
    queryset = Course.objects.all() if user.role == User.Role.INTERNAL or user.is_superuser else Course.objects.filter(teacher=user)
    return get_object_or_404(queryset.select_related("config", "teacher"), pk=course_id)


def _teacher_block_or_404(user: User, block_id: int) -> CourseBlock:
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id)
    _teacher_course_or_404(user, block.course_id)
    return block


def _student_enrollment_or_404(user: User, course_id: int) -> Enrollment:
    return get_object_or_404(
        Enrollment.objects.select_related("course", "course__config", "student"),
        course_id=course_id,
        student=user,
        status=Enrollment.Status.ACTIVE,
    )


def home(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("standalone:dashboard")
    return redirect("standalone:login")


def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("standalone:dashboard")
    form = EmailOrUsernameAuthenticationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        identifier = form.cleaned_data["username"].strip()
        password = form.cleaned_data["password"]
        user_obj = User.objects.filter(email__iexact=identifier).first() or User.objects.filter(username__iexact=identifier).first()
        user = authenticate(request, username=(user_obj.username if user_obj else identifier), password=password)
        if user is not None:
            login(request, user)
            return redirect("standalone:dashboard")
        form.add_error(None, "We couldn't sign you in with those details.")
    return render(request, "standalone/login.html", {"form": form})


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    if _is_teacher(request.user):
        return redirect("standalone:teacher_dashboard")
    if _is_student(request.user):
        return redirect("standalone:student_dashboard")
    return redirect("website:home")


@login_required
def teacher_dashboard(request: HttpRequest) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    courses = Course.objects.filter(teacher=request.user).select_related("config")
    if request.user.role == User.Role.INTERNAL or request.user.is_superuser:
        courses = Course.objects.all().select_related("config", "teacher")
    context = {
        "courses": courses,
        "teacher_invitations": TeacherInvitation.objects.order_by("-created_at")[:10],
        "student_invitations": StudentInvitation.objects.select_related("course").order_by("-created_at")[:10],
    }
    return render(request, "standalone/teacher_dashboard.html", context)


@login_required
def student_dashboard(request: HttpRequest) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    enrollments = Enrollment.objects.filter(student=request.user).select_related("course", "course__config")
    course_ids = enrollments.values_list("course_id", flat=True)
    upcoming_events = ValidationEvent.objects.filter(course_id__in=course_ids, starts_at__gte=timezone.now()).order_by("starts_at")
    return render(
        request,
        "standalone/student_dashboard.html",
        {"enrollments": enrollments, "upcoming_events": upcoming_events},
    )


@login_required
def teacher_invite_create(request: HttpRequest) -> HttpResponse:
    if not (request.user.is_superuser or request.user.role == User.Role.INTERNAL):
        raise Http404
    form = TeacherInvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invitation = form.save(invited_by=request.user)
        activate_url = request.build_absolute_uri(reverse("standalone:teacher_activate", args=[invitation.token]))
        send_logged_email(
            recipient=invitation.email,
            subject="Activate your MCQ Anchor teacher account",
            body=f"You have been invited to MCQ Anchor.\n\nActivate your account here:\n{activate_url}",
            event_type="teacher_invitation",
            related_object=str(invitation.pk),
        )
        messages.success(request, "Teacher invitation sent.")
        return redirect("standalone:teacher_dashboard")
    return render(request, "standalone/form_page.html", {"title": "Invite teacher", "form": form})


@transaction.atomic
def teacher_activate(request: HttpRequest, token) -> HttpResponse:
    invitation = get_object_or_404(TeacherInvitation, token=token)
    if invitation.accepted_at:
        messages.info(request, "This teacher invitation has already been used.")
        return redirect("standalone:login")
    if invitation.is_expired:
        messages.error(request, "This teacher invitation has expired.")
        return redirect("standalone:login")

    form = TeacherActivationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = UserCreationFromInviteMixin.build_username(invitation.email)
        teacher = User.objects.create_user(
            username=username,
            email=invitation.email,
            password=form.cleaned_data["password1"],
            role=User.Role.TEACHER,
            is_email_verified=True,
        )
        teacher.first_name = form.cleaned_data["full_name"].split(" ", 1)[0]
        teacher.last_name = form.cleaned_data["full_name"].split(" ", 1)[1] if " " in form.cleaned_data["full_name"] else ""
        teacher.save(update_fields=["first_name", "last_name"])
        TeacherProfile.objects.create(user=teacher, institution=form.cleaned_data.get("institution", ""))
        invitation.accepted_at = timezone.now()
        invitation.teacher = teacher
        invitation.save(update_fields=["accepted_at", "teacher", "updated_at"])
        login(request, teacher)
        return redirect("standalone:teacher_dashboard")
    return render(request, "standalone/form_page.html", {"title": "Activate teacher account", "form": form})


@login_required
def course_create(request: HttpRequest) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    form = CourseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        course = form.save(commit=False)
        course.teacher = request.user if request.user.role == User.Role.TEACHER else request.user
        course.save()
        CourseConfig.objects.create(course=course)
        messages.success(request, "Course created.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": "Create course", "form": form})


@login_required
def course_detail(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    return render(request, "standalone/course_detail.html", _course_detail_context(course))

def _course_detail_context(course: Course):
    blocks = list(course.blocks.select_related("config").prefetch_related("assets", "learning_objectives"))
    for block in blocks:
        return_to = f"{reverse('standalone:course_detail', args=[course.pk])}#assets-content-{block.pk}"
        block.upload_url = f"{reverse('standalone:asset_upload', args=[block.pk])}?next={quote(return_to, safe='/:?=&')}"

    return {
        "course": course,
        "blocks": blocks,
        "draft_questions": course.question_bank_items.filter(status=QuestionBankItem.Status.DRAFT).count(),
        "approved_questions": course.question_bank_items.filter(status=QuestionBankItem.Status.APPROVED).count(),
        "events": course.validation_events.all(),
        "allowed_emails": course.allowed_emails.all(),
    }


def _refresh_course_summary_after_asset_change(course: Course) -> None:
    course_fragments = [block.summary for block in course.blocks.all() if block.summary.strip()]
    if not course_fragments:
        course.summary = ""
        course.save(update_fields=["summary", "updated_at"])
        return

    course_summary, _ = summarize_block_content("\n\n".join(course_fragments), max_items=4)
    course.summary = course_summary
    course.save(update_fields=["summary", "updated_at"])


def _format_block_available_from(block: CourseBlock) -> str:
    return f"{block.available_from.day} {block.available_from:%b %Y}"


@login_required
def update_block_field(request: HttpRequest, block_id: int, field_name: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id)
    _teacher_course_or_404(request.user, block.course_id)

    form_class = {
        "available_from": BlockAvailableFromInlineForm,
        "title": BlockTitleInlineForm,
        "summary": BlockSummaryInlineForm,
    }.get(field_name)
    if form_class is None:
        raise Http404

    form = form_class(request.POST, instance=block)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get(field_name, form.non_field_errors())}, status=400)

    updated_block = form.save()
    if field_name == "available_from":
        return JsonResponse(
            {
                "ok": True,
                "value": updated_block.available_from.isoformat(),
                "raw_value": updated_block.available_from.isoformat(),
                "display_value": _format_block_available_from(updated_block),
            }
        )

    display_value = getattr(updated_block, field_name) or ("No summary." if field_name == "summary" else "")
    return JsonResponse({"ok": True, "value": getattr(updated_block, field_name), "display_value": display_value})


@login_required
def update_block_config_field(request: HttpRequest, block_id: int, field_name: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    block = _teacher_block_or_404(request.user, block_id)
    config, _ = BlockConfig.objects.get_or_create(block=block)

    form_class = {
        "target_question_count": BlockConfigTargetQuestionCountInlineForm,
    }.get(field_name)
    if form_class is None:
        raise Http404

    form = form_class(request.POST, instance=config)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get(field_name, form.non_field_errors())}, status=400)

    updated_config = form.save()
    return JsonResponse(
        {
            "ok": True,
            "value": updated_config.target_question_count,
            "raw_value": updated_config.target_question_count,
            "display_value": updated_config.target_question_count,
        }
    )


@login_required
def update_course_field(request: HttpRequest, course_id: int, field_name: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = get_object_or_404(Course, pk=course_id)
    _teacher_course_or_404(request.user, course.pk)

    form_class = {
        "title": CourseTitleInlineForm,
    }.get(field_name)
    if form_class is None:
        raise Http404

    form = form_class(request.POST, instance=course)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get(field_name, form.non_field_errors())}, status=400)

    updated_course = form.save()
    return JsonResponse({"ok": True, "value": getattr(updated_course, field_name), "display_value": getattr(updated_course, field_name)})


@login_required
def student_preview(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    preview_state = serialize_preview_state(request, course)
    return render(
        request,
        "standalone/student_preview.html",
        {
            "course": course,
            "preview_state": preview_state,
        },
    )


def _preview_payload(request: HttpRequest, course: Course, block: CourseBlock, action: str) -> JsonResponse:
    if action == "quiz":
        requested_question_type = None
        if request.body and "application/json" in (request.content_type or ""):
            try:
                data = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
            requested_question_type = str(data.get("question_type", "")).strip().lower() or None
        payload = request_preview_quiz(request, course, block, requested_question_type=requested_question_type)
        return JsonResponse({"ok": True, "preview": payload})

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)

    if action == "answer":
        selected_answers = data.get("answers")
        if not isinstance(selected_answers, list):
            selected_answer = str(data.get("answer", "")).strip()
            selected_answers = [selected_answer] if selected_answer else []
        question_id = int(data.get("question_id") or 0)
        answer_text = str(data.get("answer_text", "")).strip()
        payload = submit_preview_answer(request, course, block, question_id, selected_answers, answer_text=answer_text)
        return JsonResponse({"ok": True, "preview": payload})
    if action == "draft_answer":
        question_id = int(data.get("question_id") or 0)
        answer_text = str(data.get("answer_text", "")).strip()
        payload = draft_preview_written_answer(request, course, block, question_id, answer_text)
        return JsonResponse({"ok": True, "alignment": payload})
    if action == "chat":
        question = str(data.get("question", "")).strip()
        if not question:
            return JsonResponse({"ok": False, "error": "Please enter a course question first."}, status=400)
        payload = send_preview_chat_message(request, course, block, question)
        return JsonResponse({"ok": True, "preview": payload})
    if action == "flag":
        question_id = int(data.get("question_id") or 0)
        payload = flag_preview_question(request, course, block, question_id)
        return JsonResponse({"ok": True, "preview": payload})
    raise Http404


@login_required
def student_preview_action(request: HttpRequest, course_id: int, block_id: int, action: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id, course=course)
    return _preview_payload(request, course, block, action)


@login_required
def update_learning_objective(request: HttpRequest, objective_id: int) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    objective = get_object_or_404(LearningObjective.objects.select_related("course"), pk=objective_id)
    _teacher_course_or_404(request.user, objective.course_id)

    form = LearningObjectiveInlineForm(request.POST, instance=objective)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get("text", form.non_field_errors())}, status=400)

    updated_objective = form.save()
    return JsonResponse({"ok": True, "value": updated_objective.text, "display_value": updated_objective.text})


@login_required
def move_learning_objective_view(request: HttpRequest, objective_id: int, direction: str) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    objective = get_object_or_404(LearningObjective.objects.select_related("course", "block"), pk=objective_id)
    course = _teacher_course_or_404(request.user, objective.course_id)
    moved = move_learning_objective(objective, direction)
    if moved:
        messages.success(request, "Learning objective order updated.")
    else:
        messages.info(request, "Learning objective could not be moved further.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def delete_learning_objective_view(request: HttpRequest, objective_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    objective = get_object_or_404(LearningObjective.objects.select_related("course", "block"), pk=objective_id)
    course = _teacher_course_or_404(request.user, objective.course_id)
    delete_learning_objective_and_resequence(objective)
    messages.success(request, "Learning objective deleted.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def regenerate_block_content(request: HttpRequest, block_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id)
    course = _teacher_course_or_404(request.user, block.course_id)
    if block.regeneration_status in {CourseBlock.RegenerationStatus.QUEUED, CourseBlock.RegenerationStatus.RUNNING}:
        messages.info(request, f"Regeneration is already running for {block.title}.")
        return redirect("standalone:course_detail", course.pk)

    block.regeneration_status = CourseBlock.RegenerationStatus.QUEUED
    block.regeneration_progress = 5
    block.regeneration_error = ""
    block.save(update_fields=["regeneration_status", "regeneration_progress", "regeneration_error", "updated_at"])
    _queue_block_regeneration(block.pk)
    messages.success(request, f"Started re-generation for {block.title}. Summary and learning objectives will update when it completes.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def regenerate_course_content(request: HttpRequest, course_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    refreshed = regenerate_course_descriptions_and_objectives(course)
    if refreshed["blocks"] == 0:
        messages.info(request, "No included content was available to regenerate descriptions and learning objectives.")
    else:
        messages.success(
            request,
            f"Regenerated descriptions for {refreshed['blocks']} block(s) and refreshed {refreshed['objectives']} learning objective(s).",
        )
    return redirect("standalone:course_detail", course.pk)


@login_required
def course_config_edit(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    form = CourseConfigForm(request.POST or None, instance=course.config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Course configuration updated.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": f"Configure {course.title}", "form": form})


@login_required
def add_allowed_email(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    form = CourseAllowedEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        allowed_email = form.save(commit=False)
        allowed_email.course = course
        allowed_email.save()
        messages.success(request, "Allowed student email added.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": f"Allow self-enrol email for {course.title}", "form": form})


@login_required
def student_invite_create(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    form = StudentInvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invitation = form.save(course=course, created_by=request.user)
        accept_url = request.build_absolute_uri(reverse("standalone:student_activate", args=[invitation.token]))
        send_logged_email(
            recipient=invitation.email,
            subject=f"Join {course.title} on MCQ Anchor",
            body=f"You have been invited to join {course.title}.\n\nActivate your student access here:\n{accept_url}",
            event_type="student_invitation",
            related_object=str(invitation.pk),
        )
        messages.success(request, "Student invitation sent.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": f"Invite student to {course.title}", "form": form})


@login_required
def magic_link_create(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    if not settings.STANDALONE_ENABLE_MAGIC_LINKS:
        messages.error(request, "Magic links are disabled.")
        return redirect("standalone:course_detail", course_id)
    course = _teacher_course_or_404(request.user, course_id)
    form = MagicLinkCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        magic_link = form.save(course=course, created_by=request.user)
        messages.success(request, f"Magic link created: {request.build_absolute_uri(reverse('standalone:magic_enrol', args=[magic_link.token]))}")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": f"Create magic link for {course.title}", "form": form})


@transaction.atomic
def student_activate(request: HttpRequest, token) -> HttpResponse:
    invitation = get_object_or_404(StudentInvitation.objects.select_related("course"), token=token)
    if invitation.accepted_at:
        messages.info(request, "This student invitation has already been used.")
        return redirect("standalone:login")
    if invitation.is_expired:
        messages.error(request, "This student invitation has expired.")
        return redirect("standalone:login")

    form = StudentActivationForm(request.POST or None, locked_email=invitation.email)
    if request.method == "POST" and form.is_valid():
        user = User.objects.filter(email__iexact=invitation.email).first()
        if user is None:
            user = User.objects.create_user(
                username=UserCreationFromInviteMixin.build_username(invitation.email),
                email=invitation.email,
                password=form.cleaned_data["password1"],
                role=User.Role.STUDENT,
                is_email_verified=True,
            )
            user.first_name = form.cleaned_data["full_name"].split(" ", 1)[0]
            user.last_name = form.cleaned_data["full_name"].split(" ", 1)[1] if " " in form.cleaned_data["full_name"] else ""
            user.save(update_fields=["first_name", "last_name"])
            StudentProfile.objects.create(user=user, institution=form.cleaned_data.get("institution", ""))
        Enrollment.objects.get_or_create(course=invitation.course, student=user, defaults={"source": "invite"})
        invitation.accepted_at = timezone.now()
        invitation.enrolled_user = user
        invitation.save(update_fields=["accepted_at", "enrolled_user", "updated_at"])
        login(request, user)
        return redirect("standalone:student_dashboard")
    return render(request, "standalone/form_page.html", {"title": f"Join {invitation.course.title}", "form": form})


@transaction.atomic
def self_enrol(request: HttpRequest, course_slug: str) -> HttpResponse:
    course = get_object_or_404(Course.objects.select_related("config"), slug=course_slug, is_active=True)
    if not course.config.self_enrol_enabled or not settings.STANDALONE_ENABLE_SELF_ENROL:
        raise Http404
    form = SelfEnrolForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        domain = email.split("@")[-1]
        if course.config.self_enrol_domain and domain.lower() != course.config.self_enrol_domain.lower():
            form.add_error("email", "This email domain is not allowed for self-enrolment.")
        elif not CourseAllowedEmail.objects.filter(course=course, email__iexact=email).exists():
            form.add_error("email", "This email is not on the self-enrolment list for this course.")
        else:
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User.objects.create_user(
                    username=UserCreationFromInviteMixin.build_username(email),
                    email=email,
                    password=form.cleaned_data["password1"],
                    role=User.Role.STUDENT,
                    is_email_verified=True,
                )
                user.first_name = form.cleaned_data["full_name"].split(" ", 1)[0]
                user.last_name = form.cleaned_data["full_name"].split(" ", 1)[1] if " " in form.cleaned_data["full_name"] else ""
                user.save(update_fields=["first_name", "last_name"])
                StudentProfile.objects.create(user=user, institution=form.cleaned_data.get("institution", ""))
            Enrollment.objects.get_or_create(course=course, student=user, defaults={"source": "self_enrol"})
            send_logged_email(
                recipient=email,
                subject=f"Enrolment confirmed for {course.title}",
                body=f"You are now enrolled on {course.title} in MCQ Anchor.",
                event_type="self_enrol_confirmation",
                related_object=str(course.pk),
            )
            login(request, user)
            return redirect("standalone:student_dashboard")
    return render(request, "standalone/form_page.html", {"title": f"Self-enrol in {course.title}", "form": form})


@transaction.atomic
def magic_enrol(request: HttpRequest, token) -> HttpResponse:
    magic_link = get_object_or_404(CourseMagicLink.objects.select_related("course", "course__config"), token=token, is_active=True)
    if magic_link.is_expired or magic_link.use_count >= magic_link.max_uses:
        messages.error(request, "This magic link is no longer available.")
        return redirect("standalone:login")

    form = MagicLinkEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        domain = email.split("@")[-1]
        if magic_link.course.config.self_enrol_domain and domain.lower() != magic_link.course.config.self_enrol_domain.lower():
            form.add_error("email", "This email domain is not allowed for this course.")
        else:
            user = User.objects.filter(email__iexact=email).first()
            if user is None:
                user = User.objects.create_user(
                    username=UserCreationFromInviteMixin.build_username(email),
                    email=email,
                    password=form.cleaned_data["password1"],
                    role=User.Role.STUDENT,
                    is_email_verified=True,
                )
                user.first_name = form.cleaned_data["full_name"].split(" ", 1)[0]
                user.last_name = form.cleaned_data["full_name"].split(" ", 1)[1] if " " in form.cleaned_data["full_name"] else ""
                user.save(update_fields=["first_name", "last_name"])
                StudentProfile.objects.create(user=user, institution=form.cleaned_data.get("institution", ""))
            Enrollment.objects.get_or_create(course=magic_link.course, student=user, defaults={"source": "magic_link"})
            StudentInvitation.objects.create(
                course=magic_link.course,
                email=email,
                invitation_type=StudentInvitation.InvitationType.MAGIC,
                created_by=magic_link.created_by,
                expires_at=timezone.now() + timedelta(seconds=1),
                accepted_at=timezone.now(),
                enrolled_user=user,
            )
            magic_link.use_count += 1
            if magic_link.use_count >= magic_link.max_uses:
                magic_link.is_active = False
            magic_link.save(update_fields=["use_count", "is_active", "updated_at"])
            login(request, user)
            return redirect("standalone:student_dashboard")
    return render(request, "standalone/form_page.html", {"title": f"Join {magic_link.course.title}", "form": form})


@login_required
def block_create(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    form = CourseBlockForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        block = form.save(commit=False)
        block.course = course
        last_block = course.blocks.order_by("-order", "-pk").first()
        block.order = (last_block.order + 1) if last_block else 1
        block.save()
        from standalone.models import BlockConfig

        BlockConfig.objects.get_or_create(block=block)
        assets = form.save_assets(block=block, uploaded_by=request.user)
        return_to = f"{reverse('standalone:course_detail', args=[course.pk])}#block-content-{block.pk}"
        if assets:
            block.regeneration_status = CourseBlock.RegenerationStatus.QUEUED
            block.regeneration_progress = 5
            block.regeneration_error = ""
            block.save(update_fields=["regeneration_status", "regeneration_progress", "regeneration_error", "updated_at"])
            _queue_block_creation_processing(block.pk)
            messages.success(
                request,
                "Course block added. Uploaded files are processing in the background and the summary and learning objectives will appear when the task completes.",
            )
        else:
            messages.success(request, "Course block added.")
        return redirect(return_to)
    return render(
        request,
        "standalone/form_page.html",
        {
            "title": f"Add block to {course.title}",
            "form": form,
            "has_upload_picker": True,
            "submit_label": "Create block",
            "submit_progress_label": "Creating block...",
        },
    )


@login_required
def block_delete(request: HttpRequest, block_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id)
    course = _teacher_course_or_404(request.user, block.course_id)
    if block.regeneration_status in {CourseBlock.RegenerationStatus.QUEUED, CourseBlock.RegenerationStatus.RUNNING}:
        messages.error(request, f"Cannot delete {block.title} while re-generation is in progress.")
        return redirect("standalone:course_detail", course.pk)

    block_title = block.title
    delete_block_and_resequence(block)
    messages.success(request, f"Deleted block {block_title}.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def asset_upload(request: HttpRequest, block_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id)
    course = _teacher_course_or_404(request.user, block.course_id)
    next_url = request.POST.get("next") or request.GET.get("next") or f"{reverse('standalone:course_detail', args=[course.pk])}#assets-content-{block.pk}"
    form = ContentAssetForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        assets = form.save_assets(block=block, uploaded_by=request.user)
        for asset in assets:
            _queue_content_asset_processing(asset.pk)
        file_count = len(assets)
        noun = "file" if file_count == 1 else "files"
        messages.success(request, f"{file_count} {noun} uploaded. Processing is running in the background.")
        return redirect(next_url)
    return render(
        request,
        "standalone/form_page.html",
        {
            "title": f"Upload files to {block.title}",
            "form": form,
            "cancel_url": next_url,
            "next_url": next_url,
            "is_upload_form": True,
        },
    )


@login_required
def toggle_asset_generation(request: HttpRequest, asset_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    asset = get_object_or_404(ContentAsset.objects.select_related("block", "block__course"), pk=asset_id)
    course = _teacher_course_or_404(request.user, asset.block.course_id)
    asset.include_in_generation = not asset.include_in_generation
    asset.save(update_fields=["include_in_generation", "updated_at"])
    messages.success(request, "Asset generation setting updated.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def delete_asset(request: HttpRequest, asset_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    asset = get_object_or_404(ContentAsset.objects.select_related("block", "block__course"), pk=asset_id)
    block = asset.block
    course = _teacher_course_or_404(request.user, block.course_id)
    next_url = request.POST.get("next") or f"{reverse('standalone:course_detail', args=[course.pk])}#assets-content-{block.pk}"

    asset.file.delete(save=False)
    asset.delete()

    if block.assets.filter(include_in_generation=True).exists():
        regenerate_block_descriptions_and_objectives(block)
    else:
        block.summary = ""
        block.save(update_fields=["summary", "updated_at"])
        _refresh_course_summary_after_asset_change(course)

    messages.success(request, "Uploaded file deleted.")
    return redirect(next_url)


@login_required
def generate_course_bank(request: HttpRequest, course_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    created = generate_question_banks(course, approve=True)
    messages.success(request, f"Generated {created} question-bank items.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def approve_course_questions(request: HttpRequest, course_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    updated = course.question_bank_items.filter(status=QuestionBankItem.Status.DRAFT).update(status=QuestionBankItem.Status.APPROVED)
    messages.success(request, f"Approved {updated} questions.")
    return redirect("standalone:course_detail", course.pk)


@login_required
def validation_event_create(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    initial = {"freeze_at": timezone.now() + timedelta(hours=24)}
    form = ValidationEventForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        event = form.save(commit=False)
        event.course = course
        event.created_by = request.user
        event.save()
        messages.success(request, "Validation event created.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": f"Create validation event for {course.title}", "form": form})


@login_required
def validation_book(request: HttpRequest, event_id: int) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    enrollment = _student_enrollment_or_404(request.user, event.course_id)
    if timezone.now() >= event.freeze_at:
        messages.error(request, "Booking has closed for this validation session.")
        return redirect("standalone:student_dashboard")
    if not event.has_space:
        messages.error(request, "This validation session is already full.")
        return redirect("standalone:student_dashboard")
    booking, created = ValidationBooking.objects.get_or_create(event=event, enrollment=enrollment)
    if not created and booking.status == ValidationBooking.Status.BOOKED:
        messages.info(request, "You are already booked onto this validation.")
        return redirect("standalone:student_dashboard")
    booking.status = ValidationBooking.Status.BOOKED
    booking.cancelled_at = None
    booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    send_logged_email(
        recipient=request.user.email,
        subject=f"Validation booked for {event.course.title}",
        body=f"You are booked for {event.title} on {event.starts_at:%d %b %Y %H:%M} at {event.location}.",
        event_type="validation_booking",
        related_object=str(booking.pk),
    )
    messages.success(request, "Validation booked.")
    return redirect("standalone:student_dashboard")


@login_required
def validation_cancel(request: HttpRequest, booking_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_student(request.user):
        raise Http404
    booking = get_object_or_404(ValidationBooking.objects.select_related("event", "enrollment__student"), pk=booking_id, enrollment__student=request.user)
    if timezone.now() >= booking.event.freeze_at:
        messages.error(request, "This booking can no longer be cancelled.")
        return redirect("standalone:student_dashboard")
    booking.status = ValidationBooking.Status.CANCELLED
    booking.cancelled_at = timezone.now()
    booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    send_logged_email(
        recipient=request.user.email,
        subject=f"Validation cancelled for {booking.event.course.title}",
        body=f"Your booking for {booking.event.title} has been cancelled.",
        event_type="validation_cancellation",
        related_object=str(booking.pk),
    )
    messages.success(request, "Validation booking cancelled.")
    return redirect("standalone:student_dashboard")


@login_required
def validation_pack_pdf(request: HttpRequest, event_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    _teacher_course_or_404(request.user, event.course_id)
    bookings = list(event.bookings.filter(status=ValidationBooking.Status.BOOKED).select_related("enrollment__student"))
    pack = ValidationPack.objects.create(event=event, generated_by=request.user, generated_for_booking_count=len(bookings))
    pdf_bytes = build_validation_pack_pdf(pack, bookings)
    return HttpResponse(pdf_bytes, content_type="application/pdf", headers={"Content-Disposition": f'inline; filename="validation-pack-{event.pk}.pdf"'})


@login_required
def practice_quiz(request: HttpRequest, course_id: int) -> HttpResponse:
    enrollment = _student_enrollment_or_404(request.user, course_id)
    today = timezone.localdate()
    mode = request.GET.get("mode", PracticeAttempt.AttemptType.PRACTICE)
    if mode not in {PracticeAttempt.AttemptType.PRACTICE, PracticeAttempt.AttemptType.VALIDATION_PRACTICE}:
        mode = PracticeAttempt.AttemptType.PRACTICE
    attempt_id = request.GET.get("attempt")
    attempt = None
    if attempt_id:
        attempt = get_object_or_404(PracticeAttempt, pk=attempt_id, enrollment=enrollment)

    if request.method == "POST":
        question = get_object_or_404(
            QuestionBankItem.objects.filter(
                block__available_from__lte=today,
                question_type=QuestionBankItem.QuestionType.MCQ,
            ),
            pk=request.POST.get("question_id"),
            course=enrollment.course,
        )
        selected = request.POST.get("answer", "")
        options = [question.correct_answer, *question.distractors]
        is_correct = selected == question.correct_answer
        PracticeAttemptQuestion.objects.get_or_create(
            attempt=attempt,
            question=question,
            defaults={
                "order": attempt.attempt_questions.count() + 1,
                "selected_answer": selected,
                "is_correct": is_correct,
                "feedback": question.explanation if attempt.feedback_visible_immediately else "",
            },
        )

    seen_ids = list(
        PracticeAttemptQuestion.objects.filter(attempt__enrollment=enrollment).values_list("question_id", flat=True)
    )
    question_queryset = enrollment.course.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        question_type=QuestionBankItem.QuestionType.MCQ,
        block__available_from__lte=today,
    ).exclude(pk__in=seen_ids)

    if attempt is None:
        if not question_queryset.exists():
            messages.info(request, "No practice questions are available yet for the released blocks in this course.")
            return redirect("standalone:student_dashboard")
        attempt = PracticeAttempt.objects.create(
            enrollment=enrollment,
            attempt_type=mode,
            time_limit_minutes=20 if mode == PracticeAttempt.AttemptType.VALIDATION_PRACTICE else None,
            feedback_visible_immediately=mode == PracticeAttempt.AttemptType.PRACTICE,
        )
        return redirect(f"{reverse('standalone:practice_quiz', args=[course_id])}?attempt={attempt.pk}&mode={mode}")

    question = question_queryset.select_related("learning_objective", "block").first()

    if question is None:
        if not attempt.attempt_questions.exists():
            attempt.delete()
            messages.info(request, "No practice questions are available yet for the released blocks in this course.")
            return redirect("standalone:student_dashboard")
        total = attempt.attempt_questions.count() or 1
        correct = attempt.attempt_questions.filter(is_correct=True).count()
        attempt.score = round(correct * 100 / total, 2)
        attempt.completed_at = timezone.now()
        attempt.save(update_fields=["score", "completed_at", "updated_at"])
        if attempt.attempt_type == PracticeAttempt.AttemptType.PRACTICE:
            refresh_enrollment_metrics(enrollment)
        return render(request, "standalone/practice_summary.html", {"attempt": attempt, "enrollment": enrollment})

    options = [question.correct_answer, *question.distractors]
    return render(
        request,
        "standalone/practice_quiz.html",
        {
            "attempt": attempt,
            "question": question,
            "options": options,
            "mode": mode,
            "question_number": attempt.attempt_questions.count() + 1,
        },
    )


class StandaloneLogoutView(LogoutView):
    next_page = "website:home"
