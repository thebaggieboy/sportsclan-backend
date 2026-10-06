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
from django.db.models import F, Q
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
    AttendanceUpdateSerializer,
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
WAITLIST_OFFER_MINUTES = 15
PLAYER_REFUND_CUTOFF_HOURS = 24
PAYSTACK_CURRENCIES = {"GHS", "KES", "NGN", "USD", "ZAR"}


def player_reliability_summary(user):
    attendance = user.tournament_entries.filter(
        tournament__status=Tournament.Status.COMPLETED
    )
    hosted = Tournament.objects.filter(host=user)
    return {
        "games_attended": attendance.filter(
            attendance_status=TournamentParticipant.AttendanceStatus.ATTENDED,
            attendance_disputed=False,
        ).count(),
        "no_shows_reported": attendance.filter(
            attendance_status=TournamentParticipant.AttendanceStatus.NO_SHOW,
            attendance_disputed=False,
        ).count(),
        "attendance_disputes": attendance.filter(attendance_disputed=True).count(),
        "hosted_completed": hosted.filter(status=Tournament.Status.COMPLETED).count(),
        "hosted_cancelled": hosted.filter(status=Tournament.Status.CANCELLED).count(),
        "hosted_last_minute_cancellations": hosted.filter(
            status=Tournament.Status.CANCELLED,
            cancelled_at__isnull=False,
            cancelled_at__gte=F("starts_at") - timedelta(hours=24),
        ).count(),
    }


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


