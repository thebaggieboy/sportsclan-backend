from rest_framework.routers import DefaultRouter

from .views import CountryViewSet, SportViewSet, TournamentViewSet, VenueViewSet

router = DefaultRouter()
router.register("sports", SportViewSet, basename="sport")
router.register("countries", CountryViewSet, basename="country")
router.register("venues", VenueViewSet, basename="venue")
router.register("tournaments", TournamentViewSet, basename="tournament")

urlpatterns = router.urls