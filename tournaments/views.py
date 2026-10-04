from datetime import timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request as HttpRequest, urlopen

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    Country, PaystackTransaction, PlayerProfile, Sport, Tournament,
    TournamentMessage, TournamentParticipant, TournamentReport, TournamentWaitlist,
    UserNotification, Venue, WaitlistSignup,
)
from .permissions import IsTournamentHostOrReadOnly
from .serializers import (
    JoinTournamentSerializer,
    CountrySerializer,
    PlayerProfileSerializer,
    RegisterSerializer,
    SportSerializer,
    TournamentMessageSerializer,
    TournamentParticipantSerializer,
    TournamentReportSerializer,
    TournamentSerializer,
    TournamentWaitlistSerializer,
    UserNotificationSerializer,
    UserSerializer,
    VenueSerializer,
    WaitlistSignupSerializer,
)

User = get_user_model()
RESERVATION_MINUTES = 15
PAYSTACK_CURRENCIES = {"GHS", "KES", "NGN", "USD", "ZAR"}


class PaymentServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Paystack is not configured on the server."
    default_code = "payment_service_unavailable"


def paystack_request(path, payload=None):
    secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
    if not secret_key:
        raise PaymentServiceUnavailable()

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = HttpRequest(
        f"https://api.paystack.co/{path.lstrip('/')}",
        data=body,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise APIException("Could not reach Paystack. Try again shortly.") from error
    if not result.get("status"):
        raise APIException(result.get("message") or "Paystack could not process this payment.")
    return result.get("data", {})


def finalize_paystack_payment(payment, transaction_data):
    try:
        valid_amount = int(transaction_data.get("amount")) == payment.amount_minor
    except (TypeError, ValueError):
        valid_amount = False
    if (
        transaction_data.get("status") != "success"
        or transaction_data.get("reference") != payment.reference
        or transaction_data.get("currency", "").upper() != payment.currency
        or not valid_amount
    ):
        payment.status = PaystackTransaction.Status.FAILED
        payment.save(update_fields=["status", "updated_at"])
        return False

    with transaction.atomic():
        payment = PaystackTransaction.objects.select_for_update().get(pk=payment.pk)
        if payment.status == PaystackTransaction.Status.SUCCESS:
            return True
        participant = payment.participant
        if participant is None:
            payment.status = PaystackTransaction.Status.SUCCESS_UNALLOCATED
            payment.save(update_fields=["status", "updated_at"])
            return False

        participant = TournamentParticipant.objects.select_for_update().get(pk=participant.pk)
        tournament = Tournament.objects.select_for_update().get(pk=participant.tournament_id)
        if (
            participant.payment_status != TournamentParticipant.PaymentStatus.PENDING
            or participant.reservation_expires_at is None
            or participant.reservation_expires_at <= timezone.now()
        ):
            payment.status = PaystackTransaction.Status.SUCCESS_UNALLOCATED
            payment.save(update_fields=["status", "updated_at"])
            return False

        participant.payment_status = TournamentParticipant.PaymentStatus.PAID
        participant.reservation_expires_at = None
        participant.save(update_fields=["payment_status", "reservation_expires_at"])
        payment.status = PaystackTransaction.Status.SUCCESS
        payment.save(update_fields=["status", "updated_at"])
        if tournament.active_participants().count() >= tournament.max_players:
            tournament.status = Tournament.Status.FULL
            tournament.save(update_fields=["status", "updated_at"])
    return True


def notify_waitlist_head(tournament):
    waiting = tournament.waitlist_entries.select_related("user").first()
    if waiting:
        UserNotification.objects.create(
            user=waiting.user,
            tournament=tournament,
            kind=UserNotification.Kind.SPOT_OPEN,
            message=f"A spot opened in {tournament.title}. Claim it before it is taken.",
        )


def notify_participants(tournament, kind, message):
    user_ids = tournament.active_participants().exclude(user=tournament.host).values_list("user_id", flat=True)
    UserNotification.objects.bulk_create([
        UserNotification(user_id=user_id, tournament=tournament, kind=kind, message=message)
        for user_id in user_ids
    ])


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "user": UserSerializer(user).data,
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_201_CREATED,
        )


