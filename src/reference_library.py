"""Reference preset library — scan a directory of existing presets and pick
the most relevant ones as style references for a new generation.

Relevance ranking:
  1. Exact class_uid match
  2. Same OCSF category (class_uid / 1000 bucket)
  3. Fallback — return library default (first alphabetical, or bundled)
"""
from __future__ import annotations

import re
from pathlib import Path


def parse_preset_metadata(preset_path: Path) -> dict:
    """Extract {class_uids: [...], category_uids: [...]} from a preset's gold blocks.

    Cheap text parsing — looks for `class_uid` and `category_uid` entries under
    gold blocks. Avoids a YAML dep and works even on presets with complex templating.
    """
    try:
        text = preset_path.read_text()
    except Exception:
        return {"class_uids": [], "category_uids": [], "path": preset_path}

    class_uids = set()
    category_uids = set()

    # Match: `class_uid\n...literal: "NNNN"` or `class_uid\n...expr: "CAST(NNNN AS INT)"`
    # Both conventions exist in the team's presets
    for m in re.finditer(
        r"-\s*name:\s*class_uid\s*\n\s*(?:literal|expr|from):\s*['\"]?(?:CAST\(\s*)?(\d+)",
        text,
    ):
        class_uids.add(int(m.group(1)))

    for m in re.finditer(
        r"-\s*name:\s*category_uid\s*\n\s*(?:literal|expr|from):\s*['\"]?(?:CAST\(\s*)?(\d+)",
        text,
    ):
        category_uids.add(int(m.group(1)))

    return {
        "class_uids": sorted(class_uids),
        "category_uids": sorted(category_uids),
        "path": preset_path,
        "size_chars": len(text),
    }


def scan_library(reference_dir: str | Path) -> list[dict]:
    """Load metadata for every .yaml preset in the library."""
    d = Path(reference_dir)
    if not d.exists() or not d.is_dir():
        return []
    refs = []
    for p in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
        meta = parse_preset_metadata(p)
        if meta["class_uids"]:
            refs.append(meta)
    return refs


def select_references(
    reference_dir: str | Path | None,
    target_class_uids: list[int],
    max_refs: int = 2,
    fallback_path: str | Path | None = None,
) -> list[Path]:
    """Pick up to max_refs most relevant preset paths for the target class(es).

    Args:
        reference_dir:      Volume path to the preset library. None/missing → use fallback.
        target_class_uids:  OCSF class_uids we're generating for (e.g. [2002]).
        max_refs:           Cap on references returned. Keeps prompt size bounded.
        fallback_path:      Single preset to use if library is empty/missing.

    Returns a list of Path objects, ordered best-first.
    """
    if not reference_dir:
        return [Path(fallback_path)] if fallback_path else []

    refs = scan_library(reference_dir)
    if not refs:
        return [Path(fallback_path)] if fallback_path else []

    target_classes = set(target_class_uids)
    target_categories = {uid // 1000 for uid in target_class_uids}

    def score(ref: dict) -> tuple[int, int]:
        """Higher is better. First element = exact match count; second = category match count."""
        exact = len(set(ref["class_uids"]) & target_classes)
        categorical = len({uid // 1000 for uid in ref["class_uids"]} & target_categories)
        return (exact, categorical)

    ranked = sorted(refs, key=score, reverse=True)

    # Filter out zero-relevance unless we have literally nothing else
    relevant = [r for r in ranked if score(r) != (0, 0)]
    chosen = (relevant or ranked)[:max_refs]

    return [r["path"] for r in chosen]


def describe_selection(
    reference_dir: str | Path | None,
    target_class_uids: list[int],
    max_refs: int = 2,
) -> str:
    """Human-readable description of what the selector picked. Useful for logs."""
    if not reference_dir:
        return "(no reference library configured)"

    refs = scan_library(reference_dir)
    if not refs:
        return f"(reference library at {reference_dir} is empty)"

    selected = select_references(reference_dir, target_class_uids, max_refs)
    selected_names = {p.name for p in selected}

    lines = [f"Reference library: {reference_dir} ({len(refs)} preset(s))"]
    for r in refs:
        mark = "✓" if r["path"].name in selected_names else " "
        lines.append(
            f"  {mark} {r['path'].name:<40} "
            f"classes={r['class_uids']} categories={r['category_uids']}"
        )
    lines.append(f"Target class_uids: {target_class_uids}")
    lines.append(f"Selected {len(selected)} reference(s): {[p.name for p in selected]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: reference_library.py <dir> <class_uid> [class_uid ...]")
        sys.exit(1)
    directory = sys.argv[1]
    uids = [int(x) for x in sys.argv[2:]]
    print(describe_selection(directory, uids))
