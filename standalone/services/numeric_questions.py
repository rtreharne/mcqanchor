import ast
import json
import math
import re
import string
from dataclasses import dataclass

from django.conf import settings
from openai import OpenAI, OpenAIError


NUMERIC_SIGNAL_TERMS = (
    "calculate",
    "determine",
    "estimate",
    "rate",
    "force",
    "mass",
    "voltage",
    "density",
    "probability",
    "acceleration",
    "current",
    "resistance",
    "charge",
    "momentum",
    "energy",
    "power",
    "speed",
    "velocity",
    "frequency",
    "wavelength",
    "pressure",
)
NUMERIC_UNIT_PATTERN = re.compile(
    r"\b(?:kg|g|mg|m|cm|mm|km|s|ms|h|hz|khz|mhz|ghz|n|kn|j|kj|w|kw|v|mv|a|ma|ohm|pa|kpa|mpa|mol|m/s|m s-1|m/s\^2|m s-2|cm\^3|m\^3|%)\b",
    re.IGNORECASE,
)
NUMERIC_EXPRESSION_VERSION = "expression-v2"
MAX_ABSOLUTE_NUMERIC_VALUE = 1e100
ALLOWED_EXPRESSION_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "ln": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "radians": math.radians,
    "degrees": math.degrees,
}
ALLOWED_EXPRESSION_CONSTANTS = {"pi": math.pi, "e": math.e}
LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")
PRETTY_TEX_FUNCTIONS = {
    "sin": r"\sin",
    "cos": r"\cos",
    "tan": r"\tan",
    "asin": r"\arcsin",
    "acos": r"\arccos",
    "atan": r"\arctan",
    "ln": r"\ln",
    "log": r"\log",
    "log10": r"\log_{10}",
    "exp": r"\exp",
}
PRETTY_TEX_CONSTANTS = {
    "pi": r"\pi",
    "e": "e",
}
SUPERSCRIPT_TRANSLATION = str.maketrans({
    "-": "⁻",
    "+": "⁺",
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
})
SCIENTIFIC_NOTATION_MIN_ABS = 1e-3
SCIENTIFIC_NOTATION_MAX_ABS = 1e4
NUMERIC_DISPLAY_STYLE_SCIENTIFIC = "scientific"
NUMERIC_DISPLAY_STYLE_DECIMAL = "decimal"
NUMERIC_GIVEAWAY_FORMULA_PATTERNS = (
    re.compile(r"\b(?:use|using|apply|applying)\s+(?:the\s+)?formula\b", re.IGNORECASE),
    re.compile(r"\b(?:the\s+)?formula\s+[A-Za-z][^=]{0,20}=", re.IGNORECASE),
    re.compile(r"\b[A-Za-z](?:_[A-Za-z0-9]+)?\s*=\s*[A-Za-z][A-Za-z0-9_()^+\-−×÷*/ ]{1,40}", re.IGNORECASE),
    re.compile(
        r"\b(?:the\s+)?[A-Za-z][A-Za-z -]{2,40}\s*=\s*(?:the\s+)?[A-Za-z][A-Za-z -]{1,40}\s*"
        r"(?:[+\-−×÷*/]|\bplus\b|\bminus\b|\btimes\b|\bmultiplied by\b|\bdivided by\b)\s*"
        r"(?:the\s+)?[A-Za-z][A-Za-z -]{1,40}",
        re.IGNORECASE,
    ),
)
NUMERIC_OPERATION_TERMS = (
    "calculate",
    "determine",
    "estimate",
    "solve",
    "compute",
    "work out",
    "round",
    "nearest",
    "sum",
    "total",
    "difference",
    "product",
    "quotient",
    "average",
    "mean",
    "ratio",
    "proportion",
    "percentage",
    "percent",
    "probability",
    "perimeter",
    "area",
    "volume",
    "speed",
    "velocity",
    "acceleration",
    "force",
    "energy",
    "power",
    "density",
    "pressure",
    "frequency",
    "wavelength",
    "half-life",
    "decay",
    "place value",
)
NUMERIC_UNSUITABLE_SCHEMA_PATTERNS = (
    re.compile(r"\broman numerals?\b", re.IGNORECASE),
    re.compile(r"\barabic numerals?\b", re.IGNORECASE),
    re.compile(r"\b(?:greater than|less than|inequality)\b[\w\s-]{0,24}\b(?:symbol|sign)\b", re.IGNORECASE),
    re.compile(r"\b(?:write|read|represent|record)\b[\w\s-]{0,30}\b(?:in|using)?\s*(?:words?|figures?)\b", re.IGNORECASE),
)
NUMERIC_STRONG_SIGNAL_PATTERN = re.compile(
    r"[=±×÷^%]|//|"
    r"\b\d+(?:\.\d+)?\b[\w\s,.;:()/-]{0,32}\b\d+(?:\.\d+)?\b|"
    r"\b(?:plus|minus|times|multiplied by|divided by|shared equally|remainder)\b",
    re.IGNORECASE,
)


