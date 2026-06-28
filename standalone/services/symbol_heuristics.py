import json
import math
import re
from typing import Any

from django.conf import settings
from openai import OpenAI


_MAX_HINTS = 8
_GREEK_PLAIN_TO_TEX = {
    "λ": r"\lambda",
    "ρ": r"\rho",
    "δ": r"\delta",
    "θ": r"\theta",
    "φ": r"\phi",
}
_SUBSCRIPT_PLAIN_TO_TEX = {
    "A₀": r"A_0",
    "N₀": r"N_0",
    "t₁/₂": r"t_{1/2}",
}
_SYMBOL_TOKEN_PATTERN = re.compile(r"(?:[A-Za-z]|[λρδθφ])(?:[₀₁₂]|₁/₂)?")
_PHYSICS_HEURISTIC_PATTERNS = (
    {
        "id": "photon_energy",
        "keywords": ("photon", "planck", "frequency"),
        "heuristics": {
            "answer_symbol": "E",
            "answer_description": "the photon energy",
            "variable_hints": [
                {"match_terms": ["frequency"], "symbol": "f", "description": "the frequency"},
            ],
            "constant_hints": [
                {"symbol": "h", "description": "Planck's constant", "approx_value": 6.62607015e-34},
            ],
        },
    },
    {
        "id": "planck_constant_led",
        "keywords": ("planck", "wavelength", "voltage"),
        "heuristics": {
            "answer_symbol": "h",
            "answer_description": "Planck's constant",
            "variable_hints": [
                {"match_terms": ["wavelength"], "symbol": "λ", "description": "the wavelength"},
                {"match_terms": ["voltage"], "symbol": "V", "description": "the potential difference"},
                {"match_terms": ["speed", "light"], "symbol": "c", "description": "the speed of light"},
            ],
            "constant_hints": [
                {"symbol": "e", "description": "the elementary charge", "approx_value": 1.602176634e-19},
            ],
        },
    },
    {
        "id": "young_double_slit",
        "keywords": ("young", "double-slit", "wavelength"),
        "heuristics": {
            "answer_symbol": "λ",
            "answer_description": "the wavelength",
            "variable_hints": [
                {"match_terms": ["slit", "separation"], "symbol": "a", "description": "the slit separation"},
                {"match_terms": ["fringe", "spacing"], "symbol": "w", "description": "the fringe spacing"},
                {"match_terms": ["screen", "distance"], "symbol": "D", "description": "the screen distance"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "wave_speed",
        "keywords": ("wave", "wavelength", "frequency", "speed"),
        "heuristics": {
            "answer_symbol": "v",
            "answer_description": "the wave speed",
            "variable_hints": [
                {"match_terms": ["frequency"], "symbol": "f", "description": "the frequency"},
                {"match_terms": ["wavelength"], "symbol": "λ", "description": "the wavelength"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "suvat_displacement",
        "keywords": ("displacement", "initial velocity", "final velocity"),
        "heuristics": {
            "answer_symbol": "s",
            "answer_description": "the displacement",
            "variable_hints": [
                {"match_terms": ["initial", "velocity"], "symbol": "u", "description": "the initial velocity"},
                {"match_terms": ["final", "velocity"], "symbol": "v", "description": "the final velocity"},
                {"match_terms": ["time"], "symbol": "t", "description": "the elapsed time"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "suvat_acceleration",
        "keywords": ("acceleration", "initial velocity", "final velocity"),
        "heuristics": {
            "answer_symbol": "a",
            "answer_description": "the acceleration",
            "variable_hints": [
                {"match_terms": ["initial", "velocity"], "symbol": "u", "description": "the initial velocity"},
                {"match_terms": ["final", "velocity"], "symbol": "v", "description": "the final velocity"},
                {"match_terms": ["time"], "symbol": "t", "description": "the elapsed time"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "force_mass_acceleration",
        "keywords": ("force", "mass", "acceleration"),
        "heuristics": {
            "answer_symbol": "F",
            "answer_description": "the force",
            "variable_hints": [
                {"match_terms": ["mass"], "symbol": "m", "description": "the mass"},
                {"match_terms": ["acceleration"], "symbol": "a", "description": "the acceleration"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "ohms_law",
        "keywords": ("potential difference", "current", "resistance"),
        "heuristics": {
            "answer_symbol": "V",
            "answer_description": "the potential difference",
            "variable_hints": [
                {"match_terms": ["current"], "symbol": "I", "description": "the current"},
                {"match_terms": ["resistance"], "symbol": "R", "description": "the resistance"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "electrical_power",
        "keywords": ("power", "current", "potential difference"),
        "heuristics": {
            "answer_symbol": "P",
            "answer_description": "the power",
            "variable_hints": [
                {"match_terms": ["current"], "symbol": "I", "description": "the current"},
                {"match_terms": ["potential", "difference"], "symbol": "V", "description": "the potential difference"},
            ],
            "constant_hints": [],
        },
    },
    {
        "id": "coulomb_force",
        "keywords": ("coulomb", "electrostatic", "charge", "force"),
        "heuristics": {
            "answer_symbol": "F",
            "answer_description": "the electrostatic force",
            "variable_hints": [
                {"match_terms": ["charge"], "symbol": "q", "description": "a charge"},
                {"match_terms": ["distance"], "symbol": "r", "description": "the separation distance"},
            ],
            "constant_hints": [
                {"symbol": "k", "description": "Coulomb's constant", "approx_value": 8.9875517923e9},
            ],
        },
    },
)


def symbol_plain_to_tex(symbol: str) -> str:
    cleaned = str(symbol or "").strip()
    if not cleaned:
        return ""
    if cleaned in _GREEK_PLAIN_TO_TEX:
        return _GREEK_PLAIN_TO_TEX[cleaned]
    if cleaned in _SUBSCRIPT_PLAIN_TO_TEX:
        return _SUBSCRIPT_PLAIN_TO_TEX[cleaned]
    return cleaned


def normalize_objective_symbol_heuristics(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}

    answer_symbol_raw = str(payload.get("answer_symbol", "") or "").strip()
    answer_symbol_match = _SYMBOL_TOKEN_PATTERN.search(answer_symbol_raw)
    answer_symbol = answer_symbol_match.group(0) if answer_symbol_match else ""
    answer_description = str(payload.get("answer_description", "") or "").strip()
    source = str(payload.get("source", "") or "").strip()

    variable_hints = []
    for item in payload.get("variable_hints", []) if isinstance(payload.get("variable_hints"), list) else []:
        if not isinstance(item, dict):
            continue
        terms = [
            str(term).strip().lower()
            for term in item.get("match_terms", [])
            if str(term).strip()
        ]
        symbol = str(item.get("symbol", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        if not terms or not symbol:
            continue
        variable_hints.append(
            {
                "match_terms": terms[:4],
                "symbol": symbol,
                "description": description,
            }
        )

    constant_hints = []
    for item in payload.get("constant_hints", []) if isinstance(payload.get("constant_hints"), list) else []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "") or "").strip()
        description = str(item.get("description", "") or "").strip()
        approx_value = item.get("approx_value")
        if isinstance(approx_value, bool) or not isinstance(approx_value, (int, float)) or not math.isfinite(float(approx_value)):
            continue
        if not symbol:
            continue
        constant_hints.append(
            {
                "symbol": symbol,
                "description": description,
                "approx_value": float(approx_value),
            }
        )

    return {
        "answer_symbol": answer_symbol,
        "answer_description": answer_description,
        "variable_hints": variable_hints[:_MAX_HINTS],
        "constant_hints": constant_hints[:_MAX_HINTS],
        "source": source,
    }


def _heuristic_keywords_score(pattern: dict, context: str) -> int:
    return sum(1 for keyword in pattern["keywords"] if keyword in context)


def _deterministic_symbol_heuristics(objective_text: str, context_text: str) -> dict:
    context = " ".join(part for part in (objective_text, context_text) if part).lower()
    best_pattern = None
    best_score = 0
    for pattern in _PHYSICS_HEURISTIC_PATTERNS:
        score = _heuristic_keywords_score(pattern, context)
        if score > best_score:
            best_pattern = pattern
            best_score = score
    if best_pattern is None or best_score < 2:
        return {}
    heuristics = dict(best_pattern["heuristics"])
    heuristics["source"] = "deterministic"
    return normalize_objective_symbol_heuristics(heuristics)


def deterministic_symbol_heuristics_for_objective(objective_text: str, context_text: str) -> dict:
    return _deterministic_symbol_heuristics(objective_text, context_text)


_OPENAI_HEURISTICS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "objectives": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "objective_text": {"type": "string"},
                    "answer_symbol": {"type": "string"},
                    "answer_description": {"type": "string"},
                    "variable_hints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "match_terms": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 4,
                                },
                                "symbol": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["match_terms", "symbol", "description"],
                        },
                    },
                    "constant_hints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "symbol": {"type": "string"},
                                "description": {"type": "string"},
                                "approx_value": {"type": "number"},
                            },
                            "required": ["symbol", "description", "approx_value"],
                        },
                    },
                },
                "required": [
                    "objective_text",
                    "answer_symbol",
                    "answer_description",
                    "variable_hints",
                    "constant_hints",
                ],
            },
        }
    },
    "required": ["objectives"],
}


def _openai_symbol_heuristics(objective_texts: list[str], context_text: str) -> dict[str, dict]:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    prompt = f"""
Return strict JSON describing conventional subject symbols for each learning objective.

Rules:
- Prefer discipline-standard symbols used in the provided teaching material.
- Focus on symbol accuracy for numerical worked feedback.
- Use Greek letters when conventional, for example λ for wavelength.
- Include physical constants only when they are likely to appear in formulas.
- Keep match_terms short and practical, based on likely generated variable names.
- If no strong symbol conventions are evident for an objective, return empty strings and empty arrays for that objective.

Learning objectives:
{json.dumps(objective_texts, ensure_ascii=False)}

Teaching material context:
{context_text[:12000]}
""".strip()
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        instructions="Return one valid JSON object only.",
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        text={
            "format": {
                "type": "json_schema",
                "name": "objective_symbol_heuristics",
                "strict": True,
                "schema": _OPENAI_HEURISTICS_SCHEMA,
            }
        },
    )
    raw = json.loads(getattr(response, "output_text", "") or "{}")
    heuristics_by_objective: dict[str, dict] = {}
    for item in raw.get("objectives", []):
        if not isinstance(item, dict):
            continue
        objective_text = str(item.get("objective_text", "") or "").strip()
        if not objective_text:
            continue
        heuristics = normalize_objective_symbol_heuristics(
            {
                "answer_symbol": item.get("answer_symbol", ""),
                "answer_description": item.get("answer_description", ""),
                "variable_hints": item.get("variable_hints", []),
                "constant_hints": item.get("constant_hints", []),
                "source": "openai",
            }
        )
        heuristics_by_objective[objective_text] = heuristics
    return heuristics_by_objective


def derive_symbol_heuristics_for_objectives(objective_texts: list[str], context_text: str) -> list[dict]:
    normalized_objectives = [str(text or "").strip() for text in objective_texts]
    openai_heuristics: dict[str, dict] = {}
    if settings.OPENAI_API_KEY and normalized_objectives:
        try:
            openai_heuristics = _openai_symbol_heuristics(normalized_objectives, context_text)
        except Exception:  # noqa: BLE001
            openai_heuristics = {}

    derived: list[dict] = []
    for objective_text in normalized_objectives:
        heuristics = openai_heuristics.get(objective_text) or _deterministic_symbol_heuristics(objective_text, context_text)
        derived.append(normalize_objective_symbol_heuristics(heuristics))
    return derived
