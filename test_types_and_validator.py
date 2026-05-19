"""Tests for the OCSF type-mapping and preset validator.

Run: pytest tests/
"""
from ocsf_mapper.ocsf_types import (
    spark_type_for, is_timestamp_type, is_datetime_type, OCSF_TO_SPARK,
)
from ocsf_mapper.validator import validate_preset_text


# ─── ocsf_types ──────────────────────────────────────────────────────────────

def test_timestamp_t_is_bigint():
    # OCSF timestamp_t is epoch-millis — an integer, not a Spark TIMESTAMP.
    assert spark_type_for("timestamp_t") == "BIGINT"


def test_datetime_t_is_string():
    assert spark_type_for("datetime_t") == "STRING"


def test_int_and_long():
    assert spark_type_for("int_t") == "INT"
    assert spark_type_for("long_t") == "BIGINT"


def test_type_uid_forced_to_bigint_even_if_cache_says_int():
    # A stale schema cache might still call type_uid int_t. The name override
    # must win and produce BIGINT.
    assert spark_type_for("int_t", attr_name="type_uid") == "BIGINT"


def test_unknown_type_falls_back_to_string():
    assert spark_type_for("nonsense_t") == "STRING"
    assert spark_type_for(None) == "STRING"


def test_json_t_is_variant():
    assert spark_type_for("json_t") == "VARIANT"


def test_type_predicates():
    assert is_timestamp_type("timestamp_t")
    assert not is_timestamp_type("datetime_t")
    assert is_datetime_type("datetime_t")
    assert not is_datetime_type("timestamp_t")


# ─── validator ───────────────────────────────────────────────────────────────

_BAD_PRESET = """
gold:
  - name: vulnerability_finding_02
    fields:
      - name: class_uid
        literal: "2002"
      - name: type_uid
        expr: "CAST(200201 AS INT)"
      - name: timezone_offset
        expr: "CAST(tz AS STRING)"
      - name: created_time
        expr: "CAST(t AS TIMESTAMP)"
"""

_GOOD_PRESET = """
gold:
  - name: vulnerability_finding_02
    fields:
      - name: class_uid
        literal: 2002
      - name: type_uid
        expr: "CAST(200201 AS BIGINT)"
      - name: timezone_offset
        expr: "CAST(tz AS INT)"
      - name: created_time
        expr: "unix_millis(to_timestamp(t))"
"""


def test_validator_flags_quoted_int_literal():
    findings = validate_preset_text(_BAD_PRESET)
    msgs = [f.field for f in findings if f.field == "class_uid"]
    assert "class_uid" in msgs


def test_validator_flags_int_type_uid():
    findings = validate_preset_text(_BAD_PRESET)
    assert any(f.field == "type_uid" and f.level == "error" for f in findings)


def test_validator_flags_string_timezone_offset():
    findings = validate_preset_text(_BAD_PRESET)
    assert any(f.field == "timezone_offset" and f.level == "error" for f in findings)


def test_validator_flags_timestamp_cast():
    findings = validate_preset_text(_BAD_PRESET)
    assert any(f.field == "created_time" for f in findings)


def test_validator_passes_clean_preset():
    findings = validate_preset_text(_GOOD_PRESET)
    errors = [f for f in findings if f.level == "error"]
    assert errors == [], f"expected no errors, got: {errors}"


def test_validator_handles_bad_yaml():
    findings = validate_preset_text("this: : : not valid")
    assert any(f.level == "error" for f in findings)