def _numeric_candidate_schema(distractor_count: int) -> dict:
    variable_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "value": {"type": "number"},
            "unit": {"type": "string"},
        },
        "required": ["name", "value", "unit"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question_type": {"type": "string", "enum": ["num"]},
            "stem_template": {"type": "string", "minLength": 1},
            "variables": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": variable_schema,
            },
            "calculation_expression": {"type": "string", "minLength": 1},
            "answer_unit": {"type": "string"},
            "significant_figures": {"type": "integer", "enum": [2, 3, 4, 5, 6]},
            "explanation": {"type": "string", "minLength": 1},
            "difficulty": {"type": "string", "enum": ["foundation", "core", "stretch"]},
            "further_study_questions": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {"type": "string", "minLength": 1},
            },
        },
        "required": [
            "question_type",
            "stem_template",
            "variables",
            "calculation_expression",
            "answer_unit",
            "significant_figures",
            "explanation",
            "difficulty",
            "further_study_questions",
        ],
    }


def _trace_numeric_generation(event: str, **context) -> None:
    payload = {"event": event, **context}
    print(f"[numeric-generation] {json.dumps(payload, default=str, ensure_ascii=False)}", flush=True)


class NumericQuestionValidationError(ValueError):
    pass


class NumericQuestionRequestError(NumericQuestionValidationError):
    pass


@dataclass
class NumericQuestionResult:
    payload: dict
    metadata: dict


def chunk_has_numeric_signal(text: str) -> bool:
    lowered = str(text or "").lower()
    if re.search(r"\d", lowered):
        return True
    if NUMERIC_UNIT_PATTERN.search(lowered):
        return True
    if re.search(r"[=<>±×÷^]", lowered):
        return True
    if re.search(r"\b[a-z]\s*=\s*[-+]?\d", lowered):
        return True
    return any(term in lowered for term in NUMERIC_SIGNAL_TERMS)


def supports_local_numeric_mcq(objective_text: str, chunk_text: str) -> bool:
    combined = f"{objective_text}\n{chunk_text}"
    lowered = str(combined or "").lower()
    if not chunk_has_numeric_signal(combined):
        return False

    if any(pattern.search(combined) for pattern in NUMERIC_UNSUITABLE_SCHEMA_PATTERNS):
        return False

    if NUMERIC_UNIT_PATTERN.search(combined):
        return True
    if NUMERIC_STRONG_SIGNAL_PATTERN.search(combined):
        return True
    return any(term in lowered for term in NUMERIC_OPERATION_TERMS)


def _parse_json_object(raw_output: str) -> dict:
    normalized_output = (raw_output or "").strip()
    if not normalized_output:
        raise NumericQuestionValidationError("OpenAI returned an empty numeric question payload.")
    try:
        parsed = json.loads(normalized_output)
    except json.JSONDecodeError as exc:
        raise NumericQuestionValidationError(f"OpenAI returned invalid JSON for numeric generation ({exc}).") from exc
    if not isinstance(parsed, dict):
        raise NumericQuestionValidationError("OpenAI numeric output must be a JSON object.")
    return parsed


