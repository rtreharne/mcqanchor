import ast
import json
import math
import re
import string
from dataclasses import dataclass

from django.conf import settings
from openai import OpenAI, OpenAIError

from standalone.services.symbol_heuristics import (
    deterministic_symbol_heuristics_for_objective,
    normalize_objective_symbol_heuristics,
    symbol_plain_to_tex,
)


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
NUMERIC_OBJECTIVE_EXPLICIT_ACTION_PATTERN = re.compile(
    r"\b(?:calculate|compute|estimate|convert|quantify|solve)\b",
    re.IGNORECASE,
)
NUMERIC_OBJECTIVE_CONTEXTUAL_ACTION_PATTERN = re.compile(
    r"\b(?:determine|find|work\s+out|evaluate|apply|use)\b",
    re.IGNORECASE,
)
NUMERIC_OBJECTIVE_TARGET_PATTERN = re.compile(
    r"\b(?:"
    r"distance|speed|velocity|acceleration|force|energy|power|density|pressure|frequency|wavelength|"
    r"half-life|decay|mass|charge|current|voltage|resistance|field|concentration|moles?|amount|"
    r"radius|diameter|parallax|gradient|slope|rate|period|time|temperature|probability|percentage|"
    r"ratio|proportion|area|volume|magnitude|value|answer|result"
    r")\b",
    re.IGNORECASE,
)
GENERIC_OBJECTIVE_SCOPE_PATTERN = re.compile(
    r"\b(?:key ideas?|overview|introduction|fundamentals?|basics?|general principles?|core ideas?)\b",
    re.IGNORECASE,
)
ELEMENTARY_CHARGE = 1.602176634e-19
COULOMB_CONSTANT = 8.9875517923e9


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


def objective_has_numeric_intent(objective_text: str) -> bool:
    normalized = str(objective_text or "").strip().lower()
    if not normalized:
        return False
    if NUMERIC_OBJECTIVE_EXPLICIT_ACTION_PATTERN.search(normalized):
        return True
    if NUMERIC_OBJECTIVE_CONTEXTUAL_ACTION_PATTERN.search(normalized) and NUMERIC_OBJECTIVE_TARGET_PATTERN.search(normalized):
        return True
    if GENERIC_OBJECTIVE_SCOPE_PATTERN.search(normalized):
        return True
    return False


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


def _normalize_unit_key(unit: str) -> str:
    return re.sub(r"\s+", "", _normalize_text(unit).lower())


def _context_mentions_alpha_closest_approach(*texts: str) -> bool:
    combined = " ".join(_normalize_text(text).lower() for text in texts if text)
    return (
        "closest approach" in combined
        and "alpha" in combined
        and ("nucleus" in combined or "nuclear" in combined)
    )


def _energy_value_in_joules(values: dict[str, float], units: dict[str, str]) -> float | None:
    conversions = {
        "j": 1.0,
        "ev": ELEMENTARY_CHARGE,
        "kev": 1e3 * ELEMENTARY_CHARGE,
        "mev": 1e6 * ELEMENTARY_CHARGE,
        "gev": 1e9 * ELEMENTARY_CHARGE,
    }
    for name, value in values.items():
        normalized_unit = _normalize_unit_key(units.get(name, ""))
        if normalized_unit not in conversions:
            continue
        lowered_name = name.lower()
        if "energy" in lowered_name or "kinetic" in lowered_name:
            return value * conversions[normalized_unit]
    return None


def _relative_charge_number(values: dict[str, float], units: dict[str, str], *, name_terms: tuple[str, ...]) -> float | None:
    for name, value in values.items():
        if _normalize_unit_key(units.get(name, "")) != "e":
            continue
        lowered_name = name.lower()
        if any(term in lowered_name for term in name_terms):
            return float(value)
    return None


