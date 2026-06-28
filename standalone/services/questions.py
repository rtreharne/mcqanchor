import hashlib
import json
import re
from collections import defaultdict

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from openai import OpenAI, OpenAIError

from standalone.models import ContentChunk, Course, CourseBlock, LearningObjective, QuestionBankItem
from standalone.services.guidance import build_generation_guidance_prompt
from standalone.services.numeric_questions import (
    NumericQuestionRequestError,
    NumericQuestionValidationError,
    build_numeric_question_payload,
    objective_has_numeric_intent,
    supports_local_numeric_mcq,
)


class QuestionGenerationError(ValueError):
    pass


class QuestionGenerationUnavailableError(QuestionGenerationError):
    pass


OBJECTIVE_MATCH_STOPWORDS = {
    "about",
    "across",
    "between",
    "block",
    "compare",
    "course",
    "describe",
    "discuss",
    "explain",
    "general",
    "identify",
    "ideas",
    "into",
    "into",
    "key",
    "overview",
    "topic",
    "topics",
    "using",
    "understand",
    "understanding",
    "week",
    "with",
    "from",
    "that",
    "this",
    "their",
    "there",
    "which",
}

QUESTION_TYPE_GENERATION_PRIORITY = {
    QuestionBankItem.QuestionType.NUM: 0,
    QuestionBankItem.QuestionType.MAQ: 1,
    QuestionBankItem.QuestionType.WAQ: 2,
}

WAQ_FALLBACK_STEM_TEMPLATES = (
    "How would you explain {topic}?",
    "Why does {topic} matter here?",
    "What is the role of {topic}?",
    "What does {topic} help to explain?",
)

FURTHER_STUDY_QUESTION_COUNT = 3
MAX_STANDARD_GENERATION_ATTEMPTS = 8
LATEX_GREEK_REPLACEMENTS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "theta": "θ",
    "lambda": "λ",
    "mu": "μ",
    "pi": "π",
    "sigma": "σ",
    "phi": "φ",
    "omega": "ω",
}
STUDY_FOCUS_ACTION_VERBS = (
    "explain",
    "describe",
    "identify",
    "discuss",
    "outline",
    "summarise",
    "summarize",
    "compare",
    "show",
    "state",
    "interpret",
    "analyse",
    "analyze",
    "evaluate",
    "explore",
    "define",
    "apply",
    "relate",
    "connect",
    "infer",
)


def _trace_generation(event: str, **context) -> None:
    payload = {"event": event, **context}
    print(f"[question-generation] {json.dumps(payload, default=str, ensure_ascii=False)}", flush=True)

CODING_LANGUAGES = {
    "python",
    "r",
    "java",
    "matlab",
    "javascript",
    "typescript",
    "sql",
    "shell",
    "c",
    "cpp",
    "csharp",
    "html",
    "css",
}

CODING_LANGUAGE_LABELS = {
    "python": "Python",
    "r": "R",
    "java": "Java",
    "matlab": "MATLAB",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "sql": "SQL",
    "shell": "Shell",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "html": "HTML",
    "css": "CSS",
}

LANGUAGE_ALIASES = {
    "py": "python",
    "python3": "python",
    "rscript": "r",
    "m": "matlab",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "bash": "shell",
    "sh": "shell",
    "zsh": "shell",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "cs": "csharp",
}

EXTENSION_LANGUAGE_HINTS = {
    ".py": "python",
    ".r": "r",
    ".java": "java",
    ".m": "matlab",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".html": "html",
    ".css": "css",
}

LANGUAGES_WITH_FUNCTION_FRIENDLY_EXAMPLES = {
    "python",
    "r",
    "java",
    "matlab",
    "javascript",
    "typescript",
    "shell",
    "c",
    "cpp",
    "csharp",
}

CODING_TRIVIAL_STEM_PATTERNS = (
    r"\bwhat is the value of\b",
    r"\bwhat value (?:is|will be|would be|gets?)\b",
    r"\bwhat does [a-z_][a-z0-9_]* equal\b",
    r"\bwhat is stored in [a-z_][a-z0-9_]*\b",
    r"\bafter (?:the )?code (?:runs|executes), what is\b",
    r"\bafter running (?:the )?code, what is\b",
    r"\bmanually calculate\b",
)

CODING_INTERPRETIVE_TERMS = (
    "behav",
    "explain",
    "issue",
    "bug",
    "fix",
    "control flow",
    "state",
    "scope",
    "structure",
    "return",
    "output",
    "side effect",
    "mutat",
    "identify",
    "reason",
    "compare",
    "type",
    "subsetting",
    "indexing",
)

CODING_LANGUAGE_REFERENCE_PATTERNS = {
    "python": (r"\bpython(?:3)?\b",),
    "r": (r"(?<![A-Za-z0-9_])R(?![A-Za-z0-9_])", r"\brscript\b"),
    "java": (r"\bjava\b",),
    "matlab": (r"\bmatlab\b",),
    "javascript": (r"\bjavascript\b", r"\bnode(?:\.js)?\b"),
    "typescript": (r"\btypescript\b",),
    "sql": (r"\bsql\b",),
    "shell": (r"\bshell\b", r"\bbash\b", r"\bzsh\b"),
    "c": (r"(?<![A-Za-z0-9_])C(?![A-Za-z0-9_+#])",),
    "cpp": (r"\bc\+\+\b", r"\bcpp\b"),
    "csharp": (r"\bc#\b", r"\bcsharp\b", r"\bc sharp\b"),
    "html": (r"\bhtml\b",),
    "css": (r"\bcss\b",),
}

LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})")


def _decode_literal_unicode_escapes(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        codepoint = match.group(1) or match.group(2)
        try:
            return chr(int(codepoint, 16))
        except (TypeError, ValueError):
            return match.group(0)

    return LITERAL_UNICODE_ESCAPE_RE.sub(replace, str(text or ""))


def _keyword_set(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) >= 4 and token not in OBJECTIVE_MATCH_STOPWORDS
    }


def _normalize_coding_language(language: str) -> str:
    cleaned = re.sub(r"[^a-z0-9+#]+", "", str(language or "").strip().lower())
    cleaned = LANGUAGE_ALIASES.get(cleaned, cleaned)
    return cleaned if cleaned in CODING_LANGUAGES else ""


def _language_from_extension(extension: str) -> str:
    return EXTENSION_LANGUAGE_HINTS.get(str(extension or "").strip().lower(), "")


def _language_from_text(text: str) -> str:
    lowered = str(text or "").lower()
    fence_match = re.search(r"```([a-zA-Z0-9_+#.-]+)", text or "")
    if fence_match:
        fenced_language = _normalize_coding_language(fence_match.group(1))
        if fenced_language:
            return fenced_language

    language_markers = [
        ("python", (r"\bdef\s+\w+\s*\(", r"\bimport\s+\w+", r"\bfrom\s+\w+\s+import\b", r"\bprint\s*\(")),
        ("r", (r"\blibrary\s*\(", r"\bdata\.frame\s*\(", r"<-", r"\bggplot\s*\(", r"\b(?:read|write)\.csv\s*\(", r"\b(?:table|aggregate|mean|median|sd|plot|points|legend|expression|do\.call|print|source|runif|set\.seed|list\.files|unlink|readLines|startsWith|paste|paste0|readPNG|channel|bwlabel)\s*\(", r"\b[A-Za-z_]\w*\$[A-Za-z_]\w+")),
        ("java", (r"\bpublic\s+class\b", r"\bSystem\.out\.println\s*\(", r"\bpublic\s+static\s+void\s+main\b")),
        ("matlab", (r"^\s*end\s*$", r"^\s*function\s+\[?", r"\bdisp\s*\(", r"^\s*%")),
        ("javascript", (r"\bconsole\.log\s*\(", r"\bfunction\s+\w+\s*\(", r"\bconst\s+\w+\s*=", r"=>\s*[{(]")),
        ("typescript", (r"\binterface\s+\w+\b", r":\s*(?:string|number|boolean)\b", r"\btype\s+\w+\s*=")),
        ("sql", (r"\bselect\s+.+\bfrom\b", r"\bjoin\b", r"\bwhere\b", r"\bgroup\s+by\b")),
        ("shell", (r"^\s*#!.*\b(?:bash|sh)\b", r"\b(?:grep|awk|sed|chmod|cd|ls)\b")),
        ("cpp", (r"#include\s*<", r"\bstd::", r"\bcout\s*<<")),
        ("csharp", (r"\busing\s+System\b", r"\bConsole\.WriteLine\s*\(", r"\bnamespace\s+\w+")),
        ("c", (r"#include\s*<", r"\bprintf\s*\(", r"\bmalloc\s*\(")),
        ("html", (r"<(?:html|div|span|p|script|body|head)\b",)),
        ("css", (r"[.#]?[a-zA-Z][\w-]*\s*\{[^}]*:", r"\bdisplay\s*:", r"\bcolor\s*:")),
    ]
    scores = []
    for language, patterns in language_markers:
        score = sum(1 for pattern in patterns if re.search(pattern, lowered, flags=re.IGNORECASE | re.MULTILINE))
        if score:
            scores.append((score, language))
    if not scores:
        return ""
    scores.sort(key=lambda item: (-item[0], item[1]))
    return scores[0][1]


def _coding_language_label(language: str) -> str:
    normalized = _normalize_coding_language(language)
    return CODING_LANGUAGE_LABELS.get(normalized, normalized.upper() if normalized else "code")


def _coding_line_count(snippet: str) -> int:
    return len([line for line in str(snippet or "").splitlines() if line.strip()])


def _snippet_has_function(language: str, snippet: str) -> bool:
    normalized = _normalize_coding_language(language)
    text = str(snippet or "")
    patterns = {
        "python": r"^\s*def\s+\w+\s*\(",
        "r": r"(?:^\s*\w+\s*<-\s*function\s*\(|^\s*function\s*\()",
        "java": r"\b(?:public|private|protected)?\s*(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\(",
        "matlab": r"^\s*function\b",
        "javascript": r"\bfunction\s+\w+\s*\(|\bconst\s+\w+\s*=\s*\([^)]*\)\s*=>",
        "typescript": r"\bfunction\s+\w+\s*\(|\bconst\s+\w+\s*:\s*[^=]+=\s*\([^)]*\)\s*=>",
        "shell": r"^\s*(?:function\s+)?\w+\s*\(\)\s*\{",
        "c": r"\b(?:void|int|double|float|char)\s+\w+\s*\(",
        "cpp": r"\b(?:void|int|double|float|char|std::\w+)\s+\w+\s*\(",
        "csharp": r"\b(?:public|private|protected)?\s*(?:static\s+)?[\w<>\[\]]+\s+\w+\s*\(",
    }
    pattern = patterns.get(normalized)
    return bool(pattern and re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE))


def _coding_focus_phrase(language: str, snippet: str) -> str:
    lowered = str(snippet or "").lower()
    label = _coding_language_label(language)
    if language == "r" and ("tibble" in lowered or "data.frame" in lowered):
        return "how the code creates, subsets, or prints tabular data"
    if language == "sql":
        return "how the query combines filtering, grouping, and selected columns"
    if language in {"html", "css"}:
        return "how the structure and rules shown in the example interact"
    if _snippet_has_function(language, snippet):
        return f"how the {label} function logic and call site work together"
    if re.search(r"\b(for|while|repeat)\b", lowered):
        return "how state changes across the loop"
    if re.search(r"\b(if|else|elif|switch|case)\b", lowered):
        return "how the branch logic changes the result"
    if re.search(r"\[[^\]]+\]|\[\s*,|\[\s*\"|\$|\[\[", snippet):
        return f"how {label} indexing or subsetting affects the result"
    return "how the code structure determines the final behaviour"


def _fallback_coding_answer_focus(language: str, snippet: str, objective_text: str = "") -> list[str]:
    label = _coding_language_label(language)
    lowered = str(snippet or "").lower()
    objective_focus = re.sub(
        r"^(?:explain|describe|identify|discuss|outline|summaris(?:e|z)e|analyse|analyze)\s+",
        "",
        str(objective_text or "").strip(),
        flags=re.IGNORECASE,
    ).rstrip(".")
    answers: list[str] = []
    if _snippet_has_function(language, snippet):
        answers.append("The example defines helper logic and then uses the function's return value later in the code.")
        answers.append("To interpret the result correctly, you need to follow the data from the call site back through the function body.")
    if language == "r" and ("tibble" in lowered or "data.frame" in lowered):
        answers.append("The key idea is how the chosen tabular structure changes access, printing, or subsetting behaviour.")
    if re.search(r"\b(for|while|repeat)\b", lowered):
        answers.append("The final behaviour depends on how values are updated across iterations, not on a single arithmetic step.")
    if re.search(r"\b(if|else|elif|switch|case)\b", lowered):
        answers.append("The result depends on which branch is taken and what that branch returns or changes.")
    if re.search(r"\[[^\]]+\]|\[\s*,|\[\s*\"|\$|\[\[", snippet):
        answers.append(f"Understanding {label} indexing or subsetting rules is essential to explaining the result.")
    if objective_focus:
        answers.append(f"The key concept being tested is {objective_focus}.")
    answers.append("The important reasoning is about code structure, flow of data, and API semantics rather than hand-calculating a single variable.")

    normalized: list[str] = []
    for answer in answers:
        cleaned = re.sub(r"\s+", " ", str(answer or "")).strip()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _fallback_coding_distractors(language: str, snippet: str) -> list[str]:
    label = _coding_language_label(language)
    distractors = [
        "The example can only be interpreted by guessing hidden context that is not present in the snippet.",
        "The later lines do not depend on the earlier logic, so code structure is irrelevant here.",
        "The key behaviour comes from an external file or network service rather than the code shown.",
        f"The example works the same way no matter how {label} handles functions, indexing, or returned values.",
        "Reading the code line by line is enough; there is no need to reason about control flow or data movement.",
    ]
    if language == "r" and ("tibble" in str(snippet or "").lower() or "data.frame" in str(snippet or "").lower()):
        distractors.append("The choice of tabular structure does not affect how columns are returned or displayed.")
    return distractors


def _mentioned_coding_languages(text: str) -> set[str]:
    mentioned: set[str] = set()
    for language, patterns in CODING_LANGUAGE_REFERENCE_PATTERNS.items():
        flags = re.MULTILINE if language == "r" else re.IGNORECASE | re.MULTILINE
        for pattern in patterns:
            if re.search(pattern, str(text or ""), flags=flags):
                mentioned.add(language)
                break
    return mentioned


