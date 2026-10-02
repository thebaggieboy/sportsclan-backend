from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsTournamentHostOrReadOnly(BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        return request.user.is_staff or obj.host_id == request.user.id