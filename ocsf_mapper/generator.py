"""Generate a complete Lakewatch preset from a sample via Claude.

Accepts ANY class_uid present in the OCSF schema cache. Resolves class name,
category, and full attribute schema (with nested objects inlined) from the
cache — no hardcoded class map.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from anthropic import Anthropic

from .profiler import detect_format, profile, render_profile_for_llm
from .schema_loader import resolve_class_compact, lookup_class, list_classes
from .ocsf_types import TYPE_MAPPING_PROMPT
from .validator import validate_preset_text, format_findings
from .advisory import load_advisory


SYSTEM_PROMPT = """You are an expert at writing Lakewatch data source presets that normalize vendor security data to OCSF (Open Cybersecurity Schema Framework) schemas. You are generating a preset.yaml for the Rearc security-content-library repo.

HARD CONVENTIONS (non-negotiable for this team):

1. Silver reads from `_raw` variant column directly. NEVER use `parse_json(_raw_json)` — the bronze layer already exposes `_raw` as a variant. Use syntax like `_raw:fieldName::string`.

2. Gold table names use `_02` suffix (e.g. `vulnerability_finding_02`) because the team has MODIFY but not ALTER/DROP permissions on cyber_prod.

3. Complex OCSF fields (nested objects, arrays of objects) must be emitted as `named_struct(...)` — produces typed STRUCTs in Delta DDL. Arrays of structs: `array(named_struct(...))`.

4. OCSF classification fields (class_uid, class_name, category_uid, category_name) use `literal:` — constants for the class.

5. Timestamps in silver: `try_to_timestamp(_raw:field::string)` for ISO strings.

6. Always emit an `unmapped` field at the end of gold holding vendor-specific fields with no OCSF home. Pattern: `parse_json(to_json(named_struct(...)))`.

7. Always emit `raw_data` = `to_json(_raw)` in silver. Gold `raw_data` is typically VARIANT — use `parse_json(raw_data)`.

8. severity_id mapping follows the OCSF severity enum provided in the schema. Typically: critical=5, high=4, medium=3, low=2, informational=1, else 0.

9. Filter clause uses bronze routing column `eventCategory` with the vendor's event type value.

OUTPUT FORMAT:
- One complete preset.yaml in ```yaml ... ``` fences
- Before the YAML: `### Generation notes` markdown with:
  - The OCSF class mapped to and one-line rationale
  - Low-confidence mappings (also marked `# TODO:` inline in YAML)
  - Vendor fields left in `unmapped`
- Low-confidence fields: best-guess with `# TODO: verify — reason` inline. Never omit silently.
- Gold field ordering: classification → activity → type → time → severity → status → class-specific payload → observables → resources → enrichments → metadata → raw_data → unmapped

DO NOT invent OCSF field names. The provided OCSF schema is authoritative — only use attribute names that appear in it. Fields without an OCSF home belong in `unmapped`.

""" + TYPE_MAPPING_PROMPT


USER_TEMPLATE = """Generate a complete Lakewatch preset.yaml for a new vendor integration.

## Vendor context
- Vendor: {vendor}
- Source type: {source_type}
- Target OCSF version: {ocsf_version}
- Target OCSF class(es): {target_classes_summary}

## Sample profile
Auto-profiled from the sample. Each line: path, observed types with counts, null rate, example value.

```
{profile_text}
```

## Detected input format
- Format: `{fmt_format}`
- Record path: `{fmt_record_path}`
- Notes: {fmt_notes}

Set `autoloader.format` accordingly. If `json_wrapped`, note that bronze must explode the wrapper array; silver receives one record per array element.

## Target OCSF schemas

{target_classes_schemas}

## Multi-class output shape

{multi_class_guidance}

## Preset conventions

Authoritative instructions for this preset. Follow them exactly. They
OVERRIDE any conflicting pattern in the style reference below.

{advisory}

## Style reference: existing team preset

Follows team conventions. Mirror its structure — field ordering, named_struct shapes, unmapped pattern. Do NOT copy silver expressions verbatim; they are vendor-specific.

```yaml
{style_reference}
```

## Bronze context
Bronze exposes records as `_raw` (variant) plus promoted columns: `id`, `time`, `eventCategory`, `collected_at`. Filter on `eventCategory` = vendor's event type.

## Version propagation
Anywhere the preset needs to embed the OCSF schema version (most commonly in the gold `metadata.version` literal), use **{ocsf_version}**. If the style reference has a different version hardcoded, IGNORE it and use {ocsf_version} throughout.

