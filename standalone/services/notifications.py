from django.conf import settings
from django.core.mail import send_mail

from standalone.models import NotificationLog


def send_logged_email(*, recipient: str, subject: str, body: str, event_type: str, related_object: str = "") -> None:
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient], fail_silently=False)
    NotificationLog.objects.create(
        recipient_email=recipient,
        event_type=event_type,
        subject=subject,
        body_preview=body[:500],
        related_object=related_object,
        delivered=True,
    )

