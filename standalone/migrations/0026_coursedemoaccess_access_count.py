from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0025_courseconfig_homepage_demo_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="coursedemoaccess",
            name="access_count",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