Produce the complete preset.yaml now. `### Generation notes` first, then YAML in fences."""


_MULTI_GUIDANCE_SINGLE = """Produce ONE preset.yaml with:
- Header (name, author, title, description, version, iconURL, primaryKey, autoloader)
- `bronze:` block
- ONE `silver:` transform
- ONE entry under `gold:` for the target class above"""

_MULTI_GUIDANCE_MULTI = """Produce ONE preset.yaml with:
- Header (name, author, title, description, version, iconURL, primaryKey, autoloader)
- `bronze:` block
- ONE `silver:` transform — its fields must cover everything needed by ALL gold blocks. Don't duplicate silver per class.
- MULTIPLE entries under `gold:` — one per target class. Each reads from the same silver table. Each has its own `name` (use the `_02` suffix convention) and `input` pointing at the silver table.
- Silver fields should be named generically enough to feed all gold classes. If two classes need the same vendor attribute in different OCSF positions, extract it ONCE in silver and reference it from each gold block."""


def extract_yaml(response_text: str) -> tuple[str, str]:
    m = re.search(r"```ya?ml\s*\n(.*?)```", response_text, re.DOTALL)
    if not m:
        raise ValueError("no yaml fence found in response")
    return response_text[:m.start()].strip(), m.group(1)


def generate_preset(
    sample_path: str,
    vendor: str,
    source_type: str,
    class_uids: list[int] | int,
    ocsf_version: str = "1.8.0",
    style_reference_path: str | None = None,
    reference_dir: str | None = None,
    advisory_dir: str | None = None,
    max_references: int = 2,
    out_dir: str = "./output",
    max_profile_fields: int = 120,
    cache_dir: str | None = None,
    progress_callback=None,
) -> dict:
    """Generate a single preset.yaml that maps the vendor sample to one or more OCSF classes.

    class_uids: int or list of ints. When multiple are passed, the generated preset
                has one silver transform and multiple gold blocks (one per class).
    reference_dir: Volume path to a directory of existing presets. If provided,
                   the selector picks up to `max_references` most-relevant ones
                   (by class_uid match). Overrides style_reference_path when set.
    """
    from .reference_library import select_references, describe_selection

    # normalize to list
    if isinstance(class_uids, int):
        class_uids = [class_uids]
    if not class_uids:
        raise ValueError("class_uids cannot be empty")

    # resolve all classes
    entries = []
    schemas = {}
    for uid in class_uids:
        e = lookup_class(ocsf_version, uid, cache_dir=cache_dir)
        entries.append({"uid": uid, **e})
        schemas[uid] = resolve_class_compact(ocsf_version, uid, cache_dir=cache_dir)

    target_classes_summary = ", ".join(
        f"**{e['uid']} — {e['caption']}** ({e.get('category_name', '?')})"
        for e in entries
    )

    schema_blocks = []
    for uid, s in schemas.items():
        schema_blocks.append(
            f"### Class {uid} — {s['caption']} (category: {s.get('category_name', '?')})\n\n"
            f"```json\n{json.dumps(s, indent=2)}\n```"
        )
    target_classes_schemas = "\n\n".join(schema_blocks)

    multi_class_guidance = _MULTI_GUIDANCE_MULTI if len(class_uids) > 1 else _MULTI_GUIDANCE_SINGLE

    # profile sample
    fmt = detect_format(sample_path)
    prof = profile(sample_path, fmt, max_records=100)
    profile_text = render_profile_for_llm(prof, max_fields=max_profile_fields)

    # Build reference block — either from library (selector picks) or single file
    reference_paths: list[Path] = []
    selection_description = ""
    if reference_dir:
        reference_paths = select_references(
            reference_dir=reference_dir,
            target_class_uids=class_uids,
            max_refs=max_references,
            fallback_path=style_reference_path,
        )
        selection_description = describe_selection(reference_dir, class_uids, max_references)
    elif style_reference_path and Path(style_reference_path).exists():
        reference_paths = [Path(style_reference_path)]

    if reference_paths:
        ref_blocks = []
        for rp in reference_paths:
            try:
                content = rp.read_text()
                ref_blocks.append(
                    f"### Reference preset: {rp.name}\n\n"
                    f"```yaml\n{content}\n```"
                )
            except Exception:
                continue
        style_ref = "\n\n".join(ref_blocks)
    else:
        style_ref = ""

    # Load preset-convention instructions from the advisory folder.
    # A missing/empty advisory folder is fine — yields "".
    advisory_text = load_advisory(advisory_dir)

    client = Anthropic()
    user_prompt = USER_TEMPLATE.format(
        vendor=vendor,
        source_type=source_type,
        ocsf_version=ocsf_version,
        target_classes_summary=target_classes_summary,
        profile_text=profile_text,
        fmt_format=fmt["format"],
        fmt_record_path=fmt["record_path"] or "(none — records are top-level)",
        fmt_notes=fmt["notes"],
        target_classes_schemas=target_classes_schemas,
        multi_class_guidance=multi_class_guidance,
        advisory=advisory_text or "(no advisory file configured)",
        style_reference=style_ref or "(no style reference — follow system-prompt conventions)",
    )

    # Stream the response so the caller can show live progress.
    text_chunks: list[str] = []
    input_tokens = 0
    output_tokens = 0

    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            text_chunks.append(chunk)
            if progress_callback:
                try:
                    progress_callback("generate_token", chunk)
                except Exception:
                    pass
        final = stream.get_final_message()
        input_tokens = final.usage.input_tokens
        output_tokens = final.usage.output_tokens

    text = "".join(text_chunks)

    # Backwards-compat shim — older code expects resp.usage.{input,output}_tokens
    class _Usage:
        pass
    class _Resp:
        pass
    resp = _Resp()
    resp.usage = _Usage()
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens

    notes, yaml_body = extract_yaml(text)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "preset.yaml").write_text(yaml_body)

    # Validate the generated preset for OCSF data-type violations
    # (timestamp_t / int_t / long_t). Findings go into the report so the
    # reviewer sees them; they do NOT block generation.
    type_findings = validate_preset_text(yaml_body)
    type_report = format_findings(type_findings)

    # ── Build a simplified, verdict-first report ─────────────────────────────
    # Layout: status line → what to fix → what the model did → details.
    n_errors = sum(1 for f in type_findings if f.level == "error")
    n_warnings = sum(1 for f in type_findings if f.level == "warning")
    if n_errors:
        verdict = f"⚠️  Needs fixes — {n_errors} type error(s) before this preset is valid."
    elif n_warnings:
        verdict = f"✓  Generated — {n_warnings} warning(s) worth a look, no blocking errors."
    else:
        verdict = "✓  Generated cleanly — no OCSF data-type issues found."

    classes_line = ", ".join(f"{e['uid']} {e['caption']}" for e in entries)
    refs_line = (
        ", ".join(p.name for p in reference_paths)
        if reference_paths else "none"
    )

    report = f"""# {vendor} / {source_type} — generation report

