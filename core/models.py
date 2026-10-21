"""Domain model, following the data dictionary (Table 4.x) of the TerraForge report."""
import secrets
import uuid

from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class Profile(models.Model):
    OPERATOR, REVIEWER, ADMIN = "operator", "reviewer", "admin"
    ROLES = [(OPERATOR, "Field operator"), (REVIEWER, "Project reviewer"), (ADMIN, "Administrator")]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=16, choices=ROLES, default=REVIEWER)

    def __str__(self):
        return f"{self.user} ({self.get_role_display()})"


class ApiToken(models.Model):
    """Bearer token for the mobile app, which cannot use browser sessions."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    key = models.CharField(max_length=64, unique=True, default=secrets.token_hex)
    created_at = models.DateTimeField(auto_now_add=True)


class Site(models.Model):
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="owned_sites")
    members = models.ManyToManyField(User, blank=True, related_name="sites")
    name = models.CharField(max_length=120)
    centre_lat = models.FloatField(null=True, blank=True)
    centre_lon = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Scan(models.Model):
    UPLOADING, QUEUED, PROCESSING, READY, FAILED = "uploading", "queued", "processing", "ready", "failed"
    STATUSES = [(s, s.title()) for s in (UPLOADING, QUEUED, PROCESSING, READY, FAILED)]

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="scans")
    captured_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="scans")
    title = models.CharField(max_length=120)
    notes = models.TextField(blank=True)
    captured_at = models.DateTimeField()
    path_length_m = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=STATUSES, default=UPLOADING)
    device_model = models.CharField(max_length=80, blank=True)
    # Local ENU frame origin set by the worker from the GPS fixes.
    origin_lat = models.FloatField(null=True, blank=True)
    origin_lon = models.FloatField(null=True, blank=True)
    origin_alt = models.FloatField(null=True, blank=True)
    package_path = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-captured_at"]

    def __str__(self):
        return self.title

    @property
    def georeferenced(self):
        return self.origin_lat is not None

    def output(self, kind):
        return self.outputs.filter(kind=kind).order_by("-id").first()


class UploadSession(models.Model):
    """One resumable chunked upload. Received chunks are files on disk."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scan = models.OneToOneField(Scan, on_delete=models.CASCADE, related_name="upload")
    filename = models.CharField(max_length=200)
    size = models.BigIntegerField()
    chunk_size = models.IntegerField()
    total_chunks = models.IntegerField()
    sha256 = models.CharField(max_length=64, blank=True)
    options = models.JSONField(default=dict)  # e.g. up_axis for raw exports
    complete = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class LocationFix(models.Model):
    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="fixes")
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude = models.FloatField(null=True, blank=True)
    horizontal_accuracy = models.FloatField(null=True, blank=True)
    recorded_at = models.DateTimeField(null=True, blank=True)


class ProcessingJob(models.Model):
    QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
    STATES = [(s, s.title()) for s in (QUEUED, RUNNING, DONE, FAILED)]

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="jobs")
    state = models.CharField(max_length=16, choices=STATES, default=QUEUED)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    log = models.TextField(blank=True)


class Output(models.Model):
    POINTCLOUD, DEM, GEOTIFF, PREVIEW = "pointcloud", "dem", "geotiff", "preview"
    KINDS = [(POINTCLOUD, "Point cloud"), (DEM, "Elevation grid"), (GEOTIFF, "GeoTIFF map"), (PREVIEW, "Map preview")]

    job = models.ForeignKey(ProcessingJob, on_delete=models.CASCADE, related_name="outputs")
    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="outputs")
    kind = models.CharField(max_length=16, choices=KINDS)
    file_path = models.CharField(max_length=300)
    crs = models.CharField(max_length=40, blank=True)
    bounding_box = models.JSONField(default=dict)
    meta = models.JSONField(default=dict)


class Measurement(models.Model):
    DISTANCE, AREA, VOLUME = "distance", "area", "volume"
    KINDS = [(DISTANCE, "Distance"), (AREA, "Area"), (VOLUME, "Volume")]

    scan = models.ForeignKey(Scan, on_delete=models.CASCADE, related_name="measurements")
    output = models.ForeignKey(Output, on_delete=models.SET_NULL, null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=KINDS)
    label = models.CharField(max_length=120, blank=True)
    value = models.FloatField()
    unit = models.CharField(max_length=8)
    reference_surface = models.CharField(max_length=60, blank=True)
    points = models.JSONField(default=list)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