def request_paystack_refund(payment, release_entry=False):
    with transaction.atomic():
        payment = PaystackTransaction.objects.select_for_update().get(pk=payment.pk)
        if payment.refund_status == PaystackTransaction.RefundStatus.PROCESSED:
            return payment.refund_status
        if payment.refund_status == PaystackTransaction.RefundStatus.PENDING:
            if release_entry and not payment.release_on_refund:
                payment.release_on_refund = True
                payment.save(update_fields=["release_on_refund", "updated_at"])
            return payment.refund_status
        if payment.status != PaystackTransaction.Status.SUCCESS:
            raise ValidationError({"detail": "A successful Paystack payment is required before requesting a refund."})

        payment.refund_status = PaystackTransaction.RefundStatus.PENDING
        payment.release_on_refund = payment.release_on_refund or release_entry
        payment.save(
            update_fields=["refund_status", "release_on_refund", "updated_at"]
        )
    try:
        data = paystack_request(
            "refund/",
            {
                "transaction": payment.reference,
                "customer_note": "Tournament cancelled or player withdrew within the refund period.",
                "merchant_note": "SportsClan tournament entry refund.",
            },
        )
    except APIException as error:
        raise APIException(
            "Paystack could not confirm the refund request. The entry is held until its refund status is confirmed."
        ) from error

    provider_status = data.get("status")
    rejected = False
    with transaction.atomic():
        payment = PaystackTransaction.objects.select_for_update().get(pk=payment.pk)
        if payment.refund_status == PaystackTransaction.RefundStatus.PROCESSED:
            return payment.refund_status
        if provider_status in {"processed", "success"}:
            payment.refund_status = PaystackTransaction.RefundStatus.PROCESSED
            payment.save(update_fields=["refund_status", "updated_at"])
            participant = payment.participant
            if participant:
                participant.payment_status = TournamentParticipant.PaymentStatus.REFUNDED
                participant.save(update_fields=["payment_status"])
        elif provider_status in {"pending", "processing"}:
            payment.refund_status = PaystackTransaction.RefundStatus.PENDING
            payment.save(update_fields=["refund_status", "updated_at"])
        else:
            payment.refund_status = PaystackTransaction.RefundStatus.FAILED
            payment.save(update_fields=["refund_status", "updated_at"])
            rejected = True
        result = payment.refund_status
    if rejected:
        raise APIException("Paystack did not accept the refund request.")
    return result


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
        if not settings.TOURNAMENT_PAYMENTS_ENABLED:
            payment.status = PaystackTransaction.Status.SUCCESS_UNALLOCATED
            payment.save(update_fields=["status", "updated_at"])
            return False
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
    now = timezone.now()
    if (
        tournament.status in {
            Tournament.Status.CANCELLING,
            Tournament.Status.CANCELLED,
            Tournament.Status.COMPLETED,
        }
        or tournament.starts_at <= now
        or tournament.slots_open == 0
        or (
            tournament.entry_fee > Decimal("0.00")
            and not settings.TOURNAMENT_PAYMENTS_ENABLED
        )
    ):
        return

    expired_offers = tournament.waitlist_entries.filter(notified_at__isnull=False).filter(
        Q(offer_expires_at__lte=now) | Q(offer_expires_at__isnull=True)
    )
    expired_offers.update(
        notified_at=None,
        offered_slot_number=None,
        offer_expires_at=None,
        joined_at=now,
    )
    active_offer = tournament.waitlist_entries.filter(offer_expires_at__gt=now).first()
    if active_offer:
        return

    occupied_slots = set(
        tournament.active_participants().values_list("slot_number", flat=True)
    )
    offered_slot = next(
        (slot for slot in range(1, tournament.max_players + 1) if slot not in occupied_slots),
        None,
    )
    if offered_slot is None:
        return

    waiting = tournament.waitlist_entries.select_related("user").filter(
        notified_at__isnull=True
    ).first()
    if waiting:
        waiting.notified_at = now
        waiting.offered_slot_number = offered_slot
        waiting.offer_expires_at = now + timedelta(minutes=WAITLIST_OFFER_MINUTES)
        waiting.save(
            update_fields=["notified_at", "offered_slot_number", "offer_expires_at"]
        )
        UserNotification.objects.create(
            user=waiting.user,
            tournament=tournament,
            kind=UserNotification.Kind.SPOT_OPEN,
            message=(
                f"A spot opened in {tournament.title}. You have 15 minutes to claim "
                f"spot {offered_slot}."
            ),
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
        data["reliability"] = player_reliability_summary(request.user)
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
        data["reliability"] = player_reliability_summary(player)
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
            "payment_verify", "waitlist", "messages", "confirm_attendance",
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

    @action(detail=True, methods=["post"])
    def attendance(self, request, pk=None):
        tournament = self.get_object()
        if tournament.status != Tournament.Status.COMPLETED:
            raise ValidationError({"detail": "Attendance can be recorded after the game is completed."})
        serializer = AttendanceUpdateSerializer(data=request.data.get("participants"), many=True)
        serializer.is_valid(raise_exception=True)
        updates = serializer.validated_data
        participant_ids = [update["participant_id"] for update in updates]
        if len(participant_ids) != len(set(participant_ids)):
            raise ValidationError({"participants": "Each participant can only be updated once per request."})

        with transaction.atomic():
            participants = {
                participant.id: participant
                for participant in TournamentParticipant.objects.select_for_update().filter(
                    tournament=tournament,
                    id__in=participant_ids,
                    payment_status__in=[
                        TournamentParticipant.PaymentStatus.PAID,
                        TournamentParticipant.PaymentStatus.NOT_REQUIRED,
                    ],
                )
            }
            missing = set(participant_ids) - participants.keys()
            if missing:
                raise ValidationError(
                    {"participants": "One or more players are not confirmed participants in this game."}
                )
            for update in updates:
                participant = participants[update["participant_id"]]
                participant.attendance_status = update["attendance_status"]
                participant.attendance_disputed = participant.attendance_disputed or (
                    participant.attendance_status == TournamentParticipant.AttendanceStatus.NO_SHOW
                    and participant.attendance_confirmed
                )
                participant.save(update_fields=["attendance_status", "attendance_disputed"])

        return Response(
            TournamentParticipantSerializer(
                tournament.participants.select_related("user").order_by("slot_number"),
                many=True,
            ).data
        )

    @action(detail=True, methods=["post"], url_path="confirm-attendance")
    def confirm_attendance(self, request, pk=None):
        tournament = self.get_object()
        if tournament.status != Tournament.Status.COMPLETED:
            raise ValidationError({"detail": "You can confirm attendance after the game is completed."})
        with transaction.atomic():
            participant = get_object_or_404(
                TournamentParticipant.objects.select_for_update(),
                tournament=tournament,
                user=request.user,
                payment_status__in=[
                    TournamentParticipant.PaymentStatus.PAID,
                    TournamentParticipant.PaymentStatus.NOT_REQUIRED,
                ],
            )
            participant.attendance_confirmed = True
            update_fields = ["attendance_confirmed"]
            if participant.attendance_status == TournamentParticipant.AttendanceStatus.NO_SHOW:
                participant.attendance_disputed = True
                update_fields.append("attendance_disputed")
            participant.save(update_fields=update_fields)
        return Response(TournamentParticipantSerializer(participant).data)

    @action(detail=True, methods=["get", "post", "delete"])
    def waitlist(self, request, pk=None):
        with transaction.atomic():
            tournament = get_object_or_404(
                Tournament.objects.select_for_update(), pk=pk
            )
            if tournament.slots_open > 0:
                notify_waitlist_head(tournament)
        if request.method == "GET":
            entry = tournament.waitlist_entries.filter(user=request.user).first()
            return Response(
                TournamentWaitlistSerializer(entry).data if entry else {"joined": False}
            )
        if request.method == "DELETE":
            with transaction.atomic():
                tournament = get_object_or_404(
                    Tournament.objects.select_for_update(), pk=pk
                )
                tournament.waitlist_entries.filter(user=request.user).delete()
                notify_waitlist_head(tournament)
            return Response(status=status.HTTP_204_NO_CONTENT)
        if tournament.status not in {Tournament.Status.FULL, Tournament.Status.OPEN}:
            raise ValidationError({"detail": "This tournament is not accepting waitlist requests."})
        has_active_offer = tournament.waitlist_entries.filter(
            offer_expires_at__gt=timezone.now()
        ).exists()
        if tournament.slots_open > 0 and not has_active_offer:
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
        self.get_object()
        with transaction.atomic():
            tournament = get_object_or_404(
                Tournament.objects.select_for_update(), pk=pk
            )
            if tournament.status in {Tournament.Status.CANCELLED, Tournament.Status.COMPLETED}:
                raise ValidationError({"detail": "This tournament can no longer be cancelled."})
            if tournament.active_participants().filter(
                payment_status=TournamentParticipant.PaymentStatus.PENDING
            ).exists():
                raise ValidationError(
                    {"detail": "Wait for unpaid spot reservations to expire or be released before cancelling this tournament."}
                )
            tournament.status = Tournament.Status.CANCELLING
            tournament.save(update_fields=["status", "updated_at"])
            paid_participant_ids = list(
                tournament.active_participants().filter(
                    payment_status=TournamentParticipant.PaymentStatus.PAID
                ).values_list("id", flat=True)
            )

        for participant_id in paid_participant_ids:
            participant = TournamentParticipant.objects.filter(
                pk=participant_id,
                payment_status=TournamentParticipant.PaymentStatus.PAID,
            ).first()
            if participant is None:
                continue
            payment = participant.paystack_transactions.filter(
                status=PaystackTransaction.Status.SUCCESS
            ).first()
            if payment is None:
                raise ValidationError(
                    {"detail": "A paid entry has no verifiable Paystack transaction; contact support before cancelling."}
                )
            refund_status = request_paystack_refund(payment)
            if refund_status != PaystackTransaction.RefundStatus.PROCESSED:
                raise ValidationError(
                    {"detail": "Full refunds are processing. The tournament is closed to new joins until Paystack confirms them; retry cancellation after confirmation."}
                )

        with transaction.atomic():
            tournament = get_object_or_404(
                Tournament.objects.select_for_update(), pk=pk
            )
            if tournament.status in {
                Tournament.Status.CANCELLED,
                Tournament.Status.COMPLETED,
            }:
                return Response(self.get_serializer(tournament).data)
            if tournament.status != Tournament.Status.CANCELLING:
                raise ValidationError({"detail": "Cancellation is no longer in progress."})
            if tournament.active_participants().filter(
                payment_status__in=[
                    TournamentParticipant.PaymentStatus.PENDING,
                    TournamentParticipant.PaymentStatus.PAID,
                ]
            ).exists():
                raise ValidationError(
                    {"detail": "Refunds must be confirmed and unpaid reservations released before cancellation can finish."}
                )
            tournament.status = Tournament.Status.CANCELLED
            tournament.cancelled_at = timezone.now()
            tournament.save(update_fields=["status", "cancelled_at", "updated_at"])
            notify_participants(
                tournament,
                UserNotification.Kind.TOURNAMENT_CANCELLED,
                f"{tournament.title} has been cancelled by the host.",
            )
            waitlisted_user_ids = tournament.waitlist_entries.values_list(
                "user_id", flat=True
            )
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
        if tournament.status in {
            Tournament.Status.CANCELLING,
            Tournament.Status.CANCELLED,
            Tournament.Status.COMPLETED,
        }:
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
                if (
                    tournament.entry_fee > Decimal("0.00")
                    and not settings.TOURNAMENT_PAYMENTS_ENABLED
                ):
                    raise ValidationError(
                        {"detail": "Paid tournaments are temporarily unavailable while organizer payouts are being set up."}
                    )
                expired_reservations = TournamentParticipant.objects.filter(
                    tournament=tournament,
                    payment_status=TournamentParticipant.PaymentStatus.PENDING,
                    reservation_expires_at__lte=now,
                )
                expired_count = expired_reservations.count()
                expired_reservations.delete()
                active_count = tournament.active_participants().count()
                if tournament.status == Tournament.Status.FULL and active_count < tournament.max_players:
                    tournament.status = Tournament.Status.OPEN
                    tournament.save(update_fields=["status", "updated_at"])
                if tournament.status != Tournament.Status.OPEN:
                    raise ValidationError({"detail": "This tournament is not open."})
                if tournament.starts_at <= timezone.now():
                    raise ValidationError({"detail": "This tournament has already started."})
                if tournament.slots_open > 0:
                    notify_waitlist_head(tournament)
                active_offer = tournament.waitlist_entries.filter(
                    offer_expires_at__gt=now
                ).first()
                if active_offer and active_offer.user_id != request.user.id:
                    raise ValidationError(
                        {"detail": "This spot is temporarily reserved for the next waitlisted player."}
                    )
                if active_offer and slot_number != active_offer.offered_slot_number:
                    raise ValidationError(
                        {"slot_number": f"Claim the offered spot {active_offer.offered_slot_number}."}
                    )
                own_waitlist_entry = tournament.waitlist_entries.filter(
                    user=request.user
                ).first()
                if own_waitlist_entry and (
                    active_offer is None or active_offer.user_id != request.user.id
                ):
                    raise ValidationError(
                        {"detail": "Wait for your turn in the waitlist before claiming an open spot."}
                    )
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
                elif tournament.slots_open > 0:
                    notify_waitlist_head(tournament)
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
        if not settings.TOURNAMENT_PAYMENTS_ENABLED:
            raise ValidationError(
                {"detail": "Paid checkout is temporarily unavailable while organizer payouts are being set up."}
            )
        participant = get_object_or_404(
            TournamentParticipant.objects.select_related("user", "tournament").filter(
                tournament=tournament, user=request.user
            )
        )
        if tournament.status in {
            Tournament.Status.CANCELLING,
            Tournament.Status.CANCELLED,
        }:
            raise ValidationError({"detail": "This tournament was cancelled; payment is unavailable."})
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
        if not settings.TOURNAMENT_PAYMENTS_ENABLED:
            raise ValidationError(
                {"detail": "Paid checkout is temporarily unavailable while organizer payouts are being set up."}
            )
        participant = get_object_or_404(
            TournamentParticipant.objects.filter(tournament=tournament, user=request.user)
        )
        if tournament.status in {
            Tournament.Status.CANCELLING,
            Tournament.Status.CANCELLED,
        }:
            raise ValidationError({"detail": "This tournament was cancelled; payment cannot be verified."})
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
            detail = (
                "Payment succeeded while paid checkout was disabled. It did not secure a spot; contact support for a refund."
                if not settings.TOURNAMENT_PAYMENTS_ENABLED
                else "Payment succeeded after the slot reservation expired. Contact support for a refund."
            )
            return Response(
                {
                    "payment_status": payment.status,
                    "detail": detail,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response({"payment_status": TournamentParticipant.PaymentStatus.PAID})

    @action(detail=True, methods=["delete"])
    def leave(self, request, pk=None):
        tournament = get_object_or_404(Tournament, pk=pk)
        if (
            tournament.status in {
                Tournament.Status.CANCELLING,
                Tournament.Status.CANCELLED,
                Tournament.Status.COMPLETED,
            }
            or tournament.starts_at <= timezone.now()
        ):
            raise ValidationError(
                {"detail": "You can only leave a tournament before its scheduled start time."}
            )
        participant = get_object_or_404(
            TournamentParticipant, tournament=tournament, user=request.user
        )
        if participant.payment_status == TournamentParticipant.PaymentStatus.PAID:
            refund_deadline = tournament.starts_at - timedelta(
                hours=PLAYER_REFUND_CUTOFF_HOURS
            )
            if timezone.now() > refund_deadline:
                raise ValidationError(
                    {"detail": "Player cancellations are refundable only at least 24 hours before the game starts."}
                )
            payment = participant.paystack_transactions.filter(
                status=PaystackTransaction.Status.SUCCESS
            ).first()
            if payment is None:
                raise ValidationError(
                    {"detail": "A successful Paystack transaction could not be found; contact support before leaving."}
                )
            refund_status = request_paystack_refund(payment, release_entry=True)
            if refund_status != PaystackTransaction.RefundStatus.PROCESSED:
                raise ValidationError(
                    {"detail": "Your refund is processing. Your spot will be released once Paystack confirms it."}
                )

        with transaction.atomic():
            tournament = get_object_or_404(
                Tournament.objects.select_for_update(), pk=pk
            )
            participant = get_object_or_404(
                TournamentParticipant, tournament=tournament, user=request.user
            )
            if participant.payment_status == TournamentParticipant.PaymentStatus.PAID:
                participant.payment_status = TournamentParticipant.PaymentStatus.REFUNDED
                participant.save(update_fields=["payment_status"])
            participant.delete()
            if tournament.status == Tournament.Status.FULL:
                tournament.status = Tournament.Status.OPEN
                tournament.save(update_fields=["status", "updated_at"])
            if tournament.slots_open > 0:
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
        event_type = event.get("event")
        if event_type in {"refund.pending", "refund.processed", "refund.failed"}:
            event_data = event.get("data", {})
            reference = (
                event_data.get("transaction_reference")
                or event_data.get("reference")
            )
            payment = PaystackTransaction.objects.filter(reference=reference).first()
            if payment:
                with transaction.atomic():
                    payment = PaystackTransaction.objects.select_for_update().get(
                        pk=payment.pk
                    )
                    if event_type == "refund.processed":
                        payment.refund_status = PaystackTransaction.RefundStatus.PROCESSED
                        participant = (
                            TournamentParticipant.objects.filter(pk=payment.participant_id)
                            .first()
                        )
                        if participant and payment.release_on_refund:
                            tournament = Tournament.objects.select_for_update().get(
                                pk=participant.tournament_id
                            )
                            participant = TournamentParticipant.objects.select_for_update().get(
                                pk=participant.pk
                            )
                            participant.delete()
                            if tournament.status == Tournament.Status.FULL:
                                tournament.status = Tournament.Status.OPEN
                                tournament.save(update_fields=["status", "updated_at"])
                            if tournament.slots_open > 0:
                                notify_waitlist_head(tournament)
                        elif participant:
                            TournamentParticipant.objects.filter(pk=participant.pk).update(
                                payment_status=TournamentParticipant.PaymentStatus.REFUNDED
                            )
                    elif event_type == "refund.failed":
                        payment.refund_status = PaystackTransaction.RefundStatus.FAILED
                    else:
                        payment.refund_status = PaystackTransaction.RefundStatus.PENDING
                    payment.save(update_fields=["refund_status", "updated_at"])
            return Response({"received": True})
        if event_type != "charge.success":
            return Response({"received": True})

        reference = event.get("data", {}).get("reference")
        payment = PaystackTransaction.objects.filter(reference=reference).first()
        if payment:
            finalize_paystack_payment(payment, event["data"])
        return Response({"received": True})

# Create your views here.
