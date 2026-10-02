from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Q
from decimal import Decimal, ROUND_HALF_UP


class Currency(models.Model):
	code = models.CharField(max_length=3, primary_key=True)
	name = models.CharField(max_length=80)

	class Meta:
		ordering = ["code"]

	def __str__(self):
		return f"{self.code} - {self.name}"


class Country(models.Model):
	code = models.CharField(max_length=2, primary_key=True)
	name = models.CharField(max_length=100)
	default_currency = models.ForeignKey(
		Currency,
		null=True,
		blank=True,
		on_delete=models.PROTECT,
		related_name="default_for_countries",
	)
	currencies = models.ManyToManyField(
		Currency, through="CountryCurrency", related_name="countries"
	)

	class Meta:
		ordering = ["name"]

	def __str__(self):
		return f"{self.name} ({self.code})"


class CountryCurrency(models.Model):
	country = models.ForeignKey(
		Country, on_delete=models.CASCADE, related_name="currency_options"
	)
	currency = models.ForeignKey(
		Currency, on_delete=models.PROTECT, related_name="country_options"
	)
	is_default = models.BooleanField(default=False)

	class Meta:
		ordering = ["-is_default", "currency__code"]
		constraints = [
			models.UniqueConstraint(
				fields=["country", "currency"], name="unique_currency_per_country"
			),
			models.UniqueConstraint(
				fields=["country"],
				condition=Q(is_default=True),
				name="one_default_currency_per_country",
			),
		]

	def __str__(self):
		return f"{self.country.code}: {self.currency.code}"


class Sport(models.Model):
	name = models.CharField(max_length=80, unique=True)
	slug = models.SlugField(max_length=90, unique=True)
	is_active = models.BooleanField(default=True)
	sort_order = models.PositiveSmallIntegerField(default=0)

	class Meta:
		ordering = ["sort_order", "name"]

	def __str__(self):
		return self.name


class Venue(models.Model):
	class PricingType(models.TextChoices):
		FREE = "free", "Free"
		FIXED = "fixed", "Fixed price"
		HOURLY = "hourly", "Hourly"
		QUOTE = "quote", "Contact for price"

	name = models.CharField(max_length=160)
	address = models.CharField(max_length=240, blank=True)
	city = models.CharField(max_length=100)
	region = models.CharField(max_length=100, blank=True)
	country = models.CharField(max_length=100, blank=True)
	country_reference = models.ForeignKey(
		Country,
		null=True,
		blank=True,
		on_delete=models.PROTECT,
		related_name="venues",
	)
	latitude = models.DecimalField(
		max_digits=9, decimal_places=6, null=True, blank=True
	)
	longitude = models.DecimalField(
		max_digits=9, decimal_places=6, null=True, blank=True
	)
	sports = models.ManyToManyField(Sport, blank=True, related_name="venues")
	pricing_type = models.CharField(
		max_length=12, choices=PricingType.choices, default=PricingType.FREE
	)
	price_amount = models.DecimalField(
		max_digits=10, decimal_places=2, null=True, blank=True, default=Decimal("0.00")
	)
	price_currency = models.CharField(max_length=3, default="USD")
	is_active = models.BooleanField(default=True)
	created_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["city", "name"]
		indexes = [models.Index(fields=["city", "is_active"])]

	def __str__(self):
		return f"{self.name}, {self.city}"

	def clean(self):
		if self.pricing_type == self.PricingType.FREE:
			if self.price_amount not in (None, Decimal("0.00")):
				raise ValidationError({"price_amount": "Free venues must have a zero price."})
			self.price_amount = Decimal("0.00")
		elif self.pricing_type == self.PricingType.QUOTE:
			self.price_amount = None
		elif self.price_amount is None or self.price_amount <= 0:
			raise ValidationError({"price_amount": "Enter a price above zero for paid venues."})
		if (
			self.country_reference_id
			and self.price_currency
			and not CountryCurrency.objects.filter(
				country_id=self.country_reference_id,
				currency_id=self.price_currency,
			).exists()
		):
			raise ValidationError(
				{"price_currency": "Choose a currency supported by the venue's country."}
			)


