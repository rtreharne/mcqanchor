import ast
import csv
import hashlib
import io
import json
import math
import random
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from openai import OpenAI

from standalone.models import (
    BlockProject,
    CourseBlock,
    Enrollment,
    ProjectArtifact,
    ProjectAssignment,
    ProjectMessage,
    ProjectSubmission,
)


PROJECT_PREVIEW_SESSION_KEY = "standalone_project_preview"
SUPPORTED_PROJECT_UNITS = {
    "mm3": Decimal("1"),
    "cm3": Decimal("1000"),
    "mm^3": Decimal("1"),
    "cm^3": Decimal("1000"),
    "mm³": Decimal("1"),
    "cm³": Decimal("1000"),
}
ALLOWED_EXPR_FUNCS = {
    "abs": abs,
    "max": max,
    "min": min,
    "round": round,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
}
ALLOWED_EXPR_CONSTS = {
    "pi": math.pi,
    "e": math.e,
}


class ProjectSpecError(ValueError):
    pass


class ProjectImmutableError(ValueError):
    pass


class ProjectAuthoringError(ValueError):
    pass


def _strip_text(value) -> str:
    return str(value or "").strip()


def _parse_json_object(raw_output: str):
    normalized_output = _strip_text(raw_output)
    if not normalized_output:
        raise ProjectAuthoringError("OpenAI returned an empty JSON payload.")
    try:
        return json.loads(normalized_output)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", normalized_output, re.DOTALL)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    object_match = re.search(r"\{.*\}", normalized_output, re.DOTALL)
    if object_match:
        return json.loads(object_match.group(0))

    raise ProjectAuthoringError("OpenAI did not return parseable JSON.")


def _canonical_unit(unit: str) -> str:
    normalized = _strip_text(unit).lower().replace(" ", "")
    normalized = normalized.replace("³", "3")
    normalized = normalized.replace("^", "")
    return normalized


def _quantize(value: Decimal, decimal_places: int) -> Decimal:
    exponent = Decimal("1").scaleb(-int(decimal_places))
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


def _display_number(value: Decimal, decimal_places: int) -> str:
    quantized = _quantize(value, decimal_places)
    if decimal_places <= 0:
        return str(int(quantized))
    return f"{quantized:.{decimal_places}f}"


def _normalize_submitted_numeric_answer(raw_answer: str, *, required_unit: str, decimal_places: int) -> dict:
    submitted_text = _strip_text(raw_answer)
    if not submitted_text:
        return {
            "ok": False,
            "code": "empty",
            "message": "Enter a numeric answer first.",
        }

    normalized = submitted_text.replace(",", "").replace("−", "-").replace("–", "-")
    match = re.match(r"^\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([A-Za-z0-9^³]*)\s*$", normalized)
    if not match:
        return {
            "ok": False,
            "code": "invalid_format",
            "message": "Use a single numeric answer, with an optional unit.",
        }

    try:
        numeric_value = Decimal(match.group(1))
    except InvalidOperation:
        return {
            "ok": False,
            "code": "invalid_format",
            "message": "Use a valid numeric answer.",
        }

    submitted_unit = _canonical_unit(match.group(2) or required_unit or "")
    target_unit = _canonical_unit(required_unit)
    converted_value = numeric_value
    if submitted_unit and target_unit and submitted_unit != target_unit:
        if submitted_unit not in SUPPORTED_PROJECT_UNITS or target_unit not in SUPPORTED_PROJECT_UNITS:
            return {
                "ok": False,
                "code": "wrong_unit",
                "message": f"Please submit your answer in {required_unit}.",
            }
        converted_value = numeric_value * (SUPPORTED_PROJECT_UNITS[submitted_unit] / SUPPORTED_PROJECT_UNITS[target_unit])

    quantized = _quantize(converted_value, decimal_places)
    return {
        "ok": True,
        "numeric_value": converted_value,
        "quantized": quantized,
        "normalized": _display_number(converted_value, decimal_places),
        "submitted_unit": submitted_unit,
    }


def _stable_seed(*parts) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    numeric = int(digest[:12], 16)
    return str(100000 + (numeric % 900000))


def _allowed_ast_node(node):
    allowed_nodes = (
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Call,
        ast.Load,
        ast.Name,
        ast.Constant,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.Mod,
        ast.USub,
        ast.UAdd,
        ast.FloorDiv,
    )
    if not isinstance(node, allowed_nodes):
        raise ProjectSpecError("Unsupported expression in project spec.")
    for child in ast.iter_child_nodes(node):
        _allowed_ast_node(child)


def _safe_eval_expression(expression: str, values: dict):
    try:
        tree = ast.parse(str(expression or ""), mode="eval")
    except SyntaxError as exc:
        raise ProjectSpecError("Project expression could not be parsed.") from exc
    _allowed_ast_node(tree)

    compiled = compile(tree, "<project-expression>", "eval")
    names = {**ALLOWED_EXPR_CONSTS, **ALLOWED_EXPR_FUNCS, **values}
    try:
        return eval(compiled, {"__builtins__": {}}, names)
    except Exception as exc:  # noqa: S307 - bounded eval with strict AST and names
        raise ProjectSpecError("Project expression could not be evaluated.") from exc