def _validate_alpha_closest_approach_scale(
    *,
    stem: str,
    objective_text: str,
    chunk_text: str,
    answer_value: float,
    answer_unit: str,
    values: dict[str, float],
    units: dict[str, str],
) -> None:
    if _normalize_unit_key(answer_unit) != "m":
        return
    if not _context_mentions_alpha_closest_approach(stem, objective_text, chunk_text):
        return
    energy_joules = _energy_value_in_joules(values, units)
    nucleus_charge = _relative_charge_number(values, units, name_terms=("nucleus", "nuclear", "target", "gold", "lead"))
    projectile_charge = _relative_charge_number(values, units, name_terms=("alpha", "particle", "projectile"))
    if projectile_charge is None:
        projectile_charge = 2.0
    if energy_joules is None or nucleus_charge is None:
        return
    expected_distance = COULOMB_CONSTANT * projectile_charge * nucleus_charge * (ELEMENTARY_CHARGE ** 2) / energy_joules
    if expected_distance <= 0:
        return
    ratio = max(answer_value, expected_distance) / min(answer_value, expected_distance)
    if ratio > 100:
        raise NumericQuestionValidationError(
            "Closest-approach distance is not physically consistent with Coulomb energy conversion."
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
    formatted = format(value, f".{significant_figures}g")
    if "e" not in formatted.lower():
        return formatted
    absolute_value = abs(value)
    if SCIENTIFIC_NOTATION_MIN_ABS <= absolute_value < SCIENTIFIC_NOTATION_MAX_ABS:
        if float(value).is_integer():
            return str(int(value))
        digits_before_decimal = len(str(int(absolute_value))) if absolute_value >= 1 else 0
        decimal_places = max(significant_figures - digits_before_decimal, 0)
        return format(value, f".{decimal_places}f").rstrip("0").rstrip(".")
    return formatted


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


def _unit_tex(unit: str) -> str:
    normalized_unit = _normalize_text(unit)
    if not normalized_unit:
        return ""
    unit_text = normalized_unit.replace(" ", r"\ ")
    unit_text = re.sub(r"\s*[·*]\s*", r"}\\cdot\\mathrm{", unit_text)
    unit_text = re.sub(r"(?<=[A-Za-zΩµμ])\.(?=[A-Za-zΩµμ])", r"}\\cdot\\mathrm{", unit_text)
    return r"\,\mathrm{" + unit_text + "}"


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


def _infer_significant_figures_from_answer_text(answer_text: str, default: int = 3) -> int:
    cleaned = _normalize_text(answer_text)
    if not cleaned:
        return default
    scientific_match = re.match(r"([-+]?\d+(?:\.\d+)?)\s*(?:×\s*10|e)", cleaned, re.IGNORECASE)
    decimal_match = scientific_match or re.match(r"([-+]?\d+(?:\.\d+)?)", cleaned)
    if not decimal_match:
        return default
    mantissa = decimal_match.group(1)
    digits = [char for char in mantissa if char.isdigit()]
    return max(2, min(6, len(digits))) if digits else default


def _infer_stem_value_significant_figures(value: float) -> int:
    rendered = format(_ensure_finite_number(value, "Stem numeric value"), ".10g").lower()
    if "e" in rendered:
        mantissa = rendered.split("e", 1)[0]
        digits = [char for char in mantissa if char.isdigit()]
        return max(1, min(6, len(digits))) if digits else 3
    if "." in rendered:
        whole, fractional = rendered.split(".", 1)
        digits = [char for char in f"{whole}{fractional}" if char.isdigit()]
        leading_zero_count = 0
        for digit in digits:
            if digit != "0":
                break
            leading_zero_count += 1
        significant = len(digits[leading_zero_count:])
        return max(1, min(6, significant)) if significant else 3
    stripped = rendered.lstrip("-").lstrip("+")
    stripped = stripped.lstrip("0") or "0"
    significant = len(stripped.rstrip("0")) if stripped != "0" else 1
    return max(1, min(6, significant))


def _format_value_tex(value: float, significant_figures: int) -> str:
    return _format_number_tex(value, significant_figures, display_style=_numeric_display_style(value))


def _format_charge_multiple_tex(relative_charge: float) -> str:
    if math.isclose(relative_charge, 1.0):
        return _format_value_tex(ELEMENTARY_CHARGE, 3)
    multiplier_tex = _format_value_tex(relative_charge, 3)
    elementary_tex = _format_value_tex(ELEMENTARY_CHARGE, 3)
    return rf"{multiplier_tex} \times {elementary_tex}"


def _detect_distance_variable(inputs: dict[str, dict]) -> tuple[str, dict] | tuple[None, None]:
    for name, payload in inputs.items():
        lowered = name.lower()
        if (
            re.search(r"(?:^|_)(distance|separation|radius|length|closest_approach)(?:$|_)", lowered)
            or lowered in {"r", "radius"}
        ):
            return name, payload
    return None, None


def _detect_charge_variables(inputs: dict[str, dict]) -> list[tuple[str, dict]]:
    detected = []
    for name, payload in inputs.items():
        lowered = name.lower()
        if "charge" in lowered or _normalize_unit_key(payload.get("unit", "")) == "e":
            detected.append((name, payload))
    return detected


def _detect_energy_variable(inputs: dict[str, dict]) -> tuple[str, dict] | tuple[None, None]:
    for name, payload in inputs.items():
        lowered = name.lower()
        if "energy" in lowered or "kinetic" in lowered:
            return name, payload
    return None, None


def _detect_frequency_variable(inputs: dict[str, dict]) -> tuple[str, dict] | tuple[None, None]:
    for name, payload in inputs.items():
        lowered = name.lower()
        unit = _normalize_unit_key(payload.get("unit", ""))
        if "frequency" in lowered or lowered == "f" or unit in {"hz", "khz", "mhz", "ghz"}:
            return name, payload
    return None, None


def _detect_speed_variable(inputs: dict[str, dict]) -> tuple[str, dict] | tuple[None, None]:
    for name, payload in inputs.items():
        lowered = name.lower()
        unit = _normalize_unit_key(payload.get("unit", ""))
        if "speed" in lowered or "velocity" in lowered or unit in {"m/s", "ms^-1", "m/s^1"}:
            return name, payload
    return None, None


def _energy_tex_from_input(value: float, unit: str) -> str:
    normalized_unit = _normalize_unit_key(unit)
    if normalized_unit == "mev":
        return rf"{_format_value_tex(value, 3)} \times {_format_value_tex(1e6, 2)} \times {_format_value_tex(ELEMENTARY_CHARGE, 3)}"
    if normalized_unit == "kev":
        return rf"{_format_value_tex(value, 3)} \times {_format_value_tex(1e3, 2)} \times {_format_value_tex(ELEMENTARY_CHARGE, 3)}"
    if normalized_unit == "ev":
        return rf"{_format_value_tex(value, 3)} \times {_format_value_tex(ELEMENTARY_CHARGE, 3)}"
    return _format_value_tex(value, 3)


def _coulomb_force_feedback_tex(stem: str, inputs: dict[str, dict], answer_value: float, answer_unit: str, significant_figures: int) -> str:
    combined = _normalize_text(stem).lower()
    if "force" not in combined or ("electrostatic" not in combined and "coulomb" not in combined):
        return ""
    charge_variables = _detect_charge_variables(inputs)
    distance_name, distance_payload = _detect_distance_variable(inputs)
    if len(charge_variables) < 2 or distance_payload is None:
        return ""
    q1_name, q1_payload = charge_variables[0]
    q2_name, q2_payload = charge_variables[1]
    distance_value = float(distance_payload.get("value"))
    q1_value = float(q1_payload.get("value"))
    q2_value = float(q2_payload.get("value"))
    symbolic = r"F = k \frac{q_1 q_2}{r^2}"
    substituted = (
        r"F = "
        + _format_value_tex(COULOMB_CONSTANT, 3)
        + r" \times \frac{("
        + _format_charge_multiple_tex(q1_value)
        + r")("
        + _format_charge_multiple_tex(q2_value)
        + r")}{("
        + _format_value_tex(distance_value, 3)
        + r")^2}"
    )
    final = r"F \approx " + _format_value_tex(answer_value, significant_figures) + _unit_tex(answer_unit)
    definitions = _symbol_definitions_text(
        answer_symbol="F",
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols={
            q1_name: "q_1",
            q2_name: "q_2",
            distance_name: "r",
        },
        inputs=inputs,
        used_variable_names=[q1_name, q2_name, distance_name],
        extra_definitions=[("k", "Coulomb's constant")],
    )
    return (
        "Using Coulomb's law:\n\n"
        + (definitions + "\n\n" if definitions else "")
        + rf"\[{symbolic}\]"
        + "\n\n"
        + rf"\[{substituted}\]"
        + "\n\n"
        + rf"\[{final}\]"
    )


def _closest_approach_feedback_tex(stem: str, inputs: dict[str, dict], answer_value: float, answer_unit: str, significant_figures: int) -> str:
    combined = _normalize_text(stem).lower()
    if "closest approach" not in combined or "alpha" not in combined or "nucleus" not in combined:
        return ""
    charge_variables = _detect_charge_variables(inputs)
    energy_name, energy_payload = _detect_energy_variable(inputs)
    if not charge_variables or energy_payload is None:
        return ""
    nucleus_name = None
    nucleus_payload = None
    for name, payload in charge_variables:
        lowered = name.lower()
        if any(term in lowered for term in ("nucleus", "nuclear", "target", "gold", "lead")):
            nucleus_name = name
            nucleus_payload = payload
            break
    if nucleus_payload is None:
        nucleus_name, nucleus_payload = charge_variables[0]
    energy_value = float(energy_payload.get("value"))
    energy_unit = str(energy_payload.get("unit", ""))
    nucleus_charge = float(nucleus_payload.get("value"))
    symbolic = r"E_k = k \frac{q_1 q_2}{r}"
    rearranged = r"r = k \frac{q_1 q_2}{E_k}"
    substituted = (
        r"r = "
        + _format_value_tex(COULOMB_CONSTANT, 3)
        + r" \times \frac{("
        + _format_charge_multiple_tex(2.0)
        + r")("
        + _format_charge_multiple_tex(nucleus_charge)
        + r")}{"
        + _energy_tex_from_input(energy_value, energy_unit)
        + r"}"
    )
    final = r"r \approx " + _format_value_tex(answer_value, significant_figures) + _unit_tex(answer_unit)
    variable_symbols = {}
    if energy_name:
        variable_symbols[energy_name] = r"E_k"
    if nucleus_name:
        variable_symbols[nucleus_name] = "q_2"
    used_variable_names = [name for name in (energy_name, nucleus_name) if name]
    extra_definitions = [("k", "Coulomb's constant"), ("q₁", "the alpha-particle charge")]
    definitions = _symbol_definitions_text(
        answer_symbol="r",
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols=variable_symbols,
        inputs=inputs,
        used_variable_names=used_variable_names,
        extra_definitions=extra_definitions,
    )
    return (
        "Using energy conservation:\n\n"
        + (definitions + "\n\n" if definitions else "")
        + rf"\[{symbolic}\]"
        + "\n\n"
        + rf"\[{rearranged}\]"
        + "\n\n"
        + rf"\[{substituted}\]"
        + "\n\n"
        + rf"\[{final}\]"
    )


def _speed_feedback_tex(
    stem: str,
    inputs: dict[str, dict],
    answer_value: float,
    answer_unit: str,
    significant_figures: int,
    objective_text: str = "",
    chunk_text: str = "",
) -> str:
    combined = _normalize_text(stem).lower()
    if "speed" not in combined and "velocity" not in combined:
        return ""
    distance_payload = None
    time_payload = None
    distance_name = None
    time_name = None
    for name, payload in inputs.items():
        lowered = name.lower()
        if distance_payload is None and any(term in lowered for term in ("distance", "displacement")):
            distance_name = name
            distance_payload = payload
        if time_payload is None and "time" in lowered:
            time_name = name
            time_payload = payload
    if distance_payload is None or time_payload is None:
        return ""
    variable_symbols = _infer_variable_symbols(
        {distance_name: distance_payload, time_name: time_payload},
        stem,
        objective_text,
        chunk_text,
    )
    distance_symbol = variable_symbols.get(distance_name, "d")
    time_symbol = variable_symbols.get(time_name, "t")
    distance_value = float(distance_payload.get("value"))
    time_value = float(time_payload.get("value"))
    answer_symbol = _answer_symbol_from_stem(stem, answer_unit, objective_text, chunk_text)
    symbolic = rf"{answer_symbol} = \frac{{{distance_symbol}}}{{{time_symbol}}}"
    substituted = rf"{answer_symbol} = \frac{{{_format_value_tex(distance_value, 3)}}}{{{_format_value_tex(time_value, 3)}}}"
    final = answer_symbol + r" = " + _format_value_tex(answer_value, significant_figures) + _unit_tex(answer_unit)
    definitions = _symbol_definitions_text(
        answer_symbol=answer_symbol,
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols={distance_name: distance_symbol, time_name: time_symbol},
        inputs=inputs,
        objective_text=objective_text,
        chunk_text=chunk_text,
        used_variable_names=[distance_name, time_name],
    )
    return (
        "Using the speed formula:\n\n"
        + (definitions + "\n\n" if definitions else "")
        + rf"\[{symbolic}\]"
        + "\n\n"
        + rf"\[{substituted}\]"
        + "\n\n"
        + rf"\[{final}\]"
    )


def _centripetal_acceleration_feedback_tex(
    stem: str,
    inputs: dict[str, dict],
    answer_value: float,
    answer_unit: str,
    significant_figures: int,
) -> str:
    combined = _normalize_text(stem).lower()
    normalized_unit = _normalize_unit_key(answer_unit)
    if (
        "centripetal" not in combined
        and "circular" not in combined
        and normalized_unit not in {"m/s^2", "ms^-2", "m s-2", "m/s2"}
    ):
        return ""
    if "acceleration" not in combined and normalized_unit not in {"m/s^2", "ms^-2", "m s-2", "m/s2"}:
        return ""
    speed_name, speed_payload = _detect_speed_variable(inputs)
    radius_name, radius_payload = _detect_distance_variable(inputs)
    if speed_payload is None or radius_payload is None:
        return ""
    radius_lowered = radius_name.lower() if radius_name else ""
    radius_unit = _normalize_unit_key(radius_payload.get("unit", ""))
    if "radius" not in radius_lowered and radius_lowered not in {"r"} and radius_unit not in {"m", "cm", "mm", "km"}:
        return ""
    speed_value = float(speed_payload.get("value"))
    radius_value = float(radius_payload.get("value"))
    squared_speed_value = speed_value ** 2
    exact_answer_tex = _format_number_tex(answer_value, 10, display_style=_numeric_display_style(answer_value))
    symbolic = r"a = \frac{v^2}{r}"
    substituted = (
        r"a = \frac{"
        + _format_value_tex(speed_value, 3)
        + r"^2}{"
        + _format_value_tex(radius_value, 3)
        + r"} = \frac{"
        + _format_number_tex(squared_speed_value, 10, display_style=_numeric_display_style(squared_speed_value))
        + r"}{"
        + _format_value_tex(radius_value, 3)
        + r"} = "
        + exact_answer_tex
    )
    final = r"a \approx " + _format_value_tex(answer_value, significant_figures) + _unit_tex(answer_unit)
    return rf"\[{symbolic}\]" + "\n\n" + rf"\[{substituted}\]" + "\n\n" + rf"\[{final}\]"


def _photon_energy_feedback_tex(stem: str, inputs: dict[str, dict], answer_value: float, answer_unit: str, significant_figures: int) -> str:
    combined = _normalize_text(stem).lower()
    if "photon" not in combined or "energy" not in combined:
        return ""
    frequency_name, frequency_payload = _detect_frequency_variable(inputs)
    if frequency_payload is None:
        return ""
    frequency_value = float(frequency_payload.get("value"))
    symbolic = r"E = h f"
    substituted = (
        r"E = "
        + _format_value_tex(6.626e-34, 4)
        + r" \times "
        + _format_value_tex(frequency_value, 3)
    )
    final = r"E \approx " + _format_value_tex(answer_value, significant_figures) + _unit_tex(answer_unit)
    definitions = _symbol_definitions_text(
        answer_symbol="E",
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols={frequency_name: "f"},
        inputs=inputs,
        used_variable_names=[frequency_name],
        extra_definitions=[("h", "Planck's constant")],
    )
    return (
        "Using the photon-energy relationship:\n\n"
        + (definitions + "\n\n" if definitions else "")
        + rf"\[{symbolic}\]"
        + "\n\n"
        + rf"\[{substituted}\]"
        + "\n\n"
        + rf"\[{final}\]"
    )


def _feedback_symbol_context(stem: str, objective_text: str = "", chunk_text: str = "") -> str:
    return _normalize_text(" ".join(part for part in (stem, objective_text, chunk_text) if part)).lower()


def _is_motion_context(context_text: str) -> bool:
    return any(
        term in context_text
        for term in (
            "motion",
            "kinematics",
            "velocity",
            "speed",
            "acceleration",
            "displacement",
            "suvat",
            "distance travelled",
        )
    )


def _prefers_displacement_symbol(context_text: str) -> bool:
    return any(
        term in context_text
        for term in (
            "displacement",
            "suvat",
            "initial velocity",
            "final velocity",
            "uniform acceleration",
            "constant acceleration",
        )
    )


def _answer_symbol_from_stem(stem: str, answer_unit: str, objective_text: str = "", chunk_text: str = "") -> str:
    lowered = _feedback_symbol_context(stem, objective_text, chunk_text)
    targeted_patterns = (
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,120}\bplanck'?s constant\b", "h"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\benergy\b", "E"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\bfrequency\b", "f"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\bwavelength\b", r"\lambda"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\bacceleration\b", "a"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\bdisplacement\b", "s"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\bvelocity\b", "v"),
        (r"\b(?:what is|calculate|determine|estimate|find)\b[^.?!]{0,80}\bspeed\b", "v"),
    )
    for pattern, symbol in targeted_patterns:
        if re.search(pattern, lowered):
            return symbol
    patterns = (
        (r"\bplanck'?s constant\b", "h"),
        (r"\b(?:undecayed|remaining|remnant)\s+nuclei\b|\bnumber of undecayed nuclei\b", "N"),
        (r"\bage\b", "t"),
        (r"\bforce\b", "F"),
        (r"\bphase difference\b|\bphase angle\b", r"\delta"),
        (r"\bangle\b|\btheta\b", r"\theta"),
        (r"\bcentripetal acceleration\b|\bacceleration\b", "a"),
        (r"\bdisplacement\b|\bdistance travelled\b", "s"),
        (r"\bwavelength\b", r"\lambda"),
        (r"\bamplitude\b", "A"),
        (r"\bspeed\b|\bvelocity\b", "v"),
        (r"\bgravitational field strength\b", "g"),
        (r"\bmomentum\b", "p"),
        (r"\bperiod\b", "T"),
        (r"\btemperature\b", "T"),
        (r"\bdistance\b|\bseparation\b|\blength\b", "d"),
        (r"\bradius\b|\bclosest approach\b", "r"),
        (r"\btime\b", "t"),
        (r"\benergy\b", "E"),
        (r"\bfrequency\b", "f"),
        (r"\bpower\b", "P"),
        (r"\bpressure\b", "p"),
        (r"\bdensity\b", r"\rho"),
        (r"\bcharge\b", "q"),
        (r"\bcurrent\b", "I"),
        (r"\bvoltage\b|\bpotential difference\b", "V"),
        (r"\bresistance\b", "R"),
        (r"\bmass\b", "m"),
        (r"\bactivity\b", "A"),
        (r"\bcount rate\b", "R"),
        (r"\barea\b", "A"),
        (r"\bvolume\b", "V"),
        (r"\bconcentration\b", "c"),
        (r"\bprobability\b", "P"),
    )
    for pattern, symbol in patterns:
        if re.search(pattern, lowered):
            return symbol
    normalized_unit = _normalize_unit_key(answer_unit)
    if normalized_unit == "n":
        return "F"
    if normalized_unit in {"j·s", "j.s", "js"} and "planck" in lowered:
        return "h"
    if normalized_unit in {"m/s^2", "ms^-2", "m s-2", "m/s2"}:
        return "a"
    if normalized_unit in {"m/s", "ms^-1", "m/s^1"}:
        return "v"
    if normalized_unit in {"years", "year", "yr", "yrs", "s", "seconds", "second"} and "age" in lowered:
        return "t"
    return "x"


