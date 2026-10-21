from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from core.models import Profile, Site

USERS = [
    ("admin", Profile.ADMIN, True),
    ("operator", Profile.OPERATOR, False),
    ("reviewer", Profile.REVIEWER, False),
]
PASSWORD = "terraforge"


class Command(BaseCommand):
    help = "Create demo users (password 'terraforge') and a demo site."

    def handle(self, *args, **opts):
        users = {}
        for name, role, staff in USERS:
            user, created = User.objects.get_or_create(username=name, defaults={"email": f"{name}@terraforge.local"})
            if created:
                user.set_password(PASSWORD)
            user.is_staff = user.is_superuser = staff
            user.save()
            Profile.objects.update_or_create(user=user, defaults={"role": role})
            users[name] = user
            self.stdout.write(f"user {name:9s} role={role:9s} password={PASSWORD}")

        site, _ = Site.objects.get_or_create(
            name="FAST Lahore Campus", defaults={"owner": users["admin"], "centre_lat": 31.4815, "centre_lon": 74.3030})
        site.members.add(users["operator"], users["reviewer"])
        self.stdout.write(self.style.SUCCESS(f"site '{site.name}' (id {site.pk}) ready"))
