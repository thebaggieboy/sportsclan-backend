from django.contrib import admin
from django.urls import include, path
from rest_framework.schemas import get_schema_view
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from tournaments.views import CurrentUserView, RegisterView

api_schema = get_schema_view(
    title="SportsClan API",
    description="API for sports, venues, tournaments, and player spots.",
    version="1.0.0",
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("tournaments.urls")),
    path("api/v1/auth/register/", RegisterView.as_view(), name="register"),
    path("api/v1/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/auth/me/", CurrentUserView.as_view(), name="current_user"),
    path("api/schema/", api_schema, name="api-schema"),
]