def _activity_symbol(name: str) -> str:
    lowered = name.lower()
    if any(term in lowered for term in ("fresh", "initial", "living", "original", "starting")):
        return r"A_0"
    if any(term in lowered for term in ("sample", "measured", "current", "remaining", "observed", "final")):
        return "A"
    return "A"


def _count_rate_symbol(name: str) -> str:
    lowered = name.lower()
    if "background" in lowered:
        return r"R_b"
    if "total" in lowered:
        return r"R_{\mathrm{total}}"
    if "net" in lowered:
        return r"R_{\mathrm{net}}"
    return "R"


def _conventional_symbol_for_variable(name: str, payload: dict, stem: str, objective_text: str = "", chunk_text: str = "") -> str:
    lowered = name.lower()
    normalized_unit = _normalize_unit_key(payload.get("unit", ""))
    context_text = _feedback_symbol_context(stem, objective_text, chunk_text)
    prefers_displacement_symbol = _prefers_displacement_symbol(context_text)
    if "half_life" in lowered or "half-life" in lowered:
        return r"t_{1/2}"
    if "decay_constant" in lowered or "decay constant" in lowered:
        return r"\lambda"
    if "activity" in lowered:
        return _activity_symbol(name)
    if "phase" in lowered and "difference" in lowered:
        return r"\delta"
    if "angle" in lowered or lowered == "theta":
        return r"\theta"
    if "nuclei" in lowered or "nucleus_count" in lowered or "atom_count" in lowered:
        if any(term in lowered for term in ("initial", "fresh", "original", "starting")):
            return r"N_0"
        return "N"
    if "count" in lowered and "rate" in lowered:
        return _count_rate_symbol(name)
    if "initial" in lowered and any(term in lowered for term in ("velocity", "speed")):
        return "u"
    if any(term in lowered for term in ("final", "terminal", "resultant")) and any(term in lowered for term in ("velocity", "speed")):
        return "v"
    if "age" in lowered:
        return "t"
    if "time" in lowered:
        if "half" in lowered and "life" in lowered:
            return r"t_{1/2}"
        return "t"
    if "period" in lowered:
        return "T"
    if "temperature" in lowered:
        return "T"
    if "amplitude" in lowered:
        return "A"
    if "slit" in lowered and any(term in lowered for term in ("separation", "spacing", "distance")):
        return "a"
    if "screen" in lowered and "distance" in lowered:
        return "D"
    if "fringe" in lowered and any(term in lowered for term in ("spacing", "distance")):
        return "w"
    if "displacement" in lowered:
        return "s"
    if "distance" in lowered and prefers_displacement_symbol:
        return "s"
    if "distance" in lowered or "separation" in lowered or "length" in lowered:
        return "r" if "closest approach" in context_text else "d"
    if "radius" in lowered or "closest_approach" in lowered:
        return "r"
    if "speed" in lowered or "velocity" in lowered:
        return "v"
    if "acceleration" in lowered:
        return "a"
    if "gravitational" in lowered and "field" in lowered:
        return "g"
    if lowered in {"g", "gravity"} and "gravity" in context_text:
        return "g"
    if "kinetic_energy" in lowered:
        return r"E_k"
    if "energy" in lowered:
        return "E"
    if "mass" in lowered:
        return "m"
    if "momentum" in lowered:
        return "p"
    if "spring_constant" in lowered:
        return "k"
    if "extension" in lowered:
        return "x"
    if "height" in lowered:
        return "h"
    if "magnetic" in lowered and "field" in lowered:
        return "B"
    if "flux_density" in lowered:
        return "B"
    if "pressure" in lowered:
        return "p"
    if "density" in lowered:
        return r"\rho"
    if "frequency" in lowered:
        return "f"
    if "wavelength" in lowered:
        return r"\lambda"
    if ("speed" in lowered and "light" in lowered) or lowered in {"c", "light_speed", "speed_of_light"}:
        return "c"
    if "current" in lowered:
        return "I"
    if "voltage" in lowered or "potential" in lowered:
        return "V"
    if "resistance" in lowered:
        return "R"
    if "planck" in lowered and "constant" in lowered:
        return "h"
    if ("elementary" in lowered and "charge" in lowered) or lowered in {"electron_charge", "elementary_charge"}:
        return "e"
    if "work" in lowered and "function" in lowered:
        return r"\phi"
    if "area" in lowered:
        return "A"
    if "volume" in lowered:
        return "V"
    if "concentration" in lowered:
        return "c"
    if "moles" in lowered or lowered == "n":
        return "n"
    if "charge" in lowered or normalized_unit == "e":
        return "q"
    return ""


