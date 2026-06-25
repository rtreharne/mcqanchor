import json
import csv
from datetime import timedelta
from pathlib import Path
import re
from threading import Thread
from urllib.parse import quote, urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LogoutView
from django import forms
from django.db import close_old_connections, transaction
from django.db.models import Avg, Count, Q
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt

from standalone.forms import (
    BlockAvailableFromInlineForm,
    BlockConfigForm,
    BlockConfigTargetQuestionCountInlineForm,
    BlockProjectCreateForm,
    BlockProjectEditForm,
    BlockSummaryInlineForm,
    BlockTitleInlineForm,
    ContentAssetForm,
    CourseAllowedEmailForm,
    CourseBlockForm,
    CourseConfigForm,
    CourseForm,
    CourseImportChapterSelectionForm,
    CourseImportUploadForm,
    CourseTitleInlineForm,
    EmailOrUsernameAuthenticationForm,
    LearningObjectiveGuidanceInlineForm,
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
    BlockProject,
    ContentAsset,
    Course,
    CourseAllowedEmail,
    CourseBlock,
    CourseConfig,
    CourseDemoAccess,
    CourseImport,
    CourseMagicLink,
    Enrollment,
    LearningObjective,
    LearningObjectiveCorrection,
    PracticeAttempt,
    PracticeAttemptQuestion,
    QuestionBankItem,
    ProjectArtifact,
    ProjectAssignment,
    StudentInvitation,
    StudentProfile,
    TeacherInvitation,
    TeacherProfile,
    User,
    ValidationAttempt,
    ValidationBooking,
    ValidationEvent,
    ValidationPack,
)
from standalone.services.content import (
    delete_block_and_resequence,
    delete_learning_objective_and_resequence,
    move_course_block,
    move_learning_objective,
    regenerate_block_descriptions_and_objectives,
    regenerate_course_descriptions_and_objectives,
    summarize_block_content,
)
from standalone.services.demo_mode import (
    demo_iframe_origin_allowed,
    demo_iframe_origin_list,
    draft_demo_preview_written_answer,
    draft_demo_validation_practice_answer,
    ensure_demo_access,
    get_or_create_demo_validation_session,
    new_demo_visitor_key,
    request_demo_preview_quiz,
    reveal_demo_validation_practice_next,
    rotate_demo_access_token,
    send_demo_preview_chat_message,
    serialize_demo_preview_state,
    serialize_demo_validation_practice_state,
    skip_demo_validation_practice_question,
    submit_demo_preview_answer,
    submit_demo_validation_practice_answer,
)
from standalone.services.metrics import refresh_enrollment_metrics
from standalone.services.notifications import send_logged_email
from standalone.services.preview import (
    draft_preview_written_answer,
    flag_preview_question,
    request_preview_quiz,
    save_preview_objective_guardrail,
    send_preview_chat_message,
    serialize_preview_state,
    submit_preview_answer,
)
from standalone.services.projects import (
    ProjectAuthoringError,
    ProjectImmutableError,
    ProjectSpecError,
    archive_block_project,
    build_project_results_rows,
    generate_block_project_draft,
    open_preview_project,
    open_student_project,
    publish_block_project,
    send_preview_project_message,
    send_student_project_message,
    submit_preview_project_answer,
    submit_student_project_answer,
    validate_block_project_spec,
)
from standalone.services.preview_validation import (
    confirm_preview_student_validate,
    draft_preview_student_validate_answer,
    draft_preview_validation_answer,
    preview_validation_history_items,
    preview_validation_history_session,
    reset_preview_validation_state,
    reveal_preview_validation_next,
    reveal_preview_student_validate_next,
    serialize_preview_student_validate_state,
    serialize_preview_validation_state,
    skip_preview_student_validate_question,
    skip_preview_validation_question,
    submit_preview_student_validate_response,
    submit_preview_validation_answer,
)
from standalone.services.questions import generate_question_banks
from standalone.services.student_practice import (
    draft_student_practice_written_answer,
    flag_student_practice_question,
    request_student_practice_quiz,
    send_student_practice_chat_message,
    serialize_student_practice_state,
    submit_student_practice_answer,
)
from standalone.services.validation_pdf import build_validation_pack_pdf
from standalone.services.validation_flow import (
    ValidationFlowError,
    confirm_official_validation_instructions,
    current_room_code,
    draft_official_validation_answer,
    draft_validation_practice_answer,
    ensure_room_code_secret,
    get_or_create_official_attempt,
    get_or_create_validation_practice_attempt,
    restart_validation_practice_attempt,
    release_event_feedback,
    reveal_official_validation_next,
    reveal_validation_practice_next,
    report_validation_presence,
    room_code_client_payload,
    room_code_payload,
    serialize_official_validation_session,
    serialize_validation_practice_session,
    skip_official_validation_question,
    skip_validation_practice_question,
    submit_official_validation_response,
    submit_validation_practice_response,
)
from standalone.tasks import (
    analyze_course_pdf_import_task,
    create_blocks_from_course_import_task,
    process_content_asset_task,
    run_block_creation_processing,
    run_content_asset_processing,
    run_course_import_analysis,
    run_course_import_block_creation,
)


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


def _queue_course_import_analysis(import_id: int) -> None:
    if _celery_is_enabled():
        analyze_course_pdf_import_task.delay(import_id)
        return

    def runner() -> None:
        close_old_connections()
        try:
            run_course_import_analysis(import_id)
        finally:
            close_old_connections()

    Thread(target=runner, daemon=True).start()


def _queue_course_import_block_creation(import_id: int, selected_chapter_ids: list[int]) -> None:
    if _celery_is_enabled():
        create_blocks_from_course_import_task.delay(import_id, selected_chapter_ids)
        return

    def runner() -> None:
        close_old_connections()
        try:
            run_course_import_block_creation(import_id, selected_chapter_ids)
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


def _teacher_project_or_404(user: User, project_id: int) -> BlockProject:
    project = get_object_or_404(BlockProject.objects.select_related("block__course"), pk=project_id)
    _teacher_course_or_404(user, project.block.course_id)
    return project


def _student_enrollment_or_404(user: User, course_id: int) -> Enrollment:
    return get_object_or_404(
        Enrollment.objects.select_related("course", "course__config", "student"),
        course_id=course_id,
        student=user,
        status=Enrollment.Status.ACTIVE,
    )


def _normalise_access_email(email: str) -> str:
    return str(email or "").strip().lower()


def _email_domain(email: str) -> str:
    return _normalise_access_email(email).rsplit("@", 1)[-1]


def _student_user_for_access_form(form, email: str):
    email = _normalise_access_email(email)
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
        StudentProfile.objects.get_or_create(user=user, defaults={"institution": form.cleaned_data.get("institution", "")})
        return user

    if user.role != User.Role.STUDENT:
        form.add_error("email", "This email belongs to a staff or teacher account. Use a student email address.")
        return None
    if not user.check_password(form.cleaned_data["password1"]):
        form.add_error("password1", "Enter the password for this existing student account.")
        return None
    return user


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
    courses = courses.annotate(
        student_count=Count("enrollments", distinct=True),
        question_count=Count("question_bank_items", distinct=True),
        block_count=Count("blocks", distinct=True),
        active_block_count=Count(
            "blocks",
            filter=Q(blocks__available_from__lte=timezone.localdate()),
            distinct=True,
        ),
        practice_mastery_average=Avg("enrollments__mastery_score"),
        practice_coverage_average=Avg("enrollments__coverage_score"),
        practice_engagement_average=Avg("enrollments__engagement_score"),
        practice_target_average=Avg("enrollments__target_score"),
    )
    course_list = list(courses)
    _attach_course_practice_averages(course_list)
    validation_events = list(
        ValidationEvent.objects.filter(
            course_id__in=[course.pk for course in course_list],
            mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
        )
        .select_related("course")
        .order_by("starts_at", "created_at")
    )
    context = {
        "courses": course_list,
        "validation_events": validation_events,
        "dashboard_stats": {
            "course_count": len(course_list),
            "student_count": sum(course.student_count for course in course_list),
            "question_count": sum(course.question_count for course in course_list),
            "active_block_count": sum(course.active_block_count for course in course_list),
        },
        "teacher_invitations": TeacherInvitation.objects.order_by("-created_at")[:10],
        "student_invitations": StudentInvitation.objects.select_related("course").order_by("-created_at")[:10],
    }
    return render(request, "standalone/teacher_dashboard.html", context)


@login_required
def student_dashboard(request: HttpRequest) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    enrollments = list(
        Enrollment.objects.filter(student=request.user)
        .select_related("course", "course__config")
        .prefetch_related("validation_bookings__event")
    )
    course_ids = [enrollment.course_id for enrollment in enrollments]
    now = timezone.now()
    events = list(
        ValidationEvent.objects.filter(course_id__in=course_ids, mode=ValidationEvent.Mode.DIGITAL_INVIGILATION)
        .select_related("course")
        .order_by("starts_at", "created_at")
    )
    attempts_by_key = {
        (attempt.enrollment_id, attempt.event_id): attempt
        for attempt in ValidationAttempt.objects.filter(enrollment__student=request.user).select_related("event")
    }
    bookings_by_key = {
        (booking.enrollment_id, booking.event_id): booking
        for booking in ValidationBooking.objects.filter(enrollment__student=request.user).select_related("event")
    }
    upcoming_sessions = []
    for enrollment in enrollments:
        enrollment_events = [event for event in events if event.course_id == enrollment.course_id]
        enrollment.booked_validation_events = []
        enrollment.bookable_validation_events = []
        for event in enrollment_events:
            attempt = attempts_by_key.get((enrollment.pk, event.pk))
            booking = bookings_by_key.get((enrollment.pk, event.pk))
            event.student_attempt = attempt
            event.student_booking = booking
            event.student_spaces_left = event.spaces_left
            event.student_recent_booking_count = event.recent_booking_count(hours=24)
            if booking and booking.status == ValidationBooking.Status.BOOKED:
                enrollment.booked_validation_events.append(event)
                continue
            if event.booking_is_open(now):
                enrollment.bookable_validation_events.append(event)
                upcoming_sessions.append(event)
    return render(
        request,
        "standalone/student_dashboard.html",
        {"enrollments": enrollments, "upcoming_events": upcoming_sessions, "now": now},
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
        messages.success(request, "Course created. You can add blocks manually or import a PDF textbook to detect chapters.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": "Create course", "form": form})