def _payload_mentions_unexpected_coding_language(payload: dict, expected_language: str) -> bool:
    normalized_expected = _normalize_coding_language(expected_language)
    if not normalized_expected:
        return False
    texts: list[str] = [
        str(payload.get("stem", "")).strip(),
        str(payload.get("explanation", "")).strip(),
        str(payload.get("correct_answer", "")).strip(),
    ]
    texts.extend(str(item).strip() for item in payload.get("correct_answers", []) if str(item).strip())
    texts.extend(str(item).strip() for item in payload.get("distractors", []) if str(item).strip())
    texts.extend(str(item).strip() for item in payload.get("further_study_questions", []) if str(item).strip())
    mentioned = _mentioned_coding_languages("\n".join(texts))
    return any(language != normalized_expected for language in mentioned)


def _is_low_value_coding_stem(stem: str, code_snippet: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(stem or "").strip().lower())
    if not lowered:
        return True
    if any(re.search(pattern, lowered) for pattern in CODING_TRIVIAL_STEM_PATTERNS):
        return True
    if _coding_line_count(code_snippet) <= 4 and re.search(r"\b(?:print|output|return|returned|returns?)\b", lowered):
        interpretive = any(term in lowered for term in CODING_INTERPRETIVE_TERMS)
        if not interpretive:
            return True
    return False


def coding_question_matches_expected_language(question: QuestionBankItem, expected_language: str = "") -> bool:
    if not getattr(question, "is_coding_question", False):
        return True
    normalized_expected = _normalize_coding_language(expected_language or getattr(question, "coding_language", ""))
    question_language = _normalize_coding_language(getattr(question, "coding_language", ""))
    if normalized_expected and question_language and question_language != normalized_expected:
        return False
    combined = "\n".join(
        [
            str(getattr(question, "stem", "")).strip(),
            str(getattr(question, "explanation", "")).strip(),
            str(getattr(question, "correct_answer", "")).strip(),
            *[str(answer).strip() for answer in getattr(question, "additional_correct_answers", [])],
            *[str(option).strip() for option in getattr(question, "distractors", [])],
            *[str(prompt).strip() for prompt in getattr(question, "further_study_questions", [])],
        ]
    )
    mentioned = _mentioned_coding_languages(combined)
    return not any(language != normalized_expected for language in mentioned if normalized_expected)


def coding_question_quality_sort_key(question: QuestionBankItem) -> tuple[int, int, int]:
    if not getattr(question, "is_coding_question", False):
        return (0, 0, 0)
    language = _normalize_coding_language(getattr(question, "coding_language", ""))
    code_snippet = str(getattr(question, "code_snippet", "") or "")
    line_count = _coding_line_count(code_snippet)
    return (
        1 if _is_low_value_coding_stem(getattr(question, "stem", ""), code_snippet) else 0,
        1 if language in LANGUAGES_WITH_FUNCTION_FRIENDLY_EXAMPLES and line_count < 5 and not _snippet_has_function(language, code_snippet) else 0,
        -min(line_count, 24),
    )


def _extract_fenced_code(text: str) -> tuple[str, str]:
    for match in re.finditer(r"```([a-zA-Z0-9_+#.-]+)?\n?([\s\S]*?)```", text or ""):
        snippet = match.group(2).strip()
        if snippet:
            return snippet, _normalize_coding_language(match.group(1) or "")
    return "", ""


def _is_code_like_line(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped.strip():
        return False
    patterns = (
        r"^\s*(?:def|class|for|while|if|elif|else|try|except|return|import|from)\b",
        r"^\s*(?:public|private|protected|static|void|int|double|String|class)\b",
        r"^\s*(?:function|const|let|var)\b",
        r"^\s*(?:SELECT|select|INSERT|insert|UPDATE|update|DELETE|delete)\b",
        r"^\s*[A-Za-z_][\w.]*\s*(?:<-|=)\s*.+",
        r"^\s*[#%].+",
        r"^\s*\w+\s*\([^)]*\)\s*$",
        r"\b(?:library|read\.csv|write\.csv|print|plot|points|legend|aggregate|do\.call|ggplot|disp|printf|console\.log|System\.out\.println)\s*\(",
        r"\b[A-Za-z_]\w*\$[A-Za-z_]\w+",
        r"\[[^\]]+\]",
        r"^\s*[)}]\s*$",
        r"\b(?:print|console\.log|System\.out\.println|disp|printf)\s*\(",
        r"<[A-Za-z][^>]*>",
    )
    return any(re.search(pattern, stripped) for pattern in patterns)


def _extract_code_like_lines(text: str, *, max_lines: int = 18) -> str:
    best: list[str] = []
    current: list[str] = []
    for line in str(text or "").splitlines():
        if _is_code_like_line(line):
            current.append(line.rstrip())
            if len(current) >= max_lines:
                break
            continue
        if current:
            if len(current) > len(best):
                best = current
            current = []
    if current and len(current) > len(best):
        best = current
    return "\n".join(best).strip()


def _snippet_has_strong_language_signal(language: str, snippet: str) -> bool:
    normalized = _normalize_coding_language(language)
    text = str(snippet or "")
    patterns = {
        "python": (r"^\s*def\s+\w+\s*\(", r"\bimport\s+\w+", r"\bfrom\s+\w+\s+import\b", r"\bprint\s*\(", r"^\s*(?:for|if|while)\b.*:"),
        "r": (r"<-", r"\blibrary\s*\(", r"\bdata\.frame\s*\(", r"\b(?:read|write)\.csv\s*\(", r"\bggplot\s*\(", r"\bfunction\s*\(", r"\b(?:plot|points|legend|aggregate|do\.call|table|mean|median|sd|print)\s*\(", r"\b[A-Za-z_]\w*\$[A-Za-z_]\w+"),
        "matlab": (r"^\s*function\b", r"^\s*end\s*$", r"\bdisp\s*\(", r"^\s*%"),
        "javascript": (r"\bfunction\s+\w+\s*\(", r"\b(?:const|let|var)\s+\w+\s*=", r"=>\s*[{(]"),
        "typescript": (r"\binterface\s+\w+\b", r"\btype\s+\w+\s*=", r"\b(?:const|let)\s+\w+\s*:\s*[^=]+="),
        "sql": (r"\bselect\s+.+\bfrom\b", r"\bjoin\b", r"\bgroup\s+by\b", r"\border\s+by\b"),
        "shell": (r"^\s*#!.*\b(?:bash|sh)\b", r"^\s*(?:grep|awk|sed|chmod|cd|ls)\b"),
        "java": (r"\bpublic\s+class\b", r"\bSystem\.out\.println\s*\(", r"\bpublic\s+static\s+void\s+main\b"),
        "cpp": (r"#include\s*<", r"\bstd::", r"\bcout\s*<<"),
        "csharp": (r"\busing\s+System\b", r"\bConsole\.WriteLine\s*\(", r"\bnamespace\s+\w+"),
        "c": (r"#include\s*<", r"\bprintf\s*\(", r"\bmalloc\s*\("),
        "html": (r"<(?:html|div|span|p|script|body|head)\b",),
        "css": (r"[.#]?[a-zA-Z][\w-]*\s*\{[^}]*:", r"\bdisplay\s*:", r"\bcolor\s*:"),
    }
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) for pattern in patterns.get(normalized, ()))


def coding_signal_for_text(text: str, *, extension: str = "", filename: str = "") -> dict[str, str]:
    fenced_snippet, fenced_language = _extract_fenced_code(text)
    filename_language = _language_from_extension(f".{str(filename).rsplit('.', 1)[-1].lower()}") if "." in str(filename) else ""
    snippet = fenced_snippet or _extract_code_like_lines(text)
    snippet = re.sub(r"^\n+|\n+$", "", snippet)
    if len(snippet) > 1400:
        snippet = snippet[:1400].rsplit("\n", 1)[0].strip() or snippet[:1400].strip()
    language = _language_from_extension(extension) or filename_language
    language = language or fenced_language or _language_from_text(snippet)
    if not language or not snippet or len(snippet) < 8 or not _snippet_has_strong_language_signal(language, snippet):
        return {"language": "", "snippet": ""}
    return {"language": language, "snippet": snippet}


def block_has_coding_signal(
    block: CourseBlock,
    candidate_chunks: list[ContentChunk] | None = None,
    coding_signals: dict[int, dict[str, str]] | None = None,
) -> bool:
    if candidate_chunks is None:
        candidate_chunks = list(
            ContentChunk.objects.filter(block=block, asset__include_in_generation=True)
            .select_related("asset")
            .order_by("asset__created_at", "ordinal", "pk")
        )
    if coding_signals is None:
        coding_signals = {chunk.pk: signal for chunk, signal in _coding_chunks(candidate_chunks)}
    return any(signal.get("language") and signal.get("snippet") for signal in coding_signals.values())


def _coding_signal_for_chunk(chunk: ContentChunk) -> dict[str, str]:
    asset = getattr(chunk, "asset", None)
    return coding_signal_for_text(
        chunk.text,
        extension=getattr(asset, "extension", ""),
        filename=getattr(asset, "original_filename", ""),
    )


def normalize_explanation_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", _decode_literal_unicode_escapes(text).strip())
    if not cleaned:
        return ""

    replacements = [
        (r"\bapproved course materials\b", "the relevant concepts"),
        (r"\bapproved course material\b", "the relevant concepts"),
        (r"\bcourse materials\b", "the relevant concepts"),
        (r"\bcourse material\b", "the relevant concepts"),
        (r"\bapproved materials\b", "the relevant concepts"),
        (r"\bapproved material\b", "the relevant concepts"),
        (r"\bthe materials\b", "the relevant concepts"),
        (r"\bthe material\b", "the relevant concepts"),
        (r"\bthe presented content\b", "the relevant concepts"),
        (r"\bthe provided content\b", "the relevant concepts"),
        (r"\bthe content\b", "the relevant concepts"),
        (r"\bthe text\b", "the relevant concepts"),
        (r"\bthis item is based directly on this block for the block\b", "The correct answer reflects the key relationship being tested"),
        (r"\bthis follows directly from this block\b", "The correct answer reflects the key relationship being tested"),
    ]
    for pattern, replacement in replacements:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\bfrom this block for the block\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:in|from|according to|based on)\s+(?:the\s+)?(?:source|passage|textbook|book|chapter|notes|content|material|block)\b[:,]?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _replace_numeric_formula_clause(match: re.Match[str]) -> str:
    verb = str(match.group("verb") or "").lower()
    where_clause = re.sub(r"\s+", " ", str(match.group("where") or "").strip(" ,"))
    replacement = "are described by the key relationship" if verb == "are" else "is described by the key relationship"
    if where_clause:
        replacement += f", where {where_clause}"
    return replacement


def _normalize_numeric_explanation_prose(text: str) -> str:
    cleaned = _decode_literal_unicode_escapes(text).strip()
    if not cleaned:
        return ""

    for name, symbol in LATEX_GREEK_REPLACEMENTS.items():
        cleaned = re.sub(rf"\\{name}(?=[A-Za-z])", rf"\\{name} ", cleaned)
        cleaned = re.sub(rf"\\{name}\b", symbol, cleaned)

    cleaned = re.sub(
        (
            r"\b(?P<verb>is|are)\s+given(?:\s+by)?\s+(?:the\s+)?"
            r"(?P<label>formula|equation|relationship|law)\s+"
            r"(?P<formula>[^.?!]*?)"
            r"(?:,\s*where\s+(?P<where>[^.?!]+))?"
            r"(?=[.?!]|$)"
        ),
        _replace_numeric_formula_clause,
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:the\s+)?(?:formula|equation|relationship|law)\s+[A-Za-z][^=]{0,20}=\s*[^.?!]+(?=[.?!]|$)",
        "the key relationship",
        cleaned,
        flags=re.IGNORECASE,
    )
    return normalize_explanation_text(cleaned)


def normalize_numeric_explanation_text(text: str) -> str:
    source = _decode_literal_unicode_escapes(str(text or "").strip())
    if not source:
        return ""

    formula_marker = "\n\nFormula:\n"
    worked_solution_marker = "\n\nWorked solution:\n"
    if formula_marker not in source:
        return _normalize_numeric_explanation_prose(source)

    prose_text, remainder = source.split(formula_marker, 1)
    formula_text = remainder
    worked_solution_text = ""
    if worked_solution_marker in remainder:
        formula_text, worked_solution_text = remainder.split(worked_solution_marker, 1)

    normalized_parts = []
    normalized_prose = _normalize_numeric_explanation_prose(prose_text)
    if normalized_prose:
        normalized_parts.append(normalized_prose)

    normalized_parts.append(f"Formula:\n{formula_text.strip()}")
    if worked_solution_text.strip():
        normalized_parts.append(f"Worked solution:\n{worked_solution_text.strip()}")
    return "\n\n".join(normalized_parts)


def _normalize_answer_list(items) -> list[str]:
    normalized = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        cleaned = _decode_literal_unicode_escapes(str(item).strip())
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _option_length_profile(text: str) -> tuple[int, int]:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    return len(cleaned), len(re.findall(r"[A-Za-z0-9]+", cleaned))


def _option_specificity_profile(text: str) -> dict[str, int]:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip().lower())
    words = re.findall(r"[a-z0-9']+", cleaned)
    clause_markers = (
        "because",
        "therefore",
        "which",
        "while",
        "whereas",
        "although",
        "through",
        "across",
        "within",
        "rather than",
        "depends on",
        "results in",
        "results from",
        "leads to",
        "allows",
        "enables",
        "by",
    )
    content_words = [
        word for word in words
        if len(word) >= 5 and word not in OBJECTIVE_MATCH_STOPWORDS
    ]
    clause_score = sum(1 for marker in clause_markers if marker in cleaned)
    punctuation_score = len(re.findall(r"[,;:()]", cleaned))
    return {
        "word_count": len(words),
        "content_word_count": len(content_words),
        "clause_score": clause_score,
        "punctuation_score": punctuation_score,
    }


