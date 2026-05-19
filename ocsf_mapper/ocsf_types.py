"""OCSF data-type → Spark/Databricks SQL type mapping.

Single source of truth for how an OCSF attribute's declared `type` (or
`type_name`) translates into a Spark SQL type in a generated preset.

Why this module exists
----------------------
The OCSF schema declares scalar attributes with data types like `timestamp_t`,
`datetime_t`, `int_t`, `long_t`. The generator used to leave the choice of
Spark type up to the LLM, which inferred it from the *reference preset* — and
every reference preset was written against pre-existing `cyber_prod` DDLs that
predate OCSF type corrections. Two recurring bugs resulted:

  1. Timestamp:  OCSF `timestamp_t` is an INTEGER (epoch milliseconds). It was
     being emitted as Spark `TIMESTAMP`, and `datetime_t` (the RFC-3339 string
     sibling) was conflated with it. The OCSF validator rejects both.

  2. Integer:    OCSF changed `type_uid` (and a few other fields) from `int_t`
     to `long_t`. Presets emitting `INT` for those overflow / fail validation.

By routing every type decision through this table, the generator emits the
spec-correct Spark type deterministically instead of guessing.

References
----------
- OCSF data types: https://schema.ocsf.io/<version>/data_types
- `timestamp_t` = int64, milliseconds since Unix epoch.
- `datetime_t`  = string, RFC-3339 (e.g. "2026-04-13T10:42:11.123Z").
- `type_uid`    = long_t (changed from int_t in OCSF 1.x).
"""
from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# OCSF scalar data type  →  Spark SQL type
# ─────────────────────────────────────────────────────────────────────────────
# Keys are the OCSF `type` / `type_name` values as they appear in the schema
# cache. Complex types (objects, arrays of objects) are NOT here — those are
# emitted as named_struct() / array(named_struct()) per team convention.
OCSF_TO_SPARK: dict[str, str] = {
    # ── Time ──────────────────────────────────────────────────────────────────
    "timestamp_t": "BIGINT",      # epoch MILLISECONDS — integer, NOT a Spark TIMESTAMP
    "datetime_t":  "STRING",      # RFC-3339 human-readable string (Date/Time profile)

    # ── Integers ──────────────────────────────────────────────────────────────
    "integer_t":   "INT",
    "int_t":       "INT",
    "long_t":      "BIGINT",      # 64-bit — type_uid and friends live here

    # ── Floating point ────────────────────────────────────────────────────────
    "float_t":     "DOUBLE",

    # ── Boolean ───────────────────────────────────────────────────────────────
    "boolean_t":   "BOOLEAN",

    # ── Strings & string-like scalars ─────────────────────────────────────────
    "string_t":    "STRING",
    "bytestring_t": "STRING",
    "uuid_t":      "STRING",
    "ip_t":        "STRING",
    "ipv4_t":      "STRING",
    "ipv6_t":      "STRING",
    "mac_t":       "STRING",
    "hostname_t":  "STRING",
    "email_t":     "STRING",
    "url_t":       "STRING",
    "subnet_t":    "STRING",
    "file_name_t": "STRING",
    "file_hash_t": "STRING",
    "path_t":      "STRING",
    "process_name_t": "STRING",
    "resource_uid_t": "STRING",
    "username_t":  "STRING",
    "port_t":      "INT",          # ports are small ints in OCSF
    "json_t":      "VARIANT",      # free-form JSON → Databricks VARIANT
}

# Attributes that OCSF declares as `long_t` even though a naive reader might
# assume `int_t`. The generator double-checks these by NAME so a stale schema
# cache (fetched before the int_t→long_t correction) still produces BIGINT.
KNOWN_LONG_T_FIELDS: frozenset[str] = frozenset({
    "type_uid",
    "raw_data_size",
    "size",          # file/object sizes can exceed int32
    "count",         # aggregated counts
})

# Default when an OCSF type is unrecognized. STRING is the safe fallback —
# never silently drop to a numeric type we can't justify.
DEFAULT_SPARK_TYPE = "STRING"


def spark_type_for(
    ocsf_type: str | None,
    attr_name: str | None = None,
) -> str:
    """Return the Spark SQL type for an OCSF scalar attribute.

    Args:
        ocsf_type: the OCSF data type string, e.g. "timestamp_t". Accepts the
                   value of either the `type` or `type_name` schema key.
        attr_name: the attribute name. If it is a KNOWN_LONG_T_FIELDS member,
                   BIGINT is forced regardless of what `ocsf_type` says — this
                   guards against a stale cache predating the int_t→long_t fix.

    Returns:
        A Spark SQL type string (e.g. "BIGINT", "STRING", "VARIANT").
    """
    if attr_name and attr_name in KNOWN_LONG_T_FIELDS:
        return "BIGINT"
    if not ocsf_type:
        return DEFAULT_SPARK_TYPE
    return OCSF_TO_SPARK.get(ocsf_type.strip(), DEFAULT_SPARK_TYPE)


def is_timestamp_type(ocsf_type: str | None) -> bool:
    """True if the OCSF type is the epoch-millis integer timestamp."""
    return (ocsf_type or "").strip() == "timestamp_t"


def is_datetime_type(ocsf_type: str | None) -> bool:
    """True if the OCSF type is the RFC-3339 string datetime."""
    return (ocsf_type or "").strip() == "datetime_t"


