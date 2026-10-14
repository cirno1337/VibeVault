"""Secret redaction. Stdlib only so collectors can import it without server dependencies.

IMPORTANT: this is best-effort pattern matching. It will miss secrets that do not look like
the patterns below (and will occasionally redact harmless text). It is a safety net, not a
guarantee. See docs/privacy.md.

Redaction results record the *rule name and count* only, never the matched secret.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED:{rule}]"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0  # which capture group holds the secret (0 = whole match)


def _r(name: str, pattern: str, group: int = 0, flags: int = 0) -> Rule:
    return Rule(name, re.compile(pattern, flags), group)


DEFAULT_RULES: tuple[Rule, ...] = (
    _r(
        "private_key",
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    ),
    _r("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    _r("openai_key", r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}"),
    _r("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    _r("gitlab_token", r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    _r("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _r("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    _r("groq_key", r"\bgsk_[A-Za-z0-9]{20,}"),
    _r("slack_token", r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    _r("stripe_key", r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    _r("jwt", r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    _r("bearer_token", r"(?i)\bbearer\s+([A-Za-z0-9._~+/\-]{16,}=*)", group=1),
    _r("url_credentials", r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:([^\s/@]{3,})@", group=1, flags=re.I),
    _r(
        "secret_assignment",
        # password=..., api_key: "...", export SECRET_TOKEN=...  (value 6+ chars, not a placeholder)
        r"(?i)\b[\w.\-]*(?:password|passwd|pwd|secret|api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token|token|credential)s?\b"
        r"\s*[:=]\s*[\"']?([^\s\"'`,;&}\]]{6,})",
        group=1,
    ),
)

# Dict keys whose string values are always redacted regardless of content.
SENSITIVE_KEYS = re.compile(
    r"(?i)^(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|password|passwd|"
    r"secret|client_secret|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|private[_-]?key)$"
)

_PLACEHOLDER_VALUES = re.compile(
    r"(?i)^(?:\*+|x{3,}|<[^>]*>|\$\{?[A-Z_]+\}?|\$[A-Za-z_]+|\[REDACTED[^\]]*\]|your[_\-].*|changeme|change-me|example|none|null|true|false)$"
)


@dataclass
class RedactionReport:
    counts: Counter[str] = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def status(self) -> str:
        return "redacted" if self.total else "none"

    def merge(self, other: RedactionReport) -> None:
        self.counts.update(other.counts)

    def as_dict(self) -> dict[str, int]:
        return dict(self.counts)


class Redactor:
    def __init__(self, extra_rules: list[Rule] | None = None, use_defaults: bool = True):
        self.rules: list[Rule] = [*(DEFAULT_RULES if use_defaults else ()), *(extra_rules or [])]

    # --- configuration ---------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path | None) -> Redactor:
        """Load extra rules from JSON: [{"name": "...", "pattern": "regex", "group": 0}, ...]."""
        extra: list[Rule] = []
        if path:
            for item in json.loads(Path(path).read_text()):
                extra.append(
                    Rule(
                        name=str(item["name"]),
                        pattern=re.compile(item["pattern"]),
                        group=int(item.get("group", 0)),
                    )
                )
        return cls(extra)

    # --- text ------------------------------------------------------------
    def redact_text(self, text: str, report: RedactionReport | None = None) -> str:
        report = report if report is not None else RedactionReport()
        for rule in self.rules:

            def sub(m: re.Match[str], rule: Rule = rule) -> str:
                secret = m.group(rule.group)
                if secret is None or _PLACEHOLDER_VALUES.match(secret):
                    return m.group(0)
                report.counts[rule.name] += 1
                marker = REDACTED.format(rule=rule.name)
                if rule.group == 0:
                    return marker
                start, end = m.span(rule.group)
                base = m.start()
                whole = m.group(0)
                return whole[: start - base] + marker + whole[end - base :]

            text = rule.pattern.sub(sub, text)
        return text

    # --- structured data -------------------------------------------------
    def redact_obj(self, obj: Any, report: RedactionReport | None = None) -> Any:
        """Recursively redact strings in JSON-like data. Returns a new object."""
        report = report if report is not None else RedactionReport()
        if isinstance(obj, str):
            return self.redact_text(obj, report)
        if isinstance(obj, list):
            return [self.redact_obj(v, report) for v in obj]
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                if isinstance(k, str) and SENSITIVE_KEYS.match(k) and isinstance(v, str) and v:
                    if not _PLACEHOLDER_VALUES.match(v):
                        report.counts["sensitive_key"] += 1
                        out[k] = REDACTED.format(rule="sensitive_key")
                        continue
                out[k] = self.redact_obj(v, report)
            return out
        return obj


_default: Redactor | None = None


def default_redactor() -> Redactor:
    global _default
    if _default is None:
        _default = Redactor()
    return _default
