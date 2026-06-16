from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0005_courseblock_available_from"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="blockconfig",
            name="target_question_count",
            field=models.PositiveSmallIntegerField(default=20),
        ),
        migrations.CreateModel(
            name="EnrollmentQuestionState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("times_presented", models.PositiveIntegerField(default=0)),
                ("times_correct", models.PositiveIntegerField(default=0)),
                ("times_incorrect", models.PositiveIntegerField(default=0)),
                ("last_presented_sequence", models.PositiveIntegerField(default=0)),
                ("retired_at", models.DateTimeField(blank=True, null=True)),
                ("enrollment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="question_states", to="standalone.enrollment")),
                ("question", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="enrollment_states", to="standalone.questionbankitem")),
            ],
            options={
                "ordering": ["enrollment", "question"],
                "unique_together": {("enrollment", "question")},
            },
        ),
        migrations.CreateModel(
            name="QuestionFlag",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reason", models.TextField(blank=True)),
                ("enrollment", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="question_flags", to="standalone.enrollment")),
                ("flagged_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="question_flags", to=settings.AUTH_USER_MODEL)),
                ("question", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="flags", to="standalone.questionbankitem")),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