def _normalize_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        codepoint = match.group(1) or match.group(2)
        try:
            return chr(int(codepoint, 16))
        except (TypeError, ValueError):
            return match.group(0)

    decoded = LITERAL_UNICODE_ESCAPE_RE.sub(replace, str(value or ""))
    return re.sub(r"\s+", " ", decoded.strip())


NUMERIC_CONTEXT_STOPWORDS = {
    "about", "across", "after", "also", "an", "and", "apply", "are", "calculate",
    "determine", "evaluate", "example", "examples", "explain", "for", "from", "including",
    "into", "its", "knowledge", "maximum", "minimum", "numerical", "objective", "of", "on",
    "or", "that", "the", "their", "this", "through", "using", "what", "when", "where",
    "which", "with",
}


def _context_keywords(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", str(text or "").lower())
        if token not in NUMERIC_CONTEXT_STOPWORDS and not token.isdigit()
    }


def _validate_numeric_context_alignment(stem: str, explanation: str, objective_text: str, chunk_text: str) -> None:
    question_keywords = _context_keywords(f"{stem} {explanation}")
    objective_keywords = _context_keywords(objective_text)
    chunk_keywords = _context_keywords(chunk_text)
    if objective_keywords and question_keywords & objective_keywords:
        return
    if len(question_keywords & chunk_keywords) >= 2:
        return
    raise NumericQuestionValidationError(
        "Numeric question is not sufficiently aligned to the learning objective or block content."
    )


def _has_source_dependent_stem(stem: str) -> bool:
    lowered = _normalize_text(stem).lower()
    source_terms = r"(?:source\s+text|textbook|book|chapter|passage|notes|content|block|document)"
    source_artifacts = r"(?:figure|fig\.?|table|diagram|graph|worked\s+example|chapter|section|page|paragraph|extract|excerpt)"
    patterns = (
        rf"\b(?:according to|based on|from|in)\s+(?:the\s+)?{source_terms}\b",
        rf"\b(?:this|the)\s+{source_artifacts}\b",
        rf"\b{source_artifacts}\s+\d+[a-z]?\b",
        rf"\b(?:shown|described|presented|given)\s+in\s+(?:this|the)\s+{source_artifacts}\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _ensure_finite_number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NumericQuestionValidationError(f"{label} must be a number.")
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_ABSOLUTE_NUMERIC_VALUE:
        raise NumericQuestionValidationError(f"{label} must be finite and within a sensible range.")
    return result


def _parse_variables(raw_variables) -> tuple[dict[str, float], dict[str, str]]:
    if not isinstance(raw_variables, list) or not raw_variables:
        raise NumericQuestionValidationError("Numeric generation must provide at least one input variable.")
    values: dict[str, float] = {}
    units: dict[str, str] = {}
    for item in raw_variables:
        if not isinstance(item, dict):
            raise NumericQuestionValidationError("Every numeric input variable must be an object.")
        name = str(item.get("name", "")).strip()
        if (
            not name.isidentifier()
            or name.startswith("_")
            or name in ALLOWED_EXPRESSION_FUNCTIONS
            or name in ALLOWED_EXPRESSION_CONSTANTS
        ):
            raise NumericQuestionValidationError(f"Invalid numeric variable name: {name or '<empty>'}.")
        if name in values:
            raise NumericQuestionValidationError(f"Duplicate numeric variable name: {name}.")
        values[name] = _ensure_finite_number(item.get("value"), f"Variable '{name}'")
        units[name] = _validate_unit_text(item.get("unit", ""), f"Unit for '{name}'")
    return values, units


def _evaluate_expression(expression: str, variables: dict[str, float]) -> tuple[float, ast.Expression, set[str]]:
    expression = str(expression or "").strip()
    if not expression or len(expression) > 500:
        raise NumericQuestionValidationError("Calculation expression is empty or too long.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise NumericQuestionValidationError(f"Calculation expression is invalid: {exc.msg}.") from exc

    used_variables: set[str] = set()

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant):
            return _ensure_finite_number(node.value, "Expression constant")
        if isinstance(node, ast.Name):
            if node.id in variables:
                used_variables.add(node.id)
                return variables[node.id]
            if node.id in ALLOWED_EXPRESSION_CONSTANTS:
                return ALLOWED_EXPRESSION_CONSTANTS[node.id]
            raise NumericQuestionValidationError(f"Unknown name in calculation expression: {node.id}.")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.FloorDiv, ast.Mod)):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            elif isinstance(node.op, ast.FloorDiv):
                result = left // right
            elif isinstance(node.op, ast.Mod):
                result = left % right
            else:
                if abs(right) > 20:
                    raise NumericQuestionValidationError("Exponent is outside the allowed range.")
                result = left ** right
            return _ensure_finite_number(result, "Expression result")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function = ALLOWED_EXPRESSION_FUNCTIONS.get(node.func.id)
            if function is None or node.keywords or len(node.args) != 1:
                raise NumericQuestionValidationError("Calculation expression contains an unsupported function call.")
            return _ensure_finite_number(function(evaluate(node.args[0])), "Expression result")
        raise NumericQuestionValidationError(
            f"Calculation expression contains unsupported syntax: {node.__class__.__name__}."
        )

    try:
        result = _ensure_finite_number(evaluate(tree), "Calculated answer")
    except (ArithmeticError, ValueError, OverflowError) as exc:
        if isinstance(exc, NumericQuestionValidationError):
            raise
        raise NumericQuestionValidationError(f"Calculation expression could not be evaluated: {exc}.") from exc
    if not used_variables:
        raise NumericQuestionValidationError("Calculation expression must use at least one supplied variable.")
    return result, tree, used_variables


