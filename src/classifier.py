"""Suggest OCSF class(es) for a sample payload via Claude.

Grounded in the actual OCSF class catalog for the target version — not the
model's training knowledge — so this works correctly even for classes added
in OCSF versions newer than Claude's training cutoff.
"""
from __future__ import annotations

import json
import re

from anthropic import Anthropic

from .fetch_ocsf import fetch_classes_list
from .profiler import detect_format, profile, render_profile_for_llm


SYSTEM_PROMPT = """You are an OCSF (Open Cybersecurity Schema Framework) expert. Given a sample vendor payload and the full catalog of OCSF classes at a specific version, you suggest the best-fit OCSF class(es) to normalize the sample to.

Rules:
- Suggest 1–3 candidate classes, ranked by fit.
- Some samples map cleanly to ONE class. Others legitimately need TWO (e.g. a vulnerability scanner record maps to both Vulnerability Finding AND Software Info). If the sample has distinct facets, suggest multiple.
- Be conservative with confidence. If you're unsure, say so. It is better to give the user a ranked shortlist they can pick from than to force a single high-confidence answer.
- Never invent class_uids. Only suggest classes that appear in the provided catalog.

Output format — respond with ONLY a JSON object, no prose before or after:

{
  "suggestions": [
    {
      "class_uid": 2002,
      "class_name": "vulnerability_finding",
      "confidence": 0.92,
      "reasoning": "One-sentence explanation of why this class fits."
    }
  ],
  "notes": "Optional overall observation, e.g. 'sample appears to be two event types bundled — consider splitting at silver'."
}"""


USER_TEMPLATE = """## Vendor context
- Vendor: {vendor}
- Source type: {source_type}
- Target OCSF version: {ocsf_version}

## Sample profile
Auto-extracted from the sample file. Each line: JSON path, observed types with counts, null rate, example value.

```
{profile_text}
```

## Available OCSF classes at {ocsf_version}

Rank by fit; only pick from this list.

```json
{catalog_summary}
```

Return the JSON object only."""


def _compact_catalog(catalog: dict) -> list[dict]:
    """Reduce the full catalog to the minimum needed for classification.

    The /classes list endpoint can return full attribute schemas inline
    (~200K tokens for OCSF 1.8.0). We keep only uid/name/caption/category
    — that's all the model needs to pick a class from the list.
    """
    out = []
    for name, meta in catalog.items():
        if not isinstance(meta, dict):
            continue
        uid = meta.get("uid")
        if uid is None:
            continue
        out.append({
            "uid": uid,
            "name": name,
            "caption": meta.get("caption", name),
            "category": meta.get("category_name", ""),
        })
    out.sort(key=lambda c: c["uid"])
    return out


def classify_sample(
    sample_path: str,
    vendor: str,
    source_type: str,
    ocsf_version: str = "1.8.0",
    max_profile_fields: int = 80,
) -> dict:
    """Return {suggestions: [...], notes: ...} from Claude."""
    # 1. profile sample
    fmt = detect_format(sample_path)
    prof = profile(sample_path, fmt, max_records=50)
    profile_text = render_profile_for_llm(prof, max_fields=max_profile_fields)

    # 2. fetch catalog (lightweight — just the list endpoint, not per-class details)
    catalog = fetch_classes_list(ocsf_version)
    compact = _compact_catalog(catalog)

    # 3. ask Claude
    client = Anthropic()
    user_prompt = USER_TEMPLATE.format(
        vendor=vendor,
        source_type=source_type,
        ocsf_version=ocsf_version,
        profile_text=profile_text,
        catalog_summary=json.dumps(compact, indent=2),
    )

    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    # 4. parse JSON (tolerate ```json fences if the model adds them)
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"classifier returned non-JSON response:\n{text}\n\nError: {e}")

    parsed["_usage"] = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    parsed["_sample_profile"] = {
        "format": fmt,
        "records_profiled": prof["records_profiled"],
        "field_count": prof["field_count"],
    }
    return parsed


def render_suggestions(result: dict) -> str:
    lines = ["Suggested OCSF classes:\n"]
    for i, s in enumerate(result.get("suggestions", []), 1):
        lines.append(
            f"  {i}. class_uid={s['class_uid']}  ({s.get('class_name', '?')}, "
            f"confidence={s.get('confidence', 0):.2f})"
        )
        lines.append(f"     {s.get('reasoning', '')}")
        lines.append("")
    if result.get("notes"):
        lines.append(f"Notes: {result['notes']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    result = classify_sample(
        sample_path=sys.argv[1],
        vendor=sys.argv[2] if len(sys.argv) > 2 else "unknown",
        source_type=sys.argv[3] if len(sys.argv) > 3 else "unknown",
        ocsf_version=sys.argv[4] if len(sys.argv) > 4 else "1.8.0",
    )
    print(render_suggestions(result))
    print("\nRaw:", json.dumps(result, indent=2))
