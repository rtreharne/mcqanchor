import json
from pathlib import Path

from django.core.management.base import BaseCommand

from standalone.models import Course, QuestionBankItem
from standalone.services.questions import question_quality_issue


class Command(BaseCommand):
    help = "Scan stored question-bank MCQs for option-balance issues."

    def add_arguments(self, parser):
        parser.add_argument(
            "--course-id",
            type=int,
            help="Limit the scan to one course ID.",
        )
        parser.add_argument(
            "--bank-type",
            choices=("practice", "validation", "all"),
            default="practice",
            help="Which bank to scan. Defaults to practice to avoid double-counting linked pairs.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximum number of flagged rows to print. 0 means no limit.",
        )
        parser.add_argument(
            "--format",
            choices=("text", "json"),
            default="text",
            help="Output format.",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Optional path to write the report.",
        )

    def handle(self, *args, **options):
        queryset = (
            QuestionBankItem.objects.filter(
                status=QuestionBankItem.Status.APPROVED,
                question_type=QuestionBankItem.QuestionType.MCQ,
            )
            .select_related("course", "block", "learning_objective", "linked_question")
            .order_by("course_id", "block__order", "bank_type", "pk")
        )

        course_id = options.get("course_id")
        if course_id:
            queryset = queryset.filter(course_id=course_id)

        bank_type = options["bank_type"]
        if bank_type != "all":
            queryset = queryset.filter(bank_type=bank_type)

        rows = []
        per_course_counts: dict[int, int] = {}
        for question in queryset:
            issue = question_quality_issue(question)
            if not issue:
                continue
            per_course_counts[question.course_id] = per_course_counts.get(question.course_id, 0) + 1
            rows.append(
                {
                    "question_id": question.pk,
                    "linked_question_id": question.linked_question_id,
                    "course_id": question.course_id,
                    "course_title": question.course.title,
                    "block_id": question.block_id,
                    "block_title": question.block.title if question.block_id else "",
                    "learning_objective_id": question.learning_objective_id,
                    "bank_type": question.bank_type,
                    "issue": issue,
                    "stem": question.stem,
                    "correct_answer": question.correct_answer,
                    "distractors": list(question.distractors or []),
                }
            )

        limit = max(0, int(options.get("limit") or 0))
        display_rows = rows[:limit] if limit else rows
        summary = {
            "scan_scope": {
                "course_id": course_id,
                "bank_type": bank_type,
            },
            "flagged_count": len(rows),
            "scanned_count": queryset.count(),
            "course_count": len(per_course_counts),
            "flagged_course_ids": sorted(per_course_counts),
            "flagged_rows": display_rows,
        }

        if options["format"] == "json":
            output_text = json.dumps(summary, indent=2, ensure_ascii=False)
        else:
            lines = [
                f"Scanned {summary['scanned_count']} approved MCQ rows.",
                f"Flagged {summary['flagged_count']} rows across {summary['course_count']} course(s).",
            ]
            for row in display_rows:
                lines.extend(
                    [
                        "",
                        f"[course {row['course_id']}] {row['course_title']}",
                        f"  question_id={row['question_id']} bank_type={row['bank_type']} block={row['block_title']} ({row['block_id']})",
                        f"  issue={row['issue']}",
                        f"  stem={row['stem']}",
                        f"  correct={row['correct_answer']}",
                        "  distractors=" + " | ".join(row["distractors"]),
                    ]
                )
            if limit and len(rows) > len(display_rows):
                lines.extend(
                    [
                        "",
                        f"... truncated {len(rows) - len(display_rows)} additional flagged row(s).",
                    ]
                )
            output_text = "\n".join(lines)

        output_path = str(options.get("output") or "").strip()
        if output_path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(output_text + ("\n" if not output_text.endswith("\n") else ""), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Wrote report to {path}"))

        self.stdout.write(output_text)