def _format_number(value: float, significant_figures: int) -> str:
    value = _ensure_finite_number(value, "Formatted numeric value")
    if value == 0:
        return "0"
    return format(value, f".{significant_figures}g")


def _scientific_notation_parts(value: float, significant_figures: int) -> tuple[str, int] | None:
    return _scientific_notation_parts_for_style(
        value,
        significant_figures,
        display_style=None,
    )


def _scientific_notation_parts_for_style(
    value: float,
    significant_figures: int,
    display_style: str | None = None,
) -> tuple[str, int] | None:
    absolute_value = abs(value)
    if display_style == NUMERIC_DISPLAY_STYLE_DECIMAL:
        return None
    if (
        value == 0
        or (
            display_style != NUMERIC_DISPLAY_STYLE_SCIENTIFIC
            and SCIENTIFIC_NOTATION_MIN_ABS <= absolute_value < SCIENTIFIC_NOTATION_MAX_ABS
        )
    ):
        return None
    scientific = format(value, f".{max(significant_figures - 1, 0)}e")
    mantissa_text, exponent_text = scientific.split("e", 1)
    mantissa_text = mantissa_text.rstrip("0").rstrip(".")
    if mantissa_text in {"-0", "+0"}:
        mantissa_text = "0"
    return mantissa_text, int(exponent_text)


def _numeric_display_style(value: float) -> str:
    absolute_value = abs(_ensure_finite_number(value, "Numeric display value"))
    if absolute_value != 0 and (absolute_value < SCIENTIFIC_NOTATION_MIN_ABS or absolute_value >= SCIENTIFIC_NOTATION_MAX_ABS):
        return NUMERIC_DISPLAY_STYLE_SCIENTIFIC
    return NUMERIC_DISPLAY_STYLE_DECIMAL


