from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0021_blockconfig_advanced_question_start_percent_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="allow_pre_engagement",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="courseconfig",
            name="engagement_half_life_days",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(3650)],
            ),
        ),
    ]