def _plain_symbol_text(symbol: str) -> str:
    plain = str(symbol or "")
    replacements = {
        r"\lambda": "λ",
        r"\rho": "ρ",
        r"\delta": "δ",
        r"\theta": "θ",
        r"t_{1/2}": "t₁/₂",
        r"A_0": "A₀",
        r"N_0": "N₀",
        r"R_{\mathrm{total}}": "R_total",
        r"R_{\mathrm{net}}": "R_net",
        r"R_b": "R_b",
        r"q_1": "q₁",
        r"q_2": "q₂",
        r"E_k": "Eₖ",
    }
    for source, target in replacements.items():
        plain = plain.replace(source, target)
    plain = re.sub(r"\\mathrm\{([^}]+)\}", r"\1", plain)
    plain = plain.replace("{", "").replace("}", "")
    return plain


def _answer_symbol_description(answer_symbol: str, stem: str, answer_unit: str, objective_text: str = "", chunk_text: str = "") -> str:
    lowered = _feedback_symbol_context(stem, objective_text, chunk_text)
    if answer_symbol == "h":
        return "Planck's constant"
    if answer_symbol == "t" and "age" in lowered:
        return "the age of the sample"
    if answer_symbol == "F":
        return "the electrostatic force"
    if answer_symbol == r"\delta":
        return "the phase difference"
    if answer_symbol == r"\theta":
        return "the angle"
    if answer_symbol == "s":
        return "the displacement"
    if answer_symbol == "v":
        return "the average speed"
    if answer_symbol == "a":
        return "the acceleration"
    if answer_symbol == "g":
        return "the gravitational field strength"
    if answer_symbol == "d":
        return "the distance"
    if answer_symbol == "r":
        return "the distance of closest approach" if "closest approach" in lowered else "the radius"
    if answer_symbol == "E":
        return "the energy"
    if answer_symbol == "P":
        return "the power"
    if answer_symbol == "p":
        return "the pressure"
    if answer_symbol == r"\lambda":
        return "the wavelength"
    if answer_symbol == "A":
        return "the activity"
    if answer_symbol == "N":
        return "the number of undecayed nuclei remaining"
    if answer_symbol == "R":
        return "the count rate"
    return "the required quantity"


def _variable_symbol_description(name: str, payload: dict, symbol: str, stem: str, objective_text: str = "", chunk_text: str = "") -> str:
    lowered = name.lower()
    context_text = _feedback_symbol_context(stem, objective_text, chunk_text)
    if symbol == "h" and "planck" in lowered:
        return "Planck's constant"
    if symbol == r"t_{1/2}" or "half_life" in lowered or "half-life" in lowered:
        return "the half-life"
    if "decay_constant" in lowered or "decay constant" in lowered:
        return "the decay constant"
    if symbol == "A":
        if "amplitude" in lowered:
            return "the amplitude"
        return "the activity of the sample"
    if symbol == r"A_0":
        return "the activity of living material"
    if symbol == r"N_0":
        return "the initial number of undecayed nuclei"
    if symbol == "N":
        return "the number of undecayed nuclei remaining"
    if symbol == r"\delta":
        return "the phase difference"
    if symbol == r"\theta":
        return "the angle"
    if symbol == "t":
        return "the elapsed time" if "age" not in lowered else "the age of the sample"
    if symbol == "u":
        return "the initial velocity"
    if symbol == "s":
        return "the displacement" if _is_motion_context(context_text) else "the distance travelled"
    if symbol == "d":
        return "the distance"
    if symbol == "D":
        return "the screen distance"
    if symbol == "w":
        return "the fringe spacing"
    if symbol == "r":
        if "closest approach" in context_text:
            return "the distance of closest approach"
        if "radius" in lowered or "circular" in context_text or "centripetal" in context_text:
            return "the radius"
        return "the separation distance"
    if symbol == "a":
        if "slit" in lowered:
            return "the slit separation"
        return "the acceleration"
    if symbol == "v":
        if "velocity" in lowered:
            return "the final velocity"
        return "the speed"
    if symbol == "g":
        return "the gravitational field strength"
    if symbol == "m":
        return "the mass"
    if symbol == "h":
        return "the height"
    if symbol == "k":
        return "the spring constant"
    if symbol == "p":
        if "momentum" in lowered:
            return "the momentum"
        return "the pressure"
    if symbol == "B":
        return "the magnetic flux density"
    if symbol == "T":
        return "the period" if "period" in lowered else "the temperature"
    if symbol == r"\rho":
        return "the density"
    if symbol == "f":
        return "the frequency"
    if symbol == r"\lambda":
        return "the wavelength"
    if symbol == "c":
        if "light" in lowered:
            return "the speed of light"
        return "the concentration"
    if symbol == "I":
        return "the current"
    if symbol == "V":
        return "the potential difference" if "voltage" in lowered or "potential" in lowered else "the voltage"
    if symbol == "R":
        return "the count rate"
    if symbol == r"R_b":
        return "the background count rate"
    if symbol == r"R_{\mathrm{total}}":
        return "the total count rate"
    if symbol == r"R_{\mathrm{net}}":
        return "the net count rate"
    if symbol == "n":
        return "the amount in moles"
    if symbol == "e":
        return "the elementary charge"
    if symbol == r"\phi":
        return "the work function"
    if symbol == r"E_k":
        return "the kinetic energy"
    if symbol.startswith("q_"):
        if "proton" in lowered:
            return "the proton charge"
        if "alpha" in lowered:
            return "the alpha-particle charge"
        if any(term in lowered for term in ("nucleus", "nuclear", "target", "gold", "lead")):
            return "the nuclear charge"
        return "a charge"
    cleaned = re.sub(r"[_\s]+", " ", lowered).strip()
    return f"the {cleaned}" if cleaned else "the quantity"


def _join_definitions(definitions: list[tuple[str, str]]) -> str:
    if not definitions:
        return ""
    rendered = [f"{symbol} is {description}" for symbol, description in definitions]
    if len(rendered) == 1:
        return f"Here, {rendered[0]}."
    if len(rendered) == 2:
        return f"Here, {rendered[0]} and {rendered[1]}."
    return "Here, " + ", ".join(rendered[:-1]) + f", and {rendered[-1]}."


def _symbol_definitions_text(
    *,
    answer_symbol: str,
    stem: str,
    answer_unit: str,
    variable_symbols: dict[str, str],
    inputs: dict[str, dict],
    objective_text: str = "",
    chunk_text: str = "",
    answer_description_override: str = "",
    used_variable_names: list[str] | None = None,
    extra_definitions: list[tuple[str, str]] | None = None,
) -> str:
    definitions: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(symbol: str, description: str) -> None:
        plain = _plain_symbol_text(symbol)
        if not plain or plain in seen or not description:
            return
        seen.add(plain)
        definitions.append((plain, description))

    add(
        answer_symbol,
        answer_description_override or _answer_symbol_description(answer_symbol, stem, answer_unit, objective_text, chunk_text),
    )
    relevant_names = used_variable_names or list(inputs.keys())
    for name in relevant_names:
        if name not in variable_symbols or name not in inputs:
            continue
        symbol = variable_symbols[name]
        add(symbol, _variable_symbol_description(name, inputs[name], symbol, stem, objective_text, chunk_text))
    for symbol, description in extra_definitions or []:
        add(symbol, description)
    return _join_definitions(definitions)


def _infer_variable_symbols(inputs: dict[str, dict], stem: str, objective_text: str = "", chunk_text: str = "") -> dict[str, str]:
    charge_index = 1
    symbol_map: dict[str, str] = {}
    for name, payload in inputs.items():
        lowered = name.lower()
        symbol = ""
        conventional = _conventional_symbol_for_variable(name, payload, stem, objective_text, chunk_text)
        if conventional in {"q", ""} and ("charge" in lowered or _normalize_unit_key(payload.get("unit", "")) == "e"):
            symbol = rf"q_{charge_index}"
            charge_index += 1
        elif conventional:
            symbol = conventional
        elif re.fullmatch(r"[xyzabc]", lowered):
            symbol = lowered
        if not symbol:
            cleaned = re.sub(r"[^A-Za-z0-9]+", " ", name).strip()
            initial = cleaned[:1].lower() if cleaned else "x"
            symbol = initial
        symbol_map[name] = symbol
    return symbol_map