class WaitlistSignupView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "waitlist"

    def post(self, request):
        serializer = WaitlistSignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        _, created = WaitlistSignup.objects.get_or_create(
            email=serializer.validated_data["email"],
            defaults={"interest": serializer.validated_data["interest"]},
        )
        return Response(
            {"message": "You're on the SportsClan waitlist."},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CurrentUserView(RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class MyPlayerProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get_profile(self, request):
        profile, _ = PlayerProfile.objects.get_or_create(user=request.user)
        return profile

    def get(self, request):
        profile = self.get_profile(request)
        data = PlayerProfileSerializer(profile).data
        data["games_played"] = request.user.tournament_entries.filter(
            payment_status__in=[
                TournamentParticipant.PaymentStatus.PAID,
                TournamentParticipant.PaymentStatus.NOT_REQUIRED,
            ],
            tournament__status=Tournament.Status.COMPLETED,
        ).count()
        completed = Tournament.objects.filter(
            participants__user=request.user,
            status=Tournament.Status.COMPLETED,
            participants__payment_status__in=[
                TournamentParticipant.PaymentStatus.PAID,
                TournamentParticipant.PaymentStatus.NOT_REQUIRED,
            ],
        ).select_related("host", "sport", "venue", "country").distinct()
        data["completed_games"] = TournamentSerializer(
            completed, many=True, context={"request": request}
        ).data
        return Response(data)

    def patch(self, request):
        profile = self.get_profile(request)
        serializer = PlayerProfileSerializer(profile, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self.get(request).data)


class PublicPlayerProfileView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, username):
        player = get_object_or_404(User, username=username)
        profile = PlayerProfile.objects.filter(user=player).first()
        data = PlayerProfileSerializer(profile).data if profile else {
            "username": player.username,
            "first_name": player.first_name,
            "last_name": player.last_name,
            "bio": "",
            "preferred_sports": [],
        }
        data["games_played"] = player.tournament_entries.filter(
            payment_status__in=[
                TournamentParticipant.PaymentStatus.PAID,
                TournamentParticipant.PaymentStatus.NOT_REQUIRED,
            ],
            tournament__status=Tournament.Status.COMPLETED,
        ).count()
        completed = Tournament.objects.filter(
            participants__user=player,
            status=Tournament.Status.COMPLETED,
            participants__payment_status__in=[
                TournamentParticipant.PaymentStatus.PAID,
                TournamentParticipant.PaymentStatus.NOT_REQUIRED,
            ],
        ).select_related("host", "sport", "venue", "country").distinct()
        data["completed_games"] = TournamentSerializer(
            completed, many=True, context={"request": request}
        ).data
        return Response(data)


class NotificationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = UserNotification.objects.filter(user=request.user)[:50]
        return Response(UserNotificationSerializer(notifications, many=True).data)

    def post(self, request):
        UserNotification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"detail": "Notifications marked as read."})


class TournamentReportView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "reports"

    def post(self, request):
        serializer = TournamentReportSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        report = serializer.save(reporter=request.user)
        return Response(
            {"id": report.id, "detail": "Thanks. Your report has been sent to the SportsClan team."},
            status=status.HTTP_201_CREATED,
        )


class SportViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Sport.objects.filter(is_active=True)
    serializer_class = SportSerializer
    permission_classes = [AllowAny]
    lookup_field = "slug"
    search_fields = ["name"]
    ordering_fields = ["name", "sort_order"]


class CountryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Country.objects.select_related("default_currency").prefetch_related(
        "currency_options__currency"
    )
    serializer_class = CountrySerializer
    permission_classes = [AllowAny]
    lookup_field = "code"
    search_fields = ["code", "name"]
    ordering_fields = ["code", "name"]


class VenueViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Venue.objects.filter(is_active=True).select_related(
        "country_reference", "country_reference__default_currency"
    ).prefetch_related("sports", "country_reference__currency_options__currency")
    serializer_class = VenueSerializer
    permission_classes = [AllowAny]
    filterset_fields = ["city", "region", "country", "country_reference__code", "sports__slug"]
    search_fields = ["name", "address", "city", "region"]
    ordering_fields = ["name", "city"]


