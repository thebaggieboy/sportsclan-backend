from django.core.management.base import BaseCommand
from django.utils.text import slugify

from tournaments.models import Sport


SPORTS = [
    "Basketball",
    "Football",
    "Tennis",
    "Volleyball",
    "Cricket",
    "Rugby",
    "Badminton",
    "Table Tennis",
    "Athletics",
    "Other",
]


class Command(BaseCommand):
    help = "Add the default SportsClan sports catalog."

    def handle(self, *args, **options):
        for sort_order, name in enumerate(SPORTS):
            Sport.objects.update_or_create(
                slug=slugify(name),
                defaults={"name": name, "is_active": True, "sort_order": sort_order},
            )
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(SPORTS)} sports."))