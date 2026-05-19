# OCSF Mapper

A Databricks-hosted compiler for OCSF presets. Point it at a vendor sample, and it produces a complete Lakewatch preset — bronze ingestion, silver field extraction, gold OCSF normalization — ready for review.

> **Status:** working internal tool. Generates production-quality presets and validates them against OCSF data-type rules. Deployment to Lakewatch is manual.

## What changed in v4 / `ocsf_mapper` 0.4.0

- **Submit-for-review / PR flow removed.** The app no longer stages submissions or opens PRs. It generates, validates, and lets you **Save to Volume** or **Download**. Moving a preset into the repo is now a separate, explicit step done outside this tool.
- **OCSF data-type correctness.** The generator is now told the authoritative OCSF→Spark type mapping, and every generated preset is validated for type violations before you see it.
- **OCSF version default corrected** to `1.7.0` (`1.8.0` does not exist).

## Quick Start

Open the app, then in the Generator tab:

```
Sample path:    /Volumes/dsl_dev/internal/ocsf_mapper/samples/snyk_vulns.jsonl
Vendor:         snyk (example)
Source type:    vulnerabilities (example)
[Generate preset]
```

The tool classifies the sample to the relevant OCSF class, fetches the schema, generates the preset using existing presets in the library as style references, and runs a data-type validation pass. Review and edit in-app, then **Save to Volume** or **Download**.

## What it does

Onboarding a new security data source means mapping vendor telemetry to the canonical OCSF schema — reading sample events by hand, picking the right OCSF class, writing bronze→silver→gold SQL transforms, validating against `schema.ocsf.io`, and iterating. For a single source this takes hours to days.

OCSF Mapper compresses this into an in-app workflow:

| Stage | What the tool does |
|-------|--------------------|
| **Classify** | Profiles the sample's structure and uses Claude to select the appropriate OCSF class UIDs |
| **Fetch schema** | Pulls class attributes from `schema.ocsf.io` and caches them locally |
| **Build preset** | Generates a complete bronze/silver/gold preset using reference presets as style anchors, with OCSF→Spark types enforced |
| **Validate** | Scans the generated preset for OCSF data-type violations (timestamp / integer) and reports them |

## OCSF data-type correctness

Two recurring bug classes used to slip through because the generator inferred Spark types from reference presets, and those references were written against pre-existing `cyber_prod` DDLs that predate OCSF type corrections:

1. **Timestamp.** OCSF `timestamp_t` is an **integer** — epoch milliseconds. It was being emitted as Spark `TIMESTAMP`. The RFC-3339 string sibling `datetime_t` was conflated with it. The OCSF validator rejects both.
2. **Integer.** OCSF changed `type_uid` (and a few others) from `int_t` to `long_t`. Presets emitting `INT` overflow or fail validation. Separately, classification fields (`class_uid`, `category_uid`, `activity_id`, `timezone_offset`) were sometimes emitted as quoted `STRING`s.

Two new modules fix this:

- **`ocsf_mapper/ocsf_types.py`** — the single source of truth mapping OCSF data types to Spark SQL types (`timestamp_t→BIGINT`, `datetime_t→STRING`, `int_t→INT`, `long_t→BIGINT`, …). Its `TYPE_MAPPING_PROMPT` is injected into the generator's system prompt, so Claude is *told* the mapping instead of guessing. A name-based override forces `type_uid` to `BIGINT` even if a stale schema cache still calls it `int_t`.
- **`ocsf_mapper/validator.py`** — `validate_preset_text()` parses the generated YAML and flags type violations (quoted int literals, `INT` `type_uid`, `STRING` classification fields, `TIMESTAMP` casts on `timestamp_t` fields, bad `datetime_t` formatting). Findings are written into the generation report and surfaced in the Generator tab.

Validation is advisory — it reports, it does not block — so you can still review and edit before saving.

## Generation Pipeline

```
              Vendor Sample
                    |
                    v
  Profile (detect format, extract fields)
                    |
                    v
  Classify (Claude picks OCSF class UIDs)
                    |
                    v
  Fetch Schema (schema.ocsf.io --> local cache)
                    |
                    v
  Build Preset (Claude + reference YAMLs + OCSF type table)
                    |
                    v
  Validate (scan for OCSF datatype violations)
                    |
                    v
          preset.yaml + report.md
```

## Repository Layout

```
tools/ocsf-mapper/
├── README.md                  # this file
├── app.py                     # Streamlit entrypoint
├── app.yaml                   # Databricks Apps runtime config
├── config.toml                # Streamlit theme
├── pyproject.toml             # package build config
├── requirements.txt           # Python dependencies
├── src/ocsf_mapper/           # the ocsf_mapper package (source, not a wheel)
│   ├── __init__.py
│   ├── pipeline.py            # classify → fetch → generate orchestration
│   ├── classifier.py          # Claude picks OCSF class UIDs
│   ├── fetch_ocsf.py          # pull + cache OCSF schema
│   ├── schema_loader.py       # read cache, inline object refs
│   ├── generator.py           # Claude generates the preset
│   ├── ocsf_types.py          # NEW — OCSF→Spark type mapping
│   ├── validator.py           # NEW — preset datatype validation
│   ├── profiler.py            # sample format detection + profiling
│   ├── reference_library.py   # style-reference selection
│   ├── cli.py                 # `ocsf-mapper` console command
│   └── style_reference.yaml   # bundled fallback style anchor
└── tests/
    └── test_types_and_validator.py
```

The `ocsf_mapper` package is now plain source under `src/`, not a bundled `.whl`. To rebuild a wheel for distribution: `python -m build --wheel`.

## Configuration

Sidebar settings the user controls:

| Setting | Default | Notes |
|---------|---------|-------|
| Anthropic API key | — | Required; not persisted |
| OCSF version | `1.7.0` | Validated against `schema.ocsf.io` |
| Reference library | `/Volumes/dsl_dev/internal/ocsf_mapper/preset_library` | Style anchors for generation |
| Output volume | `/Volumes/dsl_dev/internal/ocsf_mapper/generated_presets` | "Save to Volume" target |

## Deploying

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| Databricks workspace | Apps must be enabled |
| UC Volume access | Read on `/Volumes/dsl_dev/internal/ocsf_mapper/` |
| Anthropic API key | Entered in the sidebar at runtime |

### Deploy from this repo

1. Clone this repository as a Databricks Git folder (`Workspace → Create → Git folder`).
2. Navigate to `tools/ocsf-mapper/`.
3. Create a new Databricks App, pointing its source at this folder.
4. Click **Deploy**. Databricks reads `app.yaml`, installs `requirements.txt` (which installs the `ocsf_mapper` package from `src/`), and starts the app.

### Local development

```
pip install -r requirements.txt
streamlit run app.py
```

### CLI

The package also exposes a `ocsf-mapper` console command for headless generation:

```
ocsf-mapper sample.jsonl --vendor snyk --source-type vulnerabilities --class-uid 2002,5020
```
