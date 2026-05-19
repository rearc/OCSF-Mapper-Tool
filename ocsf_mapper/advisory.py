"""Advisory context loader.

The advisory folder holds free-form team guidance — OCSF type rules, naming
conventions, known platform gotchas — that should shape every generated
preset regardless of which OCSF class is targeted.

Why this is separate from the preset library
---------------------------------------------
`preset_library/` has a strict contract: every file in it is a preset, parsed
for its `class_uid`, and ranked by relevance to the target class. An advisory
file has no `class_uid` and must reach the generator on EVERY run, not just
when its class happens to match. So it lives in its own folder:

    /Volumes/dsl_dev/internal/ocsf_mapper/advisory/
        advisory.md
        (optionally more .md files)

All `.md` files in the folder are concatenated, in sorted filename order, and
injected into the generator prompt. Editing guidance is a pure Volume edit —
no code change, no redeploy. A missing or empty folder is not an error; the
generator simply proceeds with no advisory context.
"""
from __future__ import annotations

from pathlib import Path


# Default location — a sibling of preset_library/ on the UC Volume.
DEFAULT_ADVISORY_DIR = "/Volumes/dsl_dev/internal/ocsf_mapper/advisory"


def load_advisory(advisory_dir: str | Path | None) -> str:
    """Concatenate all .md files in the advisory folder into one string.

    Args:
        advisory_dir: path to the advisory folder. May be None, missing, or
                      empty — all of which yield an empty string (no advisory).

    Returns:
        The combined advisory text, or "" if there is nothing to load.
    """
    if not advisory_dir:
        return ""
    d = Path(advisory_dir)
    if not d.is_dir():
        return ""

    md_files = sorted(d.glob("*.md"))
    if not md_files:
        return ""

    blocks: list[str] = []
    for f in md_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if text:
            # Label each file so multi-file advisories stay legible in-prompt.
            blocks.append(f"<!-- advisory: {f.name} -->\n{text}")
    return "\n\n".join(blocks)