@login_required
def course_detail(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    return render(request, "standalone/course_detail.html", _course_detail_context(course, request=request))


@login_required
def course_delete(request: HttpRequest, course_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    course_title = course.title

    for asset in ContentAsset.objects.filter(block__course=course):
        asset.file.delete(save=False)
    for course_import in course.imports.all():
        course_import.source_file.delete(save=False)

    course.delete()
    messages.success(request, f"Deleted course {course_title}.")
    return redirect("standalone:teacher_dashboard")


def _course_detail_context(course: Course, *, request: HttpRequest | None = None):
    config, _ = CourseConfig.objects.get_or_create(course=course)
    demo_access = ensure_demo_access(course)
    blocks = list(
        course.blocks.select_related("config")
        .annotate(
            asset_count=Count("assets", distinct=True),
            objective_count=Count("learning_objectives", distinct=True),
            approved_question_count=Count(
                "question_bank_items",
                filter=Q(question_bank_items__status=QuestionBankItem.Status.APPROVED),
                distinct=True,
            ),
        )
        .order_by("order", "created_at", "pk")
        .prefetch_related(
            "assets",
            "learning_objectives",
            "learning_objectives__corrections__created_by",
            "projects__assignments",
        )
    )
    _attach_course_practice_average(course)
    magic_links = list(course.magic_links.all())
    active_magic_link_count = sum(
        1
        for magic_link in magic_links
        if magic_link.is_active and not magic_link.is_expired and magic_link.use_count < magic_link.max_uses
    )
    for block in blocks:
        try:
            block_config = block.config
        except BlockConfig.DoesNotExist:
            block_config = BlockConfig(block=block, target_question_count=block.preview_target_question_count)
        block.block_config_form = BlockConfigForm(instance=block_config, prefix=f"block-{block.pk}")
        block.project_create_form = BlockProjectCreateForm(prefix=f"project-create-{block.pk}")
        block.projects_for_course_detail = []
        for project in block.projects.all():
            project.edit_form = BlockProjectEditForm(instance=project, prefix=f"project-{project.pk}")
            project.assignment_count = project.assignments.count()
            block.projects_for_course_detail.append(project)
        return_to = f"{reverse('standalone:course_detail', args=[course.pk])}#assets-content-{block.pk}"
        block.upload_url = f"{reverse('standalone:asset_upload', args=[block.pk])}?next={quote(return_to, safe='/:?=&')}"

    events = list(
        course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION)
        .prefetch_related("blocks")
        .order_by("starts_at", "created_at")
    )
    return {
        "course": course,
        "course_config_form": CourseConfigForm(instance=config),
        "blocks": blocks,
        "course_student_count": course.enrollments.count(),
        "draft_questions": course.question_bank_items.filter(status=QuestionBankItem.Status.DRAFT).count(),
        "approved_questions": course.question_bank_items.filter(status=QuestionBankItem.Status.APPROVED).count(),
        "events": events,
        "recent_imports": course.imports.order_by("-created_at")[:3],
        "allowed_emails": course.allowed_emails.all(),
        "self_enrol_enabled": config.self_enrol_enabled and settings.STANDALONE_ENABLE_SELF_ENROL,
        "self_enrol_url": reverse("standalone:self_enrol", args=[course.slug]),
        "active_magic_link_count": active_magic_link_count,
        "demo_access": demo_access,
        "demo_access_context": _demo_access_context(request, demo_access) if request is not None else {},
    }


def _attach_course_practice_averages(courses):
    for course in courses:
        _normalise_course_practice_averages(course)


def _attach_course_practice_average(course: Course) -> None:
    averages = course.enrollments.aggregate(
        practice_mastery_average=Avg("mastery_score"),
        practice_coverage_average=Avg("coverage_score"),
        practice_engagement_average=Avg("engagement_score"),
        practice_target_average=Avg("target_score"),
    )
    for key, value in averages.items():
        setattr(course, key, value)
    _normalise_course_practice_averages(course)


def _normalise_course_practice_averages(course: Course) -> None:
    metrics = {}
    for metric_name in ("mastery", "coverage", "engagement", "target"):
        attr_name = f"practice_{metric_name}_average"
        metric_value = round(float(getattr(course, attr_name, 0) or 0), 2)
        setattr(course, attr_name, metric_value)
        metrics[metric_name] = metric_value
    course.practice_overall_average = _weighted_practice_average(course, metrics)


def _demo_embed_origin_from_request(request: HttpRequest) -> str:
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        return origin
    referer = (request.headers.get("Referer") or "").strip()
    if not referer:
        return ""
    parts = urlsplit(referer)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _apply_demo_response_headers(request: HttpRequest, response: HttpResponse, course: Course, *, embed_mode: bool) -> HttpResponse:
    if embed_mode:
        allowed_origins = demo_iframe_origin_list(course.config.demo_iframe_allowed_origins)
        if not allowed_origins:
            response.status_code = 403
            return response
        frame_ancestors = " ".join(["'self'", *allowed_origins])
        response["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
    else:
        response["X-Frame-Options"] = "SAMEORIGIN"
        response["Content-Security-Policy"] = "frame-ancestors 'self'"
    return response


def _demo_embed_blocked_response(request: HttpRequest, course: Course, *, reason: str) -> HttpResponse:
    response = render(
        request,
        "standalone/demo_embed_blocked.html",
        {
            "course": course,
            "is_demo_mode": True,
            "is_embed_mode": True,
            "demo_mode_label": "Demo mode",
            "demo_home_url": "#",
            "blocked_reason": reason,
        },
        status=403,
    )
    allowed_origins = demo_iframe_origin_list(course.config.demo_iframe_allowed_origins)
    if allowed_origins:
        frame_ancestors = " ".join(["'self'", *allowed_origins])
        response["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
    return response


def _demo_query_string(*, embed: bool = False, visitor_key: str | None = None, restart: bool = False) -> str:
    params: dict[str, str] = {}
    if embed:
        params["embed"] = "1"
    if visitor_key:
        params["visitor"] = visitor_key
    if restart:
        params["restart"] = "1"
    if not params:
        return ""
    return f"?{urlencode(params)}"


def _demo_practice_url(access: CourseDemoAccess, *, embed: bool = False) -> str:
    return f"{reverse('standalone:demo_practice', args=[access.token])}{_demo_query_string(embed=embed)}"


def _demo_validation_practice_url(
    access: CourseDemoAccess,
    *,
    embed: bool = False,
    visitor_key: str | None = None,
    restart: bool = False,
) -> str:
    return (
        f"{reverse('standalone:demo_validation_practice', args=[access.token])}"
        f"{_demo_query_string(embed=embed, visitor_key=visitor_key, restart=restart)}"
    )


def _demo_validation_state_with_practice_return(
    session_state: dict,
    access: CourseDemoAccess,
    *,
    embed: bool = False,
) -> dict:
    if not session_state.get("completed"):
        return session_state
    return_url = _demo_practice_url(access, embed=embed)
    transcript = [
        message
        for message in list(session_state.get("transcript") or [])
        if str(message.get("id") or "") != "demo-validation-return-to-practice"
    ]
    transcript.append(
        {
            "id": "demo-validation-return-to-practice",
            "role": "assistant",
            "kind": "cta",
            "text": "Practice validation complete. Return to practice mode when you're ready to continue.",
            "actions": [
                {
                    "label": "Return to practice",
                    "url": return_url,
                    "style": "button",
                }
            ],
        }
    )
    return {**session_state, "transcript": transcript, "practice_return_url": return_url}


def _demo_iframe_snippet(request: HttpRequest, access: CourseDemoAccess) -> str:
    src = request.build_absolute_uri(_demo_practice_url(access, embed=True))
    return (
        f'<iframe src="{src}" title="{access.course.title} demo" '
        'width="100%" height="900" style="border:0;border-radius:1rem;overflow:hidden;" loading="lazy"></iframe>'
    )


def _demo_access_context(request: HttpRequest, access: CourseDemoAccess) -> dict:
    return {
        "enabled": bool(access.course.config.demo_enabled),
        "demo_url": request.build_absolute_uri(_demo_practice_url(access)),
        "demo_embed_url": request.build_absolute_uri(_demo_practice_url(access, embed=True)),
        "demo_iframe_snippet": _demo_iframe_snippet(request, access),
        "allowed_origins": access.course.config.demo_iframe_allowed_origins,
    }


def _demo_access_or_404(token) -> CourseDemoAccess:
    access = get_object_or_404(
        CourseDemoAccess.objects.select_related("course", "course__config"),
        token=token,
        course__is_active=True,
    )
    if not access.course.config.demo_enabled:
        raise Http404
    return access


def _student_sidebar_validation_booking_url(enrollment: Enrollment) -> str:
    now = timezone.now()
    events = list(
        ValidationEvent.objects.filter(course=enrollment.course, mode=ValidationEvent.Mode.DIGITAL_INVIGILATION)
        .order_by("starts_at", "created_at")
    )
    attempts_by_event_id = {
        attempt.event_id: attempt
        for attempt in ValidationAttempt.objects.filter(enrollment=enrollment).select_related("event")
    }
    bookings_by_event_id = {
        booking.event_id: booking
        for booking in ValidationBooking.objects.filter(enrollment=enrollment, status=ValidationBooking.Status.BOOKED)
    }

    for event in events:
        if event.pk in bookings_by_event_id:
            attempt = attempts_by_event_id.get(event.pk)
            if attempt is not None:
                return reverse("standalone:validation_attempt", args=[attempt.pk])
            if event.starts_at <= now:
                return reverse("standalone:validation_start", args=[event.pk])
            return reverse("standalone:student_dashboard")

    for event in events:
        if event.booking_is_open(now):
            return reverse("standalone:validation_book", args=[event.pk])

    return reverse("standalone:student_dashboard")


def _preview_sidebar_validation_booking_url(course: Course) -> str:
    return f"{reverse('standalone:course_detail', args=[course.pk])}#course-validation-heading"


def _preview_has_bookable_validation_sessions(course: Course, *, now=None) -> bool:
    current_time = now or timezone.now()
    return course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).exists() and any(
        event.booking_is_open(current_time)
        for event in course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).order_by("starts_at", "created_at")
    )


def _preview_practice_validation_sidebar_cta(request: HttpRequest, course: Course) -> dict | None:
    now = timezone.now()
    events = list(course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).order_by("starts_at", "created_at"))
    booked_event_id = _get_preview_validation_booking_event_id(request, course.pk)
    booked_event = next((event for event in events if event.pk == booked_event_id), None)

    if booked_event is not None and now >= booked_event.session_end_at:
        _set_preview_validation_booking_event_id(request, course.pk, None)
        booked_event = None

    if booked_event is not None:
        if booked_event.starts_at <= now < booked_event.session_end_at:
            return {
                "label": "Validate",
                "url": reverse("standalone:preview_student_validate", args=[course.pk]),
                "detail": f"Session live now • {booked_event.location}",
            }
        return {
            "label": "Practice Validation",
            "url": _preview_practice_validation_url(course.pk, restart=True),
            "detail": f"Booked for {booked_event.starts_at:%d %b %Y, %H:%M}",
        }

    booking_sessions = _serialize_preview_booking_sessions(course, now=now)
    if booking_sessions:
        detail = (
            booking_sessions[0]["datetime"]
            if len(booking_sessions) == 1
            else f"{len(booking_sessions)} sessions available"
        )
        return {
            "label": "Book Validation",
            "url": reverse("standalone:preview_student_validate", args=[course.pk]),
            "detail": detail,
        }
    return {
        "label": "Book Validation",
        "url": "",
        "detail": "No validation sessions available right now",
        "disabled": True,
    }


