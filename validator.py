"""Post-generation validator — scans a generated preset.yaml for OCSF
data-type violations before it ever reaches a reviewer or the platform.

This catches the two recurring bug classes:

  1. timestamp_t fields emitted as Spark TIMESTAMP instead of BIGINT epoch-ms.
  2. int_t / long_t classification fields emitted as STRING, or `type_uid`
     emitted as INT instead of BIGINT.

It is deliberately lightweight: it does NOT parse SQL. It walks the parsed
YAML, looks at gold field expressions, and flags patterns that are almost
always wrong. False positives are possible but rare; every finding includes
the field name so a human can confirm.

Usage:
    from ocsf_mapper.validator import validate_preset_text
    findings = validate_preset_text(yaml_text)
    for f in findings:
        print(f.level, f.field, f.message)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


@dataclass
class Finding:
    """A single validation finding."""
    level: str        # "error" | "warning"
    gold_table: str    # which gold block, e.g. "vulnerability_finding_02"
    field: str         # OCSF field name
    message: str       # human-readable explanation

    def __str__(self) -> str:
        icon = "✗" if self.level == "error" else "⚠"
        return f"{icon} [{self.gold_table}.{self.field}] {self.message}"


# ─────────────────────────────────────────────────────────────────────────────
# Heuristic patterns
# ─────────────────────────────────────────────────────────────────────────────

# OCSF classification fields that must be integers, never strings.
_INT_FIELDS = {
    "class_uid", "category_uid", "activity_id", "severity_id",
    "type_id", "timezone_offset", "disposition_id", "confidence_id",
    "impact_id", "risk_level_id", "status_id",
}

# Fields that must be BIGINT (OCSF long_t).
_LONG_FIELDS = {"type_uid", "raw_data_size"}

# A bare CAST(... AS STRING) on a time-ish field usually means a datetime_t
# was produced with the wrong (space-separated) format.
_TIME_NAME_RE = re.compile(r"(_time$|^time$|_dt$|_at$)")

# Detect a Spark TIMESTAMP cast — wrong for timestamp_t (which is BIGINT ms).
_TIMESTAMP_CAST_RE = re.compile(r"\bAS\s+TIMESTAMP\b", re.IGNORECASE)
# Detect a string literal value for a field (means it was quoted).
_STRING_AS_RE = re.compile(r"\bAS\s+STRING\b", re.IGNORECASE)


def _expr_of(field_def: dict) -> str:
    """Return the SQL expression for a gold field def, whether it uses
    `expr:` or `literal:`."""
    if not isinstance(field_def, dict):
        return ""
    if "expr" in field_def:
        return str(field_def["expr"])
    if "literal" in field_def:
        return str(field_def["literal"])
    return ""


def _is_quoted_literal(field_def: dict) -> bool:
    """True if the field uses `literal:` with a string-looking value."""
    if not isinstance(field_def, dict) or "literal" not in field_def:
        return False
    val = field_def["literal"]
    # YAML parsed "5020" (quoted) and 5020 (bare) both — but a quoted numeric
    # comes back as a str, a bare one as an int. That distinction is exactly
    # what we want to flag.
    return isinstance(val, str)


def validate_preset_text(yaml_text: str) -> list[Finding]:
    """Parse a preset.yaml string and return a list of type-violation findings.

    Returns an empty list if the preset is clean (or if PyYAML is unavailable
    and parsing fails — in that case a single warning is returned instead).
    """
    if yaml is None:
        return [Finding("warning", "-", "-",
                        "PyYAML not installed — validation skipped.")]
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return [Finding("error", "-", "-", f"preset is not valid YAML: {e}")]

    if not isinstance(doc, dict):
        return [Finding("error", "-", "-", "preset root is not a mapping")]

    findings: list[Finding] = []
    gold_blocks = doc.get("gold") or []
    if isinstance(gold_blocks, dict):       # tolerate single-block shorthand
        gold_blocks = [gold_blocks]

    for block in gold_blocks:
        if not isinstance(block, dict):
            continue
        table = block.get("name", "<unnamed gold table>")
        for field_def in block.get("fields", []) or []:
            if not isinstance(field_def, dict):
                continue
            name = field_def.get("name", "<unnamed>")
            expr = _expr_of(field_def)

            # ── Rule 1: int_t classification fields must not be STRING ───────
            if name in _INT_FIELDS:
                if _is_quoted_literal(field_def):
                    findings.append(Finding(
                        "error", table, name,
                        f"`{name}` is OCSF int_t — emit a bare integer "
                        f"(literal: {field_def['literal']}, no quotes), not a "
                        f"quoted string.",
                    ))
                elif _STRING_AS_RE.search(expr):
                    findings.append(Finding(
                        "error", table, name,
                        f"`{name}` is OCSF int_t but is CAST AS STRING. "
                        f"Cast to INT.",
                    ))

            # ── Rule 2: long_t fields must be BIGINT ─────────────────────────
            if name in _LONG_FIELDS:
                if "BIGINT" not in expr.upper() and not _is_int_literal(field_def):
                    findings.append(Finding(
                        "error", table, name,
                        f"`{name}` is OCSF long_t — must be CAST(... AS BIGINT). "
                        f"INT can overflow.",
                    ))

            # ── Rule 3: timestamp_t fields should be BIGINT, not TIMESTAMP ───
            # Heuristic: a *_time field cast AS TIMESTAMP.
            if _TIME_NAME_RE.search(name) and _TIMESTAMP_CAST_RE.search(expr):
                findings.append(Finding(
                    "warning", table, name,
                    f"`{name}` looks like an OCSF timestamp_t but is CAST AS "
                    f"TIMESTAMP. OCSF timestamp_t is BIGINT epoch-millis — "
                    f"use unix_millis(...). (Confirm against the class schema.)",
                ))

            # ── Rule 4: datetime_t via bare CAST AS STRING → bad format ──────
            # A *_dt field, or a time field cast to STRING with no date_format.
            if name.endswith("_dt") and _STRING_AS_RE.search(expr) \
                    and "date_format" not in expr.lower():
                findings.append(Finding(
                    "warning", table, name,
                    f"`{name}` is OCSF datetime_t — a bare CAST AS STRING "
                    f"yields a space-separated value that fails the RFC-3339 "
                    f"validator. Use date_format(..., "
                    f"\"yyyy-MM-dd'T'HH:mm:ss.SSSXXX\").",
                ))

    return findings


def _is_int_literal(field_def: dict) -> bool:
    """True if the field uses `literal:` with a bare integer value."""
    return (
        isinstance(field_def, dict)
        and "literal" in field_def
        and isinstance(field_def["literal"], int)
    )


def format_findings(findings: list[Finding]) -> str:
    """Render findings as a markdown block for the generation report."""
    if not findings:
        return "✓ No OCSF data-type violations detected."
    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]
    lines = [
        f"Found {len(errors)} error(s) and {len(warnings)} warning(s):",
        "",
    ]
    for f in findings:
        lines.append(f"- {f}")
    return "\n".join(lines)