def _value_replacement_tex(name: str, payload: dict, significant_figures: int) -> str:
    value = float(payload.get("value"))
    unit = str(payload.get("unit", ""))
    normalized_unit = _normalize_unit_key(unit)
    lowered = name.lower()
    if normalized_unit == "e" and "charge" in lowered:
        return _format_value_tex(value, max(significant_figures, 3))
    return _format_number_tex(value, 10, display_style=_numeric_display_style(value))


def _objective_heuristic_symbol_overrides(
    inputs: dict[str, dict],
    objective_symbol_heuristics: dict | None,
    stem: str = "",
    objective_text: str = "",
    chunk_text: str = "",
) -> tuple[str, str, dict[str, str], list[tuple[str, str]], list[dict]]:
    heuristics = normalize_objective_symbol_heuristics(objective_symbol_heuristics)
    if not heuristics:
        fallback_context = " ".join(part for part in (stem, objective_text, chunk_text) if part).strip()
        heuristics = deterministic_symbol_heuristics_for_objective(
            objective_text or stem,
            fallback_context,
        )
    if not heuristics:
        return "", "", {}, [], []

    variable_symbols: dict[str, str] = {}
    extra_definitions: list[tuple[str, str]] = []
    for hint in heuristics.get("variable_hints", []):
        terms = [str(term).strip().lower() for term in hint.get("match_terms", []) if str(term).strip()]
        symbol = symbol_plain_to_tex(str(hint.get("symbol", "") or ""))
        description = str(hint.get("description", "") or "").strip()
        if not terms or not symbol:
            continue
        for name in inputs.keys():
            lowered_name = name.lower()
            if all(term in lowered_name for term in terms):
                variable_symbols[name] = symbol
                if description:
                    extra_definitions.append((symbol, description))

    constant_aliases = []
    for hint in heuristics.get("constant_hints", []):
        symbol = symbol_plain_to_tex(str(hint.get("symbol", "") or ""))
        description = str(hint.get("description", "") or "").strip()
        approx_value = hint.get("approx_value")
        if isinstance(approx_value, bool) or not isinstance(approx_value, (int, float)):
            continue
        if not symbol:
            continue
        constant_aliases.append(
            {
                "symbol": symbol,
                "description": description,
                "approx_value": float(approx_value),
            }
        )
        if description:
            extra_definitions.append((symbol, description))

    return (
        symbol_plain_to_tex(str(heuristics.get("answer_symbol", "") or "")),
        str(heuristics.get("answer_description", "") or "").strip(),
        variable_symbols,
        extra_definitions,
        constant_aliases,
    )


def _generic_feedback_tex(
    stem: str,
    explanation_text: str,
    numeric_metadata: dict,
    inputs: dict[str, dict],
    answer_value: float,
    answer_unit: str,
    significant_figures: int,
    objective_text: str = "",
    chunk_text: str = "",
    objective_symbol_heuristics: dict | None = None,
) -> str:
    validation = numeric_metadata.get("validation") if isinstance(numeric_metadata.get("validation"), dict) else {}
    answer_expression = str(validation.get("answer_expression", "")).strip()
    if not answer_expression:
        return ""
    try:
        tree = ast.parse(answer_expression, mode="eval")
    except SyntaxError:
        return ""
    variable_symbols = _infer_variable_symbols(inputs, stem, objective_text, chunk_text)
    (
        heuristic_answer_symbol,
        heuristic_answer_description,
        heuristic_variable_symbols,
        extra_definitions,
        constant_aliases,
    ) = _objective_heuristic_symbol_overrides(
        inputs,
        objective_symbol_heuristics,
        stem,
        objective_text,
        chunk_text,
    )
    variable_symbols.update(heuristic_variable_symbols)
    symbolic_expression = _expression_to_tex(tree, variable_symbols, constant_aliases=constant_aliases)
    value_expression = _expression_to_tex(
        tree,
        {name: _value_replacement_tex(name, payload, significant_figures) for name, payload in inputs.items()},
    )
    answer_symbol = heuristic_answer_symbol or _answer_symbol_from_stem(stem, answer_unit, objective_text, chunk_text)
    intro = "Using the relevant relationship:"
    if explanation_text:
        first_sentence = re.split(r"(?<=[.?!])\s+", _normalize_text(explanation_text), maxsplit=1)[0].strip()
        if first_sentence:
            intro = first_sentence.rstrip(".") + ":"
    symbolic = rf"{answer_symbol} = {symbolic_expression}"
    substituted = rf"{answer_symbol} = {value_expression}"
    final = answer_symbol + r" = " + _format_value_tex(answer_value, significant_figures) + _unit_tex(answer_unit)
    definitions = _symbol_definitions_text(
        answer_symbol=answer_symbol,
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols=variable_symbols,
        inputs=inputs,
        objective_text=objective_text,
        chunk_text=chunk_text,
        answer_description_override=heuristic_answer_description,
        used_variable_names=list(validation.get("used_variables") or inputs.keys()),
        extra_definitions=extra_definitions,
    )
    return (
        intro
        + "\n\n"
        + (definitions + "\n\n" if definitions else "")
        + rf"\[{symbolic}\]"
        + "\n\n"
        + rf"\[{substituted}\]"
        + "\n\n"
        + rf"\[{final}\]"
    )


def format_numeric_feedback_explanation(
    *,
    stem: str,
    explanation_text: str,
    numeric_metadata: dict | None,
    objective_text: str = "",
    chunk_text: str = "",
    objective_symbol_heuristics: dict | None = None,
) -> str:
    if not isinstance(numeric_metadata, dict):
        return ""
    snapshot = numeric_metadata.get("output_snapshot") if isinstance(numeric_metadata.get("output_snapshot"), dict) else {}
    inputs = numeric_metadata.get("inputs") if isinstance(numeric_metadata.get("inputs"), dict) else {}
    answer_value = snapshot.get("answer_value")
    if isinstance(answer_value, bool) or not isinstance(answer_value, (int, float)):
        return ""
    answer_unit = str(snapshot.get("answer_unit", ""))
    significant_figures = _infer_significant_figures_from_answer_text(str(snapshot.get("correct_answer", "")))
    for formatter in (
        _coulomb_force_feedback_tex,
        _closest_approach_feedback_tex,
        _photon_energy_feedback_tex,
        _centripetal_acceleration_feedback_tex,
        _speed_feedback_tex,
    ):
        if formatter is _speed_feedback_tex:
            formatted = formatter(
                stem,
                inputs,
                float(answer_value),
                answer_unit,
                significant_figures,
                objective_text,
                chunk_text,
            )
        else:
            formatted = formatter(stem, inputs, float(answer_value), answer_unit, significant_figures)
        if formatted:
            return formatted
    return _generic_feedback_tex(
        stem,
        explanation_text,
        numeric_metadata,
        inputs,
        float(answer_value),
        answer_unit,
        significant_figures,
        objective_text,
        chunk_text,
        objective_symbol_heuristics,
    )


NUMERIC_FEEDBACK_VERSION = "option-v2"
MAX_NUMERIC_FEEDBACK_KEY_IDEA_LENGTH = 240
MAX_NUMERIC_FEEDBACK_NOTE_LENGTH = 240
NUMERIC_FEEDBACK_OPTION_LABEL_PATTERN = re.compile(
    r"\b(?:option|answer|choice|letter)\s+[A-Z]\b",
    re.IGNORECASE,
)
NUMERIC_FEEDBACK_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "key_idea": {"type": "string", "minLength": 1},
        "formula": {"type": "string", "minLength": 1},
        "substitution": {"type": "string", "minLength": 1},
        "final_answer": {"type": "string", "minLength": 1},
        "options": {
            "type": "array",
            "minItems": 2,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "answer_text": {"type": "string", "minLength": 1},
                    "is_correct": {"type": "boolean"},
                    "note": {"type": "string", "minLength": 1},
                },
                "required": ["answer_text", "is_correct", "note"],
            },
        },
    },
    "required": ["key_idea", "formula", "substitution", "final_answer", "options"],
}
PLAIN_SYMBOL_CANONICAL_MAP = {
    "λ": "lambda",
    "rho": "rho",
    "ρ": "rho",
    "δ": "delta",
    "θ": "theta",
    "A₀": "A0",
    "N₀": "N0",
    "t₁/₂": "t_half",
    "q₁": "q1",
    "q₂": "q2",
    "Eₖ": "Ek",
}
SUPERSCRIPT_REVERSE_TRANSLATION = str.maketrans({value: key for key, value in SUPERSCRIPT_TRANSLATION.items()})
NUMERIC_FEEDBACK_SYMBOL_CLAIM_PATTERN = re.compile(
    r"\b(?:where\s+)?(?P<symbol>λ|ρ|[A-Za-z](?:[₀₁₂ₖ]|₁/₂)?|[A-Za-z]_[0-9])\s+"
    r"(?:is|means|represents)\s+(?P<meaning>[^.;:\n]+)",
    re.IGNORECASE,
)


def _canonical_plain_symbol(symbol: str) -> str:
    normalized = _normalize_text(symbol).replace(" ", "")
    normalized = normalized.replace("₀", "0").replace("₁", "1").replace("₂", "2").replace("ₖ", "k")
    normalized = normalized.replace("₁/₂", "_half")
    normalized = normalized.replace("_{1/2}", "_half").replace("{1/2}", "_half")
    normalized = normalized.replace("λ", "lambda").replace("ρ", "rho")
    normalized = normalized.replace("\\lambda", "lambda").replace("\\rho", "rho")
    normalized = normalized.replace("\\", "")
    normalized = normalized.replace("{", "").replace("}", "")
    normalized = normalized.replace("_", "")
    return PLAIN_SYMBOL_CANONICAL_MAP.get(symbol, normalized)


