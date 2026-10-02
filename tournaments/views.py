from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.generics import RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Country, Sport, Tournament, TournamentParticipant, Venue
from .permissions import IsTournamentHostOrReadOnly
from .serializers import (
    JoinTournamentSerializer,
    CountrySerializer,
    RegisterSerializer,
    SportSerializer,
    TournamentParticipantSerializer,
    TournamentSerializer,
    UserSerializer,
    VenueSerializer,
)

User = get_user_model()


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


class CurrentUserView(RetrieveAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


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
        if self.action in {"join", "leave", "mine"}:
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_create(self, serializer):
        venue = serializer.validated_data["venue"]
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
                if tournament.status != Tournament.Status.OPEN:
                    raise ValidationError({"detail": "This tournament is not open."})
                if tournament.starts_at <= timezone.now():
                    raise ValidationError({"detail": "This tournament has already started."})
                if slot_number > tournament.max_players:
                    raise ValidationError(
                        {"slot_number": "Choose a slot within the available player spots."}
                    )
                if tournament.participants.filter(user=request.user).exists():
                    raise ValidationError({"detail": "You already joined this tournament."})
                if tournament.participants.count() >= tournament.max_players:
                    raise ValidationError({"detail": "All player spots are taken."})
                participant = TournamentParticipant.objects.create(
                    tournament=tournament,
                    user=request.user,
                    slot_number=slot_number,
                    entry_fee_at_join=tournament.entry_fee,
                )
                if tournament.participants.count() >= tournament.max_players:
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
        return Response(status=status.HTTP_204_NO_CONTENT)

# Create your views here.