def _single_answer_length_signal_error(correct_answer: str, distractors: list[str]) -> str:
    if not correct_answer or not distractors:
        return ""
    correct_chars, correct_words = _option_length_profile(correct_answer)
    distractor_profiles = [_option_length_profile(distractor) for distractor in distractors if str(distractor).strip()]
    if not distractor_profiles:
        return ""

    distractor_char_lengths = [profile[0] for profile in distractor_profiles]
    distractor_word_lengths = [profile[1] for profile in distractor_profiles]
    longest_distractor_chars = max(distractor_char_lengths)
    longest_distractor_words = max(distractor_word_lengths)
    average_distractor_chars = sum(distractor_char_lengths) / len(distractor_char_lengths)
    average_distractor_words = sum(distractor_word_lengths) / len(distractor_word_lengths)

    if correct_chars <= longest_distractor_chars or correct_words <= longest_distractor_words:
        return ""

    char_gap = correct_chars - longest_distractor_chars
    word_gap = correct_words - longest_distractor_words
    longest_char_ratio = correct_chars / max(longest_distractor_chars, 1)
    average_char_ratio = correct_chars / max(average_distractor_chars, 1)

    if (
        (char_gap >= 24 and longest_char_ratio >= 1.28)
        or (char_gap >= 18 and average_char_ratio >= 1.45)
        or (word_gap >= 4 and correct_words >= average_distractor_words + 4 and average_char_ratio >= 1.28)
    ):
        return "Single-answer payload makes the correct answer obviously longer than every distractor."
    return ""


def _single_answer_specificity_signal_error(correct_answer: str, distractors: list[str]) -> str:
    if not correct_answer or not distractors:
        return ""
    correct_profile = _option_specificity_profile(correct_answer)
    distractor_profiles = [
        _option_specificity_profile(distractor)
        for distractor in distractors
        if str(distractor).strip()
    ]
    if not distractor_profiles:
        return ""

    max_distractor_clause_score = max(profile["clause_score"] for profile in distractor_profiles)
    max_distractor_punctuation = max(profile["punctuation_score"] for profile in distractor_profiles)
    max_distractor_content_words = max(profile["content_word_count"] for profile in distractor_profiles)
    max_distractor_words = max(profile["word_count"] for profile in distractor_profiles)
    average_distractor_words = sum(profile["word_count"] for profile in distractor_profiles) / len(distractor_profiles)

    if (
        correct_profile["clause_score"] >= 2
        and max_distractor_clause_score == 0
        and (
            (
                correct_profile["content_word_count"] >= max_distractor_content_words + 1
                and correct_profile["word_count"] >= average_distractor_words
            )
            or max_distractor_punctuation == 0
        )
    ):
        return "Single-answer payload makes the correct answer much more qualified or multi-clause than every distractor."

    if (
        correct_profile["punctuation_score"] >= 1
        and max_distractor_punctuation == 0
        and correct_profile["word_count"] >= average_distractor_words + 3
        and correct_profile["clause_score"] >= 1
    ):
        return "Single-answer payload makes the correct answer much more qualified or multi-clause than every distractor."

    if (
        correct_profile["content_word_count"] >= max_distractor_content_words + 3
        and correct_profile["clause_score"] >= 1
        and max_distractor_clause_score == 0
        and correct_profile["word_count"] >= max_distractor_words + 1
    ):
        return "Single-answer payload makes the correct answer much more specific than every distractor."

    return ""


def _single_answer_option_balance_error(correct_answer: str, distractors: list[str]) -> str:
    return (
        _single_answer_length_signal_error(correct_answer, distractors)
        or _single_answer_specificity_signal_error(correct_answer, distractors)
    )


def _single_answer_style_signal_error(correct_answer: str, distractors: list[str]) -> str:
    cleaned_correct = re.sub(r"\s+", " ", str(correct_answer or "").strip())
    cleaned_distractors = [re.sub(r"\s+", " ", str(distractor or "").strip()) for distractor in distractors if str(distractor).strip()]
    if not cleaned_correct or not cleaned_distractors:
        return ""

    imperative_verbs = set(STUDY_FOCUS_ACTION_VERBS) | {"calculate", "determine", "state", "use", "write"}
    correct_first_word = re.findall(r"[A-Za-z]+", cleaned_correct.lower()[:24])
    if correct_first_word and correct_first_word[0] in imperative_verbs:
        return "Single-answer payload uses an instructional objective phrase as the correct answer."

    generic_distractor_starts = (
        "It focuses on a related detail",
        "It describes a different effect",
        "It confuses the cause and effect",
        "It gives a partially relevant fact",
        "It sounds plausible",
    )
    if all(any(distractor.startswith(prefix) for prefix in generic_distractor_starts) for distractor in cleaned_distractors):
        return "Single-answer payload uses generic templated distractors instead of real alternatives."

    meta_distractor_patterns = (
        r"\bdoes not fully answer the question\b",
        r"\bdoes not explain the main relationship being tested\b",
        r"\brather than the best explanation for this question\b",
        r"\bpartially relevant fact\b",
        r"\bsounds plausible\b",
        r"\bcentral mechanism or role\b",
        r"\blater lines do not depend on the earlier logic\b",
        r"\bexternal file or network service\b",
        r"\bworks the same way no matter how\b",
        r"\bthere is no need to reason about control flow or data movement\b",
    )
    if any(
        any(re.search(pattern, distractor, flags=re.IGNORECASE) for pattern in meta_distractor_patterns)
        for distractor in cleaned_distractors
    ):
        return "Single-answer payload uses meta-commentary distractors instead of direct content alternatives."

    distractor_first_words = []
    for distractor in cleaned_distractors:
        words = re.findall(r"[A-Za-z]+", distractor.lower())
        if words:
            distractor_first_words.append(words[0])
    if (
        len(distractor_first_words) == len(cleaned_distractors)
        and len(set(distractor_first_words)) == 1
        and distractor_first_words[0] in {"it", "because", "the", "this"}
    ):
        correct_chars, _correct_words = _option_length_profile(cleaned_correct)
        distractor_char_lengths = [_option_length_profile(distractor)[0] for distractor in cleaned_distractors]
        if all(length >= correct_chars + 10 for length in distractor_char_lengths):
            return "Single-answer payload uses distractors with the same opening word and noticeably longer phrasing than the answer."

    distractor_opening_phrases = []
    for distractor in cleaned_distractors:
        words = re.findall(r"[A-Za-z]+", distractor.lower())
        if len(words) >= 2:
            distractor_opening_phrases.append(" ".join(words[:2]))
    correct_opening_words = re.findall(r"[A-Za-z]+", cleaned_correct.lower())
    correct_opening_phrase = " ".join(correct_opening_words[:2]) if len(correct_opening_words) >= 2 else ""
    if (
        len(distractor_opening_phrases) == len(cleaned_distractors)
        and len(set(distractor_opening_phrases)) == 1
        and distractor_opening_phrases[0]
        and distractor_opening_phrases[0] != correct_opening_phrase
    ):
        return "Single-answer payload makes every distractor share the same opening phrase while the correct answer does not."

    return ""


def single_answer_question_balance_error(question: QuestionBankItem) -> str:
    if getattr(question, "question_type", "") != QuestionBankItem.QuestionType.MCQ:
        return ""
    correct_answer = str(getattr(question, "correct_answer", "")).strip()
    distractors = [str(option).strip() for option in getattr(question, "distractors", []) if str(option).strip()]
    return (
        _single_answer_option_balance_error(correct_answer, distractors)
        or _single_answer_style_signal_error(correct_answer, distractors)
    )


def question_quality_issue(question: QuestionBankItem) -> str:
    if getattr(question, "is_coding_question", False):
        return ""
    return (
        single_answer_question_balance_error(question)
        or _objective_alignment_error(
            stem=str(getattr(question, "stem", "") or ""),
            correct_answers=[
                answer
                for answer in [
                    str(getattr(question, "correct_answer", "") or ""),
                    *[str(answer or "") for answer in getattr(question, "additional_correct_answers", []) or []],
                ]
                if answer.strip()
            ],
            objective=getattr(question, "learning_objective", None),
        )
    )


def question_quality_sort_key(question: QuestionBankItem) -> tuple[int, int]:
    balance_error = question_quality_issue(question)
    return (
        1 if balance_error else 0,
        1 if getattr(question, "question_type", "") == QuestionBankItem.QuestionType.MCQ and not getattr(question, "distractors", []) else 0,
    )


def _keywords_overlap_count(left: set[str], right: set[str]) -> int:
    if not left or not right:
        return 0
    matched_right: set[str] = set()
    overlap = 0
    for left_token in sorted(left):
        for right_token in sorted(right):
            if right_token in matched_right:
                continue
            shorter, longer = (left_token, right_token) if len(left_token) <= len(right_token) else (right_token, left_token)
            if (
                left_token == right_token
                or (len(shorter) >= 5 and longer.startswith(shorter))
                or (min(len(left_token), len(right_token)) >= 6 and left_token[:6] == right_token[:6])
            ):
                matched_right.add(right_token)
                overlap += 1
                break
    return overlap


def _objective_alignment_error(
    *,
    stem: str,
    correct_answers: list[str],
    objective: LearningObjective | None,
) -> str:
    if objective is None:
        return ""
    objective_tokens = _keyword_set(objective.text)
    if not objective_tokens:
        return ""
    question_tokens = _keyword_set(" ".join([stem, *correct_answers]))
    overlap = _keywords_overlap_count(objective_tokens, question_tokens)
    required_overlap = 2 if len(objective_tokens) >= 8 else 1
    if overlap < required_overlap:
        return "Generated question does not stay aligned with the target learning objective."
    return ""


def _coding_question_alignment_error(
    *,
    stem: str,
    correct_answers: list[str],
    explanation: str,
    code_snippet: str,
    objective: LearningObjective | None,
) -> str:
    if objective is None:
        return ""
    objective_tokens = _keyword_set(objective.text)
    if not objective_tokens:
        return ""
    combined = " ".join([stem, explanation, code_snippet, *correct_answers])
    question_tokens = _keyword_set(combined)
    overlap = _keywords_overlap_count(objective_tokens, question_tokens)
    if overlap < 1:
        return "Coding question does not stay aligned with the target learning objective."
    return ""


def _coding_question_external_dependency_error(code_snippet: str) -> str:
    snippet = str(code_snippet or "")
    dependency_patterns = (
        r"\bread\.(?:csv|table|delim)\s*\(",
        r"\bwrite\.(?:csv|table|delim)\s*\(",
        r"\bload\s*\(",
        r"\bsource\s*\(",
        r"\bfile\s*\(",
        r"\burl\s*\(",
        r"\bdownload\.file\s*\(",
        r"\bhttr::",
        r"\breadr::(?:read|write)_(?:csv|tsv|delim)\s*\(",
        r"\bjsonlite::fromJSON\s*\(",
        r"\bfetch\s*\(",
        r"\brequests?\.",
        r"\burllib\.",
        r"\bopen\s*\([^)]*['\"][^'\"]+\.(?:csv|tsv|txt|json|xlsx?)['\"]",
    )
    if any(re.search(pattern, snippet, flags=re.IGNORECASE | re.MULTILINE) for pattern in dependency_patterns):
        return "Coding question snippet depends on an external file or service."
    return ""


def _coding_question_focus_mismatch_error(stem: str, code_snippet: str) -> str:
    lowered_stem = re.sub(r"\s+", " ", str(stem or "").strip().lower())
    snippet = str(code_snippet or "")
    if not lowered_stem or not snippet:
        return ""

    if any(term in lowered_stem for term in ("loop", "iteration", "iterative", "across iterations")):
        if not re.search(r"\b(for|while|repeat)\b", snippet, flags=re.IGNORECASE | re.MULTILINE):
            return "Coding question stem refers to loop behaviour that is not present in the code snippet."

    if any(term in lowered_stem for term in ("branch", "conditional", "condition", "if statement", "switch")):
        if not re.search(r"\b(if|else|elif|switch|case)\b", snippet, flags=re.IGNORECASE | re.MULTILINE):
            return "Coding question stem refers to conditional logic that is not present in the code snippet."

    if any(term in lowered_stem for term in ("index", "indexing", "subsetting", "subset")):
        if not re.search(r"\[[^\]]+\]|\[\[|\$[A-Za-z_]\w*", snippet, flags=re.MULTILINE):
            return "Coding question stem refers to indexing or subsetting that is not present in the code snippet."

    return ""


def _trace_generation_rejection(block: CourseBlock, question_type: str, error: str, *, chunk: ContentChunk | None = None, objective: LearningObjective | None = None) -> None:
    _trace_generation(
        "question_generation_rejected",
        requested_type=QuestionBankItem.display_label_for_question_type(question_type),
        block=block.title,
        block_id=block.pk,
        chunk_id=(chunk.pk if chunk else None),
        objective=(objective.text if objective else ""),
        error=error,
    )


def _fallback_concept_distractors(topic: str, distractor_count: int) -> list[str]:
    cleaned_topic = re.sub(r"\s+", " ", str(topic or "").strip(" ?.!")).lower() or "this topic"
    templates = [
        "It focuses on a related detail in {topic}, but it does not explain the main relationship being tested.",
        "It describes a different effect within {topic}, rather than the best explanation for this question.",
        "It confuses the cause and effect involved in {topic}, so it does not fully answer the question.",
        "It gives a partially relevant fact about {topic}, but it misses the key concept needed here.",
        "It sounds plausible for {topic}, yet it does not address the central mechanism or role in the scenario.",
    ]
    return [template.format(topic=cleaned_topic) for template in templates[:distractor_count]]


def _fallback_written_answer_keywords(*texts: str, limit: int = 6) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    ignored_starts = ("explain ", "describe ", "identify ", "discuss ", "outline ", "summarise ", "summarize ", "how ", "why ")

    for text in texts:
        for segment in re.split(r"[.;:!?]+", str(text or "")):
            cleaned = re.sub(r"\s+", " ", segment).strip(" -")
            lowered = cleaned.lower()
            if (
                cleaned
                and 2 <= len(cleaned.split()) <= 7
                and lowered not in seen
                and not lowered.startswith(ignored_starts)
            ):
                keywords.append(cleaned[:90])
                seen.add(lowered)
                if len(keywords) >= limit:
                    return keywords

        for token in sorted(_keyword_set(str(text or ""))):
            if token not in seen:
                keywords.append(token)
                seen.add(token)
                if len(keywords) >= limit:
                    return keywords

    return keywords or ["core idea"]


def _normalize_study_question(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", _decode_literal_unicode_escapes(text)).strip(" -")
    if not cleaned:
        return ""
    cleaned = cleaned.rstrip(".!")
    if not cleaned.endswith("?"):
        cleaned = f"{cleaned}?"
    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]
    return cleaned