def _meaning_kind(description: str) -> str:
    lowered = _normalize_text(description).lower()
    if "decay constant" in lowered:
        return "decay_constant"
    if "wavelength" in lowered:
        return "wavelength"
    if "power" in lowered:
        return "power"
    if "potential difference" in lowered or "voltage" in lowered:
        return "voltage"
    if "volume" in lowered:
        return "volume"
    if "current" in lowered:
        return "current"
    if "initial number of undecayed nuclei" in lowered:
        return "initial_nuclei"
    if "undecayed nuclei" in lowered or "number remaining" in lowered:
        return "remaining_nuclei"
    if "activity of living material" in lowered or "initial activity" in lowered:
        return "initial_activity"
    if "activity of the sample" in lowered or "measured activity" in lowered or "remaining activity" in lowered:
        return "sample_activity"
    if "half-life" in lowered:
        return "half_life"
    if "elapsed time" in lowered or "age of the sample" in lowered or lowered == "time":
        return "time"
    if "mass" in lowered:
        return "mass"
    if "distance" in lowered or "separation" in lowered:
        return "distance"
    if "force" in lowered:
        return "force"
    return ""


def _meaning_keywords(description: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _normalize_text(description).lower())
        if token not in {"the", "of", "and", "a", "an", "in", "to"}
    }


def _symbol_definition_expectations(
    *,
    answer_symbol: str,
    stem: str,
    answer_unit: str,
    variable_symbols: dict[str, str],
    inputs: dict[str, dict],
    objective_text: str = "",
    chunk_text: str = "",
    answer_description_override: str = "",
    used_variable_names: list[str],
) -> dict[str, str]:
    expectations: dict[str, str] = {}
    plain_answer_symbol = _plain_symbol_text(answer_symbol)
    if plain_answer_symbol:
        expectations[_canonical_plain_symbol(plain_answer_symbol)] = _answer_symbol_description(
            answer_symbol,
            stem,
            answer_unit,
            objective_text,
            chunk_text,
        ) if not answer_description_override else answer_description_override
    for name in used_variable_names:
        symbol = variable_symbols.get(name)
        payload = inputs.get(name)
        if not symbol or payload is None:
            continue
        plain_symbol = _plain_symbol_text(symbol)
        if not plain_symbol:
            continue
        expectations[_canonical_plain_symbol(plain_symbol)] = _variable_symbol_description(
            name,
            payload,
            symbol,
            stem,
            objective_text,
            chunk_text,
        )
    return expectations


def _extract_symbol_claims(text: str) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    for match in NUMERIC_FEEDBACK_SYMBOL_CLAIM_PATTERN.finditer(_normalize_text(text)):
        claims.append((match.group("symbol"), match.group("meaning")))
    return claims