def _format_number_text(value: float, significant_figures: int, display_style: str | None = None) -> str:
    value = _ensure_finite_number(value, "Formatted numeric value")
    scientific = _scientific_notation_parts_for_style(value, significant_figures, display_style=display_style)
    if scientific is None:
        return _format_number(value, significant_figures)
    mantissa_text, exponent = scientific
    return f"{mantissa_text} × 10{str(exponent).translate(SUPERSCRIPT_TRANSLATION)}"


def _format_number_tex(value: float, significant_figures: int, display_style: str | None = None) -> str:
    value = _ensure_finite_number(value, "Formatted numeric value")
    scientific = _scientific_notation_parts_for_style(value, significant_figures, display_style=display_style)
    if scientific is None:
        return _format_number(value, significant_figures)
    mantissa_text, exponent = scientific
    return rf"{mantissa_text} \times 10^{{{exponent}}}"


def normalize_numeric_answer_text(
    answer_value,
    answer_unit: str,
    significant_figures: int = 10,
    *,
    display_style: str | None = None,
) -> str:
    numeric_text = _format_number_text(float(answer_value), significant_figures, display_style=display_style)
    unit_text = _normalize_text(answer_unit)
    return f"{numeric_text} {unit_text}".strip()


def _validate_unit_text(value, label: str) -> str:
    unit = _normalize_text(value)
    if len(unit) > 40 or not re.fullmatch(r"[A-Za-z0-9%°µμΩ·./*^+()\- ]*", unit):
        raise NumericQuestionValidationError(f"{label} contains unsupported unit notation.")
    return unit


def _render_stem_template(template: str, values: dict[str, float], units: dict[str, str]) -> str:
    template = str(template or "").strip()
    formatter = string.Formatter()
    try:
        fields = [field_name for _, field_name, _, _ in formatter.parse(template) if field_name is not None]
    except ValueError as exc:
        raise NumericQuestionValidationError(f"Numeric stem template is invalid: {exc}.") from exc
    if set(fields) != set(values) or len(fields) != len(set(fields)):
        raise NumericQuestionValidationError(
            "Stem template must contain each supplied variable exactly once and no unknown placeholders."
        )
    replacements = {name: format(value, ".10g") for name, value in values.items()}
    try:
        stem = _normalize_text(template.format_map(replacements))
    except (KeyError, ValueError) as exc:
        raise NumericQuestionValidationError(f"Numeric stem template could not be rendered: {exc}.") from exc
    if not stem or _has_source_dependent_stem(stem):
        raise NumericQuestionValidationError("Numeric question stem is empty or depends on source-text artefacts.")
    return stem


def _has_giveaway_numeric_stem(stem: str) -> bool:
    normalized = _normalize_text(stem)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in NUMERIC_GIVEAWAY_FORMULA_PATTERNS)


def _tex_escape_name(name: str) -> str:
    return r"\mathrm{" + name.replace("_", r"\ ") + "}"


def _degree_tex(argument: str) -> str:
    simple_argument = re.fullmatch(r"[A-Za-z0-9\\{}_^.-]+", argument or "")
    if simple_argument:
        return rf"{argument}^{{\circ}}"
    return rf"\left({argument}\right)^{{\circ}}"