# ─────────────────────────────────────────────────────────────────────────────
# Prompt fragment — injected into the generator's system prompt so the LLM is
# TOLD the mapping rather than inferring it from a reference preset.
# ─────────────────────────────────────────────────────────────────────────────
TYPE_MAPPING_PROMPT = """\
OCSF DATA TYPE → SPARK SQL TYPE (authoritative — overrides any reference preset):

  OCSF type      Spark type   Notes
  ------------   ----------   -----------------------------------------------
  timestamp_t    BIGINT       Epoch MILLISECONDS as an integer. NEVER emit a
                              Spark TIMESTAMP for a timestamp_t attribute.
                              In silver, convert ISO strings with
                              `unix_millis(to_timestamp(_raw:field::string))`.
  datetime_t     STRING       RFC-3339 string, e.g. 2026-04-13T10:42:11.123Z.
                              Emit with date_format(ts, "yyyy-MM-dd'T'HH:mm:ss.SSSXXX").
                              A bare CAST(ts AS STRING) produces a SPACE
                              separator and FAILS OCSF's RFC-3339 validator.
  int_t          INT
  long_t         BIGINT       `type_uid` is long_t — always CAST(... AS BIGINT).
  float_t        DOUBLE
  boolean_t      BOOLEAN
  string_t       STRING
  json_t         VARIANT

RULES:
- The OCSF schema's declared `type` for each attribute is authoritative for the
  Spark type. If a reference preset casts a field differently, IGNORE the
  reference and follow the table above.
- `class_uid`, `category_uid`, `activity_id`, `severity_id`, `type_id`,
  `timezone_offset` are OCSF `int_t` — emit them as INT (or via `literal:` for
  the constant classification fields). NEVER emit them as STRING.
- `type_uid` is OCSF `long_t` — emit `CAST(<expr> AS BIGINT)`.
- For every `timestamp_t` attribute, emit `CAST(<expr> AS BIGINT)` carrying
  epoch-millis. For every `datetime_t` attribute, emit an RFC-3339 STRING.
"""


# ─────────────────────────────────────────────────────────────────────────────
# API-DERIVED TYPE MAP  (issue #47)
#
# The OCSF_TO_SPARK table above is a hand-maintained translation table — it
# says "OCSF `timestamp_t` becomes Spark `BIGINT`". What it does NOT say is
# which *field* is which OCSF type. Hand-listing that (KNOWN_LONG_T_FIELDS)
# goes stale the moment OCSF changes an attribute's type.
#
# The functions below close that gap. They read each attribute's declared
# OCSF type straight from the fetched OCSF schema (the cache populated by
# fetch_ocsf.py / read by schema_loader.py) and run it through OCSF_TO_SPARK.
#
# Result: the OCSF type of every field is API-derived; only the small
# type→type translation table stays hand-maintained. When OCSF reclassifies
# an attribute (e.g. type_uid int_t→long_t), a re-fetch picks it up with no
# code change.
# ─────────────────────────────────────────────────────────────────────────────


def _walk_attributes(attrs: dict, prefix: str = ""):
    """Yield (dotted_field_name, ocsf_type) for every scalar attribute in a
    resolved class-schema `attributes` dict, recursing into nested objects.

    `attrs` is the shape produced by schema_loader.resolve_class_compact()[
    "attributes"] — a dict of name -> {type?, object_type?, attributes?, ...}.
    Object/array-of-object attributes have no scalar `type`; we recurse into
    their nested `attributes` instead of yielding them.
    """
    if not isinstance(attrs, dict):
        return
    for name, defn in attrs.items():
        if not isinstance(defn, dict):
            continue
        dotted = f"{prefix}{name}"
        nested = defn.get("attributes")
        if isinstance(nested, dict) and nested:
            # complex object — recurse, don't yield the object itself
            yield from _walk_attributes(nested, prefix=f"{dotted}.")
        else:
            ocsf_type = defn.get("type") or defn.get("type_name")
            yield dotted, ocsf_type


def derive_type_map(resolved_class: dict) -> dict[str, str]:
    """Build {field_name: spark_type} for a class, read from the OCSF schema.

    Args:
        resolved_class: the dict returned by
                        schema_loader.resolve_class_compact(version, uid) —
                        it has an "attributes" key.

    Returns:
        Mapping of every scalar attribute (dotted for nested fields) to its
        Spark SQL type, derived from the schema's declared OCSF type. The
        attribute-name override (KNOWN_LONG_T_FIELDS) still applies as a
        safety net for a stale cache.
    """
    attrs = (resolved_class or {}).get("attributes", {})
    out: dict[str, str] = {}
    for dotted, ocsf_type in _walk_attributes(attrs):
        leaf = dotted.rsplit(".", 1)[-1]
        out[dotted] = spark_type_for(ocsf_type, attr_name=leaf)
    return out


def derive_type_map_for_classes(resolved_classes: list[dict]) -> dict[int, dict[str, str]]:
    """Same as derive_type_map but for several classes at once.

    Returns {class_uid: {field_name: spark_type}}.
    """
    result: dict[int, dict[str, str]] = {}
    for rc in resolved_classes or []:
        uid = rc.get("uid")
        if uid is not None:
            result[uid] = derive_type_map(rc)
    return result


def render_type_map_for_llm(type_map: dict[str, str]) -> str:
    """Render a derived type map as a compact block for the generator prompt.

    Lists every field with its OCSF-derived Spark type, so the model emits
    casts that match the schema rather than guessing from a reference preset.
    """
    if not type_map:
        return "(no schema-derived types available)"
    lines = ["OCSF-derived Spark types for this class (authoritative):"]
    for field, spark in sorted(type_map.items()):
        lines.append(f"  {field}  ->  {spark}")
    return "\n".join(lines)