def _normalize_further_study_questions(items, *, limit: int = FURTHER_STUDY_QUESTION_COUNT) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return normalized
    for item in items:
        cleaned = _normalize_study_question(item)
        if len(cleaned) > 140:
            cleaned = f"{cleaned[:139].rstrip(' ?.!')}?"
        lowered = cleaned.lower()
        if cleaned and lowered not in seen:
            normalized.append(cleaned)
            seen.add(lowered)
        if len(normalized) >= limit:
            break
    return normalized


def _is_weird_study_question(text: str) -> bool:
    lowered = str(text or "").lower()
    action_verbs = "|".join(STUDY_FOCUS_ACTION_VERBS)
    weird_patterns = (
        rf"\b(?:{action_verbs})\s+(?:{action_verbs})\b",
        rf"\b(?:example of|thinking about|with)\s+(?:{action_verbs})\b",
        r"\bwith\s+it\s+(?:allows|shows|means|helps?)\b",
        r"\bin your own\?$",
        r"\b[a-z]\?$",
    )
    return any(re.search(pattern, lowered) for pattern in weird_patterns)


def _usable_further_study_questions(items, *, limit: int = FURTHER_STUDY_QUESTION_COUNT) -> list[str]:
    normalized = _normalize_further_study_questions(items, limit=limit)
    if len(normalized) < min(limit, FURTHER_STUDY_QUESTION_COUNT):
        return []
    if any(_is_weird_study_question(question) for question in normalized):
        return []
    return normalized


def _focus_phrase_from_text(text: str) -> str:
    cleaned = str(text or "").replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.!:-")
    if not cleaned:
        return ""
    wrapper_patterns = (
        r"^(?:please\s+)?(?:can|could|would)\s+you\s+(?:show|give|provide|share|offer|outline|explain|describe|walk)\s+(?:me\s+)?",
        r"^(?:please\s+)?(?:tell|show|give|provide|share)\s+(?:me\s+)?",
        r"^(?:please\s+)?help\s+me\s+(?:understand|explain)\s+",
        r"^(?:please\s+)?how\s+would\s+you\s+explain\s+",
        r"^(?:please\s+)?what\s+common\s+mistake(?:s)?(?:\s+or\s+misconception(?:s)?)?\s+should\s+i\s+avoid\s+(?:with|when\s+thinking\s+about)\s+",
    )
    for pattern in wrapper_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(?:a\s+simple\s+|an?\s+)?example\s+of\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        rf"^(?:{'|'.join(STUDY_FOCUS_ACTION_VERBS)})\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:what|why|how|which|when|where)\s+(?:is|are|does|do|did|can|could|would|should|might|statements?\s+best\s+explain|statement\s+best\s+explains|best\s+explains?)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(?:in\s+your\s+own\s+words|connect\s+to\s+another\s+idea\s+in\s+this\s+block|connect\s+to\s+the\s+bigger\s+picture|matter(?:\s+here)?)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.strip(" ?.!:-")
    if not cleaned:
        return ""
    if len(cleaned) > 110:
        truncated = re.split(r"[,;:]\s+", cleaned, maxsplit=1)[0].strip(" ?.!:-")
        if len(truncated) >= 24:
            cleaned = truncated
    return cleaned[0].lower() + cleaned[1:]


def fallback_further_study_questions(
    *,
    stem: str = "",
    objective_text: str = "",
    chunk_text: str = "",
    correct_answer: str = "",
    limit: int = FURTHER_STUDY_QUESTION_COUNT,
) -> list[str]:
    focus = (
        _focus_phrase_from_text(objective_text)
        or _focus_phrase_from_text(stem)
        or _focus_phrase_from_text(correct_answer)
        or _focus_phrase_from_text(chunk_text.split(".")[0] if chunk_text else "")
        or "this idea"
    )
    questions = [
        f"Can you show a simple example of {focus}?",
        f"How would you explain {focus} in your own words?",
        f"What common mistake should I avoid when thinking about {focus}?",
        f"Why does {focus} matter?",
    ]
    return _usable_further_study_questions(questions, limit=limit) or _normalize_further_study_questions(questions, limit=limit)


def further_study_questions_for_question(question: QuestionBankItem) -> list[str]:
    return _usable_further_study_questions(question.further_study_questions) or fallback_further_study_questions(
        stem=question.stem,
        objective_text=(question.learning_objective.text if question.learning_objective else ""),
        chunk_text=(question.source_chunk.text if question.source_chunk else ""),
        correct_answer=question.correct_answer,
    )


def further_study_questions_for_chat(
    *,
    question: str = "",
    answer: str = "",
    block_title: str = "",
    objective_texts: list[str] | tuple[str, ...] | None = None,
    limit: int = FURTHER_STUDY_QUESTION_COUNT,
) -> list[str]:
    objective_focus = " ".join(str(text or "").strip() for text in (objective_texts or []) if str(text or "").strip())
    focus = (
        _focus_phrase_from_text(question)
        or _focus_phrase_from_text(objective_focus)
        or _focus_phrase_from_text(block_title)
        or _focus_phrase_from_text(answer)
        or "this idea"
    )
    questions = [
        f"Can you show a simple example of {focus}?",
        f"How would you explain {focus} in your own words?",
        f"What common mistake should I avoid when thinking about {focus}?",
        f"How does {focus} connect to the bigger picture?",
    ]
    return _usable_further_study_questions(questions, limit=limit) or _normalize_further_study_questions(questions, limit=limit)


def _select_objective_for_chunk(
    chunk: ContentChunk,
    objectives: list[LearningObjective],
    objective_keywords: dict[int, set[str]],
    chunk_index: int,
    total_chunks: int,
) -> LearningObjective | None:
    if not objectives:
        return None
    if len(objectives) == 1:
        return objectives[0]

    chunk_keywords = _keyword_set(chunk.text)
    best_objective = None
    best_score = 0
    for objective in objectives:
        overlap = len(chunk_keywords & objective_keywords.get(objective.pk, set()))
        if overlap > best_score:
            best_score = overlap
            best_objective = objective

    if best_objective is not None and best_score > 0:
        return best_objective

    scaled_index = min(len(objectives) - 1, (chunk_index * len(objectives)) // max(1, total_chunks))
    return objectives[scaled_index]


def _fallback_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    *,
    question_variant_index: int = 0,
) -> dict:
    def sanitize_focus_phrase(text: str) -> str:
        cleaned = re.sub(
            r"\b(?:figure|fig\.?|table|diagram|graph|worked\s+example|example|chapter|module|section|page|paragraph|extract|excerpt|week|lesson|unit)\s*[a-z0-9.-]*\b",
            " ",
            text or "",
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:"
            + "|".join(sorted({verb for verb in STUDY_FOCUS_ACTION_VERBS if verb}, key=len, reverse=True))
            + r")\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:this|the|a|an)\s+(?:text|source\s+text|textbook|book|chapter|passage|notes|material|materials|content|document)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"[^a-zA-Z\s-]", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
        cleaned = re.sub(r"\b(?:in|of|on|for|to|with|about|from|into|across|between)\b\s*$", "", cleaned, flags=re.IGNORECASE).strip(" -")
        return cleaned[:120]

    summary = chunk.text.split(".")[0][:180].strip() or "this topic"
    summary_for_stem = sanitize_focus_phrase(objective.text if objective else "")
    if not summary_for_stem:
        summary_for_stem = sanitize_focus_phrase(
            re.sub(r"\b(?:shows?|shown|described|covered|mentioned|discussed|explained|presented|provided)\b", " ", summary, flags=re.IGNORECASE)
        )
    if any(char.isdigit() for char in summary) and any(
        token in summary.lower()
        for token in ("calculate", "solve", "compute", "determine", "estimate")
    ):
        summary_for_stem = sanitize_focus_phrase(objective.text if objective else "") or "the relationship described in this scenario"
    summary_for_stem = summary_for_stem or "this topic"
    correct_answer = (objective.text[:90].strip() if objective else summary) or "this topic"
    source_sentences = [
        sentence.strip(" .")
        for sentence in re.split(r"[.!?]+", chunk.text)
        if sentence.strip()
    ]

    if question_type == QuestionBankItem.QuestionType.WAQ:
        canonical_answer = source_sentences[0][:180].strip() if source_sentences else correct_answer
        topic = summary.lower()
        waq_stem = WAQ_FALLBACK_STEM_TEMPLATES[question_variant_index % len(WAQ_FALLBACK_STEM_TEMPLATES)].format(topic=topic)
        return {
            "question_type": question_type,
            "stem": waq_stem,
            "correct_answers": [canonical_answer or correct_answer],
            "written_answer_keywords": _fallback_written_answer_keywords(
                objective.text if objective else "",
                canonical_answer or correct_answer,
                summary,
                chunk.text,
            ),
            "further_study_questions": fallback_further_study_questions(
                stem=waq_stem,
                objective_text=(objective.text if objective else ""),
                chunk_text=chunk.text,
                correct_answer=canonical_answer or correct_answer,
            ),
            "distractors": [],
            "explanation": "The correct answer reflects the key relationship being tested.",
            "difficulty": "core",
        }

    distractors = _fallback_concept_distractors(summary_for_stem, distractor_count)
    correct_answers = [correct_answer]
    if question_type == QuestionBankItem.QuestionType.MAQ:
        fallback_candidates = [sentence[:90].strip() for sentence in source_sentences if sentence[:90].strip()]
        for candidate in fallback_candidates:
            if candidate not in correct_answers:
                correct_answers.append(candidate)
            if len(correct_answers) >= 2:
                break
        if len(correct_answers) < 2:
            correct_answers.append(f"Another accurate point about {summary.lower()}")
    return {
        "question_type": question_type,
        "stem": (
            f"Which statements best explain {summary_for_stem.lower()}?"
            if question_type == QuestionBankItem.QuestionType.MAQ
            else f"Which statement best explains {summary_for_stem.lower()}?"
        ),
        "correct_answers": correct_answers,
        "distractors": distractors,
        "written_answer_keywords": [],
        "further_study_questions": fallback_further_study_questions(
            stem=(
                f"Which statements best explain {summary_for_stem.lower()}?"
                if question_type == QuestionBankItem.QuestionType.MAQ
                else f"Which statement best explains {summary_for_stem.lower()}?"
            ),
            objective_text=(objective.text if objective else ""),
            chunk_text=chunk.text,
            correct_answer=correct_answers[0] if correct_answers else correct_answer,
        ),
        "explanation": "The correct answer reflects the key relationship being tested.",
        "difficulty": "core",
    }


def _fallback_coding_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    coding_signal: dict[str, str],
    *,
    question_variant_index: int = 0,
) -> dict:
    language = coding_signal.get("language", "")
    snippet = coding_signal.get("snippet", "")
    if not language or not snippet:
        raise ValueError("Coding fallback requires a detected language and snippet.")

    label = _coding_language_label(language)
    focus_phrase = _coding_focus_phrase(language, snippet)
    answer_focuses = _fallback_coding_answer_focus(language, snippet, objective.text if objective else "")
    primary_answer = answer_focuses[0]
    kind = QuestionBankItem.CodingQuestionKind.DEBUG if "bug" in focus_phrase or "issue" in focus_phrase else QuestionBankItem.CodingQuestionKind.COMPREHENSION
    if question_type == QuestionBankItem.QuestionType.WAQ:
        stem = f"How would you explain {focus_phrase} in this {label} example?"
        return {
            "question_type": question_type,
            "stem": stem,
            "correct_answers": [primary_answer],
            "written_answer_keywords": _fallback_written_answer_keywords(primary_answer, focus_phrase, snippet, language),
            "further_study_questions": fallback_further_study_questions(stem=stem, objective_text=primary_answer, chunk_text=chunk.text),
            "distractors": [],
            "explanation": "A strong answer should explain the main code path and the programming detail that controls the observed behaviour.",
            "difficulty": "core",
            "is_coding_question": True,
            "coding_language": language,
            "coding_question_kind": kind,
            "code_snippet": snippet,
        }

    stem = f"Which statement best explains {focus_phrase} in this {label} example?"
    correct_answers = [primary_answer]
    if question_type == QuestionBankItem.QuestionType.MAQ:
        for candidate in answer_focuses[1:]:
            if candidate not in correct_answers:
                correct_answers.append(candidate)
            if len(correct_answers) >= 2:
                break
    distractors = [
        distractor
        for distractor in _fallback_coding_distractors(language, snippet)
        if distractor not in correct_answers
    ][:distractor_count]
    return {
        "question_type": question_type,
        "stem": stem if question_type != QuestionBankItem.QuestionType.MAQ else f"Which statements accurately describe {focus_phrase} in this {label} example?",
        "correct_answers": correct_answers,
        "distractors": distractors,
        "written_answer_keywords": [],
        "further_study_questions": fallback_further_study_questions(stem=stem, objective_text=primary_answer, chunk_text=chunk.text),
        "explanation": "The correct answer depends on interpreting the code structure, data flow, and language semantics shown in the example.",
        "difficulty": "core",
        "is_coding_question": True,
        "coding_language": language,
        "coding_question_kind": kind,
        "code_snippet": snippet,
    }


def _numeric_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    *,
    avoid_question_angles: list[str] | None = None,
) -> dict:
    objective_text = objective.text if objective else ""
    if not supports_local_numeric_mcq(objective_text, chunk.text):
        raise NumericQuestionValidationError(
            "This learning objective is not suitable for a locally evaluable numerical MCQ."
        )
    objective_text = objective_text or "this block"
    teacher_guidance = build_generation_guidance_prompt(chunk.course, block=chunk.block, objective=objective)
    result = build_numeric_question_payload(
        chunk.text,
        objective_text,
        distractor_count,
        avoid_question_angles=avoid_question_angles,
        teacher_guidance=teacher_guidance,
    )
    _trace_generation(
        "numeric_candidate_validated",
        question_type=QuestionBankItem.display_label_for_question_type(QuestionBankItem.QuestionType.NUM),
        objective=objective_text,
        chunk_id=chunk.pk,
        stem=result.payload.get("stem", ""),
        correct_answer=(result.payload.get("correct_answers") or [""])[0],
    )
    return result.payload


NUMERIC_GENERATION_ATTEMPT_LIMIT = 3