class TournamentViewSet(viewsets.ModelViewSet):
    serializer_class = TournamentSerializer
    permission_classes = [IsTournamentHostOrReadOnly]
    filterset_fields = ["status", "sport__slug", "venue__city", "country__code"]
    search_fields = ["title", "description", "venue__name", "venue__city"]
    ordering_fields = ["starts_at", "entry_fee", "created_at"]
    ordering = ["starts_at"]

    def get_queryset(self):
        queryset = (
            Tournament.objects.select_related(
                "host", "sport", "venue", "country", "country__default_currency",
                "venue__country_reference",
            ).prefetch_related(
                "venue__sports", "venue__country_reference__currency_options__currency", "participants"
            )
        )
        starts_after = self.request.query_params.get("starts_after")
        starts_before = self.request.query_params.get("starts_before")
        if starts_after:
            queryset = queryset.filter(starts_at__gte=starts_after)
        if starts_before:
            queryset = queryset.filter(starts_at__lte=starts_before)
        if self.action == "mine" and self.request.user.is_authenticated:
            queryset = queryset.filter(
                Q(host=self.request.user) | Q(participants__user=self.request.user)
            ).distinct()
        return queryset

    def get_permissions(self):
        if self.action in {
            "join", "leave", "mine", "payment_status", "payment_initialize",
            "payment_verify", "waitlist", "messages",
        }:
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_create(self, serializer):
        with transaction.atomic():
            venue = serializer.validated_data.get("venue") or serializer.create_custom_venue()
            venue_fee = Decimal("0.00") if venue.pricing_type == Venue.PricingType.FREE else None
            venue_fee_status = (
                Tournament.VenueFeeStatus.NOT_REQUIRED
                if venue.pricing_type == Venue.PricingType.FREE
                else Tournament.VenueFeeStatus.PENDING
            )
            serializer.save(
                host=self.request.user,
                venue_pricing_type_snapshot=venue.pricing_type,
                venue_rate_snapshot=venue.price_amount,
                venue_rate_currency_snapshot=venue.price_currency,
                venue_fee=venue_fee,
                venue_fee_status=venue_fee_status,
            )

    def perform_update(self, serializer):
        venue = serializer.validated_data.get("venue")
        if venue is None or venue.pk == serializer.instance.venue_id:
            serializer.save()
            return

        snapshot = {
            "venue_pricing_type_snapshot": venue.pricing_type,
            "venue_rate_snapshot": venue.price_amount,
            "venue_rate_currency_snapshot": venue.price_currency,
        }
        if venue.pricing_type == Venue.PricingType.FREE:
            snapshot.update(
                venue_fee=Decimal("0.00"),
                venue_fee_status=Tournament.VenueFeeStatus.NOT_REQUIRED,
            )
        elif "venue_fee" not in serializer.validated_data:
            snapshot.update(
                venue_fee=None,
                venue_fee_status=Tournament.VenueFeeStatus.PENDING,
            )
        serializer.save(**snapshot)

    @action(detail=False, methods=["get"])
    def mine(self, request):
        page = self.paginate_queryset(self.get_queryset())
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    @action(detail=True, methods=["get"])
    def participants(self, request, pk=None):
        tournament = self.get_object()
        participants = tournament.active_participants().select_related("user").order_by("slot_number")
        return Response(TournamentParticipantSerializer(participants, many=True).data)

    @action(detail=True, methods=["get", "post", "delete"])
    def waitlist(self, request, pk=None):
        tournament = self.get_object()
        if request.method == "GET":
            entry = tournament.waitlist_entries.filter(user=request.user).first()
            return Response(
                TournamentWaitlistSerializer(entry).data if entry else {"joined": False}
            )
        if request.method == "DELETE":
            tournament.waitlist_entries.filter(user=request.user).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        if tournament.status not in {Tournament.Status.FULL, Tournament.Status.OPEN}:
            raise ValidationError({"detail": "This tournament is not accepting waitlist requests."})
        if tournament.slots_open > 0:
            raise ValidationError({"detail": "This tournament still has open spots. Join directly."})
        if tournament.starts_at <= timezone.now():
            raise ValidationError({"detail": "This tournament has already started."})
        if tournament.active_participants().filter(user=request.user).exists():
            raise ValidationError({"detail": "You already have a spot in this tournament."})
        entry, created = TournamentWaitlist.objects.get_or_create(
            tournament=tournament, user=request.user
        )
        return Response(
            TournamentWaitlistSerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get", "post"])
    def messages(self, request, pk=None):
        tournament = self.get_object()
        is_host = tournament.host_id == request.user.id
        is_participant = tournament.active_participants().filter(user=request.user).exists()
        if not is_host and not is_participant:
            raise ValidationError({"detail": "Only the host and joined players can view game messages."})
        if request.method == "GET":
            messages = tournament.messages.select_related("sender")[:100]
            return Response(TournamentMessageSerializer(messages, many=True).data)
        if not is_host:
            raise ValidationError({"detail": "Only the host can send a tournament announcement."})
        serializer = TournamentMessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = serializer.save(tournament=tournament, sender=request.user)
        notify_participants(
            tournament,
            UserNotification.Kind.HOST_ANNOUNCEMENT,
            f"Update from {request.user.username}: {message.body[:350]}",
        )
        return Response(TournamentMessageSerializer(message).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        tournament = self.get_object()
        if tournament.status in {Tournament.Status.CANCELLED, Tournament.Status.COMPLETED}:
            raise ValidationError({"detail": "This tournament can no longer be cancelled."})
        if tournament.active_participants().filter(
            payment_status=TournamentParticipant.PaymentStatus.PAID
        ).exists():
            raise ValidationError(
                {"detail": "Refund paid entries before cancelling this tournament."}
            )
        tournament.status = Tournament.Status.CANCELLED
        tournament.save(update_fields=["status", "updated_at"])
        notify_participants(
            tournament,
            UserNotification.Kind.TOURNAMENT_CANCELLED,
            f"{tournament.title} has been cancelled by the host.",
        )
        waitlisted_user_ids = tournament.waitlist_entries.values_list("user_id", flat=True)
        UserNotification.objects.bulk_create([
            UserNotification(
                user_id=user_id,
                tournament=tournament,
                kind=UserNotification.Kind.TOURNAMENT_CANCELLED,
                message=f"{tournament.title} has been cancelled by the host.",
            )
            for user_id in waitlisted_user_ids
        ])
        tournament.waitlist_entries.all().delete()
        return Response(self.get_serializer(tournament).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        tournament = self.get_object()
        if tournament.status in {Tournament.Status.CANCELLED, Tournament.Status.COMPLETED}:
            raise ValidationError({"detail": "This tournament cannot be marked completed."})
        ends_at = tournament.starts_at + timedelta(minutes=tournament.duration_minutes)
        if ends_at > timezone.now():
            raise ValidationError({"detail": "A game can be completed after its scheduled end time."})
        tournament.status = Tournament.Status.COMPLETED
        tournament.save(update_fields=["status", "updated_at"])
        notify_participants(
            tournament,
            UserNotification.Kind.HOST_ANNOUNCEMENT,
            f"{tournament.title} has been marked as completed.",
        )
        return Response(self.get_serializer(tournament).data)

    @action(detail=True, methods=["post"])
    def join(self, request, pk=None):
        input_serializer = JoinTournamentSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        slot_number = input_serializer.validated_data["slot_number"]

        try:
            with transaction.atomic():
                tournament = get_object_or_404(
                    Tournament.objects.select_for_update(), pk=pk
                )
                now = timezone.now()
                TournamentParticipant.objects.filter(
                    tournament=tournament,
                    payment_status=TournamentParticipant.PaymentStatus.PENDING,
                    reservation_expires_at__lte=now,
                ).delete()
                active_count = tournament.active_participants().count()
                if tournament.status == Tournament.Status.FULL and active_count < tournament.max_players:
                    tournament.status = Tournament.Status.OPEN
                    tournament.save(update_fields=["status", "updated_at"])
                if tournament.status != Tournament.Status.OPEN:
                    raise ValidationError({"detail": "This tournament is not open."})
                if tournament.starts_at <= timezone.now():
                    raise ValidationError({"detail": "This tournament has already started."})
                if slot_number > tournament.max_players:
                    raise ValidationError(
                        {"slot_number": "Choose a slot within the available player spots."}
                    )
                if tournament.active_participants().filter(user=request.user).exists():
                    raise ValidationError({"detail": "You already joined this tournament."})
                if active_count >= tournament.max_players:
                    raise ValidationError({"detail": "All player spots are taken."})
                if tournament.active_participants().filter(slot_number=slot_number).exists():
                    raise ValidationError({"slot_number": "That slot has already been taken."})
                is_free = tournament.entry_fee == Decimal("0.00")
                participant = TournamentParticipant.objects.create(
                    tournament=tournament,
                    user=request.user,
                    slot_number=slot_number,
                    entry_fee_at_join=tournament.entry_fee,
                    payment_status=(
                        TournamentParticipant.PaymentStatus.NOT_REQUIRED
                        if is_free
                        else TournamentParticipant.PaymentStatus.PENDING
                    ),
                    reservation_expires_at=(
                        None if is_free else now + timedelta(minutes=RESERVATION_MINUTES)
                    ),
                )
                tournament.waitlist_entries.filter(user=request.user).delete()
                if tournament.active_participants().count() >= tournament.max_players:
                    tournament.status = Tournament.Status.FULL
                    tournament.save(update_fields=["status", "updated_at"])
        except IntegrityError as error:
            raise ValidationError(
                {"slot_number": "That slot has already been taken."}
            ) from error

        return Response(
            TournamentParticipantSerializer(participant).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="payment-status")
    def payment_status(self, request, pk=None):
        tournament = self.get_object()
        participant = get_object_or_404(
            TournamentParticipant.objects.filter(tournament=tournament, user=request.user)
        )
        latest_payment = participant.paystack_transactions.first()
        expired = (
            participant.reservation_expires_at is not None
            and participant.reservation_expires_at <= timezone.now()
            and participant.payment_status == TournamentParticipant.PaymentStatus.PENDING
        )
        return Response(
            {
                "payment_status": participant.payment_status,
                "reservation_expires_at": participant.reservation_expires_at,
                "reservation_expired": expired,
                "slot_number": participant.slot_number,
                "entry_fee": str(participant.entry_fee_at_join),
                "currency": tournament.currency,
                "reference": latest_payment.reference if latest_payment else None,
                "transaction_status": latest_payment.status if latest_payment else None,
                "authorization_url": latest_payment.authorization_url if latest_payment else None,
            }
        )

    @action(detail=True, methods=["post"], url_path="payment-initialize")
    def payment_initialize(self, request, pk=None):
        tournament = self.get_object()
        participant = get_object_or_404(
            TournamentParticipant.objects.select_related("user", "tournament").filter(
                tournament=tournament, user=request.user
            )
        )
        if participant.payment_status == TournamentParticipant.PaymentStatus.NOT_REQUIRED:
            return Response({"payment_status": participant.payment_status, "amount": "0.00"})
        if participant.payment_status == TournamentParticipant.PaymentStatus.PAID:
            return Response({"payment_status": participant.payment_status})
        if participant.payment_status != TournamentParticipant.PaymentStatus.PENDING:
            raise ValidationError({"detail": "This entry cannot be paid."})

        now = timezone.now()
        if participant.reservation_expires_at is None:
            participant.reservation_expires_at = now + timedelta(minutes=RESERVATION_MINUTES)
            participant.save(update_fields=["reservation_expires_at"])
        elif participant.reservation_expires_at <= now:
            raise ValidationError({"detail": "This slot reservation has expired. Choose an available slot again."})

        currency = tournament.currency.upper()
        if currency not in PAYSTACK_CURRENCIES:
            raise ValidationError({"currency": f"Paystack checkout is not enabled for {currency}."})
        if not participant.user.email:
            raise ValidationError({"email": "Add an email address to your account before paying."})

        existing = participant.paystack_transactions.filter(
            status=PaystackTransaction.Status.PENDING,
            authorization_url__gt="",
        ).first()
        if existing:
            return Response(
                {
                    "reference": existing.reference,
                    "authorization_url": existing.authorization_url,
                    "payment_status": existing.status,
                    "amount": str(participant.entry_fee_at_join),
                    "currency": currency,
                    "reservation_expires_at": participant.reservation_expires_at,
                }
            )

        amount_minor = int((participant.entry_fee_at_join * 100).quantize(Decimal("1")))
        reference = f"sc-{participant.id}-{secrets.token_hex(12)}"
        payment = PaystackTransaction.objects.create(
            participant=participant,
            reference=reference,
            amount_minor=amount_minor,
            currency=currency,
        )
        paystack_payload = {
            "email": participant.user.email,
            "amount": str(amount_minor),
            "currency": currency,
            "reference": reference,
            "metadata": {
                "tournament_id": tournament.id,
                "participant_id": participant.id,
                "slot_number": participant.slot_number,
            },
        }
        callback_url = getattr(settings, "PAYSTACK_CALLBACK_URL", "")
        if callback_url:
            paystack_payload["callback_url"] = callback_url

        try:
            data = paystack_request("transaction/initialize", paystack_payload)
        except APIException:
            payment.status = PaystackTransaction.Status.FAILED
            payment.save(update_fields=["status", "updated_at"])
            raise
        authorization_url = data.get("authorization_url")
        if not authorization_url:
            payment.status = PaystackTransaction.Status.FAILED
            payment.save(update_fields=["status", "updated_at"])
            raise APIException("Paystack did not return a checkout URL.")
        payment.authorization_url = authorization_url
        payment.save(update_fields=["authorization_url", "updated_at"])
        return Response(
            {
                "reference": payment.reference,
                "authorization_url": payment.authorization_url,
                "payment_status": payment.status,
                "amount": str(participant.entry_fee_at_join),
                "currency": currency,
                "reservation_expires_at": participant.reservation_expires_at,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="payment-verify")
    def payment_verify(self, request, pk=None):
        tournament = self.get_object()
        participant = get_object_or_404(
            TournamentParticipant.objects.filter(tournament=tournament, user=request.user)
        )
        reference = request.data.get("reference", "")
        payment = get_object_or_404(
            PaystackTransaction.objects.filter(participant=participant, reference=reference)
        )
        data = paystack_request(f"transaction/verify/{payment.reference}")
        if data.get("status") != "success":
            return Response(
                {"payment_status": data.get("status", "pending"), "detail": "Paystack has not confirmed this payment yet."},
                status=status.HTTP_409_CONFLICT,
            )
        allocated = finalize_paystack_payment(payment, data)
        payment.refresh_from_db()
        if not allocated:
            return Response(
                {
                    "payment_status": payment.status,
                    "detail": "Payment succeeded after the slot reservation expired. Contact support for a refund.",
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response({"payment_status": TournamentParticipant.PaymentStatus.PAID})

    @action(detail=True, methods=["delete"])
    def leave(self, request, pk=None):
        with transaction.atomic():
            tournament = get_object_or_404(
                Tournament.objects.select_for_update(), pk=pk
            )
            participant = get_object_or_404(
                TournamentParticipant, tournament=tournament, user=request.user
            )
            if participant.payment_status == TournamentParticipant.PaymentStatus.PAID:
                raise ValidationError(
                    {"detail": "Paid entries need to be refunded before leaving."}
                )
            participant.delete()
            if tournament.status == Tournament.Status.FULL:
                tournament.status = Tournament.Status.OPEN
                tournament.save(update_fields=["status", "updated_at"])
                notify_waitlist_head(tournament)
        return Response(status=status.HTTP_204_NO_CONTENT)


class PaystackWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret_key = getattr(settings, "PAYSTACK_SECRET_KEY", "")
        signature = request.headers.get("x-paystack-signature", "")
        expected_signature = hmac.new(
            secret_key.encode("utf-8"), request.body, hashlib.sha512
        ).hexdigest()
        if not secret_key or not hmac.compare_digest(signature, expected_signature):
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            event = json.loads(request.body)
        except json.JSONDecodeError:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        if event.get("event") != "charge.success":
            return Response({"received": True})

        reference = event.get("data", {}).get("reference")
        payment = PaystackTransaction.objects.filter(reference=reference).first()
        if payment:
            finalize_paystack_payment(payment, event["data"])
        return Response({"received": True})

# Create your views here.
