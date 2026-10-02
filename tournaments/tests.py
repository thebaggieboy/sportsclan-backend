from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Country, CountryCurrency, Currency, Sport, Tournament, TournamentParticipant, Venue

User = get_user_model()


class SportsClanApiTests(APITestCase):
    def setUp(self):
        self.host = User.objects.create_user(
            username="host", email="host@example.com", password="StrongPass!246"
        )
        self.player = User.objects.create_user(
            username="player", email="player@example.com", password="StrongPass!246"
        )
        self.other_player = User.objects.create_user(
            username="other", email="other@example.com", password="StrongPass!246"
        )
        self.currency = Currency.objects.create(code="USD", name="US Dollar")
        self.country = Country.objects.create(
            code="US", name="United States", default_currency=self.currency
        )
        CountryCurrency.objects.create(
            country=self.country, currency=self.currency, is_default=True
        )
        self.sport = Sport.objects.create(name="Basketball", slug="basketball")
        self.venue = Venue.objects.create(
            name="Riverside Court",
            city="Springfield",
            country="United States",
            country_reference=self.country,
            price_currency="USD",
        )
        self.venue.sports.add(self.sport)
        self.tournament = Tournament.objects.create(
            title="Sunday Hoops",
            host=self.host,
            sport=self.sport,
            venue=self.venue,
            country=self.country,
            starts_at=timezone.now() + timedelta(days=2),
            entry_fee="5.00",
            currency="USD",
            max_players=2,
        )

    def test_sports_and_venues_are_public(self):
        sports_response = self.client.get(reverse("sport-list"))
        countries_response = self.client.get(reverse("country-list"))
        venues_response = self.client.get(reverse("venue-list"))

        self.assertEqual(sports_response.status_code, status.HTTP_200_OK)
        self.assertEqual(sports_response.data["results"][0]["slug"], "basketball")
        country = next(
            item for item in countries_response.data["results"] if item["code"] == "US"
        )
        self.assertEqual(country["default_currency"], "USD")
        self.assertEqual(country["currencies"][0]["code"], "USD")
        self.assertEqual(venues_response.status_code, status.HTTP_200_OK)
        self.assertEqual(venues_response.data["results"][0]["city"], "Springfield")

    def test_registration_returns_tokens(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newplayer",
                "email": "newplayer@example.com",
                "password": "StrongPass!246",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_authenticated_host_can_create_tournament(self):
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "Saturday Tennis",
                "sport_id": self.sport.id,
                "venue_id": self.venue.id,
                "country_id": self.country.code,
                "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
                "entry_fee": "8.50",
                "currency": "USD",
                "max_players": 4,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["host"], self.host.username)
        self.assertEqual(response.data["slots_open"], 4)
        self.assertEqual(response.data["projected_prize_pool"], "34.00")
        self.assertEqual(response.data["venue_fee_estimate"], "0.00")
        self.assertEqual(response.data["venue_fee_status"], Tournament.VenueFeeStatus.NOT_REQUIRED)

    def test_hourly_venue_estimate_uses_duration_and_snapshots_rate(self):
        self.venue.pricing_type = Venue.PricingType.HOURLY
        self.venue.price_amount = "20.00"
        self.venue.price_currency = "USD"
        self.venue.save()
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "Long Court Session",
                "sport_id": self.sport.id,
                "venue_id": self.venue.id,
                "country_id": self.country.code,
                "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
                "duration_minutes": 90,
                "entry_fee": "10.00",
                "currency": "USD",
                "max_players": 4,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["venue_rate_snapshot"], "20.00")
        self.assertEqual(response.data["venue"]["pricing_type"], Venue.PricingType.HOURLY)
        self.assertEqual(response.data["venue"]["price_currency"], "USD")
        self.assertEqual(response.data["venue_fee_estimate"], "30.00")
        self.assertEqual(response.data["venue_fee"], None)
        self.assertEqual(response.data["venue_fee_status"], Tournament.VenueFeeStatus.PENDING)

    def test_tournament_currency_must_belong_to_selected_country(self):
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "Invalid Currency Game",
                "sport_id": self.sport.id,
                "venue_id": self.venue.id,
                "country_id": self.country.code,
                "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
                "entry_fee": "5.00",
                "currency": "CAD",
                "max_players": 4,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("currency", response.data)

    def test_country_filter_returns_matching_venues(self):
        response = self.client.get(reverse("venue-list"), {"country_reference__code": "US"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["results"][0]["country_info"]["code"], "US")

    def test_tournament_rejects_venue_from_another_country(self):
        canada = Country.objects.create(code="CA", name="Canada")
        cad = Currency.objects.create(code="CAD", name="Canadian Dollar")
        CountryCurrency.objects.create(country=canada, currency=cad, is_default=True)
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "Wrong Country Game",
                "country_id": canada.code,
                "sport_id": self.sport.id,
                "venue_id": self.venue.id,
                "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
                "entry_fee": "5.00",
                "currency": "CAD",
                "max_players": 4,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("venue_id", response.data)

    def test_host_can_record_agreed_venue_cost(self):
        self.client.force_authenticate(self.host)
        response = self.client.patch(
            reverse("tournament-detail", args=[self.tournament.id]),
            {"venue_fee": "35.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["venue_fee"], "35.00")
        self.assertEqual(response.data["venue_fee_status"], Tournament.VenueFeeStatus.AGREED)

    def test_join_claims_a_slot_and_full_tournament_reopens_after_leave(self):
        self.client.force_authenticate(self.player)
        join_url = reverse("tournament-join", args=[self.tournament.id])
        join_response = self.client.post(join_url, {"slot_number": 1}, format="json")

        self.assertEqual(join_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(join_response.data["entry_fee_at_join"], "5.00")

        self.client.force_authenticate(self.other_player)
        full_response = self.client.post(join_url, {"slot_number": 2}, format="json")
        self.assertEqual(full_response.status_code, status.HTTP_201_CREATED)
        detail_response = self.client.get(
            reverse("tournament-detail", args=[self.tournament.id])
        )
        self.assertEqual(detail_response.data["taken_slots"], [1, 2])
        self.assertEqual(detail_response.data["my_slot"], 2)
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, Tournament.Status.FULL)

        leave_url = reverse("tournament-leave", args=[self.tournament.id])
        self.client.force_authenticate(self.other_player)
        leave_response = self.client.delete(leave_url)
        self.assertEqual(leave_response.status_code, status.HTTP_204_NO_CONTENT)
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, Tournament.Status.OPEN)

    def test_players_cannot_claim_the_same_slot(self):
        self.client.force_authenticate(self.player)
        join_url = reverse("tournament-join", args=[self.tournament.id])
        self.client.post(join_url, {"slot_number": 1}, format="json")

        self.client.force_authenticate(self.other_player)
        response = self.client.post(join_url, {"slot_number": 1}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("slot_number", response.data)

    def test_player_cannot_join_twice(self):
        self.client.force_authenticate(self.player)
        join_url = reverse("tournament-join", args=[self.tournament.id])
        first_response = self.client.post(join_url, {"slot_number": 1}, format="json")
        second_response = self.client.post(join_url, {"slot_number": 2}, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TournamentParticipant.objects.filter(user=self.player).count(), 1)

# Create your tests here.
