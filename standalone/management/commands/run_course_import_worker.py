from django.core.management.base import BaseCommand

from standalone.services.course_imports import run_course_import_worker_once


class Command(BaseCommand):
    help = "Run one safe, resumable course-import worker step."

    def add_arguments(self, parser):
        parser.add_argument("import_id", nargs="?", type=int)
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single worker step and exit.",
        )

    def handle(self, *args, **options):
        import_id = options.get("import_id")
        processed = run_course_import_worker_once(import_id, chain_successor=True)
        if processed:
            self.stdout.write(self.style.SUCCESS("Course import worker step completed."))
        else:
            self.stdout.write("No runnable course import work found.")
