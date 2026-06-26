from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0024_consolidate_practice_scoring"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseconfig",
            name="homepage_demo_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
