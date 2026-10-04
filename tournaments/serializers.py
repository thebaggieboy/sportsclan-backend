from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .models import (
    Country, CountryCurrency, Currency, PlayerProfile, Sport, Tournament,
    TournamentMessage, TournamentParticipant, TournamentReport, UserNotification,
    TournamentWaitlist, Venue, WaitlistSignup,
)

User = get_user_model()


class WaitlistSignupSerializer(serializers.Serializer):
    email = serializers.EmailField()
    interest = serializers.ChoiceField(choices=WaitlistSignup.Interest.choices)

    def validate_email(self, value):
        return value.strip().lower()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name"]
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "password"]
        read_only_fields = ["id"]

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return email

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class SportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sport
        fields = ["id", "name", "slug"]


class PlayerProfileSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    first_name = serializers.CharField(source="user.first_name", required=False, allow_blank=True)
    last_name = serializers.CharField(source="user.last_name", required=False, allow_blank=True)
    preferred_sport_ids = serializers.PrimaryKeyRelatedField(
        source="preferred_sports",
        queryset=Sport.objects.filter(is_active=True),
        many=True,
        required=False,
        write_only=True,
    )
    preferred_sports = SportSerializer(many=True, read_only=True)

    class Meta:
        model = PlayerProfile
        fields = [
            "username", "first_name", "last_name", "bio",
            "preferred_sports", "preferred_sport_ids",
        ]

    def update(self, instance, validated_data):
        user_data = validated_data.pop("user", {})
        for field, value in user_data.items():
            setattr(instance.user, field, value)
        if user_data:
            instance.user.save(update_fields=list(user_data))
        sports = validated_data.pop("preferred_sports", None)
        instance = super().update(instance, validated_data)
        if sports is not None:
            instance.preferred_sports.set(sports)
        return instance


class CurrencyOptionSerializer(serializers.ModelSerializer):
    code = serializers.CharField(source="currency_id", read_only=True)
    name = serializers.CharField(source="currency.name", read_only=True)

    class Meta:
        model = CountryCurrency
        fields = ["code", "name", "is_default"]


class CountrySerializer(serializers.ModelSerializer):
    currencies = CurrencyOptionSerializer(source="currency_options", many=True, read_only=True)
    default_currency = serializers.CharField(source="default_currency_id", allow_null=True, read_only=True)

    class Meta:
        model = Country
        fields = ["code", "name", "default_currency", "currencies"]


class VenueSerializer(serializers.ModelSerializer):
    sports = SportSerializer(many=True, read_only=True)
    country_info = CountrySerializer(source="country_reference", read_only=True)
    venue_name = serializers.CharField(source="name", read_only=True)
    state = serializers.CharField(source="region", read_only=True)

    class Meta:
        model = Venue
        fields = [
            "id",
            "name",
            "venue_name",
            "address",
            "city",
            "region",
            "state",
            "country",
            "country_info",
            "latitude",
            "longitude",
            "sports",
            "pricing_type",
            "price_amount",
            "price_currency",
        ]


