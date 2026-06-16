from django.db import migrations, models


def set_learning_objective_positions(apps, schema_editor):
    LearningObjective = apps.get_model("standalone", "LearningObjective")
    CourseBlock = apps.get_model("standalone", "CourseBlock")

    for block in CourseBlock.objects.all().order_by("order", "created_at", "pk"):
        objectives = list(
            LearningObjective.objects.filter(block=block).order_by("code", "created_at", "pk")
        )
        for index, objective in enumerate(objectives, start=1):
            objective.position = index
            objective.code = f"{block.order}.{index}"
        if objectives:
            LearningObjective.objects.bulk_update(objectives, ["position", "code"])


class Migration(migrations.Migration):
    dependencies = [
        ("standalone", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="learningobjective",
            name="position",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.RunPython(set_learning_objective_positions, migrations.RunPython.noop),
    ]
