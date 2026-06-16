import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0006_blockconfig_target_question_count_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="maq_ratio_percent",
            field=models.PositiveSmallIntegerField(
                default=20,
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)],
            ),
        ),
        migrations.AddField(
            model_name="questionbankitem",
            name="additional_correct_answers",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="questionbankitem",
            name="question_type",
            field=models.CharField(
                choices=[("mcq", "Single-answer"), ("maq", "Multiple-answer")],
                default="mcq",
                max_length=20,
            ),
        ),
    ]