{verdict}

## What to check

{type_report}

## What was generated

- OCSF class(es): {classes_line}
- OCSF version: {ocsf_version}
- Mapped from {prof['records_profiled']} sample record(s), {prof['field_count']} distinct fields

## Model notes

{notes}

<details>
<summary>Run details</summary>

- Sample format: `{fmt['format']}` — {fmt['notes']}
- Record path: `{fmt['record_path']}`
- Style references: {refs_line}
- Model: claude-sonnet-4-5
- Tokens: {resp.usage.input_tokens} in / {resp.usage.output_tokens} out
</details>
"""
    (out / "generation_report.md").write_text(report)

    return {
        "preset_path": str(out / "preset.yaml"),
        "report_path": str(out / "generation_report.md"),
        "classes": [{"uid": e["uid"], "name": e["name"], "caption": e["caption"]} for e in entries],
        "format": fmt,
        "profile_summary": {"records": prof["records_profiled"], "fields": prof["field_count"]},
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
        "references_used": [str(p) for p in reference_paths],
        "reference_selection_log": selection_description,
        "type_findings": [
            {"level": f.level, "gold_table": f.gold_table, "field": f.field, "message": f.message}
            for f in type_findings
        ],
    }


def cli():
    import argparse
    ap = argparse.ArgumentParser(description="Generate a Lakewatch preset from a sample")
    ap.add_argument("sample", nargs="?", help="path to sample file")
    ap.add_argument("--vendor")
    ap.add_argument("--source-type")
    ap.add_argument("--class-uid", type=str,
                    help="OCSF class_uid, or comma-separated list (e.g. 2002,5020). Run --list-classes to see options.")
    ap.add_argument("--ocsf-version", default="1.8.0")
    ap.add_argument("--style-reference", default=None)
    ap.add_argument("--out-dir", default="./output")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--list-classes", action="store_true",
                    help="list available classes in the cache and exit")
    args = ap.parse_args()

    if args.list_classes:
        for c in list_classes(args.ocsf_version, cache_dir=args.cache_dir):
            print(f"  {c['uid']:>6}  {c['name']:<36}  {c['caption']}")
        return

    missing = [f for f in ["sample", "vendor", "source_type", "class_uid"]
               if getattr(args, f) in (None, "")]
    if missing:
        ap.error(f"missing required args: {missing}")

    class_uids = [int(x) for x in args.class_uid.split(",")]

    result = generate_preset(
        sample_path=args.sample,
        vendor=args.vendor,
        source_type=args.source_type,
        class_uids=class_uids,
        ocsf_version=args.ocsf_version,
        style_reference_path=args.style_reference,
        out_dir=args.out_dir,
        cache_dir=args.cache_dir,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    cli()