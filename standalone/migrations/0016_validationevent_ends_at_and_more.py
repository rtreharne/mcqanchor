from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0015_validationattempt_invalidated_reason_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="validationevent",
            name="ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="validationevent",
            name="late_booking_cutoff_minutes",
            field=models.PositiveSmallIntegerField(default=20),
        ),
    ]