def _validate_hint_plan(hint_plan_json):
    hint_plan = hint_plan_json if isinstance(hint_plan_json, dict) else {}
    hints = hint_plan.get("hints")
    if hints is None:
        hint_plan["hints"] = []
    elif not isinstance(hints, list) or any(not _strip_text(item) for item in hints):
        raise ProjectSpecError("Project hints must be a list of non-empty strings.")
    return hint_plan


def validate_block_project_spec(project: BlockProject) -> None:
    if project.engine_type not in {
        BlockProject.EngineType.TABULAR_ANALYSIS,
        BlockProject.EngineType.SEEDED_SCRIPT_OUTPUT,
    }:
        raise ProjectSpecError("Choose a supported project engine before publishing.")
    if not _strip_text(project.title):
        raise ProjectSpecError("Add a project title before publishing.")
    if not _strip_text(project.student_instructions):
        raise ProjectSpecError("Add student-facing instructions before publishing.")
    _validate_hint_plan(project.hint_plan_json)
    if not isinstance(project.spec_json, dict) or not project.spec_json:
        raise ProjectSpecError("Project spec JSON is missing.")

    if project.engine_type == BlockProject.EngineType.TABULAR_ANALYSIS:
        dataset = project.spec_json.get("dataset")
        if not isinstance(dataset, dict):
            raise ProjectSpecError("Tabular-analysis projects need a dataset spec.")
        row_count = int(dataset.get("row_count") or 0)
        if row_count < 2:
            raise ProjectSpecError("Tabular-analysis projects need at least two data rows.")
        columns = dataset.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ProjectSpecError("Tabular-analysis projects need at least one dataset column.")
        for column in columns:
            if not isinstance(column, dict):
                raise ProjectSpecError("Dataset columns must be objects.")
            if not _strip_text(column.get("name")):
                raise ProjectSpecError("Each dataset column needs a name.")
            generator = column.get("generator")
            if not isinstance(generator, dict):
                raise ProjectSpecError("Each dataset column needs a generator.")
            kind = generator.get("kind")
            if kind not in {"uniform", "randint", "sequence", "formula"}:
                raise ProjectSpecError("Unsupported dataset generator kind.")
            if kind == "formula" and not _strip_text(generator.get("expression")):
                raise ProjectSpecError("Formula dataset columns need an expression.")
        operations = project.spec_json.get("operations") or []
        if not isinstance(operations, list):
            raise ProjectSpecError("Project operations must be a list.")
        if not _strip_text(project.spec_json.get("final_answer_name")):
            raise ProjectSpecError("Tabular-analysis projects need a final answer output name.")

    if project.engine_type == BlockProject.EngineType.SEEDED_SCRIPT_OUTPUT:
        filename_template = _strip_text(project.spec_json.get("filename_template"))
        if not filename_template:
            raise ProjectSpecError("Seeded-script projects need a filename template.")
        if not isinstance(project.spec_json.get("steps"), list) or not project.spec_json.get("steps"):
            raise ProjectSpecError("Seeded-script projects need at least one deterministic step.")
        if not _strip_text(project.spec_json.get("output_name")):
            raise ProjectSpecError("Seeded-script projects need an output name.")
        for step in project.spec_json["steps"]:
            kind = _strip_text(step.get("kind"))
            if kind not in {"lcg_random", "expression"}:
                raise ProjectSpecError("Unsupported seeded-script step kind.")
            if kind == "expression" and not _strip_text(step.get("expression")):
                raise ProjectSpecError("Expression steps need an expression.")


