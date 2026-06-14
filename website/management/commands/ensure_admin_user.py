import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or update a Django admin user from environment variables."

    def handle(self, *args, **options):
        username = os.getenv("DJANGO_ADMIN_USERNAME", "").strip()
        password = os.getenv("DJANGO_ADMIN_PASSWORD", "").strip()

        if not username or not password:
            self.stdout.write(
                self.style.WARNING(
                    "Skipping admin bootstrap because DJANGO_ADMIN_USERNAME or DJANGO_ADMIN_PASSWORD is missing."
                )
            )
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": f"{username}@example.com",
                "is_staff": True,
                "is_superuser": True,
                "role": getattr(User.Role, "INTERNAL", "internal") if hasattr(User, "Role") else "internal",
                "is_email_verified": True,
            },
        )

        updated_fields = []
        if user.email != f"{username}@example.com" and not user.email:
            user.email = f"{username}@example.com"
            updated_fields.append("email")
        if not user.is_staff:
            user.is_staff = True
            updated_fields.append("is_staff")
        if not user.is_superuser:
            user.is_superuser = True
            updated_fields.append("is_superuser")
        if hasattr(user, "role") and user.role != getattr(User.Role, "INTERNAL", "internal"):
            user.role = getattr(User.Role, "INTERNAL", "internal")
            updated_fields.append("role")
        if hasattr(user, "is_email_verified") and not user.is_email_verified:
            user.is_email_verified = True
            updated_fields.append("is_email_verified")

        user.set_password(password)
        updated_fields.append("password")
        user.save(update_fields=updated_fields)

        if created:
            self.stdout.write(self.style.SUCCESS(f"Created admin user '{username}'.")) 
        else:
            self.stdout.write(self.style.SUCCESS(f"Updated admin user '{username}'.")) 
