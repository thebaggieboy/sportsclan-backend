from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    CountryViewSet, MyPlayerProfileView, NotificationsView, PublicPlayerProfileView,
    SportViewSet, TournamentReportView, TournamentViewSet, VenueViewSet,
    WaitlistSignupView,
)

router = DefaultRouter()
router.register("sports", SportViewSet, basename="sport")
router.register("countries", CountryViewSet, basename="country")
router.register("venues", VenueViewSet, basename="venue")
router.register("tournaments", TournamentViewSet, basename="tournament")

urlpatterns = [
    path("waitlist/", WaitlistSignupView.as_view(), name="waitlist-signup"),
    path("profiles/me/", MyPlayerProfileView.as_view(), name="my-player-profile"),
    path("players/<str:username>/", PublicPlayerProfileView.as_view(), name="player-profile"),
    path("notifications/", NotificationsView.as_view(), name="notifications"),
    path("reports/", TournamentReportView.as_view(), name="tournament-report"),
] + router.urls