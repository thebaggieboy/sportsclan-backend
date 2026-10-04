from django.contrib import admin

from .models import Country, CountryCurrency, Currency, Sport, Tournament, TournamentParticipant, Venue, WaitlistSignup


class CountryCurrencyInline(admin.TabularInline):
	model = CountryCurrency
	extra = 0
	autocomplete_fields = ["currency"]


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
	search_fields = ["code", "name"]
	list_display = ["code", "name"]


@admin.register(WaitlistSignup)
class WaitlistSignupAdmin(admin.ModelAdmin):
	list_display = ["email", "interest", "created_at"]
	list_filter = ["interest", "created_at"]
	search_fields = ["email"]
	readonly_fields = ["created_at"]


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
	search_fields = ["code", "name"]
	list_display = ["code", "name", "default_currency"]
	inlines = [CountryCurrencyInline]


class TournamentParticipantInline(admin.TabularInline):
	model = TournamentParticipant
	extra = 0
	readonly_fields = ["joined_at"]


@admin.register(Sport)
class SportAdmin(admin.ModelAdmin):
	list_display = ["name", "slug", "is_active", "sort_order"]
	list_editable = ["is_active", "sort_order"]
	prepopulated_fields = {"slug": ["name"]}
	search_fields = ["name", "slug"]


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
	list_display = ["name", "city", "region", "country", "pricing_type", "price_amount", "price_currency", "is_active"]
	list_filter = ["is_active", "city", "country_reference", "pricing_type", "sports"]
	search_fields = ["name", "address", "city", "region", "country"]
	filter_horizontal = ["sports"]


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
	list_display = ["title", "sport", "venue", "host", "starts_at", "duration_minutes", "venue_fee", "venue_fee_status", "status"]
	list_filter = ["status", "venue_fee_status", "sport", "venue__city"]
	search_fields = ["title", "venue__name", "venue__city", "host__username"]
	readonly_fields = ["created_at", "updated_at"]
	inlines = [TournamentParticipantInline]
