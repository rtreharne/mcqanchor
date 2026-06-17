from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0010_courseconfig_advanced_question_start_percent"),
    ]

    operations = [
        migrations.CreateModel(
            name="PracticeMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("message_id", models.CharField(max_length=100)),
                ("sequence", models.PositiveIntegerField()),
                ("role", models.CharField(max_length=20)),
                ("kind", models.CharField(max_length=30)),
                ("text", models.TextField(blank=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("source_blocks", models.JSONField(blank=True, default=list)),
                (
                    "attempt_question",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="practice_messages",
                        to="standalone.practiceattemptquestion",
                    ),
                ),
                (
                    "block",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="practice_messages",
                        to="standalone.courseblock",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="practice_messages",
                        to="standalone.enrollment",
                    ),
                ),
                (
                    "question",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="practice_messages",
                        to="standalone.questionbankitem",
                    ),
                ),
            ],
            options={
                "ordering": ["enrollment", "sequence", "created_at"],
                "unique_together": {("enrollment", "message_id"), ("enrollment", "sequence")},
            },
        ),
    ]
