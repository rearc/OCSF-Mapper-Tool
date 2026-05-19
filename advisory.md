# Lakewatch Preset Conventions

Follow these instructions when generating a Lakewatch preset. They override
any conflicting pattern in the style-reference presets.

## Bronze

Expose records as the `_raw` variant column plus the promoted columns `id`,
`time`, `eventCategory`, and `collected_at`.

## Silver

Read every field from the `_raw` variant directly — `_raw:fieldName::string`.
Never use `parse_json(_raw_json)`; it silently returns null on markdown-heavy
payloads.

Set the silver filter on the bronze routing column `eventCategory`, compared
to the vendor's event-type value. Reference `eventCategory` directly — do not
alias it.

Parse ISO timestamp strings with `try_to_timestamp(_raw:field::string)`.

## Gold

Emit complex OCSF fields (nested objects, arrays of objects) as
`named_struct(...)` and `array(named_struct(...))` — typed STRUCTs in the DDL.
Reserve `parse_json(to_json(...))` for the `unmapped` field only.

Name gold tables with the `_02` suffix (e.g. `vulnerability_finding_02`) when
the deployment target permits MODIFY but not ALTER/DROP. Use clean names
without the suffix for CI/CD deployments where ALTER is permitted.

End every gold block with an `unmapped` field holding vendor-specific fields
that have no OCSF home.

Order gold fields: classification -> activity -> type -> time -> severity ->
status -> class-specific payload -> observables -> resources -> enrichments ->
metadata -> raw_data -> unmapped.

## OCSF schema

Target OCSF 1.8.0. Embed `1.8.0` wherever the preset references a schema
version, most commonly the gold `metadata.version` literal.

Use only attribute names that appear in the provided OCSF schema. Do not
invent OCSF field names. Any field without an OCSF home goes in `unmapped`.

## Databricks SQL

`time` is a reserved word. Wrap it in backticks when used with a table alias
(`` e.`time` ``), or extract all needed fields into a CTE first to avoid the
conflict entirely.

## Generation notes

Keep the `### Generation notes` section short and scannable — it is read at a
glance before review. Use these three bullet groups, nothing more:

- **Mapping** — one line per OCSF class: which class, why it fits.
- **Needs review** — every low-confidence field, one line each, matching a
  `# TODO:` comment in the YAML. If there are none, write "none".
- **Unmapped** — vendor fields parked in `unmapped`, comma-separated. If there
  are none, write "none".

Do not restate the schema, re-explain conventions, or narrate the process.