def _numeric_avoidance_note_from_payload(payload: dict) -> str:
    stem = re.sub(r"\s+", " ", str(payload.get("stem", "")).strip())
    correct_answers = payload.get("correct_answers") or []
    correct_answer = str(correct_answers[0]).strip() if correct_answers else ""
    numeric_metadata = payload.get("numeric_metadata") if isinstance(payload.get("numeric_metadata"), dict) else {}
    note = f'- Avoid another numerical MCQ like "{stem[:180]}"'
    if correct_answer:
        note += f' with answer focus "{correct_answer[:120]}"'
    formula_focus = _numeric_formula_focus_from_metadata(numeric_metadata)
    if formula_focus:
        note += f' and formula focus "{formula_focus[:120]}"'
    return note


def _is_retryable_numeric_validation_error(error_message: str) -> bool:
    lowered = str(error_message or "").lower()
    return (
        "repeated numerical mcq" in lowered
        or "duplicated an existing question" in lowered
        or "gives away the method too explicitly" in lowered
        or "stem template must contain each supplied variable" in lowered
        or "calculation expression must use at least one supplied variable" in lowered
        or "calculation expression contains unsupported syntax" in lowered
        or "numeric feedback" in lowered
        or "stored numeric feedback" in lowered
    )


def _retry_notes_for_numeric_validation_error(error_message: str, payload: dict | None = None) -> list[str]:
    lowered = str(error_message or "").lower()
    notes: list[str] = []
    if payload is not None:
        notes.append(_numeric_avoidance_note_from_payload(payload))
    if "stem template must contain each supplied variable" in lowered:
        notes.append("- In stem_template, include every variable exactly once using Python-style placeholders like {distance}.")
        notes.append("- Do not place raw numeric givens directly in the stem outside those placeholders.")
    if "calculation expression must use at least one supplied variable" in lowered:
        notes.append("- calculation_expression must reference one or more declared variable names directly and must not be a constant.")
    if "calculation expression contains unsupported syntax" in lowered:
        notes.append("- Keep calculation_expression to supported arithmetic only: +, -, *, /, //, %, **, parentheses, and allowed functions.")
    if "repeated numerical mcq" in lowered or "duplicated an existing question" in lowered:
        notes.append("- Retry with a materially different numerical scenario, target quantity, and formula focus.")
    if "gives away the method too explicitly" in lowered:
        notes.append("- Do not name the exact formula or arithmetic step in the stem; let the student infer it from the scenario.")
    if not notes:
        notes.append("- Retry with a materially different numerical scenario, target quantity, and formula focus.")
    return notes


def _block_has_suitable_numeric_path(
    block: CourseBlock,
    candidate_chunks: list[ContentChunk],
    objectives_by_block: dict[int, list[LearningObjective]],
) -> bool:
    objectives = list(objectives_by_block.get(block.pk, []))
    return any(
        _objective_supports_local_numeric_mcq(objective, chunk.text)
        for objective in objectives
        for chunk in candidate_chunks
    )


def _format_attempted_type_errors(type_errors: dict[str, str]) -> str:
    parts = [f"{question_type}: {message}" for question_type, message in type_errors.items() if message]
    return "; ".join(parts)


def _attempt_numeric_generation_for_chunk(
    *,
    course: Course,
    block: CourseBlock,
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    existing_hashes: set[str],
    avoid_question_angles: list[str] | None = None,
) -> tuple[QuestionBankItem | None, QuestionBankItem | None, str]:
    numeric_avoid_question_angles = list(avoid_question_angles or [])
    last_generation_error = ""

    for _attempt_index in range(NUMERIC_GENERATION_ATTEMPT_LIMIT):
        payload = None
        try:
            payload, effective_question_type, expected_coding_language = _payload_for_generation_attempt(
                chunk,
                objective,
                distractor_count,
                QuestionBankItem.QuestionType.NUM,
                avoid_question_angles=numeric_avoid_question_angles,
            )
            practice, validation = _create_question_pair(
                course=course,
                block=block,
                chunk=chunk,
                objective=objective,
                question_type=effective_question_type,
                payload=payload,
                existing_hashes=existing_hashes,
                expected_coding_language=expected_coding_language,
            )
        except NumericQuestionRequestError as exc:
            last_generation_error = str(exc)
            _trace_generation(
                "numeric_request_failed",
                error=last_generation_error,
                objective=(objective.text if objective else ""),
                chunk_id=chunk.pk,
            )
            break
        except NumericQuestionValidationError as exc:
            last_generation_error = str(exc)
            _trace_generation(
                "numeric_validation_failed",
                error=last_generation_error,
                objective=(objective.text if objective else ""),
                chunk_id=chunk.pk,
            )
            if _is_retryable_numeric_validation_error(last_generation_error):
                numeric_avoid_question_angles.extend(_retry_notes_for_numeric_validation_error(last_generation_error, payload))
                continue
            break
        except ValueError as exc:
            last_generation_error = str(exc)
            _trace_generation_rejection(block, QuestionBankItem.QuestionType.NUM, last_generation_error, chunk=chunk, objective=objective)
            numeric_avoid_question_angles.extend(_retry_notes_for_numeric_validation_error(last_generation_error, payload))
            continue

        if practice is not None and validation is not None:
            return practice, validation, ""

        last_generation_error = "The generated numerical MCQ duplicated an existing question."
        if payload is not None:
            numeric_avoid_question_angles.append(_numeric_avoidance_note_from_payload(payload))
        if not _is_retryable_numeric_validation_error(last_generation_error):
            break

    return None, None, last_generation_error


def _generate_question_pair_for_question_type(
    *,
    course: Course,
    block: CourseBlock,
    question_type: str,
    candidate_chunks: list[ContentChunk],
    objectives_by_block: dict[int, list[LearningObjective]],
    objective_keywords: dict[int, set[str]],
    ordered_objectives: list[LearningObjective],
    coding_signals: dict[int, dict[str, str]],
    distractor_count: int,
    existing_hashes: set[str],
    strict_preferred_objectives: bool = False,
    relax_similarity_checks: bool = False,
) -> tuple[QuestionBankItem | None, QuestionBankItem | None, str]:
    question_type_objective_counts, question_type_chunk_counts, question_type_objective_chunk_counts = _question_type_distribution_for_block(
        block,
        question_type,
    )
    total_chunks_by_block: dict[int, int] = defaultdict(int)
    chunk_index_by_block: dict[int, int] = defaultdict(int)
    for chunk in candidate_chunks:
        total_chunks_by_block[chunk.block_id] += 1

    if question_type in {QuestionBankItem.QuestionType.NUM, QuestionBankItem.QuestionType.WAQ}:
        ordered_objectives = sorted(
            ordered_objectives,
            key=lambda objective: question_type_objective_counts.get(objective.pk, 0),
        )

    attempted_chunk_ids: set[int] = set()
    found_numeric_candidate_path = False
    generation_attempts = 0
    last_generation_error = ""
    coding_generation_due = _coding_question_generation_due(block) and bool(coding_signals)

    for objective in ordered_objectives:
        avoid_question_angles = (
            []
            if relax_similarity_checks
            else _recent_question_avoidance_notes(block, question_type, objective=objective)
        )
        ranked_chunks = _rank_candidate_chunks_for_objective(
            candidate_chunks,
            objective,
            objective_keywords,
            question_type=question_type,
            question_type_chunk_counts=question_type_chunk_counts,
            question_type_objective_chunk_counts=question_type_objective_chunk_counts,
        )
        target_keywords = objective_keywords.get(objective.pk, set())
        if strict_preferred_objectives and target_keywords:
            ranked_chunks = [chunk for chunk in ranked_chunks if (_keyword_set(chunk.text) & target_keywords)]
        if question_type == QuestionBankItem.QuestionType.NUM:
            ranked_chunks = [chunk for chunk in ranked_chunks if _objective_supports_local_numeric_mcq(objective, chunk.text)]
            if ranked_chunks:
                found_numeric_candidate_path = True
        if coding_generation_due and question_type != QuestionBankItem.QuestionType.NUM:
            ranked_chunks = [chunk for chunk in ranked_chunks if chunk.pk in coding_signals] or ranked_chunks
        for chunk in ranked_chunks:
            if question_type != QuestionBankItem.QuestionType.NUM and generation_attempts >= MAX_STANDARD_GENERATION_ATTEMPTS:
                break
            attempted_chunk_ids.add(chunk.pk)
            question_variant_index = question_type_objective_counts.get(objective.pk, 0) + question_type_objective_chunk_counts.get(
                (objective.pk, chunk.pk),
                0,
            )
            if question_type == QuestionBankItem.QuestionType.NUM:
                practice, validation, numeric_error = _attempt_numeric_generation_for_chunk(
                    course=course,
                    block=block,
                    chunk=chunk,
                    objective=objective,
                    distractor_count=distractor_count,
                    existing_hashes=existing_hashes,
                    avoid_question_angles=avoid_question_angles,
                )
                if practice is not None and validation is not None:
                    return practice, validation, ""
                if numeric_error:
                    last_generation_error = numeric_error
                continue
            try:
                generation_attempts += 1
                coding_signal = coding_signals.get(chunk.pk) if (coding_generation_due and question_type != QuestionBankItem.QuestionType.NUM) else None
                payload, effective_question_type, expected_coding_language = _payload_for_generation_attempt(
                    chunk,
                    objective,
                    distractor_count,
                    question_type,
                    coding_signal=coding_signal,
                    avoid_question_angles=avoid_question_angles,
                    question_variant_index=question_variant_index,
                )
                practice, validation = _create_question_pair(
                    course=course,
                    block=block,
                    chunk=chunk,
                    objective=objective,
                    question_type=effective_question_type,
                    payload=payload,
                    existing_hashes=existing_hashes,
                    expected_coding_language=expected_coding_language,
                    relax_similarity_checks=relax_similarity_checks,
                )
            except QuestionGenerationUnavailableError:
                raise
            except (ValueError, OpenAIError) as exc:
                last_generation_error = str(exc)
                _trace_generation_rejection(block, question_type, last_generation_error, chunk=chunk, objective=objective)
                continue
            if practice is not None and validation is not None:
                return practice, validation, ""
        if question_type != QuestionBankItem.QuestionType.NUM and generation_attempts >= MAX_STANDARD_GENERATION_ATTEMPTS:
            break

    for chunk in candidate_chunks:
        if question_type != QuestionBankItem.QuestionType.NUM and generation_attempts >= MAX_STANDARD_GENERATION_ATTEMPTS:
            break
        if chunk.pk in attempted_chunk_ids:
            continue
        chunk_index_by_block[chunk.block_id] += 1
        objective = _select_objective_for_chunk(
            chunk,
            objectives_by_block.get(chunk.block_id, []),
            objective_keywords,
            chunk_index_by_block[chunk.block_id] - 1,
            total_chunks_by_block[chunk.block_id],
        )
        if strict_preferred_objectives and objective is not None:
            target_keywords = objective_keywords.get(objective.pk, set())
            if target_keywords and not (_keyword_set(chunk.text) & target_keywords):
                continue
        if question_type == QuestionBankItem.QuestionType.NUM and (
            objective is None or not _objective_supports_local_numeric_mcq(objective, chunk.text)
        ):
            continue
        if question_type == QuestionBankItem.QuestionType.NUM:
            found_numeric_candidate_path = True
        question_variant_index = question_type_objective_counts.get(int(objective.pk) if objective else 0, 0) + question_type_objective_chunk_counts.get(
            ((objective.pk if objective else None), chunk.pk),
            0,
        )
        if question_type == QuestionBankItem.QuestionType.NUM:
            practice, validation, numeric_error = _attempt_numeric_generation_for_chunk(
                course=course,
                block=block,
                chunk=chunk,
                objective=objective,
                distractor_count=distractor_count,
                existing_hashes=existing_hashes,
                avoid_question_angles=_recent_question_avoidance_notes(block, question_type, objective=objective),
            )
            if practice is not None and validation is not None:
                return practice, validation, ""
            if numeric_error:
                last_generation_error = numeric_error
            continue
        try:
            generation_attempts += 1
            coding_signal = coding_signals.get(chunk.pk) if (coding_generation_due and question_type != QuestionBankItem.QuestionType.NUM) else None
            payload, effective_question_type, expected_coding_language = _payload_for_generation_attempt(
                chunk,
                objective,
                distractor_count,
                question_type,
                coding_signal=coding_signal,
                avoid_question_angles=_recent_question_avoidance_notes(block, question_type, objective=objective),
                question_variant_index=question_variant_index,
            )
            practice, validation = _create_question_pair(
                course=course,
                block=block,
                chunk=chunk,
                objective=objective,
                question_type=effective_question_type,
                payload=payload,
                existing_hashes=existing_hashes,
                expected_coding_language=expected_coding_language,
                relax_similarity_checks=relax_similarity_checks,
            )
        except QuestionGenerationUnavailableError:
            raise
        except (ValueError, OpenAIError) as exc:
            last_generation_error = str(exc)
            _trace_generation_rejection(block, question_type, last_generation_error, chunk=chunk, objective=objective)
            continue
        if practice is not None and validation is not None:
            return practice, validation, ""

    if question_type == QuestionBankItem.QuestionType.NUM and not found_numeric_candidate_path and not last_generation_error:
        last_generation_error = "objective not numeric-eligible"
    if question_type != QuestionBankItem.QuestionType.NUM and generation_attempts >= MAX_STANDARD_GENERATION_ATTEMPTS and not last_generation_error:
        last_generation_error = "Generation stopped after too many rejected attempts."
    return None, None, last_generation_error


def _payload_for_generation_attempt(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    *,
    coding_signal: dict[str, str] | None = None,
    avoid_question_angles: list[str] | None = None,
    question_variant_index: int = 0,
) -> tuple[dict, str, str]:
    if question_type == QuestionBankItem.QuestionType.NUM:
        payload = _numeric_question_payload(
            chunk,
            objective,
            distractor_count,
            avoid_question_angles=avoid_question_angles,
        )
        return payload, QuestionBankItem.QuestionType.NUM, ""

    if coding_signal:
        payload = _fallback_coding_question_payload(
            chunk,
            objective,
            distractor_count,
            question_type,
            coding_signal,
            question_variant_index=question_variant_index,
        )
        if settings.OPENAI_API_KEY:
            try:
                payload = _openai_coding_question_payload(
                    chunk,
                    objective,
                    distractor_count,
                    question_type,
                    coding_signal,
                    avoid_question_angles=avoid_question_angles,
                )
                if _is_source_dependent_question_stem(str(payload.get("stem", ""))):
                    raise ValueError("Question stem depends on source/meta phrasing.")
                _normalize_generated_payload(
                    payload,
                    question_type,
                    distractor_count,
                    expected_coding_language=coding_signal.get("language", ""),
                )
            except (ValueError, json.JSONDecodeError, KeyError, TypeError):
                payload = _fallback_coding_question_payload(
                    chunk,
                    objective,
                    distractor_count,
                    question_type,
                    coding_signal,
                    question_variant_index=question_variant_index,
                )
        return payload, question_type, coding_signal.get("language", "")

    payload = _fallback_question_payload(
        chunk,
        objective,
        distractor_count,
        question_type,
        question_variant_index=question_variant_index,
    )
    if settings.OPENAI_API_KEY:
        payload = _openai_question_payload(
            chunk,
            objective,
            distractor_count,
            question_type,
            avoid_question_angles=avoid_question_angles,
        )
        if _is_source_dependent_question_stem(str(payload.get("stem", ""))):
            raise ValueError("Question stem depends on source/meta phrasing.")
        _normalize_generated_payload(payload, question_type, distractor_count)
    return payload, question_type, ""