def _expression_to_tex(node, replacements: dict[str, str]) -> str:
    if isinstance(node, ast.Expression):
        return _expression_to_tex(node.body, replacements)
    if isinstance(node, ast.Constant):
        return format(float(node.value), ".10g")
    if isinstance(node, ast.Name):
        if node.id in PRETTY_TEX_CONSTANTS:
            return PRETTY_TEX_CONSTANTS[node.id]
        return replacements.get(node.id, _tex_escape_name(node.id))
    if isinstance(node, ast.UnaryOp):
        sign = "+" if isinstance(node.op, ast.UAdd) else "-"
        return sign + _expression_to_tex(node.operand, replacements)
    if isinstance(node, ast.BinOp):
        left = _expression_to_tex(node.left, replacements)
        right = _expression_to_tex(node.right, replacements)
        if isinstance(node.op, ast.Add):
            return f"{left} + {right}"
        if isinstance(node.op, ast.Sub):
            return f"{left} - {right}"
        if isinstance(node.op, ast.Mult):
            return f"{left} \\times {right}"
        if isinstance(node.op, ast.Div):
            return rf"\frac{{{left}}}{{{right}}}"
        if isinstance(node.op, ast.FloorDiv):
            return rf"\left\lfloor \frac{{{left}}}{{{right}}} \right\rfloor"
        if isinstance(node.op, ast.Mod):
            return rf"{left} \bmod {right}"
        return rf"\left({left}\right)^{{{right}}}"
    if isinstance(node, ast.Call):
        argument = _expression_to_tex(node.args[0], replacements)
        if node.func.id == "sqrt":
            return rf"\sqrt{{{argument}}}"
        if node.func.id == "abs":
            return rf"\left|{argument}\right|"
        if node.func.id == "radians":
            return _degree_tex(argument)
        pretty_function = PRETTY_TEX_FUNCTIONS.get(node.func.id)
        if pretty_function:
            return rf"{pretty_function}\left({argument}\right)"
        return rf"\operatorname{{{node.func.id}}}\left({argument}\right)"
    raise NumericQuestionValidationError("Could not render calculation expression as TeX.")


def _normalize_study_questions(items) -> list[str]:
    normalized = []
    for item in items or []:
        cleaned = _normalize_text(item).rstrip(".!")
        if cleaned and not cleaned.endswith("?"):
            cleaned += "?"
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    if len(normalized) != 3:
        raise NumericQuestionValidationError("Numeric generation must include exactly 3 unique further-study questions.")
    return normalized


def _build_local_distractors(
    answer_value: float,
    answer_unit: str,
    significant_figures: int,
    distractor_count: int,
    variables: dict[str, float],
    *,
    display_style: str | None = None,
) -> tuple[list[str], list[float]]:
    if answer_value == 0:
        scale = max((abs(value) for value in variables.values()), default=1.0) or 1.0
        candidates = [scale, -scale, 2 * scale, scale / 2, 10 * scale]
    else:
        candidates = [
            answer_value * 10,
            answer_value / 10,
            answer_value * 2,
            answer_value / 2,
            -answer_value,
            answer_value * 100,
            answer_value / 100,
        ]
    correct_answer = normalize_numeric_answer_text(
        answer_value,
        answer_unit,
        significant_figures,
        display_style=display_style,
    )
    distractors: list[str] = []
    distractor_values: list[float] = []
    for candidate_value in candidates:
        option = normalize_numeric_answer_text(
            candidate_value,
            answer_unit,
            significant_figures,
            display_style=display_style,
        )
        if option == correct_answer or option in distractors:
            continue
        distractors.append(option)
        distractor_values.append(candidate_value)
        if len(distractors) == distractor_count:
            break
    if len(distractors) != distractor_count:
        raise NumericQuestionValidationError("Could not construct enough unique numeric distractors.")
    return distractors, distractor_values


