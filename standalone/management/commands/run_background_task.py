import json

from django.core.management.base import BaseCommand, CommandError

from standalone.services.background_dispatch import _run_registered_task


class Command(BaseCommand):
    help = "Run a single registered background task."

    def add_arguments(self, parser):
        parser.add_argument("task_name")
        parser.add_argument("task_args_json", nargs="?", default="[]")

    def handle(self, *args, **options):
        task_name = str(options["task_name"] or "").strip()
        try:
            task_args = json.loads(options["task_args_json"] or "[]")
        except json.JSONDecodeError as exc:
            raise CommandError(f"Invalid task argument payload: {exc}") from exc
        if not isinstance(task_args, list):
            raise CommandError("Task argument payload must decode to a JSON list.")
        _run_registered_task(task_name, *task_args)
        self.stdout.write(self.style.SUCCESS(f"Completed background task {task_name}."))
