from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0009_venue_postal_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="tournament",
            name="cancelled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="attendance_status",
            field=models.CharField(
                choices=[
                    ("unmarked", "Not marked"),
                    ("attended", "Attended"),
                    ("no_show", "No-show reported"),
                    ("excused", "Excused"),
                ],
                default="unmarked",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="attendance_confirmed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="tournamentparticipant",
            name="attendance_disputed",
            field=models.BooleanField(default=False),
        ),
    ]