class TournamentSerializer(serializers.ModelSerializer):
    country_id = serializers.PrimaryKeyRelatedField(
        source="country", queryset=Country.objects.all(), write_only=True
    )
    country = CountrySerializer(read_only=True)
    sport_id = serializers.PrimaryKeyRelatedField(
        source="sport", queryset=Sport.objects.filter(is_active=True), write_only=True
    )
    venue_id = serializers.PrimaryKeyRelatedField(
        source="venue", queryset=Venue.objects.filter(is_active=True), write_only=True,
        required=False, allow_null=True,
    )
    venue_city = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=100)
    venue_name = serializers.CharField(required=False, allow_blank=True, max_length=160)
    state = serializers.CharField(required=False, allow_blank=True, max_length=100)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True, min_value=-90, max_value=90)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True, min_value=-180, max_value=180)
    sport = SportSerializer(read_only=True)
    venue = VenueSerializer(read_only=True)
    host = serializers.CharField(source="host.username", read_only=True)
    venue_pricing_type_snapshot = serializers.CharField(read_only=True)
    venue_rate_snapshot = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True, read_only=True
    )
    venue_rate_currency_snapshot = serializers.CharField(read_only=True)
    venue_fee_estimate = serializers.DecimalField(
        max_digits=15, decimal_places=2, allow_null=True, read_only=True
    )
    projected_prize_pool = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True
    )
    slots_taken = serializers.IntegerField(read_only=True)
    slots_open = serializers.SerializerMethodField()
    taken_slots = serializers.SerializerMethodField()
    is_joined = serializers.SerializerMethodField()
    my_slot = serializers.SerializerMethodField()
    my_payment_status = serializers.SerializerMethodField()
    is_waitlisted = serializers.SerializerMethodField()
    waitlist_position = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    venue_fee = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    venue_fee_status = serializers.ChoiceField(
        choices=Tournament.VenueFeeStatus.choices, required=False
    )

    def get_fields(self):
        fields = super().get_fields()
        if self.instance is None:
            fields["venue_fee"].read_only = True
            fields["venue_fee_status"].read_only = True
        return fields

    def create(self, validated_data):
        validated_data.pop("venue_city", None)
        venue = validated_data["venue"]
        validated_data["venue_name"] = validated_data.get("venue_name") or venue.name
        validated_data["state"] = validated_data.get("state") or venue.region
        if validated_data.get("latitude") is None and validated_data.get("longitude") is None:
            if venue.latitude is not None and venue.longitude is not None:
                validated_data["latitude"] = venue.latitude
                validated_data["longitude"] = venue.longitude
        return super().create(validated_data)

    def create_custom_venue(self):
        data = self.validated_data
        country = data["country"]
        venue = Venue.objects.create(
            name=data["venue_name"].strip(),
            city=data["venue_city"].strip(),
            region=data.get("state", "").strip(),
            country=country.name,
            country_reference=country,
            latitude=data["latitude"],
            longitude=data["longitude"],
            pricing_type=Venue.PricingType.FREE,
            price_amount=Decimal("0.00"),
            price_currency=data["currency"],
        )
        venue.sports.add(data["sport"])
        data["venue"] = venue
        return venue

    class Meta:
        model = Tournament
        fields = [
            "id",
            "title",
            "description",
            "host",
            "sport_id",
            "sport",
            "venue_id",
            "venue_city",
            "venue",
            "venue_name",
            "state",
            "latitude",
            "longitude",
            "country_id",
            "country",
            "starts_at",
            "duration_minutes",
            "venue_pricing_type_snapshot",
            "venue_rate_snapshot",
            "venue_rate_currency_snapshot",
            "venue_fee_estimate",
            "venue_fee",
            "venue_fee_status",
            "entry_fee",
            "currency",
            "max_players",
            "projected_prize_pool",
            "slots_taken",
            "slots_open",
            "taken_slots",
            "status",
            "is_joined",
            "my_slot",
            "my_payment_status",
            "is_waitlisted",
            "waitlist_position",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]

    def get_slots_open(self, tournament):
        slots_taken = tournament.active_participants().count()
        return max(0, tournament.max_players - slots_taken)

    def get_is_joined(self, tournament):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return tournament.active_participants().filter(user=request.user).exists()

    def get_taken_slots(self, tournament):
        return sorted(
            participant.slot_number for participant in tournament.active_participants()
        )

    def get_my_slot(self, tournament):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        return next(
            (
                participant.slot_number
                for participant in tournament.active_participants()
                if participant.user_id == request.user.id
            ),
            None,
        )

    def get_my_payment_status(self, tournament):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        participant = tournament.participants.filter(user=request.user).first()
        return participant.payment_status if participant else None

    def get_is_waitlisted(self, tournament):
        request = self.context.get("request")
        return bool(
            request and request.user.is_authenticated
            and tournament.waitlist_entries.filter(user=request.user).exists()
        )

    def get_waitlist_position(self, tournament):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        entry = tournament.waitlist_entries.filter(user=request.user).first()
        if entry is None:
            return None
        return tournament.waitlist_entries.filter(tournament=entry.tournament).filter(
            Q(joined_at__lt=entry.joined_at)
            | Q(joined_at=entry.joined_at, id__lt=entry.id)
        ).count() + 1

    def get_status(self, tournament):
        if (
            tournament.status == Tournament.Status.FULL
            and tournament.active_participants().count() < tournament.max_players
        ):
            return Tournament.Status.OPEN
        return tournament.status

    def validate(self, attrs):
        sport = attrs.get("sport", getattr(self.instance, "sport", None))
        venue = attrs.get("venue", getattr(self.instance, "venue", None))
        country = attrs.get("country", getattr(self.instance, "country", None))
        currency = attrs.get("currency", getattr(self.instance, "currency", None))
        starts_at = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        max_players = attrs.get("max_players", getattr(self.instance, "max_players", None))

        if self.instance is None and country is None:
            raise serializers.ValidationError({"country_id": "Choose a country."})
        if self.instance is None and venue is None:
            required_location = {
                "venue_name": attrs.get("venue_name", "").strip(),
                "venue_city": attrs.get("venue_city", "").strip(),
                "latitude": attrs.get("latitude"),
                "longitude": attrs.get("longitude"),
            }
            missing = [field for field, value in required_location.items() if value in ("", None)]
            if missing:
                raise serializers.ValidationError(
                    {field: "Required when selecting a new map location." for field in missing}
                )
        if country and currency and not country.currency_options.filter(currency_id=currency).exists():
            raise serializers.ValidationError(
                {"currency": "Choose a currency supported by the selected country."}
            )
        if country and venue:
            if venue.country_reference_id is None:
                raise serializers.ValidationError(
                    {"venue_id": "This venue needs a country before it can be used."}
                )
            if venue.country_reference_id != country.code:
                raise serializers.ValidationError(
                    {"venue_id": "Choose a venue in the selected country."}
                )
        latitude = attrs.get("latitude", getattr(self.instance, "latitude", None))
        longitude = attrs.get("longitude", getattr(self.instance, "longitude", None))
        if (latitude is None) != (longitude is None):
            raise serializers.ValidationError(
                {"latitude": "Provide both latitude and longitude for the venue pin."}
            )
        if self.instance is None and starts_at and starts_at <= timezone.now():
            raise serializers.ValidationError({"starts_at": "Choose a future date and time."})
        venue_fee = attrs.get("venue_fee", getattr(self.instance, "venue_fee", None))
        venue_fee_status = attrs.get(
            "venue_fee_status",
            getattr(self.instance, "venue_fee_status", Tournament.VenueFeeStatus.PENDING),
        )
        if venue_fee is not None and venue_fee_status in {
            Tournament.VenueFeeStatus.PENDING,
            Tournament.VenueFeeStatus.NOT_REQUIRED,
        }:
            if "venue_fee_status" not in attrs and self.instance is not None:
                attrs["venue_fee_status"] = Tournament.VenueFeeStatus.AGREED
            else:
                raise serializers.ValidationError(
                    {"venue_fee_status": "Mark the venue cost as agreed before setting an actual fee."}
                )
        if venue_fee_status in {
            Tournament.VenueFeeStatus.AGREED,
            Tournament.VenueFeeStatus.PAID,
        } and venue_fee is None:
            raise serializers.ValidationError(
                {"venue_fee": "Enter the agreed venue cost before updating its status."}
            )
        if sport and venue and venue.sports.exists() and not venue.sports.filter(pk=sport.pk).exists():
            raise serializers.ValidationError(
                {"venue_id": "This venue is not set up for the chosen sport."}
            )
        if self.instance and max_players is not None:
            if max_players < self.instance.participants.count():
                raise serializers.ValidationError(
                    {"max_players": "This cannot be lower than the number of joined players."}
                )
        return attrs


