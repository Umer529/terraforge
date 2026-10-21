from django.contrib import admin

from .models import LocationFix, Measurement, Output, ProcessingJob, Profile, Scan, Site


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)


@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "centre_lat", "centre_lon", "created_at")
    filter_horizontal = ("members",)


class JobInline(admin.TabularInline):
    model = ProcessingJob
    extra = 0
    fields = ("state", "started_at", "finished_at")
    readonly_fields = fields


@admin.register(Scan)
class ScanAdmin(admin.ModelAdmin):
    list_display = ("title", "site", "captured_by", "captured_at", "status")
    list_filter = ("status", "site")
    inlines = [JobInline]


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ("id", "scan", "state", "started_at", "finished_at")
    list_filter = ("state",)


admin.site.register(Output)
admin.site.register(LocationFix)
admin.site.register(Measurement)
admin.site.site_header = "TerraForge administration"
