from django.db import migrations, models
from django.utils import timezone


def set_course_block_available_from(apps, schema_editor):
    CourseBlock = apps.get_model("standalone", "CourseBlock")
    for block in CourseBlock.objects.all().only("pk", "created_at"):
        block.available_from = block.created_at.date() if block.created_at else timezone.localdate()
        block.save(update_fields=["available_from"])


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0004_courseblock_regeneration_error_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="courseblock",
            name="available_from",
            field=models.DateField(null=True),
        ),
        migrations.RunPython(set_course_block_available_from, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="courseblock",
            name="available_from",
            field=models.DateField(default=timezone.localdate),
        ),
    ]
