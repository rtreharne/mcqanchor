from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("standalone", "0008_courseconfig_waq_ratio_percent_and_questionbankitem_waq_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="questionbankitem",
            name="further_study_questions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
