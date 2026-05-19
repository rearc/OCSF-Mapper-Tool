"""Env-var-driven CLI. After `pip install ocsf-mapper`, run `ocsf-mapper`.

Env vars (widget parameters as env):
  OCSF_SAMPLE_PATH    (required)  path to vendor sample
  OCSF_VENDOR         (required)  vendor short name
  OCSF_SOURCE_TYPE    (required)  source type (e.g. vulnerabilities)
  OCSF_VERSION        (default 1.7.0)
  OCSF_CLASS_UIDS     (optional)  comma-separated, skips classifier
  OCSF_CONFIDENCE     (default 0.75)
  OCSF_CACHE_DIR      (default ~/.ocsf_mapper/schema_cache)
  OCSF_OUT_DIR        (default /tmp/ocsf_mapper_output)
  OCSF_STYLE_REF      (optional)  path to override bundled style reference
  ANTHROPIC_API_KEY   (required)

Also supports CLI flags that mirror env vars. Flags win over env vars.

Examples:
  OCSF_SAMPLE_PATH=/tmp/snyk.json OCSF_VENDOR=snyk OCSF_SOURCE_TYPE=vulnerabilities ocsf-mapper
  ocsf-mapper --sample /tmp/snyk.json --vendor snyk --source-type vulnerabilities
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .pipeline import run


def _get(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ocsf-mapper",
        description="Generate Lakewatch preset.yaml from a vendor sample via OCSF mapping.",
    )
    ap.add_argument("--sample", default=_get("OCSF_SAMPLE_PATH"),
                    help="path to vendor sample (env: OCSF_SAMPLE_PATH)")
    ap.add_argument("--vendor", default=_get("OCSF_VENDOR"),
                    help="vendor short name (env: OCSF_VENDOR)")
    ap.add_argument("--source-type", default=_get("OCSF_SOURCE_TYPE"),
                    help="source type (env: OCSF_SOURCE_TYPE)")
    ap.add_argument("--ocsf-version", default=_get("OCSF_VERSION", "1.7.0"),
                    help="OCSF schema version (env: OCSF_VERSION)")
    ap.add_argument("--class-uids", default=_get("OCSF_CLASS_UIDS"),
                    help="comma-sep OCSF class_uids to skip classifier (env: OCSF_CLASS_UIDS)")
    ap.add_argument("--confidence", type=float,
                    default=float(_get("OCSF_CONFIDENCE", "0.75")),
                    help="classifier auto-select threshold (env: OCSF_CONFIDENCE)")
    ap.add_argument("--cache-dir", default=_get("OCSF_CACHE_DIR"),
                    help="OCSF schema cache root (env: OCSF_CACHE_DIR)")
    ap.add_argument("--out-dir", default=_get("OCSF_OUT_DIR"),
                    help="output directory (env: OCSF_OUT_DIR)")
    ap.add_argument("--style-ref", default=_get("OCSF_STYLE_REF"),
                    help="optional style reference override (env: OCSF_STYLE_REF)")
    ap.add_argument("--json", action="store_true",
                    help="emit the final result as JSON on stdout (useful for piping)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress progress output")
    args = ap.parse_args()

    missing = [n for n, v in [("--sample", args.sample), ("--vendor", args.vendor),
                              ("--source-type", args.source_type)] if not v]
    if missing:
        ap.error(f"missing required: {missing}")

    class_uids = None
    if args.class_uids:
        class_uids = [int(x.strip()) for x in args.class_uids.split(",") if x.strip()]

    try:
        result = run(
            sample_path=args.sample,
            vendor=args.vendor,
            source_type=args.source_type,
            ocsf_version=args.ocsf_version,
            class_uids=class_uids,
            confidence_threshold=args.confidence,
            cache_dir=args.cache_dir,
            out_dir=args.out_dir,
            style_reference_path=args.style_ref,
            verbose=not args.quiet,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        safe = {k: v for k, v in result.items() if k != "classify_result"}
        print(json.dumps(safe, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
