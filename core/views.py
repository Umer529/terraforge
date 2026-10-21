"""Server-rendered dashboard pages. Data is loaded by the page scripts from the REST API."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .auth import get_scan_or_404, role_of, visible_scans, visible_sites
from .models import Profile


def _ctx(request, **extra):
    return {"role": role_of(request.user), "can_upload": role_of(request.user) != Profile.REVIEWER, **extra}


@login_required
def dashboard(request, scan_id=None):
    scans = list(visible_scans(request.user))
    current = get_scan_or_404(request.user, scan_id) if scan_id else next((s for s in scans if s.status == "ready"), None)
    return render(request, "dashboard.html", _ctx(request, scans=scans, current=current))


@login_required
def upload(request):
    return render(request, "upload.html", _ctx(request, sites=visible_sites(request.user)))


@login_required
def compare(request):
    scans = [s for s in visible_scans(request.user) if s.status == "ready"]
    return render(request, "compare.html", _ctx(request, scans=scans, sites=visible_sites(request.user)))