def _is_source_dependent_question_stem(stem: str) -> bool:
    lowered = re.sub(r"\s+", " ", str(stem or "").strip().lower())
    if not lowered:
        return False
    source_terms = r"(?:source\s+text|textbook|book|chapter|passage|notes|material|materials|content|block|uploaded\s+document|document)"
    source_artifacts = r"(?:figure|fig\.?|diagram|graph|worked\s+example|chapter|module|section|page|paragraph|extract|excerpt)"
    patterns = (
        rf"\b(?:according to|based on|from|in)\s+(?:the\s+)?{source_terms}\b",
        rf"\b(?:the\s+)?{source_terms}\s+(?:covers?|covered|provides?|states?|describes?|discusses?|mentions?|explains?|focuses\s+on)\b",
        rf"\b(?:main|key|primary|central)\s+topics?\s+(?:covered|discussed|provided|mentioned)\b",
        rf"\bwhat\s+is\s+one\s+of\s+(?:the\s+)?(?:main|key|primary|central)\s+topics?\b",
        rf"\bwhich\s+(?:topic|statement|idea)\s+is\s+(?:covered|mentioned|discussed|provided)\b",
        rf"\b(?:this|the)\s+{source_artifacts}\b",
        rf"\b(?:this|the)\s+(?:table|example)\s+\d+[a-z]?\b",
        rf"\b{source_artifacts}\s+\d+[a-z]?\b",
        rf"\b(?:shown|described|presented|given)\s+in\s+(?:this|the)\s+(?:{source_artifacts}|table|example)\b",
        rf"\b(?:text|passage|chapter|figure|table|worked\s+example|example)\s+above\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _normalize_question_stem(stem: str) -> str:
    cleaned = re.sub(r"\s+", " ", stem.strip())
    if not cleaned:
        return "Why is this important?"

    according_why_match = re.match(
        r"according to (?:the )?(?:presented|approved|provided) content,\s*why\s+(.*?)(?:\?)?$",
        cleaned,
        re.IGNORECASE,
    )
    if according_why_match:
        remainder = according_why_match.group(1).strip()
        if remainder:
            return f"Why {remainder.rstrip('?.')}?"

    cleaned = re.sub(
        r"^according to (?:the )?(?:presented|approved|provided) content,\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if cleaned.lower().startswith("why "):
        return f"Why {cleaned[4:].rstrip('?.')}?"
    return cleaned.rstrip("?.") + "?"


def _openai_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    *,
    avoid_question_angles: list[str] | None = None,
) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    objective_text = objective.text if objective else "this block"
    is_maq = question_type == QuestionBankItem.QuestionType.MAQ
    is_waq = question_type == QuestionBankItem.QuestionType.WAQ
    teacher_guidance = build_generation_guidance_prompt(chunk.course, block=chunk.block, objective=objective)
    avoidance_prompt = ""
    if avoid_question_angles:
        avoidance_prompt = "\nAvoid repeating the wording, answer angle, or explanation focus of these recent questions:\n" + "\n".join(
            avoid_question_angles[:6]
        )
    prompt = f"""
Create one educator-facing {"written-answer" if is_waq else ("multiple-answer" if is_maq else "single-answer")} question in JSON for the source text below.

Rules:
- no numerical calculation questions
- use the source text only as background context
- keep it answerable from the concepts supported by the source text below
- write the question as a standalone quiz item, independent of the source text or textbook
- do not ask what the source text, textbook, chapter, notes, material, or content covers/provides/discusses
- avoid lead-ins like "According to these notes", "Based on the passage", "In the textbook", or "From the content"
- never use stems like "What is one of the main topics covered..." or "Which topic is covered..."
- do not mention figures, worked examples, tables, diagrams, chapter numbers, page numbers, section labels, quoted local wording, or any document-position reference
- rewrite document-specific wording into a domain concept question
- when the best stem is a why-question, start it directly with "Why ..."
- set question_type to "{question_type}"
- correct_answers must be an array of strings
- {"return strict JSON with keys: question_type, stem, correct_answers, written_answer_keywords, further_study_questions, explanation, difficulty" if is_waq else "return strict JSON with keys: question_type, stem, correct_answers, distractors, further_study_questions, explanation, difficulty"}
- {"correct_answers must contain exactly 1 item" if is_waq or not is_maq else "correct_answers must contain at least 2 items"}
- {"the question must require the student to type an answer in their own words" if is_waq else ("the question must require selecting more than one correct answer" if is_maq else "the question must have only one correct answer")}
- {"written_answer_keywords must be an array of 3 to 6 short concept phrases or key terms needed for a strong answer" if is_waq else f"use exactly {distractor_count} distractors"}
- {"do not return distractors for a written-answer question" if is_waq else "distractors must be plausible and distinct from the correct answer(s)"}
- {"n/a" if is_waq else "keep all answer options similar in length, specificity, and qualification; do not make the correct answer obviously identifiable because it is the longest, most detailed, or only multi-clause option"}
- {"n/a" if is_waq else "keep each answer option concise: usually one clause or short sentence fragment, roughly 5 to 16 words unless the concept genuinely requires slightly more"}
- {"n/a" if is_waq else "each answer option must be a direct substantive claim, not commentary about whether another option is relevant, partial, plausible, or complete"}
- {"n/a" if is_waq else "do not make all distractors start with the same stock opener such as Because, It, This, or The unless the correct answer uses the same opener and comparable phrasing"}
- further_study_questions must be an array of exactly 3 concise student-facing follow-up questions
- further_study_questions should invite deeper understanding, examples, comparison, application, or common mistakes when helpful
- each further_study_questions item must be phrased as a question a student could click to ask next
- prioritise a genuinely different question angle from recent questions when possible{avoidance_prompt}

Learning objective:
{objective_text}

Source text:
{chunk.text}

{teacher_guidance}
""".strip()
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Return only valid JSON."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    )
    raw_output = (getattr(response, "output_text", "") or "").strip()
    return _parse_question_payload(raw_output)


def _openai_coding_question_payload(
    chunk: ContentChunk,
    objective: LearningObjective | None,
    distractor_count: int,
    question_type: str,
    coding_signal: dict[str, str],
    *,
    avoid_question_angles: list[str] | None = None,
) -> dict:
    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    objective_text = objective.text if objective else "programming understanding"
    language = coding_signal["language"]
    snippet = coding_signal["snippet"]
    language_label = _coding_language_label(language)
    is_maq = question_type == QuestionBankItem.QuestionType.MAQ
    is_waq = question_type == QuestionBankItem.QuestionType.WAQ
    teacher_guidance = build_generation_guidance_prompt(chunk.course, block=chunk.block, objective=objective)
    avoidance_prompt = ""
    if avoid_question_angles:
        avoidance_prompt = "\nAvoid repeating the wording, answer angle, or explanation focus of these recent questions:\n" + "\n".join(
            avoid_question_angles[:6]
        )
    prompt = f"""
Create one standalone coding {"written-answer" if is_waq else ("multiple-answer" if is_maq else "single-answer")} question in JSON.

Rules:
- set question_type to "{question_type}"
- set is_coding_question to true
- set coding_language to "{language}"
- set coding_question_kind to "comprehension" or "debug"
- keep the question entirely in {language_label}; do not mention or use any other programming language
- include code_snippet as a self-contained {language_label} example of roughly 6 to 16 meaningful lines unless the source snippet is naturally shorter and cannot be expanded without inventing unrelated logic
- when natural for {language_label}, prefer a named function or helper plus a call site or usage example; for SQL/HTML/CSS prefer a richer multi-clause or multi-rule example instead of a toy one-liner
- code_snippet must be based on the supplied snippet and must not require external files, services, network calls, packages beyond those already shown, or hidden context
- do not include fenced code blocks or repeated code_snippet text inside stem
- ask about code behaviour, data flow, return values, control flow, structure, language semantics, the likely bug, or the best fix
- prefer interpretation, issue-spotting, or reasoning questions over raw output tracing
- do not ask students to manually compute the value of a single variable or perform repetitive arithmetic tracing
- avoid toy one-liners and avoid stems that only ask "what is the value of x" or similar
- use the source text only as background context
- do not ask what the source text, notes, material, or content covers/provides/discusses
- do not mention figures, worked examples, tables, diagrams, chapter numbers, page numbers, section labels, quoted local wording, or any document-position reference
- no numerical calculation questions unless the calculation is incidental to understanding code behavior
- {"return strict JSON with keys: question_type, stem, correct_answers, written_answer_keywords, further_study_questions, explanation, difficulty, is_coding_question, coding_language, coding_question_kind, code_snippet" if is_waq else "return strict JSON with keys: question_type, stem, correct_answers, distractors, further_study_questions, explanation, difficulty, is_coding_question, coding_language, coding_question_kind, code_snippet"}
- {"correct_answers must contain exactly 1 item" if is_waq or not is_maq else "correct_answers must contain at least 2 items"}
- {"written_answer_keywords must be an array of 3 to 6 short concept phrases or key terms needed for a strong answer" if is_waq else f"use exactly {distractor_count} distractors"}
- {"do not return distractors for a written-answer question" if is_waq else "distractors must be plausible and distinct from the correct answer(s)"}
- {"the written-answer prompt should ask the student to interpret the code or identify the key issue in their own words" if is_waq else "the answer choices should test misconceptions about scope, indexing, mutation, return values, control flow, or API semantics where appropriate"}
- {"n/a" if is_waq else "keep all answer options similar in length, specificity, and qualification; do not make the correct answer obviously identifiable because it is the longest, most detailed, or only multi-clause option"}
- {"n/a" if is_waq else "keep each answer option concise: usually one clause or short sentence fragment, roughly 5 to 16 words unless the concept genuinely requires slightly more"}
- {"n/a" if is_waq else "each answer option must be a direct substantive claim about the code, not commentary about whether another option is relevant, partial, plausible, or complete"}
- {"n/a" if is_waq else "do not make all distractors start with the same stock opener such as Because, It, This, or The unless the correct answer uses the same opener and comparable phrasing"}
- further_study_questions must be an array of exactly 3 concise student-facing follow-up questions{avoidance_prompt}

Learning objective:
{objective_text}

Detected snippet:
```{language}
{snippet}
```

Source text:
{chunk.text}

{teacher_guidance}
""".strip()
    response = client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": "Return only valid JSON."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    )
    raw_output = (getattr(response, "output_text", "") or "").strip()
    return _parse_question_payload(raw_output)


def _parse_question_payload(raw_output: str) -> dict:
    if not raw_output:
        raise ValueError("OpenAI returned an empty question payload.")

    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    object_match = re.search(r"\{.*\}", raw_output, re.DOTALL)
    if object_match:
        return json.loads(object_match.group(0))

    raise ValueError("OpenAI did not return parseable JSON for question generation.")


def _released_objectives_by_block(
    course: Course,
    today=None,
    *,
    include_future_blocks: bool = False,
) -> dict[int, list[LearningObjective]]:
    today = today or timezone.localdate()
    objectives_by_block: dict[int, list[LearningObjective]] = defaultdict(list)
    objective_queryset = LearningObjective.objects.filter(course=course)
    if not include_future_blocks and not bool(getattr(course.config, "allow_pre_engagement", False)):
        objective_queryset = objective_queryset.filter(block__available_from__lte=today)

    for objective in objective_queryset.select_related("block").order_by("block__order", "position", "pk"):
        objectives_by_block[objective.block_id].append(objective)

    return objectives_by_block


def _ordered_released_chunks(course: Course, today=None, *, include_future_blocks: bool = False):
    today = today or timezone.localdate()
    chunk_queryset = ContentChunk.objects.filter(course=course, asset__include_in_generation=True)
    if not include_future_blocks and not bool(getattr(course.config, "allow_pre_engagement", False)):
        chunk_queryset = chunk_queryset.filter(block__available_from__lte=today)
    return list(
        chunk_queryset.select_related("block", "asset").order_by("block__order", "asset__created_at", "ordinal", "pk")
    )


def _ordered_generation_objectives(
    block: CourseBlock,
    objectives_by_block: dict[int, list[LearningObjective]],
    preferred_objective_ids: list[int] | None = None,
    *,
    strict_preferred_objectives: bool = False,
) -> list[LearningObjective]:
    ordered_objectives = list(objectives_by_block.get(block.pk, []))
    if not preferred_objective_ids:
        return ordered_objectives

    preferred_lookup = {objective_id: index for index, objective_id in enumerate(preferred_objective_ids)}
    preferred = [objective for objective in ordered_objectives if objective.pk in preferred_lookup]
    preferred.sort(key=lambda objective: preferred_lookup[objective.pk])
    if strict_preferred_objectives:
        return preferred
    remaining = [objective for objective in ordered_objectives if objective.pk not in preferred_lookup]
    return [*preferred, *remaining]


def _rank_candidate_chunks_for_objective(
    candidate_chunks: list[ContentChunk],
    objective: LearningObjective,
    objective_keywords: dict[int, set[str]],
    *,
    question_type: str,
    question_type_chunk_counts: dict[int, int] | None = None,
    question_type_objective_chunk_counts: dict[tuple[int | None, int | None], int] | None = None,
) -> list[ContentChunk]:
    ranked_chunks = []
    target_keywords = objective_keywords.get(objective.pk, set())
    question_type_chunk_counts = question_type_chunk_counts or {}
    question_type_objective_chunk_counts = question_type_objective_chunk_counts or {}
    for chunk in candidate_chunks:
        overlap = len(_keyword_set(chunk.text) & target_keywords)
        diversify_question_type = question_type in {
            QuestionBankItem.QuestionType.NUM,
            QuestionBankItem.QuestionType.WAQ,
        }
        ranked_chunks.append(
            (
                question_type_objective_chunk_counts.get((objective.pk, chunk.pk), 0)
                if diversify_question_type
                else 0,
                question_type_chunk_counts.get(chunk.pk, 0)
                if diversify_question_type
                else 0,
                0 if overlap > 0 else 1,
                chunk.practice_question_count,
                -overlap,
                chunk.asset.created_at,
                chunk.ordinal,
                chunk.pk,
                chunk,
            )
        )
    ranked_chunks.sort()
    return [item[-1] for item in ranked_chunks]


def _question_type_distribution_for_block(
    block: CourseBlock,
    question_type: str,
) -> tuple[dict[int, int], dict[int, int], dict[tuple[int | None, int | None], int]]:
    objective_counts: dict[int, int] = defaultdict(int)
    chunk_counts: dict[int, int] = defaultdict(int)
    objective_chunk_counts: dict[tuple[int | None, int | None], int] = defaultdict(int)
    rows = (
        QuestionBankItem.objects.filter(
            course=block.course,
            block=block,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            question_type=question_type,
        )
        .values("learning_objective_id", "source_chunk_id")
        .annotate(total=Count("id"))
    )
    for row in rows:
        total = int(row["total"] or 0)
        objective_id = row["learning_objective_id"]
        source_chunk_id = row["source_chunk_id"]
        if objective_id is not None:
            objective_counts[int(objective_id)] += total
        if source_chunk_id is not None:
            chunk_counts[int(source_chunk_id)] += total
        objective_chunk_counts[(objective_id, source_chunk_id)] += total
    return objective_counts, chunk_counts, objective_chunk_counts


def _numeric_formula_focus_from_metadata(metadata: dict | None) -> str:
    snapshot = metadata.get("output_snapshot") if isinstance(metadata, dict) else None
    if not isinstance(snapshot, dict):
        return ""
    formula = re.sub(r"\s+", " ", str(snapshot.get("formula_tex", "")).strip())
    formula = formula.replace("\\\\", "\\")
    formula = formula.replace("{{", "{").replace("}}", "}")
    return formula


def _numeric_question_repeats_recent_angle(
    *,
    block: CourseBlock,
    objective: LearningObjective | None,
    stem: str,
    correct_answer: str,
    numeric_metadata: dict | None,
    limit: int = 4,
) -> bool:
    if not isinstance(numeric_metadata, dict):
        return False

    new_formula = _numeric_formula_focus_from_metadata(numeric_metadata)
    new_tokens = _keyword_set(stem)
    new_unit = correct_answer.split()[-1].lower() if correct_answer.strip() else ""
    queryset = QuestionBankItem.objects.filter(
        course=block.course,
        block=block,
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        question_type=QuestionBankItem.QuestionType.NUM,
    )
    recent_questions = []
    if objective is not None:
        recent_questions.extend(list(queryset.filter(learning_objective=objective).order_by("-created_at", "-pk")[:limit]))
    if len(recent_questions) < limit:
        recent_ids = {question.pk for question in recent_questions}
        recent_questions.extend(
            list(queryset.exclude(pk__in=recent_ids).order_by("-created_at", "-pk")[: max(0, limit - len(recent_questions))])
        )

    for question in recent_questions:
        existing_formula = _numeric_formula_focus_from_metadata(question.numeric_metadata)
        if new_formula and existing_formula and new_formula == existing_formula:
            return True
        existing_tokens = _keyword_set(question.stem)
        union_size = len(new_tokens | existing_tokens)
        if union_size == 0:
            continue
        token_similarity = len(new_tokens & existing_tokens) / union_size
        existing_unit = question.correct_answer.split()[-1].lower() if question.correct_answer.strip() else ""
        if token_similarity >= 0.5 and new_unit and new_unit == existing_unit:
            return True
    return False


def _recent_question_avoidance_notes(
    block: CourseBlock,
    question_type: str,
    *,
    objective: LearningObjective | None = None,
    limit: int = 6,
) -> list[str]:
    queryset = QuestionBankItem.objects.filter(
        course=block.course,
        block=block,
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
        question_type=question_type,
    ).select_related("learning_objective")
    recent_questions = []
    if objective is not None:
        recent_questions.extend(list(queryset.filter(learning_objective=objective).order_by("-created_at", "-pk")[:limit]))
    if len(recent_questions) < limit:
        recent_ids = {question.pk for question in recent_questions}
        recent_questions.extend(
            list(queryset.exclude(pk__in=recent_ids).order_by("-created_at", "-pk")[: max(0, limit - len(recent_questions))])
        )
    notes = []
    for question in recent_questions:
        note = f'- "{question.stem}" with answer focus "{question.correct_answer[:120]}"'
        if question.question_type == QuestionBankItem.QuestionType.NUM:
            formula_focus = _numeric_formula_focus_from_metadata(question.numeric_metadata)
            if formula_focus:
                note += f' and formula focus "{formula_focus[:120]}"'
        notes.append(note)
    return notes


def _resolve_unique_question_identity(stem: str, question_type: str, existing_hashes: set[str]) -> tuple[str, str]:
    item_hash = hashlib.sha256(stem.lower().encode("utf-8")).hexdigest()
    if item_hash not in existing_hashes:
        return stem, item_hash
    if question_type == QuestionBankItem.QuestionType.NUM:
        raise NumericQuestionValidationError("OpenAI generated a repeated numerical MCQ that already exists for this course.")
    for variant_number in range(2, 6):
        candidate_stem = f"{stem.rstrip('?')} (variant {variant_number})?"
        candidate_hash = hashlib.sha256(candidate_stem.lower().encode("utf-8")).hexdigest()
        if candidate_hash not in existing_hashes:
            return candidate_stem, candidate_hash
    return "", ""


def _objective_supports_local_numeric_mcq(objective: LearningObjective | None, chunk_text: str) -> bool:
    if objective is None:
        return False
    if not objective_has_numeric_intent(objective.text):
        return False
    return supports_local_numeric_mcq(objective.text, chunk_text)


def _ratio_gap_ranked_question_types(
    block: CourseBlock,
    *,
    include_numeric: bool = True,
) -> list[str]:
    practice_questions = block.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
    )
    practice_total = practice_questions.count()
    candidates = []
    candidate_types = []
    if include_numeric:
        candidate_types.append((QuestionBankItem.QuestionType.NUM, block.question_numeric_ratio_percent))
    candidate_types.extend(
        [
            (QuestionBankItem.QuestionType.MAQ, block.question_maq_ratio_percent),
            (QuestionBankItem.QuestionType.WAQ, block.question_waq_ratio_percent),
        ]
    )
    for candidate_type, target_ratio in candidate_types:
        if target_ratio <= 0:
            continue
        current_total = practice_questions.filter(question_type=candidate_type).count()
        current_ratio = (current_total * 100 / practice_total) if practice_total else 0.0
        gap = target_ratio - current_ratio
        if gap > 0:
            candidates.append((gap, target_ratio, QUESTION_TYPE_GENERATION_PRIORITY[candidate_type], candidate_type))
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in candidates]