def _validate_symbol_definitions(text: str, expected_definitions: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for symbol, meaning in _extract_symbol_claims(text):
        canonical_symbol = _canonical_plain_symbol(symbol)
        actual_kind = _meaning_kind(meaning)
        if canonical_symbol not in expected_definitions:
            if canonical_symbol in {"P", "V", "I", "m", "lambda", "A0", "N0", "N", "A", "t"}:
                errors.append(f"{symbol} was introduced even though it is not part of this solution.")
            continue
        expected_kind = _meaning_kind(expected_definitions[canonical_symbol])
        if actual_kind and expected_kind and actual_kind != expected_kind:
            errors.append(
                f"{symbol} was defined as {meaning.strip()} but should mean {expected_definitions[canonical_symbol]}."
            )
            continue
        if expected_kind and not actual_kind:
            expected_keywords = _meaning_keywords(expected_definitions[canonical_symbol])
            actual_keywords = _meaning_keywords(meaning)
            if expected_keywords and len(expected_keywords & actual_keywords) == 0:
                errors.append(
                    f"{symbol} was defined as {meaning.strip()} but should mean {expected_definitions[canonical_symbol]}."
                )
    return errors


def _expand_superscript_exponents(text: str) -> str:
    expanded = str(text or "").translate(SUPERSCRIPT_REVERSE_TRANSLATION)
    return re.sub(r"10\s*([+-]?\d+)", r"10^\1", expanded)


def _parse_numeric_answer_choice(answer_text: str) -> tuple[float | None, str]:
    normalized = _expand_superscript_exponents(_normalize_text(answer_text))
    normalized = normalized.replace("−", "-").replace("–", "-").replace("—", "-")
    normalized = normalized.replace("×", "x")
    normalized = re.sub(r"([0-9.]+)\s*[xX]\s*10\s*\^?\s*([+-]?\d+)", r"\1e\2", normalized)
    match = re.match(r"\s*([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)\b(.*)", normalized, re.IGNORECASE)
    if not match:
        return None, ""
    try:
        value = float(match.group(1))
    except ValueError:
        return None, ""
    return value, _normalize_text(match.group(2))


def _first_explanation_sentence(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    return re.split(r"(?<=[.?!])\s+", normalized, maxsplit=1)[0].strip()


def _default_numeric_key_idea(stem: str, explanation_text: str, definitions_text: str) -> str:
    first_sentence = _first_explanation_sentence(explanation_text)
    if first_sentence:
        return first_sentence
    lowered = _normalize_text(stem).lower()
    if "decay" in lowered or "radioactive" in lowered:
        return "Radioactive decay is modelled exponentially, so the remaining quantity comes from the initial amount and the decay constant."
    if "force" in lowered and ("electrostatic" in lowered or "coulomb" in lowered):
        return "Use Coulomb's law to relate the force to the two charges and the square of their separation."
    if definitions_text:
        return "Match each symbol to the physical quantity it represents before substituting numbers."
    return "Identify the governing relationship first, then substitute values carefully."


def _deterministic_numeric_verdict(
    *,
    is_correct: bool,
    selected_answer_text: str,
    correct_answer_text: str,
    answer_value: float,
    answer_unit: str,
) -> str:
    if is_correct:
        return "correct"
    return "incorrect"


def _default_targeted_feedback(
    *,
    verdict: str,
    stem: str,
    selected_answer_text: str,
    answer_value: float,
    answer_unit: str,
) -> tuple[str, str]:
    lowered = _normalize_text(stem).lower()
    if verdict == "correct":
        return (
            "Your answer matches the expected value and unit.",
            "Keep matching each symbol to its physical meaning before you substitute.",
        )

    selected_value, selected_unit = _parse_numeric_answer_choice(selected_answer_text)
    if selected_value is not None:
        same_unit = _normalize_unit_key(selected_unit) == _normalize_unit_key(answer_unit)
        if same_unit and answer_value and abs(selected_value) > abs(answer_value) * 50:
            return (
                "Check the power of ten in your final answer before choosing an option.",
                "Large scientific-notation slips usually come from exponent or unit-conversion errors.",
            )
        if "decay" in lowered or "radioactive" in lowered:
            return (
                "Check that you keep the initial quantity and the remaining quantity distinct throughout the substitution.",
                "For decay, the remaining amount should come from the initial amount multiplied by an exponential factor less than 1.",
            )
    return (
        "Recheck the substitution step and make sure each symbol is matched to the correct physical quantity.",
        "A correct method usually fails first when one symbol is paired with the wrong quantity or unit.",
    )


def _extract_feedback_display_blocks(text: str) -> list[str]:
    return [block.strip() for block in re.findall(r"\\\[(.*?)\\\]", str(text or ""), flags=re.S) if block.strip()]


def _extract_feedback_definitions_text(text: str) -> str:
    match = re.search(r"(Here,\s.*?\.)", _normalize_text(text), flags=re.S)
    return match.group(1).strip() if match else ""


def _build_numeric_feedback_context(
    *,
    stem: str,
    explanation_text: str,
    numeric_metadata: dict,
    objective_text: str = "",
    chunk_text: str = "",
    objective_symbol_heuristics: dict | None = None,
) -> dict:
    if not isinstance(numeric_metadata, dict):
        return {}
    snapshot = numeric_metadata.get("output_snapshot") if isinstance(numeric_metadata.get("output_snapshot"), dict) else {}
    inputs = numeric_metadata.get("inputs") if isinstance(numeric_metadata.get("inputs"), dict) else {}
    validation = numeric_metadata.get("validation") if isinstance(numeric_metadata.get("validation"), dict) else {}
    answer_value = snapshot.get("answer_value")
    if isinstance(answer_value, bool) or not isinstance(answer_value, (int, float)):
        return {}
    answer_unit = str(snapshot.get("answer_unit", ""))
    correct_answer_text = str(snapshot.get("correct_answer", "")).strip()
    significant_figures = _infer_significant_figures_from_answer_text(correct_answer_text)
    answer_expression = str(validation.get("answer_expression", "")).strip()
    if not answer_expression:
        return {}
    try:
        tree = ast.parse(answer_expression, mode="eval")
    except SyntaxError:
        return {}
    variable_symbols = _infer_variable_symbols(inputs, stem, objective_text, chunk_text)
    (
        heuristic_answer_symbol,
        heuristic_answer_description,
        heuristic_variable_symbols,
        extra_definitions,
        constant_aliases,
    ) = _objective_heuristic_symbol_overrides(
        inputs,
        objective_symbol_heuristics,
        stem,
        objective_text,
        chunk_text,
    )
    variable_symbols.update(heuristic_variable_symbols)
    used_variable_names = list(validation.get("used_variables") or inputs.keys())
    answer_symbol = heuristic_answer_symbol or _answer_symbol_from_stem(stem, answer_unit, objective_text, chunk_text)
    formula_tex = answer_symbol + r" = " + _expression_to_tex(tree, variable_symbols, constant_aliases=constant_aliases)
    substitution_tex = answer_symbol + r" = " + _expression_to_tex(
        tree,
        {name: _value_replacement_tex(name, payload, significant_figures) for name, payload in inputs.items()},
    )
    final_tex = answer_symbol + r" \approx " + _format_value_tex(float(answer_value), significant_figures) + _unit_tex(answer_unit)
    expectations = _symbol_definition_expectations(
        answer_symbol=answer_symbol,
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols=variable_symbols,
        inputs=inputs,
        objective_text=objective_text,
        chunk_text=chunk_text,
        answer_description_override=heuristic_answer_description,
        used_variable_names=used_variable_names,
    )
    definitions_text = _symbol_definitions_text(
        answer_symbol=answer_symbol,
        stem=stem,
        answer_unit=answer_unit,
        variable_symbols=variable_symbols,
        inputs=inputs,
        objective_text=objective_text,
        chunk_text=chunk_text,
        answer_description_override=heuristic_answer_description,
        used_variable_names=used_variable_names,
        extra_definitions=extra_definitions,
    )
    detailed_explanation = format_numeric_feedback_explanation(
        stem=stem,
        explanation_text=explanation_text,
        numeric_metadata=numeric_metadata,
        objective_text=objective_text,
        chunk_text=chunk_text,
        objective_symbol_heuristics=objective_symbol_heuristics,
    )
    explanation_blocks = _extract_feedback_display_blocks(detailed_explanation)
    if len(explanation_blocks) >= 3:
        formula_tex = explanation_blocks[0]
        substitution_tex = explanation_blocks[1]
        if r"\approx" in explanation_blocks[-1]:
            final_tex = explanation_blocks[-1]
        parsed_definitions = _extract_feedback_definitions_text(detailed_explanation)
        if parsed_definitions and not normalize_objective_symbol_heuristics(objective_symbol_heuristics):
            definitions_text = parsed_definitions
    key_idea = _default_numeric_key_idea(stem, explanation_text, definitions_text)
    return {
        "answer_symbol": answer_symbol,
        "answer_unit": answer_unit,
        "answer_value": float(answer_value),
        "correct_answer_text": correct_answer_text or normalize_numeric_answer_text(
            float(answer_value),
            answer_unit,
            significant_figures,
            display_style=_numeric_display_style(float(answer_value)),
        ),
        "definitions_text": definitions_text,
        "expected_definitions": expectations,
        "formula_tex": formula_tex,
        "substitution_tex": substitution_tex,
        "final_tex": final_tex,
        "key_idea": key_idea,
        "working": [],
    }


def _normalize_numeric_feedback_option(raw_option: dict) -> dict:
    if not isinstance(raw_option, dict):
        raise NumericQuestionValidationError("Stored numeric feedback options must be objects.")
    answer_text = _normalize_text(str(raw_option.get("answer_text", "")))
    if not answer_text:
        raise NumericQuestionValidationError("Stored numeric feedback options must include answer_text.")
    is_correct = raw_option.get("is_correct")
    if not isinstance(is_correct, bool):
        raise NumericQuestionValidationError("Stored numeric feedback options must include a boolean is_correct.")
    note = _normalize_text(str(raw_option.get("note", "")))
    if not note:
        raise NumericQuestionValidationError("Stored numeric feedback options must include a note.")
    if len(note) > MAX_NUMERIC_FEEDBACK_NOTE_LENGTH:
        raise NumericQuestionValidationError("Stored numeric feedback notes must stay concise.")
    if NUMERIC_FEEDBACK_OPTION_LABEL_PATTERN.search(note):
        raise NumericQuestionValidationError("Stored numeric feedback notes must not reference answer letters.")
    return {
        "answer_text": answer_text,
        "is_correct": is_correct,
        "note": note,
    }


def _normalize_numeric_feedback_math_block(value: str, label: str) -> str:
    text = _normalize_text(str(value or ""))
    if text.startswith(r"\[") and text.endswith(r"\]"):
        text = text[2:-2].strip()
    if not text:
        raise NumericQuestionValidationError(f"Stored numeric feedback must include {label}.")
    if NUMERIC_FEEDBACK_OPTION_LABEL_PATTERN.search(text):
        raise NumericQuestionValidationError(f"Stored numeric feedback {label} must not reference answer letters.")
    return text


def _validate_numeric_feedback_payload(
    payload: dict,
    *,
    correct_answer_text: str,
    distractors: list[str],
) -> dict:
    if not isinstance(payload, dict):
        raise NumericQuestionValidationError("Stored numeric feedback must be a JSON object.")
    key_idea = _normalize_text(str(payload.get("key_idea", "")))
    if not key_idea:
        raise NumericQuestionValidationError("Stored numeric feedback must include key_idea.")
    if len(key_idea) > MAX_NUMERIC_FEEDBACK_KEY_IDEA_LENGTH:
        raise NumericQuestionValidationError("Stored numeric feedback key ideas must stay concise.")
    formula_tex = _normalize_numeric_feedback_math_block(payload.get("formula", ""), "formula")
    substitution_tex = _normalize_numeric_feedback_math_block(payload.get("substitution", ""), "substitution")
    final_tex = _normalize_numeric_feedback_math_block(payload.get("final_answer", ""), "final_answer")

    option_texts = [str(correct_answer_text or "").strip(), *[str(item or "").strip() for item in distractors]]
    allowed_option_map = {_normalize_text(option): option for option in option_texts if _normalize_text(option)}
    if len(allowed_option_map) != len(option_texts):
        raise NumericQuestionValidationError("Stored numeric feedback requires unique answer options.")

    raw_options = payload.get("options")
    if not isinstance(raw_options, list):
        raise NumericQuestionValidationError("Stored numeric feedback must include an options array.")

    validated_by_text: dict[str, dict] = {}
    for raw_option in raw_options:
        option = _normalize_numeric_feedback_option(raw_option)
        normalized_answer_text = _normalize_text(option["answer_text"])
        matched_option_text = allowed_option_map.get(normalized_answer_text)
        if matched_option_text is None:
            raise NumericQuestionValidationError("Stored numeric feedback introduced an unknown answer option.")
        if normalized_answer_text in validated_by_text:
            raise NumericQuestionValidationError("Stored numeric feedback must cover each answer option once.")
        option["answer_text"] = matched_option_text
        validated_by_text[normalized_answer_text] = option

    missing_option_texts = [option for option in option_texts if _normalize_text(option) not in validated_by_text]
    if missing_option_texts:
        raise NumericQuestionValidationError("Stored numeric feedback must cover every answer option exactly once.")

    correct_options = [option for option in validated_by_text.values() if option["is_correct"]]
    if len(correct_options) != 1:
        raise NumericQuestionValidationError("Stored numeric feedback must mark exactly one option as correct.")
    if correct_options[0]["answer_text"] != correct_answer_text:
        raise NumericQuestionValidationError("Stored numeric feedback marked the wrong option as correct.")

    return {
        "version": NUMERIC_FEEDBACK_VERSION,
        "key_idea": key_idea,
        "formula_tex": formula_tex,
        "substitution_tex": substitution_tex,
        "final_tex": final_tex,
        "options": [validated_by_text[_normalize_text(option)] for option in option_texts],
    }


def _stored_numeric_feedback_option(
    numeric_metadata: dict | None,
    *,
    selected_answer_text: str,
) -> tuple[dict, dict] | tuple[None, None]:
    if not isinstance(numeric_metadata, dict):
        return None, None
    raw_feedback = numeric_metadata.get("feedback_v2")
    if not isinstance(raw_feedback, dict):
        return None, None
    snapshot = numeric_metadata.get("output_snapshot") if isinstance(numeric_metadata.get("output_snapshot"), dict) else {}
    correct_answer_text = str(snapshot.get("correct_answer", "")).strip()
    distractors = [
        str(item).strip()
        for item in snapshot.get("distractors", [])
        if str(item).strip()
    ] if isinstance(snapshot.get("distractors"), list) else []
    if not correct_answer_text or not distractors:
        return None, None
    try:
        validated_feedback = _validate_numeric_feedback_payload(
            raw_feedback,
            correct_answer_text=correct_answer_text,
            distractors=distractors,
        )
    except NumericQuestionValidationError:
        return None, None

    normalized_selected_answer = _normalize_text(selected_answer_text)
    for option in validated_feedback["options"]:
        if _normalize_text(option["answer_text"]) == normalized_selected_answer:
            return validated_feedback, option
    return None, None


def _openai_numeric_feedback_payload(
    *,
    stem: str,
    explanation_text: str,
    correct_answer_text: str,
    distractors: list[str],
    numeric_metadata: dict,
) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    option_lines = []
    for option_text in [correct_answer_text, *distractors]:
        option_lines.append(f"- {option_text}")
    inputs = numeric_metadata.get("inputs") if isinstance(numeric_metadata.get("inputs"), dict) else {}
    validation = numeric_metadata.get("validation") if isinstance(numeric_metadata.get("validation"), dict) else {}
    given_lines = []
    for name, payload in inputs.items():
        value = payload.get("value")
        unit = _normalize_text(str(payload.get("unit", "")))
        given_line = f"- {name} = {value}"
        if unit:
            given_line += f" {unit}"
        given_lines.append(given_line)
    verified_expression = _normalize_text(str(validation.get("answer_expression", "")))
    prompt = f"""
Return learner-facing numeric MCQ feedback as strict JSON.

Rules:
- Cover every answer option exactly once using the exact answer_text provided below.
- Keep each note concise and specific to that option.
- Mark exactly one option as correct, and it must be the actual correct answer.
- Choose the conventional symbols that fit the question naturally.
- `formula`, `substitution`, and `final_answer` must be LaTeX-ready maths only, without surrounding \\[ \\].
- In `substitution`, show the numeric working clearly.
- Do not mention option letters, answer letters, or "the correct answer is option...".
- Base the working on the verified expression and givens below, but do not force the raw variable names into the final notation.

Question:
{stem}

Available numeric givens:
{chr(10).join(given_lines) or "- No givens provided."}

Verified calculation expression:
{verified_expression or "Not provided."}

Verified correct answer:
{correct_answer_text}

Answer options to cover exactly once:
{chr(10).join(option_lines)}

Teacher explanation seed:
{_normalize_text(explanation_text) or "No additional explanation provided."}
""".strip()
    try:
        response = client.responses.create(
            model=getattr(settings, "OPENAI_MODEL", "gpt-4.1"),
            instructions=(
                "Return one valid JSON object only. "
                "Cover each answer option exactly once and choose standard symbols that suit the question."
            ),
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "numeric_answer_feedback",
                    "strict": True,
                    "schema": NUMERIC_FEEDBACK_JSON_SCHEMA,
                }
            },
            temperature=0.2,
        )
    except OpenAIError as exc:
        raise NumericQuestionValidationError(f"OpenAI request for numeric feedback failed: {exc}") from exc
    return _parse_json_object(getattr(response, "output_text", ""))


