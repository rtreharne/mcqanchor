import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0018_courseconfig_assistant_guidance_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="demo_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="courseconfig",
            name="demo_iframe_allowed_origins",
            field=models.TextField(blank=True),
        ),
        migrations.CreateModel(
            name="CourseDemoAccess",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("shared_practice_state", models.JSONField(blank=True, default=dict)),
                ("course", models.OneToOneField(on_delete=models.deletion.CASCADE, related_name="demo_access", to="standalone.course")),
            ],
            options={
                "ordering": ["course__title"],
            },
        ),
        migrations.CreateModel(
            name="CourseDemoValidationSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("visitor_key", models.CharField(max_length=64)),
                ("validation_state", models.JSONField(blank=True, default=dict)),
                ("demo_access", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="validation_sessions", to="standalone.coursedemoaccess")),
            ],
            options={
                "ordering": ["-updated_at", "-created_at"],
                "unique_together": {("demo_access", "visitor_key")},
            },
        ),
    ]