def _project_authoring_prompt(project: BlockProject) -> str:
    return f"""
You are designing a deterministic educational mini-project.

Allowed engines:
1. tabular_analysis
2. seeded_script_output

Constraints:
- Runtime must not call OpenAI.
- Only create seeded numeric final-answer projects.
- Output strict JSON only.
- If the prompt cannot be lowered safely, return supported=false and explain why.
- Use `tabular_analysis` for CSV/data-analysis tasks.
- Use `seeded_script_output` only for deterministic whitelisted pseudo-script tasks.
- Do not emit executable arbitrary code in the spec.

Supported tabular_analysis spec shape:
{{
  "dataset": {{
    "filename_template": "snails_{{seed}}.csv",
    "row_count": 24,
    "columns": [
      {{
        "name": "Mass (g)",
        "alias": "mass_g",
        "generator": {{"kind": "uniform", "min": 2, "max": 18, "decimals": 2}}
      }}
    ]
  }},
  "operations": [
    {{
      "kind": "derive_column",
      "name": "Volume V (mm3)",
      "alias": "volume_mm3",
      "expression": "(4/3) * pi * ((height_mm / 2) ** 3)",
      "decimals": 3
    }},
    {{
      "kind": "linear_regression",
      "x_alias": "mass_g",
      "y_alias": "volume_mm3",
      "slope_name": "slope",
      "intercept_name": "intercept"
    }},
    {{
      "kind": "predict_linear",
      "slope_name": "slope",
      "intercept_name": "intercept",
      "x_value": 10,
      "output_name": "predicted_volume_mm3"
    }},
    {{
      "kind": "convert_unit",
      "input_name": "predicted_volume_mm3",
      "factor": 0.001,
      "output_name": "predicted_volume_cm3"
    }},
    {{
      "kind": "round",
      "input_name": "predicted_volume_cm3",
      "decimal_places": 1,
      "output_name": "final_answer"
    }}
  ],
  "final_answer_name": "final_answer"
}}

Supported seeded_script_output spec shape:
{{
  "filename_template": "seeded_project_{{seed}}.R",
  "language": "r",
  "steps": [
    {{"kind": "lcg_random", "name": "random_number", "decimals": 4}},
    {{"kind": "expression", "name": "final_answer", "expression": "random_number"}}
  ],
  "output_name": "final_answer"
}}

Return JSON with exactly these keys:
supported, title, engine_type, student_instructions, answer_label, answer_unit, decimal_places, hint_plan, spec, unsupported_reason

`hint_plan` must be an object with keys: intro, hints, wrong_unit, wrong_precision, wrong_value, completion.

Teacher prompt:
{project.teacher_prompt}

Example text:
{project.example_text or "(none)"}
""".strip()


def generate_block_project_draft(project: BlockProject) -> BlockProject:
    if not settings.OPENAI_API_KEY:
        raise ProjectAuthoringError("Set OPENAI_API_KEY before generating project drafts.")

    project.generation_status = BlockProject.GenerationStatus.RUNNING
    project.generation_error = ""
    project.save(update_fields=["generation_status", "generation_error", "updated_at"])

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Return only valid JSON."}]},
            {"role": "user", "content": [{"type": "input_text", "text": _project_authoring_prompt(project)}]},
        ],
    )
    payload = _parse_json_object(getattr(response, "output_text", ""))
    if not payload.get("supported"):
        project.generation_status = BlockProject.GenerationStatus.UNSUPPORTED
        project.generation_error = _strip_text(payload.get("unsupported_reason")) or "This prompt could not be lowered into a supported deterministic project."
        project.save(update_fields=["generation_status", "generation_error", "updated_at"])
        return project

    project.title = _strip_text(payload.get("title")) or project.title or "Untitled project"
    project.engine_type = _strip_text(payload.get("engine_type"))
    project.student_instructions = _strip_text(payload.get("student_instructions"))
    project.answer_label = _strip_text(payload.get("answer_label")) or "Answer"
    project.answer_unit = _strip_text(payload.get("answer_unit"))
    project.decimal_places = max(0, min(6, int(payload.get("decimal_places") or 0)))
    project.spec_json = payload.get("spec") or {}
    project.hint_plan_json = payload.get("hint_plan") or {}
    validate_block_project_spec(project)
    project.generation_status = BlockProject.GenerationStatus.READY
    project.generation_error = ""
    project.save(
        update_fields=[
            "title",
            "engine_type",
            "student_instructions",
            "answer_label",
            "answer_unit",
            "decimal_places",
            "spec_json",
            "hint_plan_json",
            "generation_status",
            "generation_error",
            "updated_at",
        ]
    )
    return project


def _round_if_needed(value, decimals):
    if decimals is None:
        return value
    return round(float(value), int(decimals))


def _column_alias(column: dict) -> str:
    return _strip_text(column.get("alias")) or re.sub(r"[^a-z0-9]+", "_", _strip_text(column.get("name")).lower()).strip("_")


def _row_generator_value(rng: random.Random, generator: dict, row_index: int, row_values: dict):
    kind = generator.get("kind")
    decimals = generator.get("decimals")
    if kind == "uniform":
        value = rng.uniform(float(generator.get("min", 0)), float(generator.get("max", 0)))
        return _round_if_needed(value, decimals)
    if kind == "randint":
        return int(rng.randint(int(generator.get("min", 0)), int(generator.get("max", 0))))
    if kind == "sequence":
        start = float(generator.get("start", 0))
        step = float(generator.get("step", 1))
        jitter = float(generator.get("jitter", 0) or 0)
        value = start + (step * row_index)
        if jitter:
            value += rng.uniform(-jitter, jitter)
        return _round_if_needed(value, decimals)
    if kind == "formula":
        expression = _strip_text(generator.get("expression"))
        base_value = _safe_eval_expression(expression, {**row_values, "row_index": row_index + 1})
        noise = generator.get("noise") or {}
        if noise.get("kind") == "uniform":
            base_value += rng.uniform(float(noise.get("min", 0)), float(noise.get("max", 0)))
        return _round_if_needed(base_value, decimals)
    raise ProjectSpecError("Unsupported dataset generator.")