def _validate_numeric_candidate(candidate: dict, distractor_count: int, objective_text: str, chunk_text: str) -> tuple[dict, dict]:
    if _normalize_text(candidate.get("question_type", "")).lower() != "num":
        raise NumericQuestionValidationError("question_type must be 'num'.")
    values, units = _parse_variables(candidate.get("variables"))
    stem = _render_stem_template(candidate.get("stem_template", ""), values, units)
    if _has_giveaway_numeric_stem(stem):
        raise NumericQuestionValidationError(
            "Numeric question stem gives away the method too explicitly. Do not include explicit formula cues or named quantity equations in the stem."
        )
    significant_figures = candidate.get("significant_figures")
    if isinstance(significant_figures, bool) or significant_figures not in {2, 3, 4, 5, 6}:
        raise NumericQuestionValidationError("significant_figures must be an integer from 2 to 6.")

    answer_value, answer_tree, used_variables = _evaluate_expression(
        candidate.get("calculation_expression", ""),
        values,
    )
    unused_variables = sorted(set(values) - used_variables)
    if unused_variables:
        _trace_numeric_generation("unused_variables", variables=unused_variables)
    answer_unit = _validate_unit_text(candidate.get("answer_unit", ""), "Answer unit")
    display_style = _numeric_display_style(answer_value)
    correct_answer = normalize_numeric_answer_text(
        answer_value,
        answer_unit,
        significant_figures,
        display_style=display_style,
    )

    distractors, distractor_values = _build_local_distractors(
        answer_value,
        answer_unit,
        significant_figures,
        distractor_count,
        values,
        display_style=display_style,
    )

    explanation = _normalize_text(candidate.get("explanation", ""))
    if not explanation:
        raise NumericQuestionValidationError("Numeric generation must include an explanation.")
    _validate_numeric_context_alignment(stem, explanation, objective_text, chunk_text)
    formula_tex = _expression_to_tex(answer_tree, {})
    substituted_tex = _expression_to_tex(
        answer_tree,
        {name: format(value, ".10g") for name, value in values.items()},
    )
    answer_tex = _format_number_tex(answer_value, significant_figures, display_style=display_style)
    worked_solution_tex = f"{substituted_tex} = {answer_tex}"
    if answer_unit:
        worked_solution_tex += r"\,\mathrm{" + answer_unit.replace(" ", r"\ ") + "}"
    full_explanation = (
        f"{explanation}\n\nFormula:\n\\[{formula_tex}\\]"
        f"\n\nWorked solution:\n\\[{worked_solution_tex}\\]"
    )
    output = {
        "stem": stem,
        "correct_answer": correct_answer,
        "distractors": distractors,
        "explanation": full_explanation,
        "difficulty": _normalize_text(candidate.get("difficulty", "")) or "core",
        "further_study_questions": _normalize_study_questions(candidate.get("further_study_questions")),
        "answer_value": answer_value,
        "answer_unit": answer_unit,
        "formula_tex": formula_tex,
        "worked_solution_tex": worked_solution_tex,
    }
    validation = {
        "expression_evaluated_locally": True,
        "answer_expression": candidate.get("calculation_expression", ""),
        "used_variables": sorted(used_variables),
        "unused_variables": unused_variables,
        "distractors_generated_locally": True,
        "distractor_values": distractor_values,
    }
    return output, validation


def _numeric_prompt(
    chunk_text: str,
    objective_text: str,
    distractor_count: int,
    avoid_question_angles: list[str] | None = None,
    teacher_guidance: str = "",
) -> str:
    avoidance_section = ""
    if avoid_question_angles:
        avoidance_section = (
            "\nAvoid the wording, scenario, calculation, and formula focus of these recent questions:\n"
            + "\n".join(avoid_question_angles[:6])
        )
    return f"""
Create one self-contained numerical single-answer MCQ using the supplied strict JSON schema.

Rules:
- Anchor the calculation to the learning objective. Use source text only to understand the subject context.
- Never refer to source text, blocks, figures, diagrams, examples, chapters, pages, or document position.
- Only create a question when the answer can be computed from explicit numeric givens using arithmetic on the supplied variables.
- Do not create transcription or notation-conversion tasks such as Roman numeral decoding, number-word transcription, or symbol-selection questions for this schema.
- stem_template must be a complete standalone question in plain prose.
- Do not include an explicit formula, worked relationship, or named-quantity equation in the stem, such as "net count rate = total count rate - background count rate" or "using the formula F = qE", unless the learning objective is specifically to test recalling that relationship.
- Prefer students to infer the relationship from the scenario and givens instead of spelling out the exact arithmetic step inside the stem.
- If recent examples are provided, choose a materially different numerical angle, target quantity, and formula focus rather than restating the same calculation with new numbers.
- Put each given numerical quantity in variables and insert its value in stem_template as {{variable_name}}.
- A placeholder is replaced by the numeric value only. Write its unit immediately after the placeholder in stem_template.
- Every variable placeholder must occur exactly once in stem_template. Do not put literal numerical givens elsewhere in the stem.
- Use simple Python-style arithmetic expressions only: +, -, *, /, //, %, **, parentheses, pi, e, abs, round, floor, ceil, sqrt, sin, cos, tan, asin, acos, atan, ln, log10, exp, radians, degrees.
- calculation_expression must compute the single objectively correct answer from the supplied variables.
- Include only variables needed by calculation_expression unless surplus data is an intentional part of the question.
- The application computes the correct option and {distractor_count} bounded distractors locally; do not provide answer strings, distractor expressions, or Python code.
- Use SI units unless the learning objective requires another convention. Use dimensionless as the answer unit when appropriate.
- significant_figures must reflect the precision of the givens.
- explanation must state the correct physical principle without referring to answer letters or source material.
- Give exactly 3 relevant further-study questions.
{avoidance_section}

Learning objective:
{objective_text}

Subject context (not visible to the student):
{chunk_text}

{teacher_guidance}
""".strip()


