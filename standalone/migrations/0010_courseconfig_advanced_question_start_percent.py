from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0009_questionbankitem_further_study_questions"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="advanced_question_start_percent",
            field=models.PositiveSmallIntegerField(
                default=50,
                validators=[MinValueValidator(0), MaxValueValidator(100)],
            ),
        ),
    ]
