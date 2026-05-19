"""ocsf_mapper — generate Lakewatch presets from vendor samples.

Public API:
    from ocsf_mapper import run
    run(sample_path=..., vendor=..., source_type=...)

For more control, individual stages are importable:
    from ocsf_mapper import classify_sample, generate_preset, fetch_all

Type correctness:
    from ocsf_mapper import spark_type_for, validate_preset_text
"""
from .pipeline import run
from .classifier import classify_sample, render_suggestions
from .fetch_ocsf import fetch_all
from .generator import generate_preset
from .schema_loader import list_classes, lookup_class, summarize_cache
from .profiler import detect_format, profile
from .reference_library import scan_library, select_references, describe_selection
from .ocsf_types import spark_type_for, OCSF_TO_SPARK, is_timestamp_type, is_datetime_type
from .validator import validate_preset_text, format_findings, Finding

__version__ = "0.4.0"

__all__ = [
    "run",
    "classify_sample",
    "render_suggestions",
    "fetch_all",
    "generate_preset",
    "list_classes",
    "lookup_class",
    "summarize_cache",
    "detect_format",
    "profile",
    "spark_type_for",
    "OCSF_TO_SPARK",
    "is_timestamp_type",
    "is_datetime_type",
    "validate_preset_text",
    "format_findings",
    "Finding",
    "__version__",
]
