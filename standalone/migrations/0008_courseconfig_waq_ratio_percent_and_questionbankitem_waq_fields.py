import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0007_courseconfig_maq_ratio_percent_and_questionbankitem_maq_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="waq_ratio_percent",
            field=models.PositiveSmallIntegerField(
                default=10,
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100)],
            ),
        ),
        migrations.AddField(
            model_name="questionbankitem",
            name="written_answer_keywords",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="questionbankitem",
            name="question_type",
            field=models.CharField(
                choices=[("mcq", "Single-answer"), ("maq", "Multiple-answer"), ("waq", "Written-answer")],
                default="mcq",
                max_length=20,
            ),
        ),
    ]