def _student_practice_validation_sidebar_cta(enrollment: Enrollment) -> dict | None:
    state = _student_validate_event_state(enrollment)
    if state["state"] == "live":
        return {
            "label": "Validate",
            "url": reverse("standalone:student_validate", args=[enrollment.course_id]),
            "detail": f"Session live now • {state['event'].location}",
        }
    if state["state"] == "booked_future":
        event = state["event"]
        return {
            "label": "Practice Validation",
            "url": _practice_validation_url(enrollment.course_id, restart=True),
            "detail": f"Booked for {event.starts_at:%d %b %Y, %H:%M}",
        }
    if state["state"] == "bookable":
        event = state["event"]
        return {
            "label": "Book Validation",
            "url": reverse("standalone:student_validate", args=[enrollment.course_id]),
            "detail": f"{event.starts_at:%d %b %Y, %H:%M} to {event.session_end_at:%H:%M}",
        }
    return {
        "label": "Book Validation",
        "url": "",
        "detail": "No validation sessions available right now",
        "disabled": True,
    }


def _preview_validation_booking_session_key(course_id: int) -> str:
    return f"preview_validation_booking:{course_id}"


def _get_preview_validation_booking_event_id(request: HttpRequest, course_id: int) -> int | None:
    try:
        return int(request.session.get(_preview_validation_booking_session_key(course_id)) or 0) or None
    except (TypeError, ValueError):
        return None


def _set_preview_validation_booking_event_id(request: HttpRequest, course_id: int, event_id: int | None) -> None:
    session_key = _preview_validation_booking_session_key(course_id)
    if event_id:
        request.session[session_key] = int(event_id)
    else:
        request.session.pop(session_key, None)
    request.session.modified = True


def _serialize_preview_booking_sessions(course: Course, *, now=None) -> list[dict]:
    current_time = now or timezone.now()
    base_url = reverse("standalone:preview_student_validate", args=[course.pk])
    sessions = []
    for event in course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).order_by("starts_at", "created_at"):
        if not event.booking_is_open(current_time):
            continue
        sessions.append(
            {
                "id": event.pk,
                "title": f"{event.starts_at:%a %d %b}",
                "datetime": f"{event.starts_at:%d %b %Y, %H:%M} to {event.session_end_at:%H:%M}",
                "location": event.location,
                "spaces_left": event.spaces_left,
                "recent_booking_count": event.recent_booking_count(hours=24),
                "question_count": event.question_count,
                "url": f"{base_url}?book_event={event.pk}",
            }
        )
    return sessions


def _course_config(course: Course) -> CourseConfig:
    try:
        return course.config
    except CourseConfig.DoesNotExist:
        config, _created = CourseConfig.objects.get_or_create(course=course)
        return config


def _weighted_practice_average(course: Course, metrics: dict) -> float:
    config = _course_config(course)
    total_weight = (
        config.mastery_weight
        + config.coverage_weight
        + config.engagement_weight
        + config.target_weight
    )
    if total_weight <= 0:
        return 0.0
    weighted_total = (
        metrics["mastery"] * config.mastery_weight
        + metrics["coverage"] * config.coverage_weight
        + metrics["engagement"] * config.engagement_weight
        + metrics["target"] * config.target_weight
    )
    return round(weighted_total / total_weight, 2)


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
        config_form = BlockConfigForm(instance=config)
        if field_name not in config_form.fields:
            raise Http404
        override_value = request.POST.get(field_name, "")
        form = BlockConfigForm(_block_config_form_payload(config, {field_name: override_value}), instance=config)
        if not form.is_valid():
            return JsonResponse({"ok": False, "errors": form.errors.get(field_name, form.non_field_errors())}, status=400)

        updated_config = form.save()
        value = getattr(updated_config, field_name)
        return JsonResponse(
            {
                "ok": True,
                "value": value,
                "raw_value": value,
                "message": "Block settings updated.",
            }
        )

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


def _course_config_form_payload(config: CourseConfig, field_overrides: dict[str, object] | None = None) -> dict[str, object]:
    field_overrides = field_overrides or {}
    form = CourseConfigForm(instance=config)
    payload: dict[str, object] = {}
    for name, field in form.fields.items():
        if name in field_overrides:
            value = field_overrides[name]
        else:
            value = getattr(config, name)
        if isinstance(field, forms.BooleanField):
            payload[name] = "on" if value else ""
        else:
            payload[name] = "" if value is None else value
    return payload


def _block_config_form_payload(config: BlockConfig, field_overrides: dict[str, object] | None = None) -> dict[str, object]:
    field_overrides = field_overrides or {}
    form = BlockConfigForm(instance=config)
    payload: dict[str, object] = {}
    for name in form.fields:
        if name in field_overrides:
            value = field_overrides[name]
        else:
            value = getattr(config, name)
        payload[name] = "" if value is None else value
    return payload


def _course_detail_anchor_url(course: Course, anchor: str = "") -> str:
    url = reverse("standalone:course_detail", args=[course.pk])
    return f"{url}#{anchor}" if anchor else url


@login_required
def block_project_create(request: HttpRequest, block_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    block = _teacher_block_or_404(request.user, block_id)
    form = BlockProjectCreateForm(request.POST)
    if not form.is_valid():
        context = _course_detail_context(block.course, request=request)
        for context_block in context["blocks"]:
            if context_block.pk == block.pk:
                context_block.project_create_form = form
        return render(request, "standalone/course_detail.html", context, status=400)

    project = BlockProject.objects.create(
        block=block,
        title="Draft project",
        teacher_prompt=form.cleaned_data["teacher_prompt"],
        example_text=form.cleaned_data.get("example_text", ""),
        generation_status=BlockProject.GenerationStatus.IDLE,
    )
    try:
        generate_block_project_draft(project)
        messages.success(request, f"Created project draft for {block.title}.")
    except (ProjectAuthoringError, ProjectSpecError) as exc:
        project.generation_status = BlockProject.GenerationStatus.FAILED
        project.generation_error = str(exc)
        project.save(update_fields=["generation_status", "generation_error", "updated_at"])
        messages.error(request, str(exc))
    return redirect(_course_detail_anchor_url(block.course, f"project-{project.pk}"))


@login_required
def block_project_update(request: HttpRequest, project_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    project = _teacher_project_or_404(request.user, project_id)
    if project.is_locked:
        messages.error(request, "This published project already has student assignments, so it is now immutable.")
        return redirect(_course_detail_anchor_url(project.block.course, f"project-{project.pk}"))

    form = BlockProjectEditForm(request.POST, instance=project, prefix=f"project-{project.pk}")
    if not form.is_valid():
        context = _course_detail_context(project.block.course, request=request)
        for context_block in context["blocks"]:
            for context_project in getattr(context_block, "projects_for_course_detail", []):
                if context_project.pk == project.pk:
                    context_project.edit_form = form
        return render(request, "standalone/course_detail.html", context, status=400)

    updated_project = form.save()
    try:
        validate_block_project_spec(updated_project)
    except ProjectSpecError as exc:
        updated_project.generation_error = str(exc)
        updated_project.generation_status = BlockProject.GenerationStatus.FAILED
        updated_project.save(update_fields=["generation_error", "generation_status", "updated_at"])
        messages.error(request, str(exc))
        return redirect(_course_detail_anchor_url(project.block.course, f"project-{project.pk}"))

    updated_project.generation_status = BlockProject.GenerationStatus.READY
    updated_project.generation_error = ""
    updated_project.save(update_fields=["generation_status", "generation_error", "updated_at"])
    messages.success(request, "Project draft updated.")
    return redirect(_course_detail_anchor_url(project.block.course, f"project-{project.pk}"))


@login_required
def block_project_publish(request: HttpRequest, project_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    project = _teacher_project_or_404(request.user, project_id)
    try:
        publish_block_project(project)
    except (ProjectSpecError, ProjectImmutableError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Published project: {project.title}.")
    return redirect(_course_detail_anchor_url(project.block.course, f"project-{project.pk}"))


@login_required
def block_project_archive_view(request: HttpRequest, project_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    project = _teacher_project_or_404(request.user, project_id)
    archive_block_project(project)
    messages.success(request, f"Archived project: {project.title}.")
    return redirect(_course_detail_anchor_url(project.block.course, f"project-{project.pk}"))


@login_required
def block_project_results_export(request: HttpRequest, block_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    block = _teacher_block_or_404(request.user, block_id)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="block-{block.pk}-project-results.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "course",
            "block",
            "project",
            "student",
            "seed",
            "status",
            "completed_at",
            "submission_count",
            "latest_submitted_answer",
            "expected_display_answer",
        ]
    )
    for row in build_project_results_rows(block):
        writer.writerow(
            [
                row["course"],
                row["block"],
                row["project"],
                row["student"],
                row["seed"],
                row["status"],
                row["completed_at"],
                row["submission_count"],
                row["latest_submitted_answer"],
                row["expected_display_answer"],
            ]
        )
    return response


@login_required
def block_project_preview_artifact_download(request: HttpRequest, project_id: int, artifact_key: str) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    project = _teacher_project_or_404(request.user, project_id)
    open_preview_project(request, project, viewer_identifier=f"teacher:{request.user.pk}")

    from standalone.services.projects import materialize_project_instance  # local import keeps startup surface small

    project_state = request.session.get("standalone_project_preview", {}).get(str(project.block.course_id), {}).get(str(project.pk), {})
    materialized = materialize_project_instance(project, project_state.get("seed"))
    artifact = next((item for item in materialized["artifacts"] if item["key"] == artifact_key), None)
    if artifact is None:
        raise Http404
    response = HttpResponse(
        artifact["content"],
        content_type=artifact.get("metadata", {}).get("content_type", "application/octet-stream"),
    )
    response["Content-Disposition"] = f'attachment; filename="{Path(artifact["filename"]).name}"'
    return response


@login_required
def project_artifact_download(request: HttpRequest, artifact_id: int) -> HttpResponse:
    artifact = get_object_or_404(
        ProjectArtifact.objects.select_related("assignment__enrollment__student", "assignment__block_project__block__course"),
        pk=artifact_id,
    )
    if _is_teacher(request.user):
        _teacher_course_or_404(request.user, artifact.assignment.block_project.block.course_id)
    elif _is_student(request.user):
        if artifact.assignment.enrollment.student_id != request.user.pk:
            raise Http404
    else:
        raise Http404
    return FileResponse(artifact.file.open("rb"), as_attachment=True, filename=Path(artifact.file.name).name)


@login_required
def update_course_config_field(request: HttpRequest, course_id: int, field_name: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    config, _ = CourseConfig.objects.get_or_create(course=course)
    config_form = CourseConfigForm(instance=config)
    if field_name not in config_form.fields:
        raise Http404

    field = config_form.fields[field_name]
    override_value: object
    if isinstance(field, forms.BooleanField):
        override_value = request.POST.get(field_name) in {"1", "true", "True", "on", "yes"}
    else:
        override_value = request.POST.get(field_name, "")

    form = CourseConfigForm(_course_config_form_payload(config, {field_name: override_value}), instance=config)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get(field_name, form.non_field_errors())}, status=400)

    updated_config = form.save()
    value = getattr(updated_config, field_name)
    return JsonResponse(
        {
            "ok": True,
            "value": value,
            "raw_value": value,
            "checked": bool(value) if isinstance(field, forms.BooleanField) else None,
            "message": "Settings updated.",
        }
    )


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
            "action_url_template": reverse("standalone:student_preview_action", args=[course.pk, 0, "ACTION"]),
            "is_student_practice": False,
            "practice_validation_url": _preview_practice_validation_url(course.pk, restart=True),
            "validation_entry_url": reverse("standalone:preview_student_validate", args=[course.pk]),
            "validation_sidebar_cta": _preview_practice_validation_sidebar_cta(request, course),
        },
    )


def _preview_payload(request: HttpRequest, course: Course, block: CourseBlock, action: str) -> JsonResponse:
    if action == "quiz":
        requested_question_type = None
        preferred_objective_id = None
        force_new = False
        if request.body and "application/json" in (request.content_type or ""):
            try:
                data = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
            requested_question_type = str(data.get("question_type", "")).strip().lower() or None
            preferred_objective_id = int(data.get("learning_objective_id") or 0) or None
            force_new = bool(data.get("force_new"))
        try:
            payload = request_preview_quiz(
                request,
                course,
                block,
                requested_question_type=requested_question_type,
                preferred_objective_id=preferred_objective_id,
                force_new=force_new,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "preview": payload})

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)

    if action in {"project_open", "project_chat", "project_submit"}:
        project_id = int(data.get("project_id") or 0)
        project = block.projects.filter(pk=project_id, status=BlockProject.Status.PUBLISHED).first()
        if project is None:
            return JsonResponse({"ok": False, "error": "Choose a published project for this block."}, status=404)
        viewer_identifier = f"teacher:{request.user.pk}"
        if action == "project_open":
            open_preview_project(request, project, viewer_identifier=viewer_identifier)
        elif action == "project_chat":
            message_text = str(data.get("message", "")).strip()
            send_preview_project_message(request, project, viewer_identifier=viewer_identifier, text=message_text)
        else:
            raw_answer = str(data.get("answer", "")).strip()
            submit_preview_project_answer(request, project, viewer_identifier=viewer_identifier, raw_answer=raw_answer)
        payload = serialize_preview_state(request, course, active_block_id=block.pk)
        return JsonResponse({"ok": True, "preview": payload})

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
        try:
            payload = flag_preview_question(
                request,
                course,
                block,
                question_id,
                instruction=str(data.get("instruction", "")).strip(),
                learning_objective_id=int(data.get("learning_objective_id") or 0) or None,
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "preview": payload})
    if action == "guardrail":
        try:
            payload = save_preview_objective_guardrail(
                request,
                course,
                block,
                int(data.get("learning_objective_id") or 0),
                str(data.get("instruction", "")).strip(),
            )
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=400)
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

    field_name = "assistant_guidance" if "assistant_guidance" in request.POST else "text"
    form_class = {
        "text": LearningObjectiveInlineForm,
        "assistant_guidance": LearningObjectiveGuidanceInlineForm,
    }.get(field_name)
    if form_class is None:
        raise Http404

    form = form_class(request.POST, instance=objective)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get(field_name, form.non_field_errors())}, status=400)

    updated_objective = form.save()
    value = getattr(updated_objective, field_name)
    display_value = value or ("No assistant guidance yet." if field_name == "assistant_guidance" else "")
    return JsonResponse({"ok": True, "value": value, "display_value": display_value, "raw_value": value})