class Tournament(models.Model):
	class Status(models.TextChoices):
		OPEN = "open", "Open"
		FULL = "full", "Full"
		CANCELLED = "cancelled", "Cancelled"
		COMPLETED = "completed", "Completed"

	class VenueFeeStatus(models.TextChoices):
		NOT_REQUIRED = "not_required", "Not required"
		PENDING = "pending", "Pending booking cost"
		AGREED = "agreed", "Agreed cost"
		PAID = "paid", "Marked paid"
		WAIVED = "waived", "Waived"

	title = models.CharField(max_length=140)
	description = models.TextField(blank=True)
	host = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="hosted_tournaments",
	)
	sport = models.ForeignKey(Sport, on_delete=models.PROTECT, related_name="tournaments")
	venue = models.ForeignKey(Venue, on_delete=models.PROTECT, related_name="tournaments")
	country = models.ForeignKey(
		Country,
		null=True,
		blank=True,
		on_delete=models.PROTECT,
		related_name="tournaments",
	)
	starts_at = models.DateTimeField()
	duration_minutes = models.PositiveSmallIntegerField(
		default=60,
		validators=[MinValueValidator(15), MaxValueValidator(1440)],
	)
	venue_pricing_type_snapshot = models.CharField(
		max_length=12, choices=Venue.PricingType.choices, default=Venue.PricingType.FREE
	)
	venue_rate_snapshot = models.DecimalField(
		max_digits=10, decimal_places=2, null=True, blank=True
	)
	venue_rate_currency_snapshot = models.CharField(max_length=3, default="USD")
	venue_fee = models.DecimalField(
		max_digits=12,
		decimal_places=2,
		null=True,
		blank=True,
		validators=[MinValueValidator(0)],
	)
	venue_fee_status = models.CharField(
		max_length=12,
		choices=VenueFeeStatus.choices,
		default=VenueFeeStatus.PENDING,
	)
	entry_fee = models.DecimalField(
		max_digits=10,
		decimal_places=2,
		default=0,
		validators=[MinValueValidator(0)],
	)
	currency = models.CharField(max_length=3, default="USD")
	max_players = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
	status = models.CharField(
		max_length=12,
		choices=Status.choices,
		default=Status.OPEN,
	)
	created_at = models.DateTimeField(auto_now_add=True)
	updated_at = models.DateTimeField(auto_now=True)

	class Meta:
		ordering = ["starts_at"]
		indexes = [models.Index(fields=["status", "starts_at"])]
		constraints = [
			models.CheckConstraint(
				condition=Q(entry_fee__gte=0), name="tournament_entry_fee_nonnegative"
			),
			models.CheckConstraint(
				condition=Q(max_players__gte=1), name="tournament_has_player_spots"
			),
		]

	def __str__(self):
		return self.title

	def clean(self):
		if self.country_id and not CountryCurrency.objects.filter(
			country_id=self.country_id, currency_id=self.currency
		).exists():
			raise ValidationError(
				{"currency": "Choose a currency supported by the tournament's country."}
			)
		if (
			self.venue_id
			and self.country_id
			and self.venue.country_reference_id
			and self.venue.country_reference_id != self.country_id
		):
			raise ValidationError({"country": "The venue must be in the tournament's country."})

	@property
	def slots_taken(self):
		return self.participants.count()

	@property
	def slots_open(self):
		return max(0, self.max_players - self.slots_taken)

	@property
	def venue_fee_estimate(self):
		if self.venue_pricing_type_snapshot == Venue.PricingType.FREE:
			return Decimal("0.00")
		if self.venue_pricing_type_snapshot == Venue.PricingType.QUOTE:
			return None
		if self.venue_rate_snapshot is None:
			return None
		if self.venue_pricing_type_snapshot == Venue.PricingType.HOURLY:
			amount = self.venue_rate_snapshot * Decimal(self.duration_minutes) / Decimal(60)
			return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
		return self.venue_rate_snapshot

	@property
	def projected_prize_pool(self):
		return self.entry_fee * self.max_players


class TournamentParticipant(models.Model):
	class PaymentStatus(models.TextChoices):
		PENDING = "pending", "Pending"
		PAID = "paid", "Paid"
		REFUNDED = "refunded", "Refunded"

	tournament = models.ForeignKey(
		Tournament, on_delete=models.CASCADE, related_name="participants"
	)
	user = models.ForeignKey(
		settings.AUTH_USER_MODEL,
		on_delete=models.CASCADE,
		related_name="tournament_entries",
	)
	slot_number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
	entry_fee_at_join = models.DecimalField(max_digits=10, decimal_places=2)
	payment_status = models.CharField(
		max_length=10,
		choices=PaymentStatus.choices,
		default=PaymentStatus.PENDING,
	)
	joined_at = models.DateTimeField(auto_now_add=True)

	class Meta:
		ordering = ["slot_number"]
		constraints = [
			models.UniqueConstraint(
				fields=["tournament", "user"], name="unique_tournament_participant"
			),
			models.UniqueConstraint(
				fields=["tournament", "slot_number"], name="unique_tournament_slot"
			),
		]

	def __str__(self):
		return f"{self.user} in {self.tournament} (slot {self.slot_number})"
