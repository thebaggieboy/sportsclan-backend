from datetime import timedelta
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .models import (
    Country, CountryCurrency, Currency, PaystackTransaction, Sport, Tournament,
    TournamentMessage, TournamentParticipant, TournamentReport, UserNotification,
    TournamentWaitlist, Venue, WaitlistSignup,
)

User = get_user_model()


@override_settings(TOURNAMENT_PAYMENTS_ENABLED=True)
class SportsClanApiTests(APITestCase):
    @staticmethod
    def mock_paystack_response(data):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"status": True, "data": data}
        ).encode("utf-8")
        return response

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

    def test_tournaments_can_be_filtered_by_country(self):
        us_games = self.client.get(reverse("tournament-list"), {"country__code": "US"})
        ca_games = self.client.get(reverse("tournament-list"), {"country__code": "CA"})

        self.assertEqual(us_games.status_code, status.HTTP_200_OK)
        self.assertEqual(us_games.data["count"], 1)
        self.assertEqual(ca_games.status_code, status.HTTP_200_OK)
        self.assertEqual(ca_games.data["count"], 0)

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

    def test_waitlist_signup_is_public_and_deduplicates_email(self):
        first = self.client.post(
            reverse("waitlist-signup"),
            {"email": "  NewPlayer@Example.com ", "interest": "tester"},
            format="json",
        )
        duplicate = self.client.post(
            reverse("waitlist-signup"),
            {"email": "newplayer@example.com", "interest": "player"},
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(duplicate.status_code, status.HTTP_200_OK)
        self.assertEqual(first.data, duplicate.data)
        self.assertEqual(WaitlistSignup.objects.count(), 1)
        signup = WaitlistSignup.objects.get()
        self.assertEqual(signup.email, "newplayer@example.com")
        self.assertEqual(signup.interest, WaitlistSignup.Interest.TESTER)

    def test_waitlist_signup_rejects_invalid_email_and_interest(self):
        response = self.client.post(
            reverse("waitlist-signup"),
            {"email": "not-an-email", "interest": "spectator"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(WaitlistSignup.objects.count(), 0)

    def test_waitlist_allows_production_frontend_cors_preflight(self):
        origin = "https://sportsclanui.vercel.app"
        with override_settings(CORS_ALLOWED_ORIGINS=[origin]):
            response = self.client.options(
                reverse("waitlist-signup"),
                HTTP_ORIGIN=origin,
                HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
                HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Access-Control-Allow-Origin"], origin)

    def test_authenticated_host_can_create_tournament(self):
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "Saturday Tennis",
                "sport_id": self.sport.id,
                "venue_id": self.venue.id,
                "venue_name": "North Court Entrance",
                "state": "North District",
                "latitude": 53.480800,
                "longitude": -2.242600,
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
        self.assertEqual(response.data["venue_name"], "North Court Entrance")
        self.assertEqual(response.data["state"], "North District")
        self.assertEqual(response.data["latitude"], "53.480800")
        self.assertEqual(response.data["longitude"], "-2.242600")
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

    def test_host_can_create_tournament_with_a_new_map_venue(self):
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "New Pin Tournament",
                "sport_id": self.sport.id,
                "venue_city": "Springfield",
                "venue_address": "123 River Road, west entrance",
                "postal_code": "62701",
                "venue_name": "Westside Community Court",
                "state": "Illinois",
                "latitude": 39.7817,
                "longitude": -89.6501,
                "country_id": self.country.code,
                "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
                "entry_fee": "5.00",
                "currency": "USD",
                "max_players": 4,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["venue_name"], "Westside Community Court")
        self.assertEqual(response.data["state"], "Illinois")
        self.assertEqual(response.data["venue"]["city"], "Springfield")
        self.assertEqual(response.data["venue"]["address"], "123 River Road, west entrance")
        self.assertEqual(response.data["venue"]["postal_code"], "62701")
        self.assertEqual(response.data["latitude"], "39.781700")
        self.assertEqual(response.data["longitude"], "-89.650100")
        tournament = Tournament.objects.get(pk=response.data["id"])
        self.assertEqual(tournament.latitude, Decimal("39.781700"))
        self.assertEqual(tournament.longitude, Decimal("-89.650100"))
        self.assertEqual(tournament.venue.latitude, Decimal("39.781700"))
        self.assertEqual(tournament.venue.longitude, Decimal("-89.650100"))
        self.assertEqual(tournament.venue.address, "123 River Road, west entrance")
        self.assertEqual(tournament.venue.postal_code, "62701")

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

    def test_expired_reservation_releases_slot_for_another_player(self):
        self.client.force_authenticate(self.player)
        join_url = reverse("tournament-join", args=[self.tournament.id])
        first_response = self.client.post(join_url, {"slot_number": 1}, format="json")
        participant = TournamentParticipant.objects.get(pk=first_response.data["id"])
        participant.reservation_expires_at = timezone.now() - timedelta(seconds=1)
        participant.save(update_fields=["reservation_expires_at"])

        self.client.force_authenticate(self.other_player)
        second_response = self.client.post(join_url, {"slot_number": 1}, format="json")

        self.assertEqual(second_response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(TournamentParticipant.objects.filter(pk=participant.pk).exists())
        self.assertEqual(second_response.data["slot_number"], 1)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    @patch("tournaments.views.urlopen")
    def test_paystack_initialize_uses_server_amount_and_returns_checkout(self, mock_urlopen):
        self.client.force_authenticate(self.player)
        self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        mock_urlopen.return_value = self.mock_paystack_response(
            {"authorization_url": "https://checkout.paystack.com/test"}
        )

        response = self.client.post(
            reverse("tournament-payment-initialize", args=[self.tournament.id]),
            {},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["authorization_url"], "https://checkout.paystack.com/test")
        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(sent_payload["amount"], "500")
        self.assertEqual(sent_payload["currency"], "USD")

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    @patch("tournaments.views.urlopen")
    def test_paystack_verification_marks_entry_paid(self, mock_urlopen):
        self.client.force_authenticate(self.player)
        self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        mock_urlopen.return_value = self.mock_paystack_response(
            {"authorization_url": "https://checkout.paystack.com/test"}
        )
        initialized = self.client.post(
            reverse("tournament-payment-initialize", args=[self.tournament.id]),
            {},
            format="json",
        )
        reference = initialized.data["reference"]
        mock_urlopen.return_value = self.mock_paystack_response(
            {
                "reference": reference,
                "status": "success",
                "amount": 500,
                "currency": "USD",
            }
        )

        response = self.client.post(
            reverse("tournament-payment-verify", args=[self.tournament.id]),
            {"reference": reference},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        participant = TournamentParticipant.objects.get(user=self.player, tournament=self.tournament)
        self.assertEqual(participant.payment_status, TournamentParticipant.PaymentStatus.PAID)
        self.assertIsNone(participant.reservation_expires_at)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    def test_paystack_webhook_requires_signature_and_marks_entry_paid(self):
        self.client.force_authenticate(self.player)
        joined = self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        participant = TournamentParticipant.objects.get(pk=joined.data["id"])
        payment = PaystackTransaction.objects.create(
            participant=participant,
            reference="sc-test-webhook",
            amount_minor=500,
            currency="USD",
        )
        event_body = json.dumps(
            {
                "event": "charge.success",
                "data": {
                    "reference": payment.reference,
                    "status": "success",
                    "amount": 500,
                    "currency": "USD",
                },
            }
        )
        signature = hmac.new(
            b"sk_test_example", event_body.encode("utf-8"), hashlib.sha512
        ).hexdigest()

        response = self.client.post(
            reverse("paystack-webhook"),
            event_body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        participant.refresh_from_db()
        self.assertEqual(participant.payment_status, TournamentParticipant.PaymentStatus.PAID)

    def test_full_tournament_waitlist_notifies_next_player_after_spot_opens(self):
        self.client.force_authenticate(self.player)
        join_url = reverse("tournament-join", args=[self.tournament.id])
        self.client.post(join_url, {"slot_number": 1}, format="json")
        self.client.force_authenticate(self.other_player)
        self.client.post(join_url, {"slot_number": 2}, format="json")

        waiting_user = User.objects.create_user(
            username="waiter", email="waiter@example.com", password="StrongPass!246"
        )
        second_waiting_user = User.objects.create_user(
            username="waiter2", email="waiter2@example.com", password="StrongPass!246"
        )
        self.client.force_authenticate(waiting_user)
        waitlist_url = reverse("tournament-waitlist", args=[self.tournament.id])
        joined = self.client.post(waitlist_url, format="json")
        self.assertEqual(joined.status_code, status.HTTP_201_CREATED)
        self.assertEqual(joined.data["position"], 1)
        self.client.force_authenticate(second_waiting_user)
        second_joined = self.client.post(waitlist_url, format="json")
        self.assertEqual(second_joined.data["position"], 2)

        self.client.force_authenticate(self.other_player)
        left = self.client.delete(reverse("tournament-leave", args=[self.tournament.id]))
        self.assertEqual(left.status_code, status.HTTP_204_NO_CONTENT)
        notification = UserNotification.objects.get(user=waiting_user)
        self.assertEqual(notification.kind, UserNotification.Kind.SPOT_OPEN)
        self.assertFalse(UserNotification.objects.filter(user=second_waiting_user).exists())
        self.client.force_authenticate(self.player)
        left_again = self.client.delete(reverse("tournament-leave", args=[self.tournament.id]))
        self.assertEqual(left_again.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(UserNotification.objects.filter(user=waiting_user).count(), 1)
        self.assertFalse(UserNotification.objects.filter(user=second_waiting_user).exists())
        waiting_entry = TournamentWaitlist.objects.get(
            tournament=self.tournament, user=waiting_user
        )
        waiting_entry.offer_expires_at = timezone.now() - timedelta(seconds=1)
        waiting_entry.save(update_fields=["offer_expires_at"])
        self.client.force_authenticate(second_waiting_user)
        current_offer = self.client.get(waitlist_url)
        self.assertEqual(current_offer.status_code, status.HTTP_200_OK)
        self.assertEqual(
            current_offer.data["offered_slot_number"],
            1,
        )
        self.assertEqual(
            UserNotification.objects.get(user=second_waiting_user).kind,
            UserNotification.Kind.SPOT_OPEN,
        )

    def test_waitlist_offer_reserves_spot_for_fifo_user_until_claim_or_expiry(self):
        self.client.force_authenticate(self.player)
        self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        self.client.force_authenticate(self.other_player)
        self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 2},
            format="json",
        )
        first = User.objects.create_user(
            username="firstwaiter", email="first@example.com", password="test-password-123"
        )
        second = User.objects.create_user(
            username="secondwaiter", email="second@example.com", password="test-password-123"
        )
        waitlist_url = reverse("tournament-waitlist", args=[self.tournament.id])
        join_url = reverse("tournament-join", args=[self.tournament.id])
        self.client.force_authenticate(first)
        self.client.post(waitlist_url, format="json")
        self.client.force_authenticate(second)
        self.client.post(waitlist_url, format="json")

        self.client.force_authenticate(self.other_player)
        self.client.delete(reverse("tournament-leave", args=[self.tournament.id]))
        first_entry = TournamentWaitlist.objects.get(tournament=self.tournament, user=first)
        self.assertEqual(first_entry.offered_slot_number, 2)
        self.assertGreater(first_entry.offer_expires_at, timezone.now())

        late_joiner = User.objects.create_user(
            username="latejoiner", email="late@example.com", password="test-password-123"
        )
        self.client.force_authenticate(late_joiner)
        blocked = self.client.post(join_url, {"slot_number": 2}, format="json")
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reserved for the next waitlisted player", blocked.data["detail"])

        self.client.force_authenticate(first)
        claimed = self.client.post(join_url, {"slot_number": 2}, format="json")
        self.assertEqual(claimed.status_code, status.HTTP_201_CREATED)
        self.assertFalse(
            TournamentWaitlist.objects.filter(tournament=self.tournament, user=first).exists()
        )

    @override_settings(TOURNAMENT_PAYMENTS_ENABLED=False)
    def test_paid_tournament_creation_and_join_are_paused_until_host_payouts_exist(self):
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-list"),
            {
                "title": "Paid game",
                "sport_id": self.sport.id,
                "venue_id": self.venue.id,
                "country_id": self.country.code,
                "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
                "entry_fee": "5.00",
                "currency": "USD",
                "max_players": 4,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("organizer payouts", response.data["entry_fee"][0])

        self.client.force_authenticate(self.player)
        joined = self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        self.assertEqual(joined.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("organizer payouts", joined.data["detail"])

    @override_settings(TOURNAMENT_PAYMENTS_ENABLED=False, PAYSTACK_SECRET_KEY="sk_test_example")
    def test_late_paystack_success_does_not_allocate_a_spot_while_payments_are_disabled(self):
        participant = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PENDING,
            reservation_expires_at=timezone.now() + timedelta(minutes=10),
        )
        payment = PaystackTransaction.objects.create(
            participant=participant,
            reference="sc-disabled-payment",
            amount_minor=500,
            currency="USD",
        )
        event_body = json.dumps({
            "event": "charge.success",
            "data": {
                "reference": payment.reference,
                "status": "success",
                "amount": 500,
                "currency": "USD",
            },
        }).encode()
        signature = hmac.new(
            b"sk_test_example", event_body, hashlib.sha512
        ).hexdigest()
        response = self.client.post(
            reverse("paystack-webhook"),
            event_body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payment.refresh_from_db()
        participant.refresh_from_db()
        self.assertEqual(
            payment.status, PaystackTransaction.Status.SUCCESS_UNALLOCATED
        )
        self.assertEqual(
            participant.payment_status, TournamentParticipant.PaymentStatus.PENDING
        )

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    @patch("tournaments.views.urlopen")
    def test_player_can_leave_paid_entry_and_refund_before_24_hour_cutoff(self, mock_urlopen):
        participant = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PAID,
        )
        payment = PaystackTransaction.objects.create(
            participant=participant,
            reference="sc-refund-player",
            amount_minor=500,
            currency="USD",
            status=PaystackTransaction.Status.SUCCESS,
        )
        mock_urlopen.return_value = self.mock_paystack_response({"status": "processed"})
        self.client.force_authenticate(self.player)
        response = self.client.delete(
            reverse("tournament-leave", args=[self.tournament.id])
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        payment.refresh_from_db()
        self.assertEqual(
            payment.refund_status, PaystackTransaction.RefundStatus.PROCESSED
        )
        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(sent_payload["transaction"], payment.reference)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    @patch("tournaments.views.urlopen")
    def test_player_cannot_request_refund_within_24_hours(self, mock_urlopen):
        self.tournament.starts_at = timezone.now() + timedelta(hours=12)
        self.tournament.save(update_fields=["starts_at"])
        participant = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PAID,
        )
        PaystackTransaction.objects.create(
            participant=participant,
            reference="sc-refund-late",
            amount_minor=500,
            currency="USD",
            status=PaystackTransaction.Status.SUCCESS,
        )
        self.client.force_authenticate(self.player)
        response = self.client.delete(
            reverse("tournament-leave", args=[self.tournament.id])
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at least 24 hours", response.data["detail"])
        mock_urlopen.assert_not_called()

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    @patch("tournaments.views.urlopen")
    def test_host_cancellation_waits_for_full_paystack_refund(self, mock_urlopen):
        participant = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PAID,
        )
        payment = PaystackTransaction.objects.create(
            participant=participant,
            reference="sc-refund-host",
            amount_minor=500,
            currency="USD",
            status=PaystackTransaction.Status.SUCCESS,
        )
        mock_urlopen.return_value = self.mock_paystack_response({"status": "pending"})
        self.client.force_authenticate(self.host)
        cancel_url = reverse("tournament-cancel", args=[self.tournament.id])
        waiting = self.client.post(cancel_url, format="json")
        self.assertEqual(waiting.status_code, status.HTTP_400_BAD_REQUEST)
        self.tournament.refresh_from_db()
        self.assertNotEqual(self.tournament.status, Tournament.Status.CANCELLED)
        payment.refresh_from_db()
        self.assertEqual(payment.refund_status, PaystackTransaction.RefundStatus.PENDING)

        event_body = json.dumps({
            "event": "refund.processed",
            "data": {"transaction_reference": payment.reference},
        }).encode()
        signature = hmac.new(
            b"sk_test_example", event_body, hashlib.sha512
        ).hexdigest()
        self.client.post(
            reverse("paystack-webhook"),
            event_body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )
        payment.refresh_from_db()
        self.assertEqual(
            payment.refund_status, PaystackTransaction.RefundStatus.PROCESSED
        )
        mock_urlopen.return_value = self.mock_paystack_response({"status": "processed"})
        cancelled = self.client.post(cancel_url, format="json")
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, Tournament.Status.CANCELLED)

    @override_settings(PAYSTACK_SECRET_KEY="sk_test_example")
    def test_processed_player_refund_releases_spot_and_offers_it_to_waitlist(self):
        paid_entry = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PAID,
        )
        TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.other_player,
            slot_number=2,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.NOT_REQUIRED,
        )
        self.tournament.status = Tournament.Status.FULL
        self.tournament.save(update_fields=["status"])
        waiter = User.objects.create_user(
            username="refundwaiter",
            email="refundwaiter@example.com",
            password="test-password-123",
        )
        self.client.force_authenticate(waiter)
        self.client.post(
            reverse("tournament-waitlist", args=[self.tournament.id]),
            format="json",
        )
        payment = PaystackTransaction.objects.create(
            participant=paid_entry,
            reference="sc-refund-webhook-player",
            amount_minor=500,
            currency="USD",
            status=PaystackTransaction.Status.SUCCESS,
            refund_status=PaystackTransaction.RefundStatus.PENDING,
            release_on_refund=True,
        )
        event_body = json.dumps({
            "event": "refund.processed",
            "data": {"transaction_reference": payment.reference},
        }).encode()
        signature = hmac.new(
            b"sk_test_example", event_body, hashlib.sha512
        ).hexdigest()
        response = self.client.post(
            reverse("paystack-webhook"),
            event_body,
            content_type="application/json",
            HTTP_X_PAYSTACK_SIGNATURE=signature,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            TournamentParticipant.objects.filter(pk=paid_entry.pk).exists()
        )
        offered = TournamentWaitlist.objects.get(
            tournament=self.tournament, user=waiter
        )
        self.assertEqual(offered.offered_slot_number, 1)

    def test_host_cannot_change_price_after_a_player_pays(self):
        TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PAID,
        )
        self.client.force_authenticate(self.host)
        response = self.client.patch(
            reverse("tournament-detail", args=[self.tournament.id]),
            {"entry_fee": "8.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "cannot be changed after a player has paid",
            str(response.data["detail"]),
        )

    def test_free_join_confirms_immediately_without_payment_reservation(self):
        self.tournament.entry_fee = Decimal("0.00")
        self.tournament.save(update_fields=["entry_fee"])
        self.client.force_authenticate(self.player)
        response = self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["payment_status"],
            TournamentParticipant.PaymentStatus.NOT_REQUIRED,
        )
        self.assertIsNone(response.data["reservation_expires_at"])

    def test_participant_roster_includes_public_player_card_fields(self):
        self.client.force_authenticate(self.player)
        joined = self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        self.assertEqual(joined.status_code, status.HTTP_201_CREATED)
        roster = self.client.get(reverse("tournament-participants", args=[self.tournament.id]))
        self.assertEqual(roster.status_code, status.HTTP_200_OK)
        self.assertEqual(roster.data[0]["user_id"], self.player.id)
        self.assertEqual(roster.data[0]["username"], self.player.username)
        self.assertEqual(roster.data[0]["slot_number"], 1)

    def test_host_records_attendance_and_player_can_dispute_no_show_report(self):
        player_entry = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.NOT_REQUIRED,
        )
        attended_entry = TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.other_player,
            slot_number=2,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.NOT_REQUIRED,
        )
        self.tournament.starts_at = timezone.now() - timedelta(hours=3)
        self.tournament.save(update_fields=["starts_at"])
        self.client.force_authenticate(self.host)
        completed = self.client.post(
            reverse("tournament-complete", args=[self.tournament.id]), format="json"
        )
        self.assertEqual(completed.status_code, status.HTTP_200_OK)

        attendance = self.client.post(
            reverse("tournament-attendance", args=[self.tournament.id]),
            {
                "participants": [
                    {
                        "participant_id": player_entry.id,
                        "attendance_status": TournamentParticipant.AttendanceStatus.NO_SHOW,
                    },
                    {
                        "participant_id": attended_entry.id,
                        "attendance_status": TournamentParticipant.AttendanceStatus.ATTENDED,
                    },
                ]
            },
            format="json",
        )
        self.assertEqual(attendance.status_code, status.HTTP_200_OK, attendance.data)

        self.client.force_authenticate(self.player)
        confirmed = self.client.post(
            reverse("tournament-confirm-attendance", args=[self.tournament.id]),
            format="json",
        )
        self.assertEqual(confirmed.status_code, status.HTTP_200_OK, confirmed.data)
        player_entry.refresh_from_db()
        self.assertTrue(player_entry.attendance_disputed)
        self.assertTrue(player_entry.attendance_confirmed)

        profile = self.client.get(reverse("my-player-profile"))
        self.assertEqual(profile.data["reliability"]["games_attended"], 0)
        self.assertEqual(profile.data["reliability"]["no_shows_reported"], 0)
        self.assertEqual(profile.data["reliability"]["attendance_disputes"], 1)
        self.client.force_authenticate(self.other_player)
        attended_profile = self.client.get(reverse("my-player-profile"))
        self.assertEqual(attended_profile.data["reliability"]["games_attended"], 1)

    def test_host_last_minute_cancellation_is_recorded(self):
        self.tournament.starts_at = timezone.now() + timedelta(hours=12)
        self.tournament.save(update_fields=["starts_at"])
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-cancel", args=[self.tournament.id]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.tournament.refresh_from_db()
        self.assertIsNotNone(self.tournament.cancelled_at)
        self.client.force_authenticate(self.host)
        profile = self.client.get(reverse("my-player-profile"))
        self.assertEqual(profile.data["reliability"]["hosted_cancelled"], 1)
        self.assertEqual(profile.data["reliability"]["hosted_last_minute_cancellations"], 1)

    def test_host_can_announce_to_players_and_cancel_with_notifications(self):
        self.tournament.entry_fee = Decimal("0.00")
        self.tournament.save(update_fields=["entry_fee"])
        self.client.force_authenticate(self.player)
        self.client.post(
            reverse("tournament-join", args=[self.tournament.id]),
            {"slot_number": 1},
            format="json",
        )
        self.client.force_authenticate(self.host)
        message_url = reverse("tournament-messages", args=[self.tournament.id])
        message = self.client.post(message_url, {"body": "Bring both jerseys."}, format="json")
        self.assertEqual(message.status_code, status.HTTP_201_CREATED)
        self.assertEqual(TournamentMessage.objects.count(), 1)
        self.assertEqual(
            UserNotification.objects.get(user=self.player).kind,
            UserNotification.Kind.HOST_ANNOUNCEMENT,
        )
        cancelled = self.client.post(
            reverse("tournament-cancel", args=[self.tournament.id]), format="json"
        )
        self.assertEqual(cancelled.status_code, status.HTTP_200_OK)
        self.tournament.refresh_from_db()
        self.assertEqual(self.tournament.status, Tournament.Status.CANCELLED)
        self.assertEqual(UserNotification.objects.filter(user=self.player).count(), 2)

    def test_player_profile_supports_sports_and_completed_game_history(self):
        self.client.force_authenticate(self.player)
        endpoint = reverse("my-player-profile")
        response = self.client.patch(
            endpoint,
            {
                "first_name": "Jordan",
                "bio": "Weekend basketball player.",
                "preferred_sport_ids": [self.sport.id],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["first_name"], "Jordan")
        self.assertEqual(response.data["preferred_sports"][0]["slug"], "basketball")

        TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.NOT_REQUIRED,
        )
        self.tournament.starts_at = timezone.now() - timedelta(hours=2)
        self.tournament.save(update_fields=["starts_at"])
        self.client.force_authenticate(self.host)
        completed = self.client.post(
            reverse("tournament-complete", args=[self.tournament.id]), format="json"
        )
        self.assertEqual(completed.status_code, status.HTTP_200_OK)
        self.client.force_authenticate(self.player)
        history = self.client.get(endpoint)
        self.assertEqual(history.data["games_played"], 1)
        self.assertEqual(history.data["completed_games"][0]["id"], self.tournament.id)

    def test_player_can_report_tournament_or_another_player(self):
        self.client.force_authenticate(self.player)
        game_report = self.client.post(
            reverse("tournament-report"),
            {"tournament": self.tournament.id, "reason": "spam", "details": "Misleading details."},
            format="json",
        )
        self.assertEqual(game_report.status_code, status.HTTP_201_CREATED)
        player_report = self.client.post(
            reverse("tournament-report"),
            {"reported_player": self.other_player.id, "reason": "abuse"},
            format="json",
        )
        self.assertEqual(player_report.status_code, status.HTTP_201_CREATED)
        invalid = self.client.post(
            reverse("tournament-report"),
            {"tournament": self.tournament.id, "reported_player": self.other_player.id, "reason": "other"},
            format="json",
        )
        self.assertEqual(invalid.status_code, status.HTTP_400_BAD_REQUEST)

    def test_host_cannot_cancel_after_player_payment_without_refund(self):
        TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PAID,
        )
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-cancel", args=[self.tournament.id]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.tournament.refresh_from_db()
        self.assertNotEqual(self.tournament.status, Tournament.Status.CANCELLED)

    def test_host_cannot_cancel_while_paid_reservation_is_pending(self):
        TournamentParticipant.objects.create(
            tournament=self.tournament,
            user=self.player,
            slot_number=1,
            entry_fee_at_join="5.00",
            payment_status=TournamentParticipant.PaymentStatus.PENDING,
            reservation_expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.client.force_authenticate(self.host)
        response = self.client.post(
            reverse("tournament-cancel", args=[self.tournament.id]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unpaid spot reservations", response.data["detail"])
