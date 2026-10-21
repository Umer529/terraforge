from django.contrib.auth import views as auth_views
from django.urls import path

from . import api, views

urlpatterns = [
    # Dashboard pages
    path("", views.dashboard, name="dashboard"),
    path("scans/<int:scan_id>/", views.dashboard, name="scan"),
    path("upload/", views.upload, name="upload"),
    path("compare/", views.compare, name="compare"),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # REST interface
    path("api/auth/login", api.login),
    path("api/sites", api.sites),
    path("api/scans", api.scans),
    path("api/scans/<int:scan_id>", api.scan_detail),
    path("api/scans/<int:scan_id>/reprocess", api.scan_reprocess),
    path("api/scans/<int:scan_id>/files/<str:kind>", api.scan_file),
    path("api/scans/<int:scan_id>/measurements", api.measurements),
    path("api/measurements/<int:measurement_id>", api.measurement_delete),
    path("api/uploads", api.upload_create),
    path("api/uploads/<uuid:upload_id>", api.upload_status),
    path("api/uploads/<uuid:upload_id>/chunks/<int:index>", api.upload_chunk),
    path("api/uploads/<uuid:upload_id>/complete", api.upload_complete),
    path("api/compare", api.compare),
    path("api/compare/<int:a_id>/<int:b_id>/diff.png", api.compare_image),
]