def _ordered_generation_question_types(
    block: CourseBlock,
    *,
    explicit_question_type: str | None = None,
    allow_type_fallback: bool = False,
) -> list[str]:
    if explicit_question_type and not allow_type_fallback:
        return [explicit_question_type]
    if explicit_question_type is None and not allow_type_fallback:
        ranked = _ratio_gap_ranked_question_types(block)
        return [ranked[0] if ranked else QuestionBankItem.QuestionType.MCQ]

    ordered: list[str] = []
    if explicit_question_type:
        ordered.append(explicit_question_type)
    for question_type in _ratio_gap_ranked_question_types(block):
        if question_type not in ordered:
            ordered.append(question_type)
    if QuestionBankItem.QuestionType.MCQ not in ordered:
        ordered.append(QuestionBankItem.QuestionType.MCQ)
    return ordered


def _preferred_generated_question_type(block: CourseBlock) -> str:
    return _ordered_generation_question_types(block)[0]


def _preferred_standard_generated_question_type(block: CourseBlock) -> str:
    ranked = _ratio_gap_ranked_question_types(block, include_numeric=False)
    return ranked[0] if ranked else QuestionBankItem.QuestionType.MCQ


def _coding_question_generation_due(block: CourseBlock) -> bool:
    target_ratio = getattr(block, "question_coding_question_ratio_percent", 0)
    if target_ratio <= 0:
        return False
    practice_questions = block.question_bank_items.filter(
        bank_type=QuestionBankItem.BankType.PRACTICE,
        status=QuestionBankItem.Status.APPROVED,
    )
    practice_total = practice_questions.count()
    if practice_total == 0:
        return True
    coding_total = practice_questions.filter(is_coding_question=True).count()
    current_ratio = coding_total * 100 / practice_total
    return current_ratio < target_ratio


def _coding_chunks(chunks: list[ContentChunk]) -> list[tuple[ContentChunk, dict[str, str]]]:
    detected = []
    for chunk in chunks:
        signal = _coding_signal_for_chunk(chunk)
        if signal["language"] and signal["snippet"]:
            detected.append((chunk, signal))
    return detected


def preferred_coding_language_for_block(
    block: CourseBlock,
    candidate_chunks: list[ContentChunk] | None = None,
    coding_signals: dict[int, dict[str, str]] | None = None,
) -> str:
    if candidate_chunks is None:
        candidate_chunks = list(
            ContentChunk.objects.filter(block=block, asset__include_in_generation=True)
            .select_related("asset")
            .order_by("asset__created_at", "ordinal", "pk")
        )

    language_counts: dict[str, int] = defaultdict(int)
    first_seen_order: dict[str, int] = {}
    if coding_signals is None:
        coding_signals = {chunk.pk: signal for chunk, signal in _coding_chunks(candidate_chunks)}

    for index, chunk in enumerate(candidate_chunks):
        signal = coding_signals.get(chunk.pk)
        language = signal.get("language", "") if signal else ""
        if not language:
            continue
        language_counts[language] += 1
        first_seen_order.setdefault(language, index)

    block_title = str(getattr(block, "title", "") or "")
    if re.search(r"(?:^|[^A-Za-z])R(?:[^A-Za-z]|$)", block_title) and language_counts.get("r"):
        return "r"

    if language_counts:
        ranked = sorted(
            language_counts.items(),
            key=lambda item: (-item[1], first_seen_order.get(item[0], 10**6), item[0]),
        )
        return ranked[0][0]
    return ""


def _normalize_generated_payload(
    payload: dict,
    question_type: str,
    distractor_count: int,
    *,
    expected_coding_language: str = "",
) -> dict:
    normalized_type = question_type

    correct_answers = _normalize_answer_list(payload.get("correct_answers"))
    if not correct_answers and payload.get("correct_answer"):
        correct_answers = [str(payload["correct_answer"]).strip()]
    written_answer_keywords = _normalize_answer_list(payload.get("written_answer_keywords"))
    further_study_questions = _usable_further_study_questions(payload.get("further_study_questions"))

    distractors = [
        distractor
        for distractor in _normalize_answer_list(payload.get("distractors"))
        if distractor not in correct_answers
    ][:distractor_count]
    stem = _decode_literal_unicode_escapes(str(payload.get("stem", "")).strip())
    explanation = _decode_literal_unicode_escapes(str(payload.get("explanation", "")).strip())
    difficulty = _decode_literal_unicode_escapes(str(payload.get("difficulty", "core")).strip()) or "core"
    numeric_metadata = payload.get("numeric_metadata") if isinstance(payload.get("numeric_metadata"), dict) else {}
    is_coding_question = bool(payload.get("is_coding_question"))
    coding_language = _normalize_coding_language(str(payload.get("coding_language", "")))
    coding_question_kind = str(payload.get("coding_question_kind", "")).strip().lower()
    code_snippet = str(payload.get("code_snippet", "")).strip()

    if normalized_type == QuestionBankItem.QuestionType.WAQ:
        if len(correct_answers) != 1:
            raise ValueError("WAQ payload must contain exactly one correct answer.")
        distractors = []
        written_answer_keywords = written_answer_keywords or _fallback_written_answer_keywords(correct_answers[0], stem)
    elif normalized_type in {QuestionBankItem.QuestionType.MCQ, QuestionBankItem.QuestionType.NUM}:
        if len(correct_answers) != 1:
            raise ValueError(f"{normalized_type.upper()} payload must contain exactly one correct answer.")
        written_answer_keywords = []
    else:
        if len(correct_answers) < 2:
            raise ValueError("MAQ payload must contain at least two correct answers.")
        written_answer_keywords = []

    if normalized_type == QuestionBankItem.QuestionType.NUM:
        if len(distractors) != distractor_count:
            raise ValueError("NUM payload must contain the configured number of distractors.")
        is_coding_question = False
    else:
        numeric_metadata = {}

    further_study_questions = further_study_questions or fallback_further_study_questions(
        stem=stem,
        objective_text="",
        correct_answer=correct_answers[0] if correct_answers else "",
    )

    if is_coding_question:
        if not code_snippet or len(code_snippet) < 8:
            raise ValueError("Coding question payload must include a meaningful code snippet.")
        stem = re.sub(r"```[\w+-]*\n?[\s\S]*?```", " ", stem).strip()
        stem = re.sub(r"\s+", " ", stem)
        if expected_coding_language:
            coding_language = expected_coding_language
        if not coding_language:
            detected_language = _language_from_text(code_snippet)
            if detected_language:
                coding_language = detected_language
        if coding_language not in CODING_LANGUAGES:
            raise ValueError("Coding question payload must include a supported coding language.")
        if _payload_mentions_unexpected_coding_language(payload, coding_language):
            raise ValueError("Coding question payload mentions an unexpected programming language.")
        if coding_question_kind not in {
            QuestionBankItem.CodingQuestionKind.COMPREHENSION,
            QuestionBankItem.CodingQuestionKind.DEBUG,
        }:
            coding_question_kind = QuestionBankItem.CodingQuestionKind.COMPREHENSION
        if _is_low_value_coding_stem(stem, code_snippet):
            raise ValueError("Coding question stem is too trivial.")
        coding_dependency_error = _coding_question_external_dependency_error(code_snippet)
        if coding_dependency_error:
            raise ValueError(coding_dependency_error)
        coding_focus_error = _coding_question_focus_mismatch_error(stem, code_snippet)
        if coding_focus_error:
            raise ValueError(coding_focus_error)
    else:
        coding_language = ""
        coding_question_kind = ""
        code_snippet = ""

    if normalized_type == QuestionBankItem.QuestionType.MCQ:
        option_balance_error = (
            _single_answer_option_balance_error(
                correct_answers[0] if correct_answers else "",
                distractors,
            )
            or _single_answer_style_signal_error(
                correct_answers[0] if correct_answers else "",
                distractors,
            )
        )
        if option_balance_error:
            raise ValueError(option_balance_error)

    return {
        "question_type": normalized_type,
        "stem": stem,
        "correct_answers": correct_answers,
        "distractors": distractors,
        "written_answer_keywords": written_answer_keywords,
        "further_study_questions": further_study_questions,
        "explanation": explanation,
        "difficulty": difficulty,
        "numeric_metadata": numeric_metadata,
        "is_coding_question": is_coding_question,
        "coding_language": coding_language,
        "coding_question_kind": coding_question_kind,
        "code_snippet": code_snippet,
    }


