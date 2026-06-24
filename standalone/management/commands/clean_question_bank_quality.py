import json
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from standalone.models import QuestionBankItem
from standalone.services.questions import (
    QuestionGenerationError,
    generate_question_pair_for_block,
    question_quality_issue,
)


class Command(BaseCommand):
    help = "Flag stored low-quality MCQs and generate replacement question pairs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--course-id",
            type=int,
            help="Limit cleanup to one course ID.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum number of flagged practice MCQs to process. 0 means no limit.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the cleanup. Without this flag the command only reports what would happen.",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Optional path to write a JSON report.",
        )

    def handle(self, *args, **options):
        queryset = (
            QuestionBankItem.objects.filter(
                bank_type=QuestionBankItem.BankType.PRACTICE,
                status=QuestionBankItem.Status.APPROVED,
                question_type=QuestionBankItem.QuestionType.MCQ,
            )
            .select_related("course", "block", "learning_objective", "linked_question")
            .order_by("course_id", "block__order", "pk")
        )
        course_id = options.get("course_id")
        if course_id:
            queryset = queryset.filter(course_id=course_id)

        flagged_practice_rows: list[QuestionBankItem] = []
        for question in queryset:
            if question_quality_issue(question):
                flagged_practice_rows.append(question)

        limit = max(0, int(options.get("limit") or 0))
        if limit:
            flagged_practice_rows = flagged_practice_rows[:limit]

        apply_changes = bool(options.get("apply"))
        report_rows: list[dict] = []
        retired_question_ids: list[int] = []
        regenerated_pairs: list[dict] = []
        generation_failures: list[dict] = []
        existing_hashes_by_course: dict[int, set[str]] = {}

        for question in flagged_practice_rows:
            issue = question_quality_issue(question)
            linked_question = question.linked_question
            row = {
                "practice_question_id": question.pk,
                "validation_question_id": linked_question.pk if linked_question else None,
                "course_id": question.course_id,
                "course_title": question.course.title,
                "block_id": question.block_id,
                "block_title": question.block.title if question.block_id else "",
                "learning_objective_id": question.learning_objective_id,
                "issue": issue,
                "stem": question.stem,
            }
            report_rows.append(row)
            if not apply_changes:
                continue

            with transaction.atomic():
                question.status = QuestionBankItem.Status.FLAGGED
                question.save(update_fields=["status", "updated_at"])
                retired_question_ids.append(question.pk)
                if linked_question and linked_question.status != QuestionBankItem.Status.FLAGGED:
                    linked_question.status = QuestionBankItem.Status.FLAGGED
                    linked_question.save(update_fields=["status", "updated_at"])
                    retired_question_ids.append(linked_question.pk)

            existing_hashes = existing_hashes_by_course.get(question.course_id)
            if existing_hashes is None:
                existing_hashes = set(
                    QuestionBankItem.objects.filter(course=question.course).values_list("question_hash", flat=True)
                )
                existing_hashes_by_course[question.course_id] = existing_hashes

            preferred_objective_ids = [int(question.learning_objective_id)] if question.learning_objective_id else None
            try:
                practice, validation = generate_question_pair_for_block(
                    question.block,
                    existing_hashes=existing_hashes,
                    preferred_objective_ids=preferred_objective_ids,
                    strict_preferred_objectives=bool(preferred_objective_ids),
                    question_type=QuestionBankItem.QuestionType.MCQ,
                    raise_generation_errors=True,
                )
            except QuestionGenerationError as exc:
                generation_failures.append(
                    {
                        **row,
                        "error": str(exc),
                    }
                )
                continue

            replacement_issue = question_quality_issue(practice) if practice is not None else "Replacement practice question was not created."
            if replacement_issue:
                with transaction.atomic():
                    if practice is not None and practice.status != QuestionBankItem.Status.FLAGGED:
                        practice.status = QuestionBankItem.Status.FLAGGED
                        practice.save(update_fields=["status", "updated_at"])
                        retired_question_ids.append(practice.pk)
                    if validation is not None and validation.status != QuestionBankItem.Status.FLAGGED:
                        validation.status = QuestionBankItem.Status.FLAGGED
                        validation.save(update_fields=["status", "updated_at"])
                        retired_question_ids.append(validation.pk)
                generation_failures.append(
                    {
                        **row,
                        "error": f"Generated replacement failed quality checks: {replacement_issue}",
                        "replacement_practice_question_id": practice.pk if practice else None,
                        "replacement_validation_question_id": validation.pk if validation else None,
                    }
                )
                continue

            regenerated_pairs.append(
                {
                    **row,
                    "replacement_practice_question_id": practice.pk if practice else None,
                    "replacement_validation_question_id": validation.pk if validation else None,
                }
            )

        summary = {
            "course_id": course_id,
            "apply": apply_changes,
            "flagged_practice_count": len(flagged_practice_rows),
            "retired_question_count": len(retired_question_ids),
            "retired_question_ids": retired_question_ids,
            "replacement_pair_count": len(regenerated_pairs),
            "generation_failure_count": len(generation_failures),
            "processed_rows": report_rows,
            "regenerated_pairs": regenerated_pairs,
            "generation_failures": generation_failures,
        }

        output_text = json.dumps(summary, indent=2, ensure_ascii=False)
        output_path = str(options.get("output") or "").strip()
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output_text + ("\n" if not output_text.endswith("\n") else ""), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote report to {path}"))

        if apply_changes:
            per_course = defaultdict(int)
            for row in flagged_practice_rows:
                per_course[(row.course_id, row.course.title)] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"Flagged {len(flagged_practice_rows)} practice MCQ(s), retired {len(retired_question_ids)} row(s), "
                    f"generated {len(regenerated_pairs)} replacement pair(s), failures={len(generation_failures)}."
                )
            )
            for (cid, title), count in sorted(per_course.items(), key=lambda item: (-item[1], item[0][0])):
                self.stdout.write(f"- course {cid}: {title} -> {count} flagged practice MCQ(s)")
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run only. {len(flagged_practice_rows)} flagged practice MCQ(s) would be processed."
                )
            )

        self.stdout.write(output_text)