def _linear_regression(x_values, y_values):
    count = len(x_values)
    if count < 2:
        raise ProjectSpecError("Linear regression needs at least two rows.")
    x_mean = sum(x_values) / count
    y_mean = sum(y_values) / count
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    if not denominator:
        raise ProjectSpecError("Linear regression denominator is zero.")
    slope = numerator / denominator
    intercept = y_mean - (slope * x_mean)
    return slope, intercept


def _materialize_tabular_analysis(project: BlockProject, seed: str) -> dict:
    spec = project.spec_json
    dataset_spec = spec["dataset"]
    rng = random.Random(int(seed))
    rows = []
    aliases_by_name = {}
    for row_index in range(int(dataset_spec["row_count"])):
        row_values = {}
        row_display = {}
        for column in dataset_spec["columns"]:
            alias = _column_alias(column)
            value = _row_generator_value(rng, column["generator"], row_index, row_values)
            row_values[alias] = value
            row_display[column["name"]] = value
            aliases_by_name[column["name"]] = alias
        rows.append({"values": row_values, "display": row_display})

    scalars = {}
    for operation in spec.get("operations", []):
        kind = operation.get("kind")
        if kind == "derive_column":
            alias = _column_alias({"alias": operation.get("alias"), "name": operation.get("name")})
            for row in rows:
                value = _safe_eval_expression(operation.get("expression"), row["values"])
                value = _round_if_needed(value, operation.get("decimals"))
                row["values"][alias] = value
                row["display"][operation["name"]] = value
        elif kind == "linear_regression":
            x_alias = _strip_text(operation.get("x_alias"))
            y_alias = _strip_text(operation.get("y_alias"))
            x_values = [float(row["values"][x_alias]) for row in rows]
            y_values = [float(row["values"][y_alias]) for row in rows]
            slope, intercept = _linear_regression(x_values, y_values)
            scalars[_strip_text(operation.get("slope_name")) or "slope"] = slope
            scalars[_strip_text(operation.get("intercept_name")) or "intercept"] = intercept
        elif kind == "predict_linear":
            slope = float(scalars[_strip_text(operation.get("slope_name"))])
            intercept = float(scalars[_strip_text(operation.get("intercept_name"))])
            x_value = float(operation.get("x_value"))
            scalars[_strip_text(operation.get("output_name"))] = (slope * x_value) + intercept
        elif kind == "convert_unit":
            input_name = _strip_text(operation.get("input_name"))
            factor = float(operation.get("factor"))
            scalars[_strip_text(operation.get("output_name"))] = float(scalars[input_name]) * factor
        elif kind == "round":
            input_name = _strip_text(operation.get("input_name"))
            decimals = int(operation.get("decimal_places") or 0)
            scalars[_strip_text(operation.get("output_name"))] = round(float(scalars[input_name]), decimals)
        else:
            raise ProjectSpecError("Unsupported project operation.")

    final_answer_name = _strip_text(spec.get("final_answer_name"))
    if final_answer_name not in scalars:
        raise ProjectSpecError("Final answer output was not produced by the project spec.")

    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=list(rows[0]["display"].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow(row["display"])

    filename_template = _strip_text(dataset_spec.get("filename_template")) or "dataset_{seed}.csv"
    return {
        "expected_value": Decimal(str(scalars[final_answer_name])),
        "payload": {
            "engine_type": BlockProject.EngineType.TABULAR_ANALYSIS,
            "rows": [row["display"] for row in rows],
            "scalars": scalars,
        },
        "artifacts": [
            {
                "key": "dataset",
                "kind": ProjectArtifact.Kind.DATASET,
                "label": "Dataset CSV",
                "filename": filename_template.replace("{seed}", str(seed)),
                "content": csv_buffer.getvalue().encode("utf-8"),
                "metadata": {"content_type": "text/csv"},
            }
        ],
    }


def _lcg_random(seed_value: int) -> float:
    state = (int(seed_value) * 9301 + 49297) % 233280
    return state / 233280


def _render_seeded_script(project: BlockProject, seed: str, values: dict) -> str:
    lines = [f"seed <- {int(seed)}", ""]
    for step in project.spec_json.get("steps", []):
        kind = step.get("kind")
        name = _strip_text(step.get("name"))
        if kind == "lcg_random":
            lines.extend(
                [
                    "state <- (seed * 9301 + 49297) %% 233280",
                    f"{name} <- state / 233280",
                    "",
                ]
            )
        elif kind == "expression":
            expression = _strip_text(step.get("expression"))
            lines.append(f"{name} <- {expression}")
            lines.append("")
    lines.append(f"print({project.spec_json.get('output_name')})")
    return "\n".join(lines).strip() + "\n"


def _materialize_seeded_script_output(project: BlockProject, seed: str) -> dict:
    values = {"seed": int(seed)}
    for step in project.spec_json.get("steps", []):
        kind = step.get("kind")
        name = _strip_text(step.get("name"))
        if kind == "lcg_random":
            value = _lcg_random(values["seed"])
            values[name] = _round_if_needed(value, step.get("decimals"))
        elif kind == "expression":
            values[name] = _safe_eval_expression(step.get("expression"), values)
        else:
            raise ProjectSpecError("Unsupported seeded-script step.")

    output_name = _strip_text(project.spec_json.get("output_name"))
    if output_name not in values:
        raise ProjectSpecError("Seeded-script output name was not produced by the project spec.")

    filename_template = _strip_text(project.spec_json.get("filename_template")) or "seeded_project_{seed}.R"
    return {
        "expected_value": Decimal(str(values[output_name])),
        "payload": {
            "engine_type": BlockProject.EngineType.SEEDED_SCRIPT_OUTPUT,
            "values": values,
            "script_language": _strip_text(project.spec_json.get("language")) or "r",
        },
        "artifacts": [
            {
                "key": "starter_script",
                "kind": ProjectArtifact.Kind.STARTER_SCRIPT,
                "label": "Starter script",
                "filename": filename_template.replace("{seed}", str(seed)),
                "content": _render_seeded_script(project, seed, values).encode("utf-8"),
                "metadata": {"content_type": "text/plain"},
            }
        ],
    }


def materialize_project_instance(project: BlockProject, seed: str) -> dict:
    validate_block_project_spec(project)
    if project.engine_type == BlockProject.EngineType.TABULAR_ANALYSIS:
        return _materialize_tabular_analysis(project, seed)
    if project.engine_type == BlockProject.EngineType.SEEDED_SCRIPT_OUTPUT:
        return _materialize_seeded_script_output(project, seed)
    raise ProjectSpecError("Unsupported project engine.")


def _project_expected_display(project: BlockProject, expected_value: Decimal) -> str:
    number = _display_number(expected_value, project.decimal_places)
    if _strip_text(project.answer_unit):
        return f"{number} {project.answer_unit}".strip()
    return number


def _serialize_project_message(message_id: str, sequence: int, role: str, kind: str, text: str, payload=None) -> dict:
    return {
        "id": message_id,
        "sequence": sequence,
        "created_at": timezone.now().isoformat(),
        "role": role,
        "kind": kind,
        "text": text,
        **(payload or {}),
    }


def _preview_project_state_root(request, course_id: int) -> dict:
    root = request.session.setdefault(PROJECT_PREVIEW_SESSION_KEY, {})
    return root.setdefault(str(course_id), {})


def _preview_project_session_state(request, project: BlockProject) -> dict | None:
    return _preview_project_state_root(request, project.block.course_id).get(str(project.pk))


def _next_preview_message_id(project_state: dict) -> tuple[str, int]:
    project_state["message_counter"] = int(project_state.get("message_counter", 0)) + 1
    counter = project_state["message_counter"]
    return f"preview-project-message-{counter}", counter


def _append_preview_message(project_state: dict, role: str, kind: str, text: str, payload=None) -> dict:
    message_id, sequence = _next_preview_message_id(project_state)
    message = _serialize_project_message(message_id, sequence, role, kind, text, payload)
    project_state.setdefault("transcript", []).append(message)
    return message


def _initialize_preview_project_state(request, project: BlockProject, viewer_identifier: str) -> dict:
    project_root = _preview_project_state_root(request, project.block.course_id)
    project_state = project_root.get(str(project.pk))
    if project_state:
        return project_state

    seed = _stable_seed("preview", viewer_identifier, project.pk)
    materialized = materialize_project_instance(project, seed)
    expected_value = materialized["expected_value"]
    project_state = {
        "seed": seed,
        "status": ProjectAssignment.Status.NOT_STARTED,
        "completed_at": None,
        "submission_count": 0,
        "latest_submitted_answer": "",
        "latest_normalized_answer": "",
        "expected_answer_display": _project_expected_display(project, expected_value),
        "normalized_expected_answer": _display_number(expected_value, project.decimal_places),
        "engine_payload_json": materialized["payload"],
        "transcript": [],
        "message_counter": 0,
    }
    intro = _strip_text(project.hint_plan_json.get("intro")) or "This project is ready when you are."
    _append_preview_message(project_state, "assistant", "project_intro", intro)
    project_root[str(project.pk)] = project_state
    request.session.modified = True
    return project_state


def ensure_project_assignment(enrollment: Enrollment, project: BlockProject) -> ProjectAssignment:
    assignment = ProjectAssignment.objects.filter(enrollment=enrollment, block_project=project).first()
    if assignment is not None:
        return assignment

    seed = _stable_seed("student", enrollment.student_id, enrollment.course_id, project.pk)
    materialized = materialize_project_instance(project, seed)
    expected_value = materialized["expected_value"]
    with transaction.atomic():
        assignment, created = ProjectAssignment.objects.get_or_create(
            enrollment=enrollment,
            block_project=project,
            defaults={
                "seed": seed,
                "expected_answer_display": _project_expected_display(project, expected_value),
                "normalized_expected_answer": _display_number(expected_value, project.decimal_places),
                "engine_payload_json": materialized["payload"],
            },
        )
        if created:
            for artifact in materialized["artifacts"]:
                ProjectArtifact.objects.create(
                    assignment=assignment,
                    kind=artifact["kind"],
                    label=artifact["label"],
                    file=ContentFile(artifact["content"], name=artifact["filename"]),
                    metadata=artifact.get("metadata") or {},
                )
            ProjectMessage.objects.create(
                assignment=assignment,
                message_id="project-message-1",
                sequence=1,
                role="assistant",
                kind="project_intro",
                text=_strip_text(project.hint_plan_json.get("intro")) or "This project is ready when you are.",
                payload={},
            )
    return assignment


def _serialize_preview_project_payload(project: BlockProject, project_state: dict | None) -> dict:
    materialized = project_state is not None
    transcript = list(project_state.get("transcript") or []) if materialized else []
    downloads = []
    if materialized:
        for artifact in materialize_project_instance(project, project_state["seed"])["artifacts"]:
            downloads.append(
                {
                    "label": artifact["label"],
                    "kind": artifact["kind"],
                    "url": reverse("standalone:block_project_preview_artifact_download", args=[project.pk, artifact["key"]]),
                }
            )
    return {
        "id": project.pk,
        "title": project.title,
        "status": project.status,
        "engine_type": project.engine_type,
        "student_instructions": project.student_instructions,
        "answer_label": project.answer_label,
        "answer_unit": project.answer_unit,
        "decimal_places": project.decimal_places,
        "seed": project_state.get("seed") if materialized else "",
        "assignment_status": project_state.get("status") if materialized else ProjectAssignment.Status.NOT_STARTED,
        "completed_at": project_state.get("completed_at") if materialized else None,
        "submission_count": int(project_state.get("submission_count", 0) or 0) if materialized else 0,
        "latest_submitted_answer": project_state.get("latest_submitted_answer", "") if materialized else "",
        "expected_display_answer": project_state.get("expected_answer_display", "") if materialized else "",
        "materialized": materialized,
        "transcript": transcript,
        "downloads": downloads,
        "hints_remaining": max(
            0,
            len(project.hint_plan_json.get("hints") or [])
            - len([item for item in transcript if item.get("kind") == "project_hint"]),
        ) if materialized else len(project.hint_plan_json.get("hints") or []),
    }


def _serialize_assignment_project_payload(project: BlockProject, assignment: ProjectAssignment | None) -> dict:
    materialized = assignment is not None
    messages = []
    downloads = []
    if assignment is not None:
        messages = [
            {
                **(message.payload or {}),
                "id": message.message_id,
                "sequence": message.sequence,
                "created_at": message.created_at.isoformat(),
                "role": message.role,
                "kind": message.kind,
                "text": message.text,
            }
            for message in assignment.messages.order_by("sequence", "created_at")
        ]
        downloads = [
            {
                "id": artifact.pk,
                "label": artifact.label,
                "kind": artifact.kind,
                "url": reverse("standalone:project_artifact_download", args=[artifact.pk]),
            }
            for artifact in assignment.artifacts.all()
        ]
    return {
        "id": project.pk,
        "title": project.title,
        "status": project.status,
        "engine_type": project.engine_type,
        "student_instructions": project.student_instructions,
        "answer_label": project.answer_label,
        "answer_unit": project.answer_unit,
        "decimal_places": project.decimal_places,
        "seed": assignment.seed if assignment is not None else "",
        "assignment_status": assignment.status if assignment is not None else ProjectAssignment.Status.NOT_STARTED,
        "completed_at": assignment.completed_at.isoformat() if assignment and assignment.completed_at else None,
        "submission_count": assignment.submission_count if assignment is not None else 0,
        "latest_submitted_answer": assignment.latest_submitted_answer if assignment is not None else "",
        "expected_display_answer": assignment.expected_answer_display if assignment is not None else "",
        "materialized": materialized,
        "transcript": messages,
        "downloads": downloads,
        "hints_remaining": max(
            0,
            len(project.hint_plan_json.get("hints") or [])
            - len([message for message in messages if message.get("kind") == "project_hint"]),
        ) if materialized else len(project.hint_plan_json.get("hints") or []),
    }


def serialize_projects_for_blocks(blocks, *, request=None, enrollment: Enrollment | None = None, include_projects: bool = True):
    project_map = {block.pk: [] for block in blocks}
    if not include_projects:
        return project_map

    projects = (
        BlockProject.objects.filter(block__in=blocks, status=BlockProject.Status.PUBLISHED)
        .select_related("block")
        .order_by("created_at", "pk")
    )
    assignment_map = {}
    if enrollment is not None:
        assignments = (
            ProjectAssignment.objects.filter(enrollment=enrollment, block_project__in=projects)
            .prefetch_related("artifacts", "messages")
            .select_related("block_project")
        )
        assignment_map = {assignment.block_project_id: assignment for assignment in assignments}

    for project in projects:
        if enrollment is not None:
            payload = _serialize_assignment_project_payload(project, assignment_map.get(project.pk))
        elif request is not None:
            payload = _serialize_preview_project_payload(project, _preview_project_session_state(request, project))
        else:
            payload = _serialize_preview_project_payload(project, None)
        project_map.setdefault(project.block_id, []).append(payload)
    return project_map


def publish_block_project(project: BlockProject) -> BlockProject:
    validate_block_project_spec(project)
    if project.generation_status not in {
        BlockProject.GenerationStatus.READY,
        BlockProject.GenerationStatus.IDLE,
    }:
        raise ProjectSpecError("Generate a supported project draft before publishing.")
    if project.is_locked:
        raise ProjectImmutableError("This published project already has student assignments, so it can no longer be changed.")
    project.status = BlockProject.Status.PUBLISHED
    project.archived_at = None
    if project.published_at is None:
        project.published_at = timezone.now()
    project.save(update_fields=["status", "archived_at", "published_at", "updated_at"])
    return project


def archive_block_project(project: BlockProject) -> BlockProject:
    project.status = BlockProject.Status.ARCHIVED
    project.archived_at = timezone.now()
    project.save(update_fields=["status", "archived_at", "updated_at"])
    return project


def _latest_hint_index(transcript: list[dict]) -> int:
    return len([message for message in transcript if message.get("kind") == "project_hint"])


def _deterministic_hint_text(project: BlockProject, transcript: list[dict], fallback: str = "") -> str:
    hints = list(project.hint_plan_json.get("hints") or [])
    if not hints:
        return fallback or "Work through the instructions step by step and check each intermediate calculation carefully."
    hint_index = min(_latest_hint_index(transcript), len(hints) - 1)
    return _strip_text(hints[hint_index]) or fallback


def _feedback_text_for_incorrect_submission(project: BlockProject, feedback_code: str) -> str:
    hint_plan = project.hint_plan_json or {}
    if feedback_code == "wrong_unit":
        return _strip_text(hint_plan.get("wrong_unit")) or f"Check the required unit and convert your answer into {project.answer_unit}."
    if feedback_code == "wrong_precision":
        return _strip_text(hint_plan.get("wrong_precision")) or f"Round your answer to {project.decimal_places} decimal places."
    if feedback_code == "wrong_value":
        return _strip_text(hint_plan.get("wrong_value")) or "Recheck the key calculation step and compare it with the project instructions."
    return _strip_text(hint_plan.get("wrong_value")) or "Try again and review the instructions."


def _grade_project_answer(project: BlockProject, raw_answer: str, expected_normalized: str) -> dict:
    parsed = _normalize_submitted_numeric_answer(
        raw_answer,
        required_unit=project.answer_unit,
        decimal_places=project.decimal_places,
    )
    if not parsed["ok"]:
        return {
            "is_correct": False,
            "feedback_code": parsed["code"],
            "feedback_text": parsed["message"],
            "normalized_answer": "",
        }

    normalized_answer = parsed["normalized"]
    if normalized_answer == expected_normalized:
        completion_text = _strip_text(project.hint_plan_json.get("completion")) or "Project complete."
        return {
            "is_correct": True,
            "feedback_code": "correct",
            "feedback_text": completion_text,
            "normalized_answer": normalized_answer,
        }

    normalized_expected = Decimal(expected_normalized)
    submitted_quantized = parsed["quantized"]
    feedback_code = "wrong_value"
    if parsed.get("submitted_unit") and _canonical_unit(parsed.get("submitted_unit")) != _canonical_unit(project.answer_unit):
        feedback_code = "wrong_unit"
    elif parsed["numeric_value"] != submitted_quantized and submitted_quantized == normalized_expected:
        feedback_code = "wrong_precision"
    return {
        "is_correct": False,
        "feedback_code": feedback_code,
        "feedback_text": _feedback_text_for_incorrect_submission(project, feedback_code),
        "normalized_answer": normalized_answer,
    }


def open_preview_project(request, project: BlockProject, *, viewer_identifier: str) -> None:
    _initialize_preview_project_state(request, project, viewer_identifier)


def send_preview_project_message(request, project: BlockProject, *, viewer_identifier: str, text: str) -> None:
    project_state = _initialize_preview_project_state(request, project, viewer_identifier)
    cleaned = _strip_text(text)
    if not cleaned:
        cleaned = "hint"
    _append_preview_message(project_state, "user", "project_message", cleaned)
    assistant_text = _deterministic_hint_text(project, project_state.get("transcript") or [])
    _append_preview_message(project_state, "assistant", "project_hint", assistant_text)
    if project_state["status"] == ProjectAssignment.Status.NOT_STARTED:
        project_state["status"] = ProjectAssignment.Status.IN_PROGRESS
    request.session.modified = True


def submit_preview_project_answer(request, project: BlockProject, *, viewer_identifier: str, raw_answer: str) -> None:
    project_state = _initialize_preview_project_state(request, project, viewer_identifier)
    project_state["submission_count"] = int(project_state.get("submission_count", 0) or 0) + 1
    project_state["latest_submitted_answer"] = raw_answer
    grading = _grade_project_answer(project, raw_answer, project_state["normalized_expected_answer"])
    project_state["latest_normalized_answer"] = grading["normalized_answer"]
    _append_preview_message(project_state, "user", "project_submission", raw_answer, {"normalized_answer": grading["normalized_answer"]})
    _append_preview_message(project_state, "assistant", "project_feedback", grading["feedback_text"], {"correct": grading["is_correct"]})
    if grading["is_correct"]:
        project_state["status"] = ProjectAssignment.Status.COMPLETE
        if not project_state.get("completed_at"):
            project_state["completed_at"] = timezone.now().isoformat()
    elif project_state["status"] == ProjectAssignment.Status.NOT_STARTED:
        project_state["status"] = ProjectAssignment.Status.IN_PROGRESS
    request.session.modified = True


def open_student_project(enrollment: Enrollment, project: BlockProject) -> ProjectAssignment:
    return ensure_project_assignment(enrollment, project)


def send_student_project_message(enrollment: Enrollment, project: BlockProject, text: str) -> ProjectAssignment:
    assignment = ensure_project_assignment(enrollment, project)
    cleaned = _strip_text(text) or "hint"
    with transaction.atomic():
        next_sequence = int(assignment.messages.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
        ProjectMessage.objects.create(
            assignment=assignment,
            message_id=f"project-message-{next_sequence}",
            sequence=next_sequence,
            role="user",
            kind="project_message",
            text=cleaned,
            payload={},
        )
        assistant_text = _deterministic_hint_text(
            project,
            [
                {"kind": message.kind}
                for message in assignment.messages.order_by("sequence").only("kind")
            ] + [{"kind": "project_message"}],
        )
        next_sequence += 1
        ProjectMessage.objects.create(
            assignment=assignment,
            message_id=f"project-message-{next_sequence}",
            sequence=next_sequence,
            role="assistant",
            kind="project_hint",
            text=assistant_text,
            payload={},
        )
        if assignment.status == ProjectAssignment.Status.NOT_STARTED:
            assignment.status = ProjectAssignment.Status.IN_PROGRESS
            assignment.save(update_fields=["status", "updated_at"])
    return assignment


def submit_student_project_answer(enrollment: Enrollment, project: BlockProject, raw_answer: str) -> ProjectAssignment:
    assignment = ensure_project_assignment(enrollment, project)
    grading = _grade_project_answer(project, raw_answer, assignment.normalized_expected_answer)
    with transaction.atomic():
        assignment.submission_count += 1
        assignment.latest_submitted_answer = raw_answer
        assignment.latest_normalized_answer = grading["normalized_answer"]
        if grading["is_correct"] and assignment.status != ProjectAssignment.Status.COMPLETE:
            assignment.status = ProjectAssignment.Status.COMPLETE
            assignment.completed_at = assignment.completed_at or timezone.now()
        elif assignment.status == ProjectAssignment.Status.NOT_STARTED:
            assignment.status = ProjectAssignment.Status.IN_PROGRESS
        assignment.save(
            update_fields=[
                "submission_count",
                "latest_submitted_answer",
                "latest_normalized_answer",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
        next_sequence = int(assignment.messages.order_by("-sequence").values_list("sequence", flat=True).first() or 0) + 1
        ProjectSubmission.objects.create(
            assignment=assignment,
            raw_answer=raw_answer,
            normalized_answer=grading["normalized_answer"],
            is_correct=grading["is_correct"],
            feedback_code=grading["feedback_code"],
            feedback_text=grading["feedback_text"],
        )
        ProjectMessage.objects.create(
            assignment=assignment,
            message_id=f"project-message-{next_sequence}",
            sequence=next_sequence,
            role="user",
            kind="project_submission",
            text=raw_answer,
            payload={"normalized_answer": grading["normalized_answer"]},
        )
        next_sequence += 1
        ProjectMessage.objects.create(
            assignment=assignment,
            message_id=f"project-message-{next_sequence}",
            sequence=next_sequence,
            role="assistant",
            kind="project_feedback",
            text=grading["feedback_text"],
            payload={"correct": grading["is_correct"]},
        )
    return assignment


def build_project_results_rows(block: CourseBlock):
    assignments = (
        ProjectAssignment.objects.filter(block_project__block=block)
        .select_related("block_project", "enrollment__student", "enrollment__course")
        .order_by("block_project__created_at", "enrollment__student__email")
    )
    for assignment in assignments:
        yield {
            "course": assignment.enrollment.course.title,
            "block": assignment.block_project.block.title,
            "project": assignment.block_project.title,
            "student": assignment.enrollment.student.email,
            "seed": assignment.seed,
            "status": assignment.status,
            "completed_at": assignment.completed_at.isoformat() if assignment.completed_at else "",
            "submission_count": assignment.submission_count,
            "latest_submitted_answer": assignment.latest_submitted_answer,
            "expected_display_answer": assignment.expected_answer_display,
        }
