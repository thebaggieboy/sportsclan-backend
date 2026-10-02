from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers

from .models import Country, CountryCurrency, Currency, Sport, Tournament, TournamentParticipant, Venue

User = get_user_model()


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

    class Meta:
        model = Venue
        fields = [
            "id",
            "name",
            "address",
            "city",
            "region",
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
        source="venue", queryset=Venue.objects.filter(is_active=True), write_only=True
    )
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
            "venue",
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
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]

    def get_slots_open(self, tournament):
        slots_taken = getattr(tournament, "slots_taken", None)
        if slots_taken is None:
            slots_taken = tournament.participants.count()
        return max(0, tournament.max_players - slots_taken)

    def get_is_joined(self, tournament):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return tournament.participants.filter(user=request.user).exists()

    def get_taken_slots(self, tournament):
        return sorted(
            participant.slot_number for participant in tournament.participants.all()
        )

    def get_my_slot(self, tournament):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        return next(
            (
                participant.slot_number
                for participant in tournament.participants.all()
                if participant.user_id == request.user.id
            ),
            None,
        )

    def validate(self, attrs):
        sport = attrs.get("sport", getattr(self.instance, "sport", None))
        venue = attrs.get("venue", getattr(self.instance, "venue", None))
        country = attrs.get("country", getattr(self.instance, "country", None))
        currency = attrs.get("currency", getattr(self.instance, "currency", None))
        starts_at = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        max_players = attrs.get("max_players", getattr(self.instance, "max_players", None))

        if self.instance is None and country is None:
            raise serializers.ValidationError({"country_id": "Choose a country."})
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
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = TournamentParticipant
        fields = [
            "id",
            "username",
            "slot_number",
            "entry_fee_at_join",
            "payment_status",
            "joined_at",
        ]
        read_only_fields = fields