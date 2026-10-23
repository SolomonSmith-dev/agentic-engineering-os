"""Redact secrets out of tool-call arguments before they reach the run record.

Two layers: any value under a credential-looking key is replaced whole, and any
string is scrubbed for known token shapes and `NAME=value` assignments whose
name looks like a credential. Redaction runs before truncation, so a secret is
never stored in part either.
"""

import json
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"
MAX_SUMMARY_CHARS = 500

_SENSITIVE_KEY = re.compile(
    r"(token|secret|passw(or)?d|passphrase|api[_-]?key|private[_-]?key|credential|"
    r"authorization|auth|cookie|session[_-]?id|^key$)",
    re.IGNORECASE,
)

_TOKEN_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic API and OAuth tokens
    re.compile(r"sk-(proj-)?[A-Za-z0-9_\-]{20,}"),  # OpenAI-style keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),  # Slack
    re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),  # Google API keys
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWTs
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+=*"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{8,}=*"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # user:password@ in URLs
)

# FOO_TOKEN=value, --api-key value, --password=value, "password": "value" and the
# escaped \"password\":\"value\" form inside a string. A quoted value is redacted up
# to its closing quote, so a value with spaces is not stored in part.
_ASSIGNMENT = re.compile(
    r"(?P<name>[A-Za-z0-9_\-]*(token|secret|passw(or)?d|passphrase|api[_-]?key|private[_-]?key|credential)"
    r"[A-Za-z0-9_\-]*)(?P<q1>\\?[\"']?)(?P<sep>\s*[=:]\s*|\s+)"
    r"(?:(?P<quote>\\?[\"'])(?P<qvalue>.*?)(?P=quote)|(?P<value>[^\s\"',;&|\\]+))",
    re.IGNORECASE | re.DOTALL,
)


def _redact_assignment(m: re.Match[str]) -> str:
    prefix = f"{m['name']}{m['q1']}{m['sep']}"
    if m["quote"] is not None:
        if m["qvalue"] in ("", REDACTED):
            return m[0]
        return f"{prefix}{m['quote']}{REDACTED}{m['quote']}"
    if m["value"] == REDACTED:
        return m[0]
    return f"{prefix}{REDACTED}"


def redact_text(text: str, extra_patterns: Iterable[str] = ()) -> str:
    """Scrub token shapes, credential assignments and any extra policy patterns from one string."""
    for pattern in _TOKEN_SHAPES:
        text = pattern.sub(REDACTED, text)
    text = _ASSIGNMENT.sub(_redact_assignment, text)
    for extra in extra_patterns:
        text = re.sub(extra, REDACTED, text)
    return text


def redact_value(value: Any, extra_patterns: Iterable[str] = ()) -> Any:
    """Recursively redact a JSON-like value: credential keys lose their value, strings are scrubbed."""
    extra = tuple(extra_patterns)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SENSITIVE_KEY.search(name) and item not in (None, "", REDACTED):
                out[name] = REDACTED
            else:
                out[name] = redact_value(item, extra)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item, extra) for item in value]
    if isinstance(value, str):
        return redact_text(value, extra)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(repr(value), extra)


def summarize_args(args: Any, extra_patterns: Iterable[str] = (), limit: int = MAX_SUMMARY_CHARS) -> str:
    """Redact, then serialize to compact sorted JSON, then truncate. The only form args are stored in."""
    redacted = redact_value(args, extra_patterns)
    text = json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr)
    # Serialization can join fragments a per-string pass saw apart; scrub the whole once more.
    text = redact_text(text, extra_patterns)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text
