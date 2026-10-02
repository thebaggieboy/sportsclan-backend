import pycountry
from babel.numbers import get_currency_name, get_territory_currencies
from django.core.management.base import BaseCommand

from tournaments.models import Country, CountryCurrency, Currency, Tournament, Venue


class Command(BaseCommand):
    help = "Seed ISO countries and their current legal-tender currencies."

    def handle(self, *args, **options):
        country_count = 0
        currency_count = 0
        countries_by_code = {}

        for iso_country in pycountry.countries:
            country, _ = Country.objects.update_or_create(
                code=iso_country.alpha_2,
                defaults={"name": iso_country.name},
            )
            countries_by_code[iso_country.alpha_2] = country
            countries_by_code[iso_country.alpha_3] = country
            currency_codes = get_territory_currencies(
                iso_country.alpha_2,
                tender=True,
                non_tender=False,
            )
            valid_codes = []

            for code in dict.fromkeys(currency_codes):
                iso_currency = pycountry.currencies.get(alpha_3=code)
                name = iso_currency.name if iso_currency else get_currency_name(code, locale="en")
                Currency.objects.update_or_create(code=code, defaults={"name": name})
                valid_codes.append(code)
                currency_count += 1

            CountryCurrency.objects.filter(country=country).exclude(
                currency_id__in=valid_codes
            ).delete()

            for index, code in enumerate(valid_codes):
                CountryCurrency.objects.update_or_create(
                    country=country,
                    currency_id=code,
                    defaults={"is_default": index == 0},
                )

            country.default_currency_id = valid_codes[0] if valid_codes else None
            country.save(update_fields=["default_currency"])
            country_count += 1

        backfilled_venues = 0
        for venue in Venue.objects.filter(country_reference__isnull=True).exclude(country=""):
            country_value = venue.country.strip()
            country = countries_by_code.get(country_value.upper())
            if country is None:
                country = Country.objects.filter(name__iexact=country_value).first()
            if country is None:
                continue

            venue.country_reference = country
            if not country.currency_options.filter(currency_id=venue.price_currency).exists():
                venue.price_currency = country.default_currency_id or venue.price_currency
            venue.save(update_fields=["country_reference", "price_currency"])
            backfilled_venues += 1

        backfilled_tournaments = 0
        legacy_tournaments = Tournament.objects.filter(country__isnull=True).select_related(
            "venue", "venue__country_reference"
        )
        for tournament in legacy_tournaments:
            country = tournament.venue.country_reference
            if country is None:
                continue

            tournament.country = country
            if not country.currency_options.filter(currency_id=tournament.currency).exists():
                tournament.currency = country.default_currency_id or tournament.currency
            tournament.venue_pricing_type_snapshot = tournament.venue.pricing_type
            tournament.venue_rate_snapshot = tournament.venue.price_amount
            tournament.venue_rate_currency_snapshot = tournament.venue.price_currency
            if tournament.venue.pricing_type == Venue.PricingType.FREE and tournament.venue_fee is None:
                tournament.venue_fee = 0
                tournament.venue_fee_status = Tournament.VenueFeeStatus.NOT_REQUIRED
            tournament.save(
                update_fields=[
                    "country",
                    "currency",
                    "venue_pricing_type_snapshot",
                    "venue_rate_snapshot",
                    "venue_rate_currency_snapshot",
                    "venue_fee",
                    "venue_fee_status",
                ]
            )
            backfilled_tournaments += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {country_count} countries and {currency_count} country currency options; "
                f"linked {backfilled_venues} venues and {backfilled_tournaments} tournaments."
            )
        )