def _openai_numeric_candidate(
    chunk_text: str,
    objective_text: str,
    distractor_count: int,
    avoid_question_angles: list[str] | None = None,
    teacher_guidance: str = "",
) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    try:
        response = client.responses.create(
            model=settings.OPENAI_MODEL,
            instructions="Return one valid JSON object matching the supplied schema. Do not generate Python code.",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _numeric_prompt(
                                chunk_text,
                                objective_text,
                                distractor_count,
                                avoid_question_angles=avoid_question_angles,
                                teacher_guidance=teacher_guidance,
                            ),
                        }
                    ],
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "numeric_question_specification",
                    "strict": True,
                    "schema": _numeric_candidate_schema(distractor_count),
                }
            },
            temperature=0.2,
        )
    except OpenAIError as exc:
        raise NumericQuestionRequestError(f"OpenAI request for numeric generation failed: {exc}") from exc
    return _parse_json_object(getattr(response, "output_text", ""))


def build_numeric_question_payload(
    chunk_text: str,
    objective_text: str,
    distractor_count: int,
    *,
    avoid_question_angles: list[str] | None = None,
    teacher_guidance: str = "",
) -> NumericQuestionResult:
    if not settings.OPENAI_API_KEY:
        raise NumericQuestionValidationError("Numeric generation requires OPENAI_API_KEY.")

    candidate = _openai_numeric_candidate(
        chunk_text,
        objective_text,
        distractor_count,
        avoid_question_angles=avoid_question_angles,
        teacher_guidance=teacher_guidance,
    )
    try:
        validated_output, validation = _validate_numeric_candidate(
            candidate,
            distractor_count,
            objective_text,
            chunk_text,
        )
    except NumericQuestionValidationError as exc:
        _trace_numeric_generation("validation_failed", reason=str(exc))
        raise

    variables = {
        item["name"]: {"value": item["value"], "unit": item["unit"]}
        for item in candidate["variables"]
    }
    metadata = {
        "script_source": "",
        "script_version": NUMERIC_EXPRESSION_VERSION,
        "seed": None,
        "inputs": variables,
        "validation": validation,
        "output_snapshot": validated_output,
        "repair_attempts": [],
    }
    payload = {
        "question_type": "num",
        "stem": validated_output["stem"],
        "correct_answers": [validated_output["correct_answer"]],
        "distractors": validated_output["distractors"],
        "written_answer_keywords": [],
        "further_study_questions": validated_output["further_study_questions"],
        "explanation": validated_output["explanation"],
        "difficulty": validated_output["difficulty"],
        "numeric_metadata": metadata,
    }
    _trace_numeric_generation(
        "validated",
        generator=NUMERIC_EXPRESSION_VERSION,
        stem=validated_output["stem"],
        correct_answer=validated_output["correct_answer"],
        distractors=validated_output["distractors"],
    )
    return NumericQuestionResult(payload=payload, metadata=metadata)