def _build_stored_numeric_feedback(
    *,
    stem: str,
    explanation_text: str,
    numeric_metadata: dict,
    correct_answer_text: str,
    distractors: list[str],
    objective_text: str = "",
    chunk_text: str = "",
) -> dict:
    raw_payload = _openai_numeric_feedback_payload(
        stem=stem,
        explanation_text=explanation_text,
        correct_answer_text=correct_answer_text,
        distractors=distractors,
        numeric_metadata=numeric_metadata,
    )
    return _validate_numeric_feedback_payload(
        raw_payload,
        correct_answer_text=correct_answer_text,
        distractors=distractors,
    )


def _render_numeric_feedback_payload(context: dict, verdict: str, *, key_idea: str, option_note: str = "") -> str:
    formula_tex = str(context.get("formula_tex", "")).strip()
    substitution_tex = str(context.get("substitution_tex", "")).strip()
    final_tex = str(context.get("final_tex", "")).strip()
    lines: list[str] = [verdict.replace("_", " ").capitalize()]
    if verdict != "correct":
        message = option_note or key_idea
        if message:
            lines.append(message)
    lines.extend(
        [
            rf"\[{formula_tex}\]",
            rf"\[{substitution_tex}\]",
            rf"\[{final_tex}\]",
        ]
    )
    return "\n\n".join(lines)


def format_numeric_answer_feedback(
    *,
    stem: str,
    explanation_text: str,
    numeric_metadata: dict | None,
    selected_answer_text: str,
    is_correct: bool,
    objective_text: str = "",
    chunk_text: str = "",
    objective_symbol_heuristics: dict | None = None,
) -> str:
    context = _build_numeric_feedback_context(
        stem=stem,
        explanation_text=explanation_text,
        numeric_metadata=numeric_metadata or {},
        objective_text=objective_text,
        chunk_text=chunk_text,
        objective_symbol_heuristics=objective_symbol_heuristics,
    )
    if not context:
        explanation = format_numeric_feedback_explanation(
            stem=stem,
            explanation_text=explanation_text,
            numeric_metadata=numeric_metadata,
            objective_text=objective_text,
            chunk_text=chunk_text,
            objective_symbol_heuristics=objective_symbol_heuristics,
        ) or normalize_numeric_explanation_text(explanation_text)
        if is_correct:
            return f"Correct\n\n{explanation}" if explanation else "Correct"
        body = explanation or "Not quite."
        return f"Incorrect\n\n{body}"

    context["stem"] = stem
    context["selected_answer_text"] = selected_answer_text
    verdict = _deterministic_numeric_verdict(
        is_correct=is_correct,
        selected_answer_text=selected_answer_text,
        correct_answer_text=context["correct_answer_text"],
        answer_value=float(context["answer_value"]),
        answer_unit=str(context["answer_unit"]),
    )
    stored_feedback, selected_option = _stored_numeric_feedback_option(
        numeric_metadata or {},
        selected_answer_text=selected_answer_text,
    )
    if stored_feedback and selected_option:
        return _render_numeric_feedback_payload(
            {**context, "formula_tex": stored_feedback["formula_tex"], "substitution_tex": stored_feedback["substitution_tex"], "final_tex": stored_feedback["final_tex"]},
            verdict,
            key_idea=stored_feedback["key_idea"],
            option_note=selected_option["note"],
        )
    return _render_numeric_feedback_payload(
        context,
        verdict,
        key_idea=context["key_idea"],
    )


def _validate_unit_text(value, label: str) -> str:
    unit = _normalize_text(value)
    if unit.lower() == "dimensionless":
        return ""
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
    replacements = {
        name: _format_number_text(
            value,
            _infer_stem_value_significant_figures(value),
            display_style=_numeric_display_style(value),
        )
        for name, value in values.items()
    }
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


def _expression_to_tex(node, replacements: dict[str, str], constant_aliases: list[dict] | None = None) -> str:
    if isinstance(node, ast.Expression):
        return _expression_to_tex(node.body, replacements, constant_aliases=constant_aliases)
    if isinstance(node, ast.Constant):
        value = float(node.value)
        for alias in constant_aliases or []:
            approx_value = float(alias.get("approx_value"))
            if math.isclose(value, approx_value, rel_tol=1e-9, abs_tol=max(abs(approx_value) * 1e-12, 1e-18)):
                return str(alias.get("symbol", "") or format(value, ".10g"))
        return format(value, ".10g")
    if isinstance(node, ast.Name):
        if node.id in PRETTY_TEX_CONSTANTS:
            return PRETTY_TEX_CONSTANTS[node.id]
        return replacements.get(node.id, _tex_escape_name(node.id))
    if isinstance(node, ast.UnaryOp):
        sign = "+" if isinstance(node.op, ast.UAdd) else "-"
        return sign + _expression_to_tex(node.operand, replacements, constant_aliases=constant_aliases)
    if isinstance(node, ast.BinOp):
        left = _expression_to_tex(node.left, replacements, constant_aliases=constant_aliases)
        right = _expression_to_tex(node.right, replacements, constant_aliases=constant_aliases)
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
        argument = _expression_to_tex(node.args[0], replacements, constant_aliases=constant_aliases)
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


def _display_resolution(value: float, significant_figures: int) -> float:
    magnitude = abs(_ensure_finite_number(value, "Distractor display resolution"))
    if magnitude == 0:
        return 10 ** (1 - significant_figures)
    exponent = math.floor(math.log10(magnitude))
    return 10 ** (exponent - significant_figures + 1)


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
        increment = _display_resolution(scale, significant_figures)
        candidates = [
            2 * increment,
            -3 * increment,
            7 * increment,
            -11 * increment,
            17 * increment,
            -23 * increment,
        ]
    else:
        increment = _display_resolution(answer_value, significant_figures)
        candidates = [
            answer_value + (2 * increment),
            answer_value - (3 * increment),
            answer_value + (7 * increment),
            answer_value - (11 * increment),
            answer_value + (17 * increment),
            answer_value - (23 * increment),
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
    _validate_alpha_closest_approach_scale(
        stem=stem,
        objective_text=objective_text,
        chunk_text=chunk_text,
        answer_value=answer_value,
        answer_unit=answer_unit,
        values=values,
        units=units,
    )
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
- For alpha-particle closest-approach questions, use energy conservation with electrostatic potential energy so distance is proportional to charge product and inversely proportional to kinetic energy.
- Include only variables needed by calculation_expression unless surplus data is an intentional part of the question.
- The application computes the correct option and {distractor_count} bounded distractors locally; do not provide answer strings, distractor expressions, or Python code.
- Use SI units unless the learning objective requires another convention. Leave answer_unit blank when the quantity is dimensionless.
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
            model=getattr(settings, "OPENAI_NUMERIC_MODEL", settings.OPENAI_MODEL),
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
    raw_explanation_text = _normalize_text(candidate.get("explanation", ""))
    feedback_v2 = _build_stored_numeric_feedback(
        stem=validated_output["stem"],
        explanation_text=raw_explanation_text,
        numeric_metadata=metadata,
        correct_answer_text=validated_output["correct_answer"],
        distractors=validated_output["distractors"],
        objective_text=objective_text,
        chunk_text=chunk_text,
    )
    metadata["feedback_v2"] = feedback_v2
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