def _create_question_pair(
    *,
    course: Course,
    block: CourseBlock,
    chunk: ContentChunk,
    objective: LearningObjective | None,
    question_type: str,
    payload: dict,
    existing_hashes: set[str],
    expected_coding_language: str = "",
    relax_similarity_checks: bool = False,
):
    from standalone.services.question_builder import course_question_generation_budget

    normalized_payload = _normalize_generated_payload(
        payload,
        question_type,
        block.question_distractor_count,
        expected_coding_language=expected_coding_language,
    )
    if normalized_payload["is_coding_question"]:
        objective_alignment_error = _coding_question_alignment_error(
            stem=normalized_payload["stem"],
            correct_answers=normalized_payload["correct_answers"],
            explanation=normalized_payload["explanation"],
            code_snippet=normalized_payload["code_snippet"],
            objective=objective,
        )
    else:
        objective_alignment_error = _objective_alignment_error(
            stem=normalized_payload["stem"],
            correct_answers=normalized_payload["correct_answers"],
            objective=objective,
        )
    if objective_alignment_error:
        raise ValueError(objective_alignment_error)
    stem = _normalize_question_stem(normalized_payload["stem"])
    if (
        normalized_payload["question_type"] != QuestionBankItem.QuestionType.NUM
        and any(char.isdigit() for char in stem)
        and any(token in stem.lower() for token in ("calculate", "solve", "compute"))
    ):
        return None, None

    stem, item_hash = _resolve_unique_question_identity(
        stem,
        normalized_payload["question_type"],
        existing_hashes,
    )
    if not stem or not item_hash:
        return None, None

    if (
        normalized_payload["question_type"] == QuestionBankItem.QuestionType.NUM
        and not relax_similarity_checks
        and _numeric_question_repeats_recent_angle(
        block=block,
        objective=objective,
        stem=stem,
        correct_answer=normalized_payload["correct_answers"][0],
        numeric_metadata=normalized_payload["numeric_metadata"],
        )
    ):
        raise NumericQuestionValidationError("OpenAI generated a repeated numerical MCQ angle too similar to recent questions in this block.")

    with transaction.atomic():
        course_config = course.config.__class__.objects.select_for_update().get(pk=course.config.pk)
        course.config = course_config
        budget = course_question_generation_budget(course)
        if not budget.can_generate:
            raise QuestionGenerationUnavailableError(budget.message)

        current_hashes = set(course.question_bank_items.values_list("question_hash", flat=True))
        stem, item_hash = _resolve_unique_question_identity(
            stem,
            normalized_payload["question_type"],
            current_hashes,
        )
        if not stem or not item_hash:
            return None, None

        practice = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.PRACTICE,
            status=QuestionBankItem.Status.APPROVED,
            stem=stem,
            question_type=normalized_payload["question_type"],
            correct_answer=normalized_payload["correct_answers"][0],
            additional_correct_answers=normalized_payload["correct_answers"][1:],
            written_answer_keywords=normalized_payload["written_answer_keywords"],
            further_study_questions=normalized_payload["further_study_questions"],
            distractors=normalized_payload["distractors"],
            explanation=(
                normalized_payload["explanation"]
                if normalized_payload["question_type"] == QuestionBankItem.QuestionType.NUM
                else normalize_explanation_text(normalized_payload["explanation"])
            ),
            difficulty=normalized_payload["difficulty"],
            question_hash=item_hash,
            is_numerical=normalized_payload["question_type"] == QuestionBankItem.QuestionType.NUM,
            numeric_metadata=normalized_payload["numeric_metadata"],
            is_coding_question=normalized_payload["is_coding_question"],
            coding_language=normalized_payload["coding_language"],
            coding_question_kind=normalized_payload["coding_question_kind"],
            code_snippet=normalized_payload["code_snippet"],
        )
        validation = QuestionBankItem.objects.create(
            course=course,
            block=block,
            learning_objective=objective,
            source_chunk=chunk,
            bank_type=QuestionBankItem.BankType.VALIDATION,
            status=QuestionBankItem.Status.APPROVED,
            stem=stem,
            question_type=practice.question_type,
            correct_answer=practice.correct_answer,
            additional_correct_answers=practice.additional_correct_answers,
            written_answer_keywords=practice.written_answer_keywords,
            further_study_questions=practice.further_study_questions,
            distractors=practice.distractors,
            explanation=practice.explanation,
            difficulty=practice.difficulty,
            question_hash=hashlib.sha256(f"{item_hash}:validation".encode("utf-8")).hexdigest(),
            is_numerical=practice.is_numerical,
            numeric_metadata=practice.numeric_metadata,
            is_coding_question=practice.is_coding_question,
            coding_language=practice.coding_language,
            coding_question_kind=practice.coding_question_kind,
            code_snippet=practice.code_snippet,
            linked_question=practice,
        )
        practice.linked_question = validation
        practice.save(update_fields=["linked_question", "updated_at"])

    _trace_generation(
        "question_persisted",
        requested_type=QuestionBankItem.display_label_for_question_type(question_type),
        persisted_type=practice.question_type_label(),
        block=block.title,
        block_id=block.pk,
        chunk_id=(chunk.pk if chunk else None),
        objective=(objective.text if objective else ""),
        stem=practice.stem,
        correct_answer=practice.correct_answer,
        distractors=practice.distractors,
        is_coding_question=practice.is_coding_question,
    )
    existing_hashes.add(item_hash)
    return practice, validation


def generate_question_pair_for_block(
    block: CourseBlock,
    *,
    existing_hashes: set[str] | None = None,
    preferred_objective_ids: list[int] | None = None,
    strict_preferred_objectives: bool = False,
    question_type: str | None = None,
    raise_generation_errors: bool = False,
    include_future_blocks: bool = False,
    relax_similarity_checks: bool = False,
    allow_type_fallback: bool = False,
    allow_relaxed_objective_scope_fallback: bool = False,
):
    course = block.course
    existing_hashes = existing_hashes or set(course.question_bank_items.values_list("question_hash", flat=True))
    objectives_by_block = _released_objectives_by_block(course, include_future_blocks=include_future_blocks)
    objective_keywords = {
        objective.pk: _keyword_set(objective.text)
        for objectives in objectives_by_block.values()
        for objective in objectives
    }
    candidate_chunks = list(
        ContentChunk.objects.filter(block=block, asset__include_in_generation=True)
        .select_related("course", "block", "asset")
        .annotate(
            practice_question_count=Count(
                "question_bank_items",
                filter=Q(question_bank_items__bank_type=QuestionBankItem.BankType.PRACTICE),
            )
        )
        .order_by("practice_question_count", "asset__created_at", "ordinal", "pk")
    )
    if not candidate_chunks:
        return None, None

    coding_signals = {chunk.pk: signal for chunk, signal in _coding_chunks(candidate_chunks)}
    preferred_coding_language = preferred_coding_language_for_block(block, candidate_chunks, coding_signals)
    if preferred_coding_language:
        coding_signals = {
            chunk_id: signal
            for chunk_id, signal in coding_signals.items()
            if signal.get("language") == preferred_coding_language
        }
    explicit_question_type = question_type if question_type in {
        QuestionBankItem.QuestionType.MCQ,
        QuestionBankItem.QuestionType.NUM,
        QuestionBankItem.QuestionType.MAQ,
        QuestionBankItem.QuestionType.WAQ,
    } else None
    should_allow_type_fallback = allow_type_fallback and explicit_question_type is None
    ordered_question_types = _ordered_generation_question_types(
        block,
        explicit_question_type=explicit_question_type,
        allow_type_fallback=should_allow_type_fallback,
    )
    if (
        QuestionBankItem.QuestionType.NUM in ordered_question_types
        and explicit_question_type is None
        and not should_allow_type_fallback
        and not _block_has_suitable_numeric_path(block, candidate_chunks, objectives_by_block)
    ):
        ordered_question_types = [
            candidate_type
            for candidate_type in ordered_question_types
            if candidate_type != QuestionBankItem.QuestionType.NUM
        ]
        if not ordered_question_types:
            ordered_question_types = [QuestionBankItem.QuestionType.MCQ]
    _trace_generation(
        "question_generation_requested",
        requested_types=[QuestionBankItem.display_label_for_question_type(candidate_type) for candidate_type in ordered_question_types],
        block=block.title,
        block_id=block.pk,
        strict_preferred_objectives=strict_preferred_objectives,
        allow_relaxed_objective_scope_fallback=allow_relaxed_objective_scope_fallback,
    )

    strict_objectives = _ordered_generation_objectives(
        block,
        objectives_by_block,
        preferred_objective_ids,
        strict_preferred_objectives=strict_preferred_objectives,
    )
    fallback_objectives = (
        _ordered_generation_objectives(
            block,
            objectives_by_block,
            preferred_objective_ids,
            strict_preferred_objectives=False,
        )
        if allow_relaxed_objective_scope_fallback and strict_preferred_objectives
        else []
    )
    objective_scopes: list[tuple[str, list[LearningObjective], bool]] = [("preferred", strict_objectives, strict_preferred_objectives)]
    if fallback_objectives:
        objective_scopes.append(("block", fallback_objectives, False))

    attempted_type_errors: dict[str, str] = {}
    distractor_count = block.question_distractor_count
    for scope_name, ordered_objectives, scope_is_strict in objective_scopes:
        if not ordered_objectives:
            continue
        for candidate_type in ordered_question_types:
            _trace_generation(
                "question_generation_type_attempt",
                question_type=candidate_type,
                block=block.title,
                block_id=block.pk,
                scope=scope_name,
                strict_preferred_objectives=scope_is_strict,
            )
            practice, validation, attempt_error = _generate_question_pair_for_question_type(
                course=course,
                block=block,
                question_type=candidate_type,
                candidate_chunks=candidate_chunks,
                objectives_by_block=objectives_by_block,
                objective_keywords=objective_keywords,
                ordered_objectives=ordered_objectives,
                coding_signals=coding_signals,
                distractor_count=distractor_count,
                existing_hashes=existing_hashes,
                strict_preferred_objectives=scope_is_strict,
                relax_similarity_checks=relax_similarity_checks,
            )
            if practice is not None and validation is not None:
                _trace_generation(
                    "question_generation_succeeded",
                    question_type=practice.question_type,
                    block=block.title,
                    block_id=block.pk,
                    scope=scope_name,
                )
                return practice, validation
            if attempt_error:
                attempted_type_errors.setdefault(candidate_type, attempt_error)
                _trace_generation(
                    "question_generation_type_rejected",
                    question_type=candidate_type,
                    block=block.title,
                    block_id=block.pk,
                    scope=scope_name,
                    error=attempt_error,
                )
    last_generation_error = _format_attempted_type_errors(attempted_type_errors)
    if raise_generation_errors and last_generation_error:
        if explicit_question_type == QuestionBankItem.QuestionType.NUM:
            message = "Could not generate a numerical MCQ for this block."
        else:
            message = "Could not generate a high-quality question for this block."
        raise QuestionGenerationError(f"{message} {last_generation_error}".strip())
    return None, None


def generate_question_banks(course: Course, *, approve: bool = False) -> int:
    today = timezone.localdate()
    question_count = 0
    existing_hashes = set(course.question_bank_items.values_list("question_hash", flat=True))
    objectives_by_block = _released_objectives_by_block(course, today=today)
    objective_keywords = {
        objective.pk: _keyword_set(objective.text)
        for objectives in objectives_by_block.values()
        for objective in objectives
    }
    chunks = _ordered_released_chunks(course, today=today)
    total_chunks_by_block: dict[int, int] = defaultdict(int)
    chunk_index_by_block: dict[int, int] = defaultdict(int)
    for chunk in chunks:
        total_chunks_by_block[chunk.block_id] += 1

    for chunk in chunks:
        chunk_index_by_block[chunk.block_id] += 1
        distractor_count = chunk.block.question_distractor_count
        question_type = _preferred_generated_question_type(chunk.block)
        objective = _select_objective_for_chunk(
            chunk,
            objectives_by_block.get(chunk.block_id, []),
            objective_keywords,
            chunk_index_by_block[chunk.block_id] - 1,
            total_chunks_by_block[chunk.block_id],
        )
        if (
            question_type == QuestionBankItem.QuestionType.NUM
            and objective is not None
            and not _objective_supports_local_numeric_mcq(objective, chunk.text)
        ):
            question_type = _preferred_standard_generated_question_type(chunk.block)
        coding_signal = _coding_signal_for_chunk(chunk) if (_coding_question_generation_due(chunk.block) and question_type != QuestionBankItem.QuestionType.NUM) else {"language": "", "snippet": ""}
        preferred_coding_language = preferred_coding_language_for_block(chunk.block, [chunk], {chunk.pk: coding_signal} if coding_signal["language"] else {})
        if preferred_coding_language and coding_signal["language"] and coding_signal["language"] != preferred_coding_language:
            coding_signal = {"language": "", "snippet": ""}
        payload, effective_question_type, expected_coding_language = _payload_for_generation_attempt(
            chunk,
            objective,
            distractor_count,
            question_type,
            coding_signal=(coding_signal if coding_signal["language"] and coding_signal["snippet"] else None),
        )
        try:
            practice, validation = _create_question_pair(
                course=course,
                block=chunk.block,
                chunk=chunk,
                objective=objective,
                question_type=effective_question_type,
                payload=payload,
                existing_hashes=existing_hashes,
                expected_coding_language=expected_coding_language,
            )
        except QuestionGenerationUnavailableError:
            break
        if practice is not None and validation is not None:
            question_count += 2

    return question_count
