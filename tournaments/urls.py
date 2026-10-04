from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CountryViewSet, SportViewSet, TournamentViewSet, VenueViewSet, WaitlistSignupView

router = DefaultRouter()
router.register("sports", SportViewSet, basename="sport")
router.register("countries", CountryViewSet, basename="country")
router.register("venues", VenueViewSet, basename="venue")
router.register("tournaments", TournamentViewSet, basename="tournament")

urlpatterns = [path("waitlist/", WaitlistSignupView.as_view(), name="waitlist-signup")] + router.urls