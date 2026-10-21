"""Authentication and access rules shared by the API and the dashboard.

Browsers use Django sessions (with CSRF). The mobile app sends
`Authorization: Token <key>`; such requests carry no cookies, so CSRF does
not apply to them (the same rule Django REST framework uses).
"""
import functools
import json

from django.db.models import Q
from django.http import Http404, JsonResponse

from .models import ApiToken, Profile, Scan, Site


class TokenAuthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if header.startswith("Token "):
            token = ApiToken.objects.select_related("user").filter(key=header[6:].strip()).first()
            if token and token.user.is_active:
                request.user = token.user
                request._dont_enforce_csrf_checks = True
        return self.get_response(request)


def role_of(user):
    if user.is_superuser:
        return Profile.ADMIN
    profile = getattr(user, "profile", None)
    return profile.role if profile else Profile.REVIEWER


def visible_sites(user):
    """Admins see every site; others see sites they own or are members of."""
    if role_of(user) == Profile.ADMIN:
        return Site.objects.all()
    return Site.objects.filter(Q(owner=user) | Q(members=user)).distinct()


def visible_scans(user):
    return Scan.objects.filter(site__in=visible_sites(user)).select_related("site", "captured_by")


def get_scan_or_404(user, scan_id):
    scan = visible_scans(user).filter(pk=scan_id).first()
    if scan is None:
        raise Http404("Scan not found")
    return scan


def api_view(methods):
    """JSON endpoint: checks method and login, turns errors into JSON."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return JsonResponse({"error": "method not allowed"}, status=405)
            if not request.user.is_authenticated:
                return JsonResponse({"error": "authentication required"}, status=401)
            try:
                return fn(request, *args, **kwargs)
            except Http404 as exc:
                return JsonResponse({"error": str(exc) or "not found"}, status=404)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return JsonResponse({"error": f"bad request: {exc}"}, status=400)

        return wrapper

    return decorator


def json_body(request):
    return json.loads(request.body or b"{}")
