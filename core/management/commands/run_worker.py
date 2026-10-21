import time

from django.core.management.base import BaseCommand

from core.pipeline import claim_next_job, run_job


class Command(BaseCommand):
    help = "Run the TerraForge processing worker (polls the job queue in the database)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="process queued jobs and exit")

    def handle(self, *args, once=False, **opts):
        self.stdout.write("TerraForge worker started, waiting for jobs... (Ctrl+C to stop)")
        while True:
            job = claim_next_job()
            if job:
                self.stdout.write(f"Processing job {job.pk} for scan {job.scan_id} ({job.scan.title})")
                run_job(job)
                self.stdout.write(self.style.SUCCESS(f"Job {job.pk} finished: {job.state}"))
            elif once:
                return
            else:
                time.sleep(1.0)
