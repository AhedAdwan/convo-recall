"""Secret redaction for ingested message content.

Replaces well-known credential token shapes with stable `«REDACTED-…»`
placeholders before content reaches the FTS / vector index. Always-on by
default; opt out with `CONVO_RECALL_REDACT=off`.

The pattern set is deliberately narrow: each regex matches a credential
shape that is unambiguously a secret (long, structured, prefixed). This
avoids false positives on prose that incidentally looks key-shaped.
"""

import re

# Order matters only in that more-specific Anthropic `sk-ant-` precedes the
# generic OpenAI `sk-` so the Anthropic placeholder is what users see for
# Anthropic keys. The patterns themselves don't overlap (each prefix is
# distinct), but applying specific-to-generic is the safer default.
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r'sk-ant-(?:api\d+-)?[A-Za-z0-9_-]{20,}'),
     '«REDACTED-ANTHROPIC-KEY»'),
    (re.compile(r'sk-[A-Za-z0-9]{20,}'),
     '«REDACTED-OPENAI-KEY»'),
    (re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
     '«REDACTED-GITHUB-TOKEN»'),
    (re.compile(r'AKIA[0-9A-Z]{16}'),
     '«REDACTED-AWS-KEY»'),
    (re.compile(r'eyJ[A-Za-z0-9_-]+?\.[A-Za-z0-9_-]+?\.[A-Za-z0-9_-]+'),
     '«REDACTED-JWT»'),
    (re.compile(r'xox[baprs]-[A-Za-z0-9-]{10,}'),
     '«REDACTED-SLACK-TOKEN»'),
)


def redact_secrets(text: str) -> str:
    """Apply all redaction patterns. Returns the redacted text."""
    for pattern, placeholder in _PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


def scan_secrets(text: str) -> dict[str, int]:
    """Count matches per pattern. Used by `recall doctor --scan-secrets`."""
    counts: dict[str, int] = {}
    for pattern, placeholder in _PATTERNS:
        n = len(pattern.findall(text))
        if n:
            counts[placeholder] = counts.get(placeholder, 0) + n
    return counts