class JoinTournamentSerializer(serializers.Serializer):
    slot_number = serializers.IntegerField(min_value=1)


class TournamentParticipantSerializer(serializers.ModelSerializer):
    user_id = serializers.IntegerField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    first_name = serializers.CharField(source="user.first_name", read_only=True)
    last_name = serializers.CharField(source="user.last_name", read_only=True)

    class Meta:
        model = TournamentParticipant
        fields = [
            "id",
            "user_id",
            "username",
            "first_name",
            "last_name",
            "slot_number",
            "entry_fee_at_join",
            "payment_status",
            "reservation_expires_at",
            "joined_at",
        ]
        read_only_fields = fields


class TournamentWaitlistSerializer(serializers.ModelSerializer):
    position = serializers.SerializerMethodField()

    class Meta:
        model = TournamentWaitlist
        fields = ["id", "position", "joined_at"]
        read_only_fields = fields

    def get_position(self, entry):
        return TournamentWaitlist.objects.filter(tournament=entry.tournament).filter(
            Q(joined_at__lt=entry.joined_at)
            | Q(joined_at=entry.joined_at, id__lt=entry.id)
        ).count() + 1


class TournamentMessageSerializer(serializers.ModelSerializer):
    sender = serializers.CharField(source="sender.username", read_only=True)

    class Meta:
        model = TournamentMessage
        fields = ["id", "sender", "body", "created_at"]
        read_only_fields = ["id", "sender", "created_at"]

    def validate_body(self, value):
        return value.strip()


class TournamentReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = TournamentReport
        fields = ["tournament", "reported_player", "reason", "details"]

    def validate(self, attrs):
        if bool(attrs.get("tournament")) == bool(attrs.get("reported_player")):
            raise serializers.ValidationError("Choose exactly one game or player to report.")
        if attrs.get("reported_player") == self.context["request"].user:
            raise serializers.ValidationError({"reported_player": "You cannot report yourself."})
        return attrs


class UserNotificationSerializer(serializers.ModelSerializer):
    tournament_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = UserNotification
        fields = ["id", "kind", "message", "tournament_id", "is_read", "created_at"]
        read_only_fields = fields