from django.db import migrations


def backfill_missing_course_configs(apps, schema_editor):
    Course = apps.get_model("standalone", "Course")
    CourseConfig = apps.get_model("standalone", "CourseConfig")

    existing_course_ids = set(CourseConfig.objects.values_list("course_id", flat=True))
    missing_configs = [
        CourseConfig(course_id=course_id)
        for course_id in Course.objects.exclude(pk__in=existing_course_ids).values_list("pk", flat=True)
    ]
    if missing_configs:
        CourseConfig.objects.bulk_create(missing_configs, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0019_courseconfig_demo_fields_and_demo_models"),
    ]

    operations = [
        migrations.RunPython(backfill_missing_course_configs, migrations.RunPython.noop),
    ]
