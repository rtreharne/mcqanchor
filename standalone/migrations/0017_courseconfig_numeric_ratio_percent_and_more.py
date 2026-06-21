import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0016_validationevent_ends_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="numeric_ratio_percent",
            field=models.PositiveSmallIntegerField(
                default=0,
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)],
            ),
        ),
        migrations.AddField(
            model_name="questionbankitem",
            name="numeric_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AlterField(
            model_name="questionbankitem",
            name="question_type",
            field=models.CharField(
                choices=[("mcq", "Single-answer"), ("num", "Numeric"), ("maq", "Multiple-answer"), ("waq", "Written-answer")],
                default="mcq",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="validationattemptquestion",
            name="question_type",
            field=models.CharField(
                choices=[("mcq", "Single-answer"), ("num", "Numeric"), ("maq", "Multiple-answer"), ("waq", "Written-answer")],
                max_length=20,
            ),
        ),
    ]
