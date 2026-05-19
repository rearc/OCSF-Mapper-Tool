# OCSF Mapper

Generate Lakewatch presets from vendor security telemetry by mapping it to OCSF v1.8.0 via Claude.

One job: read a vendor sample (Snyk, AWS CloudTrail, Wiz, anything), pick the right OCSF class, and write a complete bronze/silver/gold `preset.yaml` ready to drop into the security-content-library. No deployment, no orchestration — just the preset.

## Install

The app runs on Databricks Apps. Source lives in this repo; the `ocsf_mapper` package is imported directly from `./ocsf_mapper/` — there is no wheel build.

```
pip install -r requirements.txt
streamlit run app.py
```

Runtime deps: streamlit, streamlit-ace, databricks-sdk, anthropic, pyyaml, requests. Python 3.11+.

## Use

Open the app, then in the Generator tab:

```
Sample path:   /Volumes/dsl_dev/internal/ocsf_mapper/samples/snyk_vulns.jsonl
Vendor:        snyk
Source type:   vulnerabilities
[Generate preset]
```

The tool classifies the sample to the relevant OCSF class, fetches the schema, runs Claude with the class schema + curated style references, and validates the output against OCSF data-type rules. Edit in the in-app YAML editor, then **Save to Volume** or **Download**.

## Configuration

Sidebar controls:

| Setting | Default | Notes |
|---|---|---|
| Anthropic API key | — | Required; entered at runtime, not persisted |
| OCSF version | `1.8.0` | Validated against schema.ocsf.io |
| Reference library | `/Volumes/dsl_dev/internal/ocsf_mapper/preset_library` | Preset YAMLs used as style anchors. Selected by `class_uid` relevance to the target. |
| Advisory folder | `/Volumes/dsl_dev/internal/ocsf_mapper/advisory` | All `.md` files in this folder are read each run and injected as prompt instructions. Edit on the Volume to change generator behavior with no code deploy. |
| Output volume | `/Volumes/dsl_dev/internal/ocsf_mapper/generated_presets` | Where "Save to Volume" lands |

## Data-type correctness

OCSF types map to Spark types via `ocsf_mapper/ocsf_types.py` (`timestamp_t → BIGINT`, `int_t → INT`, `long_t → BIGINT`, `datetime_t → STRING`, `json_t → VARIANT`, …). The generator reads each field's declared OCSF type from the fetched schema and maps it through this table, so casts match OCSF rather than whatever the style-reference preset happened to do.

After generation, `ocsf_mapper/validator.py` scans the YAML for type violations — quoted int literals, `INT` `type_uid`, Spark `TIMESTAMP` on `timestamp_t` fields, RFC-3339 formatting. Findings appear in the generation report and a status pill in the Generator tab.

## Schema source

The OCSF v1.8.0 schema is fetched from `schema.ocsf.io` on first use per class and cached at `/Volumes/dsl_dev/internal/ocsf_mapper/schema_cache/<version>/`. Subsequent runs are offline.

OCSF v1.8.0 advertises 82 event classes; the 7 Windows extension classes (uids in the 201000 / 205000 ranges) have no per-name resolver endpoint and are skipped. The cached schema contains 75 event classes.

## Tabs

- **Generator** — sample path + vendor + source type → `preset.yaml`. Live progress, in-tab YAML editor, validation status.
- **Sample Inspector** — peek at a sample's structure, distinct fields, and parsed records before generating.
- **OCSF Explorer** — browse all 75 classes in the cached schema, see required/recommended/optional attributes per class, push a class into the Generator.
- **Library** — list, view, download, or delete reference presets on the Volume.

## API

`run()` is the headless entry point — the same code path the Streamlit app uses:

```python
from ocsf_mapper import run

result = run(
    sample_path="/Volumes/.../samples/snyk_vulns.jsonl",
    vendor="snyk",
    source_type="vulnerabilities",
    ocsf_version="1.8.0",
    reference_dir="/Volumes/.../preset_library",
    advisory_dir="/Volumes/.../advisory",
    cache_dir="/Volumes/.../schema_cache",
    out_dir="/tmp/ocsf_mapper_output",
)
# result["preset_path"], result["report_path"], result["type_findings"]
```

A CLI is also installed:

```
ocsf-mapper <sample> --vendor snyk --source-type vulnerabilities --ocsf-version 1.8.0
```

## Tests

```
pip install pytest
pytest tests/ -q
```

Unit tests cover the OCSF→Spark type mapping, the schema-derived type-map walker, and the preset validator. 18 tests, ~1 second.

## Repository layout

```
OCSF-Mapper-Tool/
├── app.py                       Streamlit entrypoint
├── app.yaml                     Databricks Apps runtime config
├── config.toml                  Streamlit theme
├── requirements.txt
├── advisory.md                  Reference copy of the advisory (live one lives on the Volume)
├── ocsf_mapper/                 the package (imported directly, not pip-installed)
│   ├── pipeline.py              classify → fetch → generate orchestration
│   ├── classifier.py            Claude picks OCSF class UIDs
│   ├── fetch_ocsf.py            pulls + caches OCSF schema
│   ├── schema_loader.py         reads cache, inlines object refs
│   ├── generator.py             Claude generates the preset
│   ├── ocsf_types.py            OCSF→Spark type mapping + schema-derived type map
│   ├── validator.py             preset datatype validation
│   ├── profiler.py              sample format detection + profiling
│   ├── reference_library.py     selects style-anchor presets by class_uid
│   ├── advisory.py              loads .md advisory files from the Volume
│   └── cli.py                   `ocsf-mapper` console command
└── tests/
    └── test_types_and_validator.py
```

## Notes

- The advisory folder is the place to change generator behavior at runtime — edit `advisory/advisory.md` on the Volume; the next run picks it up. Code deploys are only needed when changing the type translation table itself or the validator.
- The `preset_library/` folder has a strict contract: every file is a preset YAML with `class_uids`. The advisory folder is for free-form markdown instructions. The two are deliberately separate.
- Related work in the rearc OCSF toolchain: `rearc/Relinker` (full compiler-based pipeline generation) and `rearc/gold-ocsf-table-creator` (DDL-only generation). This tool sits in between — produces presets, not pipelines, not raw DDL.
