from django.contrib import admin

from .models import (
	Country, CountryCurrency, Currency, PlayerProfile, Sport, Tournament,
	TournamentMessage, TournamentParticipant, TournamentReport,
	TournamentWaitlist, UserNotification, Venue, WaitlistSignup,
)


@admin.register(PlayerProfile)
class PlayerProfileAdmin(admin.ModelAdmin):
	list_display = ["user", "updated_at"]
	search_fields = ["user__username", "user__email", "bio"]
	filter_horizontal = ["preferred_sports"]


@admin.register(TournamentReport)
class TournamentReportAdmin(admin.ModelAdmin):
	list_display = ["id", "reporter", "tournament", "reported_player", "reason", "status", "created_at"]
	list_filter = ["status", "reason", "created_at"]
	search_fields = ["reporter__username", "reported_player__username", "tournament__title", "details"]
	readonly_fields = ["reporter", "tournament", "reported_player", "reason", "details", "created_at"]


@admin.register(TournamentWaitlist)
class TournamentWaitlistAdmin(admin.ModelAdmin):
	list_display = ["tournament", "user", "joined_at"]
	list_filter = ["joined_at"]
	search_fields = ["tournament__title", "user__username"]


@admin.register(TournamentMessage)
class TournamentMessageAdmin(admin.ModelAdmin):
	list_display = ["tournament", "sender", "created_at"]
	search_fields = ["tournament__title", "sender__username", "body"]
	readonly_fields = ["created_at"]


@admin.register(UserNotification)
class UserNotificationAdmin(admin.ModelAdmin):
	list_display = ["user", "kind", "tournament", "is_read", "created_at"]
	list_filter = ["kind", "is_read", "created_at"]
	search_fields = ["user__username", "message", "tournament__title"]
	readonly_fields = ["created_at"]


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
