from django.db import migrations, models


def clear_legacy_waitlist_notifications(apps, schema_editor):
    TournamentWaitlist = apps.get_model("tournaments", "TournamentWaitlist")
    TournamentWaitlist.objects.filter(notified_at__isnull=False).update(
        notified_at=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("tournaments", "0010_tournament_reliability"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tournament",
            name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"),
                    ("full", "Full"),
                    ("cancelling", "Cancelling"),
                    ("cancelled", "Cancelled"),
                    ("completed", "Completed"),
                ],
                default="open",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="tournamentwaitlist",
            name="offered_slot_number",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tournamentwaitlist",
            name="offer_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="paystacktransaction",
            name="refund_status",
            field=models.CharField(
                choices=[
                    ("not_requested", "Not requested"),
                    ("pending", "Pending"),
                    ("processed", "Processed"),
                    ("failed", "Failed"),
                ],
                default="not_requested",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="paystacktransaction",
            name="release_on_refund",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(
            clear_legacy_waitlist_notifications,
            migrations.RunPython.noop,
        ),
    ]
