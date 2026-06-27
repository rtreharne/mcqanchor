import time

from django.conf import settings
from django.core.management.base import BaseCommand

from standalone.services.question_builder import run_question_bank_builder_cycle


class Command(BaseCommand):
    help = "Run the per-course background question bank builder loop."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single builder cycle and exit.",
        )

    def handle(self, *args, **options):
        once = bool(options.get("once"))
        poll_seconds = max(5, int(settings.QUESTION_BANK_BUILDER_POLL_SECONDS or 60))

        while True:
            results = run_question_bank_builder_cycle()
            generated = sum(1 for result in results if result.generated)
            self.stdout.write(
                f"question-bank-builder cycle complete: scanned={len(results)} generated={generated}"
            )
            if once:
                return
            time.sleep(poll_seconds)