@login_required
def delete_learning_objective_correction(request: HttpRequest, correction_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    correction = get_object_or_404(
        LearningObjectiveCorrection.objects.select_related("learning_objective__course"),
        pk=correction_id,
    )
    course = _teacher_course_or_404(request.user, correction.learning_objective.course_id)
    correction.delete()
    messages.success(request, "Correction note deleted.")
    return redirect("standalone:course_detail", course.pk)


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
def move_block_view(request: HttpRequest, block_id: int, direction: str) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    block = _teacher_block_or_404(request.user, block_id)
    moved = move_course_block(block, direction)
    if moved:
        messages.success(request, "Block order updated.")
    else:
        messages.info(request, "Block could not be moved further.")
    return redirect(f"{reverse('standalone:course_detail', args=[block.course_id])}#course-blocks-heading")


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
    config, _ = CourseConfig.objects.get_or_create(course=course)
    form = CourseConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Course configuration updated.")
        return redirect(f"{reverse('standalone:course_detail', args=[course.pk])}#course-settings-content")
    if request.method == "GET":
        return redirect(f"{reverse('standalone:course_detail', args=[course.pk])}#course-settings-content")
    return render(request, "standalone/form_page.html", {"title": f"Configure {course.title}", "form": form})


@login_required
def add_allowed_email(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    form = CourseAllowedEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = _normalise_access_email(form.cleaned_data["email"])
        if CourseAllowedEmail.objects.filter(course=course, email__iexact=email).exists():
            messages.info(request, "That student email is already on the self-enrol allowlist.")
        else:
            CourseAllowedEmail.objects.create(course=course, email=email)
            messages.success(request, "Allowed student email added.")
        return redirect("standalone:course_detail", course.pk)
    return render(
        request,
        "standalone/form_page.html",
        {"title": f"Add self-enrol allowlist email for {course.title}", "form": form, "submit_label": "Add email"},
    )


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
        magic_url = request.build_absolute_uri(reverse("standalone:magic_enrol", args=[magic_link.token]))
        messages.success(
            request,
            f"Enrolment magic link created. It expires on {magic_link.expires_at:%d %b %Y, %H:%M} and can enrol {magic_link.max_uses} new student(s): {magic_url}",
        )
        return redirect("standalone:course_detail", course.pk)
    return render(
        request,
        "standalone/form_page.html",
        {"title": f"Create enrolment magic link for {course.title}", "form": form, "submit_label": "Create magic link"},
    )


@login_required
def demo_link_regenerate(request: HttpRequest, course_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    access = ensure_demo_access(course)
    rotate_demo_access_token(access)
    messages.success(request, "Demo link regenerated.")
    return redirect(f"{reverse('standalone:course_detail', args=[course.pk])}#course-settings-content")


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
        email = _normalise_access_email(form.cleaned_data["email"])
        domain = _email_domain(email)
        if course.config.self_enrol_domain and domain != course.config.self_enrol_domain.lower():
            form.add_error("email", "This email domain is not allowed for self-enrolment.")
        elif not CourseAllowedEmail.objects.filter(course=course, email__iexact=email).exists():
            form.add_error("email", "This email is not on the self-enrolment list for this course.")
        else:
            user = _student_user_for_access_form(form, email)
            if user is not None:
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
    return render(request, "standalone/form_page.html", {"title": f"Self-enrol in {course.title}", "form": form, "submit_label": "Join course"})


@transaction.atomic
def magic_enrol(request: HttpRequest, token) -> HttpResponse:
    magic_link = get_object_or_404(CourseMagicLink.objects.select_related("course", "course__config"), token=token, is_active=True)
    if magic_link.is_expired or magic_link.use_count >= magic_link.max_uses:
        messages.error(request, "This magic link is no longer available.")
        return redirect("standalone:login")

    form = MagicLinkEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = _normalise_access_email(form.cleaned_data["email"])
        domain = _email_domain(email)
        if magic_link.course.config.self_enrol_domain and domain != magic_link.course.config.self_enrol_domain.lower():
            form.add_error("email", "This email domain is not allowed for this course.")
        else:
            user = _student_user_for_access_form(form, email)
            if user is not None:
                _enrollment, created = Enrollment.objects.get_or_create(course=magic_link.course, student=user, defaults={"source": "magic_link"})
                if created:
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
    return render(request, "standalone/form_page.html", {"title": f"Join {magic_link.course.title}", "form": form, "submit_label": "Join course"})


def _demo_embed_mode(request: HttpRequest) -> bool:
    return request.GET.get("embed") == "1"


def _normalized_demo_visitor_key(value: str | None) -> str:
    candidate = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", candidate):
        return candidate
    return ""


def _demo_validation_visitor_key(request: HttpRequest) -> str:
    return (
        _normalized_demo_visitor_key(request.GET.get("visitor"))
        or _normalized_demo_visitor_key(request.headers.get("X-Demo-Visitor-Key"))
        or _normalized_demo_visitor_key(request.POST.get("visitor"))
    )


def _blocked_demo_embed_response_if_needed(request: HttpRequest, access: CourseDemoAccess) -> HttpResponse | None:
    if not _demo_embed_mode(request):
        return None
    allowed_origins = demo_iframe_origin_list(access.course.config.demo_iframe_allowed_origins)
    if not allowed_origins:
        return _demo_embed_blocked_response(
            request,
            access.course,
            reason="This demo is not currently enabled for iframe embedding.",
        )
    request_origin = _demo_embed_origin_from_request(request)
    if request_origin and not demo_iframe_origin_allowed(access.course.config.demo_iframe_allowed_origins, request_origin):
        return _demo_embed_blocked_response(
            request,
            access.course,
            reason="This embedding origin is not allowed for this demo.",
        )
    return None


def _demo_preview_payload(access: CourseDemoAccess, block: CourseBlock, action: str, request: HttpRequest) -> JsonResponse:
    if action == "quiz":
        requested_question_type = None
        preferred_objective_id = None
        force_new = False
        if request.body and "application/json" in (request.content_type or ""):
            try:
                data = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
            requested_question_type = str(data.get("question_type", "")).strip().lower() or None
            preferred_objective_id = int(data.get("learning_objective_id") or 0) or None
            force_new = bool(data.get("force_new"))
        try:
            payload = request_demo_preview_quiz(
                access,
                block,
                requested_question_type=requested_question_type,
                preferred_objective_id=preferred_objective_id,
                force_new=force_new,
            )
        except ValueError as error:
            return JsonResponse({"ok": False, "error": str(error)}, status=400)
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
        payload = submit_demo_preview_answer(access, block, question_id, selected_answers, answer_text=answer_text)
        return JsonResponse({"ok": True, "preview": payload})
    if action == "draft_answer":
        question_id = int(data.get("question_id") or 0)
        answer_text = str(data.get("answer_text", "")).strip()
        payload = draft_demo_preview_written_answer(access, block, question_id, answer_text)
        return JsonResponse({"ok": True, "alignment": payload})
    if action == "chat":
        question = str(data.get("question", "")).strip()
        if not question:
            return JsonResponse({"ok": False, "error": "Please enter a course question first."}, status=400)
        payload = send_demo_preview_chat_message(access, block, question)
        return JsonResponse({"ok": True, "preview": payload})
    raise Http404


@xframe_options_exempt
def demo_practice(request: HttpRequest, token) -> HttpResponse:
    access = _demo_access_or_404(token)
    blocked = _blocked_demo_embed_response_if_needed(request, access)
    if blocked is not None:
        return blocked
    preview_state = serialize_demo_preview_state(access)
    embed_mode = _demo_embed_mode(request)
    response = render(
        request,
        "standalone/student_preview.html",
        {
            "course": access.course,
            "preview_state": preview_state,
            "action_url_template": reverse("standalone:demo_practice_action", args=[access.token, 0, "ACTION"]),
            "is_student_practice": True,
            "practice_validation_url": _demo_validation_practice_url(access, embed=embed_mode),
            "validation_entry_url": "",
            "validation_sidebar_cta": None,
            "is_demo_mode": True,
            "is_embed_mode": embed_mode,
            "demo_mode_label": "Demo mode",
            "demo_mode_copy": "Practice activity is shared across everyone using this demo link.",
            "demo_home_url": _demo_practice_url(access, embed=embed_mode),
            "hide_flag_actions": True,
        },
    )
    return _apply_demo_response_headers(request, response, access.course, embed_mode=embed_mode)


@csrf_exempt
def demo_practice_action(request: HttpRequest, token, block_id: int, action: str) -> JsonResponse:
    if request.method != "POST":
        raise Http404
    access = _demo_access_or_404(token)
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id, course=access.course)
    return _demo_preview_payload(access, block, action, request)


@xframe_options_exempt
def demo_validation_practice(request: HttpRequest, token) -> HttpResponse:
    access = _demo_access_or_404(token)
    blocked = _blocked_demo_embed_response_if_needed(request, access)
    if blocked is not None:
        return blocked
    embed_mode = _demo_embed_mode(request)
    visitor_key = _demo_validation_visitor_key(request)
    if not visitor_key:
        visitor_key = new_demo_visitor_key()
        return redirect(
            _demo_validation_practice_url(
                access,
                embed=embed_mode,
                visitor_key=visitor_key,
                restart=request.GET.get("restart") == "1",
            )
        )
    validation_session = get_or_create_demo_validation_session(access, visitor_key)
    session_state = _demo_validation_state_with_practice_return(
        serialize_demo_validation_practice_state(
            access,
            validation_session,
            restart=request.GET.get("restart") == "1",
        ),
        access,
        embed=embed_mode,
    )
    sidebar_preview_state = serialize_demo_preview_state(access)
    response = render(
        request,
        "standalone/student_validate.html",
        {
            "course": access.course,
            "session_state": session_state,
            "sidebar_state": {},
            "sidebar_preview_state": sidebar_preview_state,
            "practice_validation_url": _demo_validation_practice_url(access, embed=embed_mode, visitor_key=visitor_key),
            "validation_sidebar_cta": None,
            "practice_url": _demo_practice_url(access, embed=embed_mode),
            "session_action_url": (
                f"{reverse('standalone:demo_validation_practice_action', args=[access.token, 'ACTION'])}"
                f"{_demo_query_string(embed=embed_mode, visitor_key=visitor_key)}"
            ),
            "exit_url": _demo_practice_url(access, embed=embed_mode),
            "exit_label": "Back to practice",
            "is_demo_mode": True,
            "is_embed_mode": embed_mode,
            "demo_mode_label": "Demo mode",
            "demo_mode_copy": "Practice validation stays private to this browser. Shared demo averages still come from the public demo practice space.",
            "demo_visitor_key": visitor_key,
            "demo_home_url": _demo_practice_url(access, embed=embed_mode),
            "show_validation_sidebar_cta": False,
        },
    )
    return _apply_demo_response_headers(request, response, access.course, embed_mode=embed_mode)


@csrf_exempt
def demo_validation_practice_action(request: HttpRequest, token, action: str) -> JsonResponse:
    if request.method != "POST":
        raise Http404
    access = _demo_access_or_404(token)
    visitor_key = _demo_validation_visitor_key(request)
    if not visitor_key:
        return JsonResponse({"ok": False, "error": "Missing demo visitor key."}, status=400)
    validation_session = get_or_create_demo_validation_session(access, visitor_key)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
    try:
        if action == "next":
            payload = reveal_demo_validation_practice_next(access, validation_session)
            payload = _demo_validation_state_with_practice_return(
                payload,
                access,
                embed=_demo_embed_mode(request),
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "draft_answer":
            payload = draft_demo_validation_practice_answer(
                access,
                validation_session,
                int(data.get("question_id") or 0),
                str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "alignment": payload})
        if action == "submit":
            payload = submit_demo_validation_practice_answer(
                access,
                validation_session,
                int(data.get("question_id") or 0),
                data.get("answers") or ([str(data.get("answer", "")).strip()] if data.get("answer") else []),
                answer_text=str(data.get("answer_text", "")).strip(),
            )
            payload = _demo_validation_state_with_practice_return(
                payload,
                access,
                embed=_demo_embed_mode(request),
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "skip":
            payload = skip_demo_validation_practice_question(
                access,
                validation_session,
                int(data.get("question_id") or 0),
            )
            payload = _demo_validation_state_with_practice_return(
                payload,
                access,
                embed=_demo_embed_mode(request),
            )
            return JsonResponse({"ok": True, "session": payload})
    except ValidationFlowError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    raise Http404


@login_required
def course_import_upload(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    form = CourseImportUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        course_import = form.save(course=course, uploaded_by=request.user)
        _queue_course_import_analysis(course_import.pk)
        messages.success(request, "PDF uploaded. Chapter detection is running in the background.")
        return redirect("standalone:course_import_review", course_import.pk)
    return render(
        request,
        "standalone/form_page.html",
        {
            "title": f"Import PDF textbook into {course.title}",
            "form": form,
            "has_upload_picker": True,
            "submit_label": "Analyze PDF",
            "submit_progress_label": "Uploading PDF...",
            "cancel_url": reverse("standalone:course_detail", args=[course.pk]),
        },
    )


@login_required
def course_import_review(request: HttpRequest, import_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course_import = get_object_or_404(CourseImport.objects.select_related("course", "uploaded_by"), pk=import_id)
    course = _teacher_course_or_404(request.user, course_import.course_id)
    chapters = list(course_import.chapters.order_by("order", "pk"))
    initial = {
        "selected_chapters": [
            str(chapter.pk)
            for chapter in chapters
            if chapter.selected and chapter.created_block_id is None
        ]
    }
    form = CourseImportChapterSelectionForm(request.POST or None, chapters=chapters, initial=initial)

    if request.method == "POST":
        if course_import.status != CourseImport.Status.READY:
            messages.error(request, "This import is not ready for chapter selection yet.")
            return redirect("standalone:course_import_review", course_import.pk)
        if form.is_valid():
            selected_chapter_ids = form.cleaned_data["selected_chapters"]
            _queue_course_import_block_creation(course_import.pk, selected_chapter_ids)
            messages.success(request, "Selected chapters are being converted into course blocks.")
            return redirect("standalone:course_import_review", course_import.pk)

    return render(
        request,
        "standalone/course_import_review.html",
        {
            "course": course,
            "course_import": course_import,
            "chapters": chapters,
            "form": form,
        },
    )


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
    initial = {
        "starts_at": timezone.now() + timedelta(days=1),
        "ends_at": timezone.now() + timedelta(days=1, hours=3),
        "late_booking_cutoff_minutes": 20,
        "feedback_release_mode": (
            ValidationEvent.FeedbackReleaseMode.IMMEDIATE
            if course.config.show_validation_feedback_immediately
            else ValidationEvent.FeedbackReleaseMode.MANUAL
        ),
    }
    form = ValidationEventForm(request.POST or None, initial=initial, course=course)
    if request.method == "POST" and form.is_valid():
        event = form.save(commit=False)
        event.course = course
        event.created_by = request.user
        event.mode = ValidationEvent.Mode.DIGITAL_INVIGILATION
        event.save()
        if event.mode == ValidationEvent.Mode.DIGITAL_INVIGILATION:
            ensure_room_code_secret(event)
        messages.success(request, "Validation session created.")
        return redirect("standalone:course_detail", course.pk)
    return render(request, "standalone/form_page.html", {"title": f"Create validation session for {course.title}", "form": form})


@login_required
def validation_event_delete(request: HttpRequest, event_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    course = _teacher_course_or_404(request.user, event.course_id)
    if event.has_student_submissions:
        messages.error(request, "This validation session cannot be deleted because a student has already submitted validation.")
        return redirect("standalone:course_detail", course.pk)
    event.delete()
    messages.success(request, "Deleted validation session.")
    return redirect("standalone:course_detail", course.pk)


def _event_start_is_available(event: ValidationEvent, now=None) -> bool:
    current_time = now or timezone.now()
    return event.starts_at <= current_time < event.session_end_at


@login_required
def validation_book(request: HttpRequest, event_id: int) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    enrollment = _student_enrollment_or_404(request.user, event.course_id)
    if event.mode != ValidationEvent.Mode.DIGITAL_INVIGILATION:
        messages.error(request, "Only digital invigilated validations can be booked.")
        return redirect("standalone:student_dashboard")
    if not event.requires_booking:
        messages.error(request, "This validation does not require booking.")
        return redirect("standalone:student_dashboard")
    if not event.booking_is_open():
        deadline = event.booking_deadline
        if deadline and timezone.now() >= deadline:
            messages.error(request, "Booking has closed for this validation session.")
        elif not event.has_space:
            messages.error(request, "This validation session is already full.")
        else:
            messages.error(request, "This validation is not currently open for booking.")
        return redirect("standalone:student_dashboard")
    deadline = event.booking_deadline
    if deadline and timezone.now() >= deadline:
        messages.error(request, "Booking has closed for this validation session.")
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
        body=f"You are booked for a validation session on {event.starts_at:%d %b %Y %H:%M} at {event.location}.",
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
    deadline = booking.event.booking_deadline
    if deadline and timezone.now() >= deadline:
        messages.error(request, "This booking can no longer be cancelled.")
        return redirect("standalone:student_dashboard")
    booking.status = ValidationBooking.Status.CANCELLED
    booking.cancelled_at = timezone.now()
    booking.save(update_fields=["status", "cancelled_at", "updated_at"])
    send_logged_email(
        recipient=request.user.email,
        subject=f"Validation cancelled for {booking.event.course.title}",
        body="Your validation session booking has been cancelled.",
        event_type="validation_cancellation",
        related_object=str(booking.pk),
    )
    messages.success(request, "Validation booking cancelled.")
    return redirect("standalone:student_dashboard")


def _practice_validation_url(course_id: int, *, restart: bool = False) -> str:
    url = reverse("standalone:validation_practice_session", args=[course_id])
    return f"{url}?restart=1" if restart else url


def _preview_practice_validation_url(course_id: int, *, restart: bool = False) -> str:
    url = reverse("standalone:preview_validation_practice", args=[course_id])
    return f"{url}?restart=1" if restart else url


@login_required
def validation_practice_session(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    enrollment = _student_enrollment_or_404(request.user, course_id)
    review_attempt_id = int(request.GET.get("review") or 0)
    try:
        if review_attempt_id > 0:
            attempt = get_object_or_404(
                PracticeAttempt.objects.select_related("enrollment", "enrollment__course"),
                pk=review_attempt_id,
                enrollment=enrollment,
                attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
                completed_at__isnull=False,
            )
        elif request.GET.get("restart") == "1":
            attempt = restart_validation_practice_attempt(enrollment)
        else:
            attempt = get_or_create_validation_practice_attempt(enrollment)
    except ValidationFlowError as error:
        messages.error(request, str(error))
        return redirect("standalone:student_dashboard")
    session_state = serialize_validation_practice_session(attempt, request=request)
    session_state["eyebrow"] = "Practice validation"
    sidebar_state = _student_validate_sidebar_state(
        enrollment=enrollment,
        title="Practice validation",
        copy="This validation rehearsal is untimed. Work through the locked set in order. Immediate feedback is hidden.",
        primary_action={"label": "Start again", "url": _practice_validation_url(course_id, restart=True), "style": "button"},
        secondary_action={"label": "Back to validate", "url": reverse("standalone:student_validate", args=[course_id]), "style": "secondary"},
        active_history_id=review_attempt_id if review_attempt_id > 0 else (attempt.pk if attempt.completed_at else None),
    )
    return render(
        request,
        "standalone/student_validate.html",
        {
            "course": enrollment.course,
            "session_state": session_state,
            "sidebar_state": sidebar_state,
            "practice_url": reverse("standalone:practice_quiz", args=[course_id]),
            "session_action_url": reverse("standalone:validation_practice_action", args=[course_id, attempt.pk, "ACTION"]),
            "exit_url": reverse("standalone:practice_quiz", args=[course_id]),
            "exit_label": "Back to practice",
        },
    )


@login_required
def preview_validation_practice(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    review_history_id = int(request.GET.get("review") or 0)
    try:
        if review_history_id > 0:
            session_state = preview_validation_history_session(request, course, review_history_id)
        else:
            if request.GET.get("restart") == "1":
                reset_preview_validation_state(request, course)
            session_state = serialize_preview_validation_state(request, course)
    except ValidationFlowError as error:
        messages.error(request, str(error))
        return redirect("standalone:student_preview", course.pk)
    session_state["eyebrow"] = "Practice validation"
    sidebar_state = _teacher_validate_sidebar_state(
        request,
        course,
        title="Practice validation",
        copy="This validation rehearsal is untimed. Work through the locked set in order. Immediate feedback is hidden.",
        primary_action={"label": "Start again", "url": _preview_practice_validation_url(course.pk, restart=True), "style": "button"},
        secondary_action={"label": "Back to validate", "url": reverse("standalone:preview_student_validate", args=[course.pk]), "style": "secondary"},
        active_history_id=review_history_id,
    )
    return _render_teacher_preview_validate(
        request,
        course,
        session_state,
        sidebar_state,
        exit_url=reverse("standalone:student_preview", args=[course.pk]),
        exit_label="Back to practice",
        session_action_url=(
            reverse("standalone:preview_validation_practice_action", args=[course.pk, "ACTION"])
            if review_history_id <= 0
            else ""
        ),
    )


def _teacher_validate_course_metrics(request: HttpRequest, course: Course) -> dict:
    return dict(serialize_preview_state(request, course).get("course", {}).get("metrics", {}))


def _teacher_validation_sidebar_preview_state(request: HttpRequest, course: Course) -> dict:
    return serialize_preview_state(request, course)


def _teacher_validate_sidebar_state(
    request: HttpRequest,
    course: Course,
    *,
    title: str,
    copy: str,
    meta_rows: list[str] | None = None,
    primary_action: dict | None = None,
    secondary_action: dict | None = None,
    active_history_id: int | None = None,
    booking_sessions: list[dict] | None = None,
    compact_booking_only: bool = False,
    hide_validation_panel: bool = False,
) -> dict:
    history = []
    base_url = reverse("standalone:preview_validation_practice", args=[course.pk])
    for entry in preview_validation_history_items(request, course):
        completed_at = str(entry.get("completed_at") or "")
        history.append(
            {
                "id": entry["id"],
                "label": timezone.datetime.fromisoformat(completed_at).strftime("%d %b %Y, %H:%M")
                if completed_at
                else "Completed preview",
                "score": float(entry.get("score") or 0),
                "question_count": int(entry.get("question_count") or 0),
                "url": f"{base_url}?review={entry['id']}",
                "is_active": int(active_history_id or 0) == int(entry["id"]),
            }
        )
    return {
        "title": title,
        "copy": copy,
        "meta_rows": meta_rows or [],
        "primary_action": primary_action,
        "secondary_action": secondary_action,
        "booking_sessions": booking_sessions or [],
        "compact_booking_only": compact_booking_only,
        "hide_validation_panel": hide_validation_panel,
        "course_metrics": _teacher_validate_course_metrics(request, course),
        "practice_validation_history": history,
    }


def _render_teacher_preview_validate(
    request: HttpRequest,
    course: Course,
    session_state: dict,
    sidebar_state: dict,
    *,
    session_action_url: str = "",
    exit_url: str | None = None,
    exit_label: str | None = None,
):
    return render(
        request,
        "standalone/student_validate.html",
        {
            "course": course,
            "session_state": session_state,
            "sidebar_state": sidebar_state,
            "sidebar_preview_state": _teacher_validation_sidebar_preview_state(request, course),
            "practice_validation_url": _preview_practice_validation_url(course.pk, restart=True),
            "validation_sidebar_cta": _preview_practice_validation_sidebar_cta(request, course),
            "session_action_url": session_action_url,
            "exit_url": exit_url or reverse("standalone:student_preview", args=[course.pk]),
            "exit_label": exit_label or "Exit student view",
        },
    )


@login_required
def preview_student_validate(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    now = timezone.now()
    preview_events = list(
        course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).order_by("starts_at", "created_at")
    )
    book_event_id = request.GET.get("book_event")
    if book_event_id:
        selected_event = next((event for event in preview_events if str(event.pk) == str(book_event_id)), None)
        if selected_event is not None and selected_event.booking_is_open(now):
            _set_preview_validation_booking_event_id(request, course.pk, selected_event.pk)
        return redirect("standalone:preview_student_validate", course.pk)

    booked_event_id = _get_preview_validation_booking_event_id(request, course.pk)
    booked_event = next((event for event in preview_events if event.pk == booked_event_id), None)
    if booked_event is not None and now >= booked_event.session_end_at:
        _set_preview_validation_booking_event_id(request, course.pk, None)
        booked_event = None

    if booked_event is not None and booked_event.starts_at <= now < booked_event.session_end_at:
        try:
            session_state = serialize_preview_student_validate_state(request, course, booked_event)
        except ValidationFlowError as error:
            messages.error(request, str(error))
            return redirect("standalone:student_preview", course.pk)
        sidebar_state = _teacher_validate_sidebar_state(
            request,
            course,
            title="Validation session",
            copy="A validation session is live now. Complete your validation here on this device.",
            meta_rows=[
                f"{booked_event.starts_at:%d %b %Y, %H:%M} to {booked_event.session_end_at:%d %b %Y, %H:%M}",
                f"Location: {booked_event.location}",
            ],
        )
        return _render_teacher_preview_validate(
            request,
            course,
            session_state,
            sidebar_state,
            session_action_url=reverse("standalone:preview_student_validate_action", args=[course.pk, "ACTION"]),
        )

    if booked_event is not None and booked_event.starts_at > now:
        session_state = _empty_validate_session_state(
            course,
            title="Validate",
            transcript=[
                _validation_status_message(
                    "validate-booked-future",
                    (
                        f"Your validation is booked for {booked_event.starts_at:%d %b %Y, %H:%M}. "
                        "You're currently out of session. Would you like to start a practice validation until it begins?"
                    ),
                    actions=[{"label": "Start practice validation", "url": _preview_practice_validation_url(course.pk, restart=True), "style": "button"}],
                )
            ],
        )
        sidebar_state = _teacher_validate_sidebar_state(
            request,
            course,
            title="Validation booked",
            copy="Your validation session is booked and ready for you at the scheduled time.",
            meta_rows=[
                f"{booked_event.starts_at:%d %b %Y, %H:%M} to {booked_event.session_end_at:%d %b %Y, %H:%M}",
                f"Location: {booked_event.location}",
            ],
        )
        return _render_teacher_preview_validate(request, course, session_state, sidebar_state)

    booking_sessions = _serialize_preview_booking_sessions(course, now=now)
    if booking_sessions:
        session_state = _empty_validate_session_state(
            course,
            title="Validate",
            transcript=[
                _validation_status_message(
                    "validate-bookable",
                    "No validation session is active right now. Would you like to start a practice validation while you wait?",
                    actions=[{"label": "Start practice validation", "url": _preview_practice_validation_url(course.pk, restart=True), "style": "button"}],
                )
            ],
        )
        sidebar_state = _teacher_validate_sidebar_state(
            request,
            course,
            title="Book validation",
            copy=(
                "Choose a validation session from chat when you're ready."
                if len(booking_sessions) == 1
                else f"{len(booking_sessions)} validation sessions are currently open for booking."
            ),
            meta_rows=[f"{len(booking_sessions)} session{'s' if len(booking_sessions) != 1 else ''} available"],
            primary_action={"label": "Book validation", "style": "button", "kind": "booking_options"},
            booking_sessions=booking_sessions,
            compact_booking_only=True,
        )
        return _render_teacher_preview_validate(request, course, session_state, sidebar_state)

    session_state = _empty_validate_session_state(
        course,
        title="Validate",
        transcript=[
            _validation_status_message(
                "validate-out-of-session",
                "There is no live validation session for this course right now. Would you like to start a practice validation?",
                actions=[{"label": "Start practice validation", "url": _preview_practice_validation_url(course.pk, restart=True), "style": "button"}],
            )
        ],
    )
    sidebar_state = _teacher_validate_sidebar_state(
        request,
        course,
        title="Validation unavailable",
        copy="There is no bookable or live validation session for this course right now.",
        secondary_action={"label": "Exit student view", "url": reverse("standalone:student_preview", args=[course.pk]), "style": "secondary"},
        hide_validation_panel=True,
    )
    return _render_teacher_preview_validate(request, course, session_state, sidebar_state)


@login_required
def preview_student_validate_action(request: HttpRequest, course_id: int, action: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    now = timezone.now()
    booked_event_id = _get_preview_validation_booking_event_id(request, course.pk)
    event = next(
        (
            candidate
            for candidate in course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).order_by("starts_at", "created_at")
            if candidate.pk == booked_event_id and candidate.starts_at <= now < candidate.session_end_at
        ),
        None,
    )
    if event is None:
        return JsonResponse({"ok": False, "error": "There is no live validation session to preview right now."}, status=400)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
    try:
        if action == "confirm":
            payload = confirm_preview_student_validate(request, course, event)
            return JsonResponse({"ok": True, "session": payload})
        if action == "next":
            payload = reveal_preview_student_validate_next(request, course, event)
            return JsonResponse({"ok": True, "session": payload})
        if action == "draft_answer":
            payload = draft_preview_student_validate_answer(
                request,
                course,
                event,
                int(data.get("question_id") or 0),
                str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "alignment": payload})
        if action == "submit":
            payload = submit_preview_student_validate_response(
                request,
                course,
                event,
                question_id=int(data.get("question_id") or 0) or None,
                selected_answers=data.get("answers") or ([str(data.get("answer", "")).strip()] if data.get("answer") else []),
                answer_text=str(data.get("answer_text", "")).strip(),
                audit_prompt_id=data.get("audit_prompt_id"),
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "skip":
            payload = skip_preview_student_validate_question(
                request,
                course,
                event,
                question_id=int(data.get("question_id") or 0),
            )
            return JsonResponse({"ok": True, "session": payload})
    except ValidationFlowError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    raise Http404


@login_required
def preview_validation_practice_action(request: HttpRequest, course_id: int, action: str) -> JsonResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    course = _teacher_course_or_404(request.user, course_id)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
    try:
        if action == "next":
            payload = reveal_preview_validation_next(request, course)
            return JsonResponse({"ok": True, "session": payload})
        if action == "draft_answer":
            payload = draft_preview_validation_answer(
                request,
                course,
                int(data.get("question_id") or 0),
                str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "alignment": payload})
        if action == "submit":
            payload = submit_preview_validation_answer(
                request,
                course,
                int(data.get("question_id") or 0),
                data.get("answers") or ([str(data.get("answer", "")).strip()] if data.get("answer") else []),
                answer_text=str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "skip":
            payload = skip_preview_validation_question(
                request,
                course,
                int(data.get("question_id") or 0),
            )
            return JsonResponse({"ok": True, "session": payload})
    except ValidationFlowError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    raise Http404


@login_required
def validation_practice_action(request: HttpRequest, course_id: int, attempt_id: int, action: str) -> JsonResponse:
    if request.method != "POST" or not _is_student(request.user):
        raise Http404
    enrollment = _student_enrollment_or_404(request.user, course_id)
    attempt = get_object_or_404(
        PracticeAttempt.objects.select_related("enrollment", "enrollment__course"),
        pk=attempt_id,
        enrollment=enrollment,
        attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
    )
    try:
        if action == "next":
            payload = reveal_validation_practice_next(request, attempt)
            return JsonResponse({"ok": True, "session": payload})
        if action == "draft_answer":
            data = json.loads(request.body.decode("utf-8"))
            payload = draft_validation_practice_answer(
                request,
                attempt,
                int(data.get("question_id") or 0),
                str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "alignment": payload})
        if action == "submit":
            data = json.loads(request.body.decode("utf-8"))
            payload = submit_validation_practice_response(
                request,
                attempt,
                int(data.get("question_id") or 0),
                data.get("answers") or ([str(data.get("answer", "")).strip()] if data.get("answer") else []),
                answer_text=str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "skip":
            data = json.loads(request.body.decode("utf-8"))
            payload = skip_validation_practice_question(
                request,
                attempt,
                int(data.get("question_id") or 0),
            )
            return JsonResponse({"ok": True, "session": payload})
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
    except ValidationFlowError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    raise Http404


def _student_validate_course_metrics(enrollment: Enrollment) -> dict:
    return dict(serialize_student_practice_state(enrollment).get("course", {}).get("metrics", {}))


def _student_practice_validation_history(enrollment: Enrollment, *, active_attempt_id: int | None = None) -> list[dict]:
    attempts = (
        PracticeAttempt.objects.filter(
            enrollment=enrollment,
            attempt_type=PracticeAttempt.AttemptType.VALIDATION_PRACTICE,
            completed_at__isnull=False,
        )
        .annotate(question_count=Count("attempt_questions"))
        .order_by("-completed_at", "-started_at")[:8]
    )
    base_url = reverse("standalone:validation_practice_session", args=[enrollment.course_id])
    return [
        {
            "id": attempt.pk,
            "label": attempt.completed_at.strftime("%d %b %Y, %H:%M") if attempt.completed_at else "Completed attempt",
            "score": float(attempt.score or 0),
            "question_count": int(attempt.question_count or 0),
            "url": f"{base_url}?review={attempt.pk}",
            "is_active": int(active_attempt_id or 0) == attempt.pk,
        }
        for attempt in attempts
    ]


def _student_validate_sidebar_state(
    *,
    enrollment: Enrollment,
    title: str,
    copy: str,
    meta_rows: list[str] | None = None,
    primary_action: dict | None = None,
    secondary_action: dict | None = None,
    active_history_id: int | None = None,
) -> dict:
    return {
        "title": title,
        "copy": copy,
        "meta_rows": meta_rows or [],
        "primary_action": primary_action,
        "secondary_action": secondary_action,
        "course_metrics": _student_validate_course_metrics(enrollment),
        "practice_validation_history": _student_practice_validation_history(enrollment, active_attempt_id=active_history_id),
    }


def _empty_validate_session_state(course: Course, *, title: str, eyebrow: str = "Validate", transcript=None) -> dict:
    return {
        "mode": "student_validate",
        "attempt_id": None,
        "title": title,
        "course_title": course.title,
        "eyebrow": eyebrow,
        "transcript": list(transcript or []),
        "pending_question": None,
        "pending_audit": None,
        "completed": False,
        "review_visible": False,
        "score": 0.0,
        "feedback_release_mode": ValidationEvent.FeedbackReleaseMode.IMMEDIATE,
        "time_limit_minutes": 0,
        "expires_at": "",
        "time_remaining_seconds": 0,
        "timer_running": False,
        "show_timer": False,
        "progress": {
            "current_index": 0,
            "total_questions": 0,
            "answered_count": 0,
            "remaining_count": 0,
        },
        "waq_draft": {},
        "room_code": None,
        "room_code_client": None,
        "selected_blocks": [],
        "navigation_grace_seconds": 10,
        "navigation_warning_count": 0,
        "invalidated_reason": "",
        "awaiting_attendance_audit": False,
        "instructions_confirmed": False,
        "next_available": False,
        "show_block_switcher": False,
    }


def _validation_status_message(message_id: str, text: str, *, actions=None) -> dict:
    return {
        "id": message_id,
        "role": "assistant",
        "kind": "cta" if actions else "text",
        "text": text,
        "actions": list(actions or []),
    }


def _digital_validation_events(course: Course):
    return list(course.validation_events.filter(mode=ValidationEvent.Mode.DIGITAL_INVIGILATION).order_by("starts_at", "created_at"))


def _booked_validation_lookup(enrollment: Enrollment):
    bookings = {
        booking.event_id: booking
        for booking in ValidationBooking.objects.filter(
            enrollment=enrollment,
            status=ValidationBooking.Status.BOOKED,
            event__mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
        ).select_related("event")
    }
    attempts = {
        attempt.event_id: attempt
        for attempt in ValidationAttempt.objects.filter(
            enrollment=enrollment,
            event__mode=ValidationEvent.Mode.DIGITAL_INVIGILATION,
        ).select_related("event", "booking")
    }
    return bookings, attempts


def _student_validate_event_state(enrollment: Enrollment):
    now = timezone.now()
    course = enrollment.course
    events = _digital_validation_events(course)
    bookings_by_event_id, attempts_by_event_id = _booked_validation_lookup(enrollment)
    live_booking = None
    future_booking = None
    bookable_event = None

    for event in events:
        booking = bookings_by_event_id.get(event.pk)
        if booking and event.starts_at <= now < event.session_end_at:
            return {
                "state": "live",
                "event": event,
                "booking": booking,
                "attempt": attempts_by_event_id.get(event.pk),
            }
        if booking and future_booking is None and event.starts_at > now:
            future_booking = {"event": event, "booking": booking, "attempt": attempts_by_event_id.get(event.pk)}
        if booking is None and bookable_event is None and event.booking_is_open(now):
            bookable_event = event

    if future_booking is not None:
        return {"state": "booked_future", **future_booking}
    if bookable_event is not None:
        return {"state": "bookable", "event": bookable_event, "booking": None, "attempt": None}
    return {"state": "out_of_session", "event": None, "booking": None, "attempt": None}


def _render_student_validate(request: HttpRequest, enrollment: Enrollment, session_state: dict, sidebar_state: dict) -> HttpResponse:
    return render(
        request,
        "standalone/student_validate.html",
        {
            "course": enrollment.course,
            "session_state": session_state,
            "sidebar_state": sidebar_state,
            "sidebar_preview_state": serialize_student_practice_state(enrollment),
            "practice_validation_url": _practice_validation_url(enrollment.course_id, restart=True),
            "validation_sidebar_cta": _student_practice_validation_sidebar_cta(enrollment),
            "practice_url": reverse("standalone:practice_quiz", args=[enrollment.course_id]),
            "session_action_url": (
                reverse("standalone:validation_attempt_action", args=[session_state["attempt_id"], "ACTION"])
                if session_state.get("attempt_id")
                else ""
            ),
            "exit_url": reverse("standalone:student_dashboard"),
            "exit_label": "Dashboard",
        },
    )


@login_required
def student_validate(request: HttpRequest, course_id: int) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    enrollment = _student_enrollment_or_404(request.user, course_id)
    state = _student_validate_event_state(enrollment)
    practice_url = reverse("standalone:practice_quiz", args=[course_id])

    if state["state"] == "live":
        event = state["event"]
        booking = state["booking"]
        try:
            attempt = state["attempt"] or get_or_create_official_attempt(enrollment, event, booking=booking)
        except ValidationFlowError as error:
            messages.error(request, str(error))
            return redirect("standalone:student_dashboard")
        session_state = serialize_official_validation_session(attempt, request=request)
        session_state["eyebrow"] = "Validate"
        sidebar_state = _student_validate_sidebar_state(
            enrollment=enrollment,
            title="Validation session",
            copy="A validation session is live now. Complete your validation here on this device.",
            meta_rows=[
                f"{event.starts_at:%d %b %Y, %H:%M} to {event.session_end_at:%d %b %Y, %H:%M}",
                f"Location: {event.location}",
            ],
            secondary_action={"label": "Continue practice", "url": practice_url, "style": "secondary"},
        )
        return _render_student_validate(request, enrollment, session_state, sidebar_state)

    if state["state"] == "booked_future":
        event = state["event"]
        session_state = _empty_validate_session_state(
            enrollment.course,
            title="Validate",
            transcript=[
                _validation_status_message(
                    "validate-booked-future",
                    (
                        f"Your validation is booked for {event.starts_at:%d %b %Y, %H:%M}. "
                        "You're currently out of session. Would you like to start a practice validation until it begins?"
                    ),
                    actions=[{"label": "Start practice validation", "url": _practice_validation_url(course_id, restart=True), "style": "button"}],
                )
            ],
        )
        sidebar_state = _student_validate_sidebar_state(
            enrollment=enrollment,
            title="Validation booked",
            copy="Your validation session is booked and ready for you at the scheduled time.",
            meta_rows=[
                f"{event.starts_at:%d %b %Y, %H:%M} to {event.session_end_at:%d %b %Y, %H:%M}",
                f"Location: {event.location}",
            ],
            primary_action={"label": "Start practice validation", "url": _practice_validation_url(course_id, restart=True), "style": "button"},
            secondary_action={"label": "Return to practice", "url": practice_url, "style": "secondary"},
        )
        return _render_student_validate(request, enrollment, session_state, sidebar_state)

    if state["state"] == "bookable":
        event = state["event"]
        session_state = _empty_validate_session_state(
            enrollment.course,
            title="Validate",
            transcript=[
                _validation_status_message(
                    "validate-bookable",
                    "No validation session is active right now. Would you like to start a practice validation while you wait?",
                    actions=[{"label": "Start practice validation", "url": _practice_validation_url(course_id, restart=True), "style": "button"}],
                )
            ],
        )
        sidebar_state = _student_validate_sidebar_state(
            enrollment=enrollment,
            title="Book validation",
            copy="A validation session is currently open for booking.",
            meta_rows=[
                f"{event.starts_at:%d %b %Y, %H:%M} to {event.session_end_at:%d %b %Y, %H:%M}",
                f"Spaces left {event.spaces_left}",
                f"Bookings in last 24 hours {event.recent_booking_count(hours=24)}",
            ],
            primary_action={"label": "Book validation", "url": reverse("standalone:validation_book", args=[event.pk]), "style": "button"},
            secondary_action={"label": "Start practice validation", "url": _practice_validation_url(course_id, restart=True), "style": "secondary"},
        )
        return _render_student_validate(request, enrollment, session_state, sidebar_state)

    session_state = _empty_validate_session_state(
        enrollment.course,
        title="Validate",
        transcript=[
            _validation_status_message(
                "validate-out-of-session",
                "There is no live validation session for this course right now. Would you like to start a practice validation?",
                actions=[{"label": "Start practice validation", "url": _practice_validation_url(course_id, restart=True), "style": "button"}],
            )
        ],
    )
    sidebar_state = _student_validate_sidebar_state(
        enrollment=enrollment,
        title="Validation unavailable",
        copy="There is no bookable or live validation session for this course right now.",
        primary_action={"label": "Start practice validation", "url": _practice_validation_url(course_id, restart=True), "style": "button"},
        secondary_action={"label": "Return to practice", "url": practice_url, "style": "secondary"},
    )
    return _render_student_validate(request, enrollment, session_state, sidebar_state)


def _student_validation_attempt_or_404(user: User, attempt_id: int) -> ValidationAttempt:
    return get_object_or_404(
        ValidationAttempt.objects.select_related("event", "event__course", "enrollment", "enrollment__student"),
        pk=attempt_id,
        enrollment__student=user,
    )


@login_required
def validation_start(request: HttpRequest, event_id: int) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    enrollment = _student_enrollment_or_404(request.user, event.course_id)
    if event.mode != ValidationEvent.Mode.DIGITAL_INVIGILATION:
        messages.error(request, "Only digital invigilated validations are available right now.")
        return redirect("standalone:student_dashboard")
    if timezone.now() < event.starts_at:
        messages.info(request, "This validation is not open yet.")
        return redirect("standalone:student_dashboard")
    if timezone.now() >= event.session_end_at:
        messages.info(request, "This validation session has ended.")
        return redirect("standalone:student_dashboard")
    booking = None
    if event.requires_booking:
        booking = ValidationBooking.objects.filter(
            event=event,
            enrollment=enrollment,
            status=ValidationBooking.Status.BOOKED,
        ).first()
        if booking is None:
            messages.error(request, "You need a booking before starting this validation.")
            return redirect("standalone:student_dashboard")
    try:
        attempt = get_or_create_official_attempt(enrollment, event, booking=booking)
    except ValidationFlowError as error:
        messages.error(request, str(error))
        return redirect("standalone:student_dashboard")
    return redirect("standalone:student_validate", event.course_id)


@login_required
def validation_attempt(request: HttpRequest, attempt_id: int) -> HttpResponse:
    if not _is_student(request.user):
        raise Http404
    attempt = _student_validation_attempt_or_404(request.user, attempt_id)
    enrollment = attempt.enrollment
    session_state = serialize_official_validation_session(attempt, request=request)
    session_state["eyebrow"] = "Validate"
    sidebar_state = _student_validate_sidebar_state(
        enrollment=enrollment,
        title="Validation session",
        copy="This validation session is being completed here on this device.",
        meta_rows=[
            f"{attempt.event.starts_at:%d %b %Y, %H:%M} to {attempt.event.session_end_at:%d %b %Y, %H:%M}",
            f"Location: {attempt.event.location}",
        ],
        secondary_action={"label": "Continue practice", "url": reverse("standalone:practice_quiz", args=[enrollment.course_id]), "style": "secondary"},
    )
    return _render_student_validate(request, enrollment, session_state, sidebar_state)


@login_required
def validation_attempt_action(request: HttpRequest, attempt_id: int, action: str) -> JsonResponse:
    if request.method != "POST" or not _is_student(request.user):
        raise Http404
    attempt = _student_validation_attempt_or_404(request.user, attempt_id)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
    try:
        if action == "confirm":
            payload = confirm_official_validation_instructions(request, attempt)
            return JsonResponse({"ok": True, "session": payload})
        if action == "next":
            payload = reveal_official_validation_next(request, attempt)
            return JsonResponse({"ok": True, "session": payload})
        if action == "draft_answer":
            payload = draft_official_validation_answer(
                request,
                attempt,
                int(data.get("question_id") or 0),
                str(data.get("answer_text", "")).strip(),
            )
            return JsonResponse({"ok": True, "alignment": payload})
        if action == "presence":
            payload = report_validation_presence(
                request,
                attempt,
                int(data.get("away_seconds") or 0),
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "submit":
            payload = submit_official_validation_response(
                request,
                attempt,
                question_id=int(data.get("question_id") or 0) or None,
                selected_answers=data.get("answers") or ([str(data.get("answer", "")).strip()] if data.get("answer") else []),
                answer_text=str(data.get("answer_text", "")).strip(),
                audit_prompt_id=int(data.get("audit_prompt_id") or 0) or None,
            )
            return JsonResponse({"ok": True, "session": payload})
        if action == "skip":
            payload = skip_official_validation_question(
                request,
                attempt,
                question_id=int(data.get("question_id") or 0),
            )
            return JsonResponse({"ok": True, "session": payload})
    except ValidationFlowError as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    raise Http404


@login_required
def validation_room_display(request: HttpRequest, event_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    _teacher_course_or_404(request.user, event.course_id)
    if event.mode != ValidationEvent.Mode.DIGITAL_INVIGILATION:
        raise Http404
    ensure_room_code_secret(event)
    return render(
        request,
        "standalone/validation_room_display.html",
        {
            "event": event,
            "room_code": room_code_payload(event),
            "room_code_client": room_code_client_payload(event),
        },
    )


@login_required
def validation_room_display_data(request: HttpRequest, event_id: int) -> JsonResponse:
    if not _is_teacher(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    _teacher_course_or_404(request.user, event.course_id)
    if event.mode != ValidationEvent.Mode.DIGITAL_INVIGILATION:
        raise Http404
    return JsonResponse({"ok": True, "room_code": room_code_payload(event)})


@login_required
def validation_feedback_release(request: HttpRequest, event_id: int) -> HttpResponse:
    if request.method != "POST" or not _is_teacher(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    _teacher_course_or_404(request.user, event.course_id)
    released = release_event_feedback(event)
    messages.success(request, f"Released review for {released} validation attempt(s).")
    return redirect("standalone:course_detail", event.course_id)


@login_required
def validation_pack_pdf(request: HttpRequest, event_id: int) -> HttpResponse:
    if not _is_teacher(request.user):
        raise Http404
    event = get_object_or_404(ValidationEvent.objects.select_related("course"), pk=event_id)
    _teacher_course_or_404(request.user, event.course_id)
    if event.mode != ValidationEvent.Mode.PAPER_INVIGILATION:
        messages.error(request, "PDF packs are only available for paper invigilation events.")
        return redirect("standalone:course_detail", event.course_id)
    bookings = list(event.bookings.filter(status=ValidationBooking.Status.BOOKED).select_related("enrollment__student"))
    pack = ValidationPack.objects.create(event=event, generated_by=request.user, generated_for_booking_count=len(bookings))
    pdf_bytes = build_validation_pack_pdf(pack, bookings)
    return HttpResponse(pdf_bytes, content_type="application/pdf", headers={"Content-Disposition": f'inline; filename="validation-pack-{event.pk}.pdf"'})


def _legacy_practice_quiz(request: HttpRequest, course_id: int) -> HttpResponse:
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


@login_required
def practice_quiz(request: HttpRequest, course_id: int) -> HttpResponse:
    mode = request.GET.get("mode", PracticeAttempt.AttemptType.PRACTICE)
    if mode == PracticeAttempt.AttemptType.VALIDATION_PRACTICE:
        return redirect("standalone:validation_practice_session", course_id)
    if request.method == "POST":
        return _legacy_practice_quiz(request, course_id)
    if mode != PracticeAttempt.AttemptType.PRACTICE:
        mode = PracticeAttempt.AttemptType.PRACTICE
    enrollment = _student_enrollment_or_404(request.user, course_id)
    preview_state = serialize_student_practice_state(enrollment)
    return render(
        request,
        "standalone/student_preview.html",
        {
            "course": enrollment.course,
            "preview_state": preview_state,
            "action_url_template": reverse("standalone:student_practice_action", args=[enrollment.course_id, 0, "ACTION"]),
            "is_student_practice": True,
            "mode": mode,
            "practice_validation_url": _practice_validation_url(enrollment.course_id, restart=True),
            "validation_entry_url": reverse("standalone:student_validate", args=[enrollment.course_id]),
            "validation_sidebar_cta": _student_practice_validation_sidebar_cta(enrollment),
        },
    )


def _student_practice_payload(enrollment: Enrollment, block: CourseBlock, action: str, request: HttpRequest) -> JsonResponse:
    if action == "quiz":
        requested_question_type = None
        if request.body and "application/json" in (request.content_type or ""):
            try:
                data = json.loads(request.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)
            requested_question_type = str(data.get("question_type", "")).strip().lower() or None
        payload = request_student_practice_quiz(enrollment, block, requested_question_type=requested_question_type)
        return JsonResponse({"ok": True, "preview": payload})

    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "Please send valid JSON."}, status=400)

    if action in {"project_open", "project_chat", "project_submit"}:
        project_id = int(data.get("project_id") or 0)
        project = block.projects.filter(pk=project_id, status=BlockProject.Status.PUBLISHED).first()
        if project is None:
            return JsonResponse({"ok": False, "error": "Choose a published project for this block."}, status=404)
        if action == "project_open":
            open_student_project(enrollment, project)
        elif action == "project_chat":
            message_text = str(data.get("message", "")).strip()
            send_student_project_message(enrollment, project, message_text)
        else:
            raw_answer = str(data.get("answer", "")).strip()
            submit_student_project_answer(enrollment, project, raw_answer)
        payload = serialize_student_practice_state(enrollment, active_block_id=block.pk)
        return JsonResponse({"ok": True, "preview": payload})

    if action == "answer":
        selected_answers = data.get("answers")
        if not isinstance(selected_answers, list):
            selected_answer = str(data.get("answer", "")).strip()
            selected_answers = [selected_answer] if selected_answer else []
        question_id = int(data.get("question_id") or 0)
        answer_text = str(data.get("answer_text", "")).strip()
        payload = submit_student_practice_answer(enrollment, block, question_id, selected_answers, answer_text=answer_text)
        return JsonResponse({"ok": True, "preview": payload})
    if action == "draft_answer":
        question_id = int(data.get("question_id") or 0)
        answer_text = str(data.get("answer_text", "")).strip()
        payload = draft_student_practice_written_answer(enrollment, block, question_id, answer_text)
        return JsonResponse({"ok": True, "alignment": payload})
    if action == "chat":
        question = str(data.get("question", "")).strip()
        if not question:
            return JsonResponse({"ok": False, "error": "Please enter a course question first."}, status=400)
        payload = send_student_practice_chat_message(enrollment, block, question)
        return JsonResponse({"ok": True, "preview": payload})
    if action == "flag":
        question_id = int(data.get("question_id") or 0)
        payload = flag_student_practice_question(enrollment, block, question_id)
        return JsonResponse({"ok": True, "preview": payload})
    raise Http404


@login_required
def student_practice_action(request: HttpRequest, course_id: int, block_id: int, action: str) -> JsonResponse:
    if request.method != "POST" or not _is_student(request.user):
        raise Http404
    enrollment = _student_enrollment_or_404(request.user, course_id)
    block = get_object_or_404(CourseBlock.objects.select_related("course"), pk=block_id, course=enrollment.course)
    return _student_practice_payload(enrollment, block, action, request)


class StandaloneLogoutView(LogoutView):
    next_page = "website:home"
