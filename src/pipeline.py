"""End-to-end pipeline: classify → fetch → generate.

The wheel-consumer entry point. The runner notebook can also import and call
this, but the notebook keeps the steps broken out for debuggability.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .classifier import classify_sample, render_suggestions
from .fetch_ocsf import fetch_all
from .generator import generate_preset
from .schema_loader import summarize_cache


DEFAULT_CACHE_DIR = Path.home() / ".ocsf_mapper" / "schema_cache"
DEFAULT_OUT_DIR = Path("/tmp/ocsf_mapper_output")
# Optional packaged style reference. The canonical style reference lives on a
# UC Volume (passed as reference_dir by the app); this bundled copy is only a
# fallback for local/CLI use and may legitimately be absent.
_BUNDLED_STYLE_REFERENCE = Path(__file__).parent / "style_reference.yaml"
DEFAULT_STYLE_REFERENCE = _BUNDLED_STYLE_REFERENCE if _BUNDLED_STYLE_REFERENCE.exists() else None

# Progress callback signature: fn(phase, message)
# phase ∈ {"classify_start", "classify_done", "fetch_start", "fetch_done",
#          "generate_start", "generate_token", "generate_done"}
ProgressCallback = Callable[[str, str], None]


def run(
    sample_path: str,
    vendor: str,
    source_type: str,
    ocsf_version: str = "1.7.0",
    class_uids: list[int] | None = None,
    confidence_threshold: float = 0.75,
    cache_dir: str | Path | None = None,
    out_dir: str | Path | None = None,
    style_reference_path: str | Path | None = None,
    reference_dir: str | Path | None = None,
    max_references: int = 2,
    api_key: str | None = None,
    verbose: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Run the full mapper pipeline.

    Args:
        sample_path:            Path to vendor sample (json / jsonl / wrapped).
        vendor:                 Vendor short name, e.g. "snyk".
        source_type:            e.g. "vulnerabilities".
        ocsf_version:           OCSF schema version to target.
        class_uids:             If provided, skip the classifier and use these.
                                If None, classifier runs and auto-selects.
        confidence_threshold:   Classifier auto-selects any suggestion ≥ this.
        cache_dir:              OCSF schema cache root. Default: ~/.ocsf_mapper/schema_cache.
        out_dir:                Where to write preset.yaml + report.
        style_reference_path:   Optional override for the bundled style reference.
        api_key:                Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
        verbose:                If True, print progress to stdout.
        progress_callback:      If set, called as fn(phase, message) at each step.
                                Phases: classify_start, classify_done, fetch_start,
                                        fetch_done, generate_start, generate_token,
                                        generate_done.

    Returns:
        {
          "preset_path", "report_path",
          "classes": [{uid, name, caption}],
          "classify_result": ... or None if overridden,
          "format", "profile_summary", "usage"
        }
    """
    if api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set (pass api_key=... or export it)")

    if not Path(sample_path).exists():
        raise FileNotFoundError(f"sample not found: {sample_path}")

    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    out_dir = Path(out_dir) if out_dir else DEFAULT_OUT_DIR
    style_reference_path = Path(style_reference_path) if style_reference_path else DEFAULT_STYLE_REFERENCE

    def log(msg):
        if verbose:
            print(msg, flush=True)

    def cb(phase: str, msg: str = ""):
        if progress_callback:
            try:
                progress_callback(phase, msg)
            except Exception:
                pass  # never let callback errors break the pipeline

    # 1. classify (or skip if classes were given)
    classify_result = None
    if class_uids is None:
        cb("classify_start", f"Classifying sample against OCSF {ocsf_version}...")
        log(f"[1/3] Classifying sample against OCSF {ocsf_version}...")
        classify_result = classify_sample(
            sample_path=sample_path,
            vendor=vendor,
            source_type=source_type,
            ocsf_version=ocsf_version,
        )
        log(render_suggestions(classify_result))
        class_uids = [
            s["class_uid"] for s in classify_result["suggestions"]
            if s.get("confidence", 0) >= confidence_threshold
        ]
        if not class_uids and classify_result["suggestions"]:
            class_uids = [classify_result["suggestions"][0]["class_uid"]]
            log(f"⚠ No suggestion ≥ {confidence_threshold}. Using top: {class_uids}")
        if not class_uids:
            raise RuntimeError("classifier returned no suggestions")
        # Build a one-line summary of what we picked
        picks = [
            f"{s['class_uid']} ({s.get('class_caption', '?')}) — conf {s.get('confidence', 0):.2f}"
            for s in classify_result["suggestions"]
            if s["class_uid"] in class_uids
        ]
        cb("classify_done", "Selected: " + "; ".join(picks))
    else:
        cb("classify_done", f"Skipped — using provided class_uids: {class_uids}")
        log(f"[1/3] Using provided class_uids: {class_uids}")

    # 2. fetch only the classes we need (+ all objects)
    cb("fetch_start", f"Fetching OCSF {ocsf_version} schema for classes {class_uids}...")
    log(f"[2/3] Fetching OCSF {ocsf_version} schema for classes {class_uids}...")
    fetch_all(
        version=ocsf_version,
        cache_dir=cache_dir / ocsf_version,
        only_classes=[str(uid) for uid in class_uids],
    )
    log(summarize_cache(ocsf_version, cache_dir=cache_dir))
    cb("fetch_done", f"Schema cached at {cache_dir / ocsf_version}")

    # 3. generate
    cb("generate_start", f"Generating preset for {len(class_uids)} class(es) with Claude...")
    log(f"[3/3] Generating preset for {len(class_uids)} class(es)...")
    gen_result = generate_preset(
        sample_path=sample_path,
        vendor=vendor,
        source_type=source_type,
        class_uids=class_uids,
        ocsf_version=ocsf_version,
        style_reference_path=str(style_reference_path) if style_reference_path else None,
        reference_dir=str(reference_dir) if reference_dir else None,
        max_references=max_references,
        out_dir=str(out_dir),
        cache_dir=str(cache_dir),
        progress_callback=progress_callback,
    )
    cb(
        "generate_done",
        f"Preset written. Tokens: {gen_result['usage']['input_tokens']} in / "
        f"{gen_result['usage']['output_tokens']} out",
    )

    log(f"\n✓ preset:  {gen_result['preset_path']}")
    log(f"✓ report:  {gen_result['report_path']}")
    log(f"  tokens:  {gen_result['usage']['input_tokens']} in / {gen_result['usage']['output_tokens']} out")

    return {
        **gen_result,
        "classify_result": classify_result,
    }
