"""OCSF schema loader — reads the cache, resolves object references.

Core operations:
  - load_index(version)            → {class_uid: {name, caption, ...}}
  - resolve_class(version, uid)    → full class dict with inlined objects
  - list_classes(version)          → [{uid, name, caption, category}]

When a class attribute has `object_type: "foo"`, the loader substitutes the
corresponding `objects/foo.json` contents (recursively, with cycle protection)
so the model sees the complete nested schema.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


def _cache_root() -> Path:
    return Path(__file__).parent / "ocsf_schema_cache"


def version_dir(version: str, cache_dir: str | Path | None = None) -> Path:
    root = Path(cache_dir) if cache_dir else _cache_root()
    return root / version


def check_version_available(version: str, cache_dir: str | Path | None = None) -> None:
    v = version_dir(version, cache_dir)
    idx = v / "_index.json"
    if not idx.exists():
        raise FileNotFoundError(
            f"OCSF {version} not cached at {v}. Run:\n"
            f"    python fetch_ocsf.py {version}\n"
            f"from an environment with network access to schema.ocsf.io."
        )


def load_index(version: str, cache_dir: str | Path | None = None) -> dict:
    check_version_available(version, cache_dir)
    return json.loads((version_dir(version, cache_dir) / "_index.json").read_text())


def list_classes(version: str, cache_dir: str | Path | None = None) -> list[dict]:
    idx = load_index(version, cache_dir)
    return [{"uid": int(uid), **meta} for uid, meta in sorted(idx.items(), key=lambda kv: int(kv[0]))]


def lookup_class(version: str, class_uid: int, cache_dir: str | Path | None = None) -> dict:
    """Return index entry for class_uid; raises if not found."""
    idx = load_index(version, cache_dir)
    entry = idx.get(str(class_uid))
    if not entry:
        available = sorted([int(k) for k in idx.keys()])
        raise ValueError(
            f"class_uid {class_uid} not in OCSF {version} index. "
            f"Available ({len(available)} classes): {available[:20]}... "
            f"See _index.json for the full list."
        )
    return entry


def _load_class_file(version: str, name: str, cache_dir) -> dict:
    p = version_dir(version, cache_dir) / "classes" / f"{name}.json"
    if not p.exists():
        raise FileNotFoundError(f"class file missing: {p}")
    data = json.loads(p.read_text())
    if "attributes" in data:
        data["attributes"] = _normalize_attributes(data["attributes"])
    return data


def _load_object_file(version: str, name: str, cache_dir) -> dict | None:
    p = version_dir(version, cache_dir) / "objects" / f"{name}.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if "attributes" in data:
        data["attributes"] = _normalize_attributes(data["attributes"])
    return data


def _normalize_attributes(attrs) -> dict:
    """Different OCSF versions return attributes as either a dict or a list.

    Normalize to dict[name -> meta] regardless of source shape:
    - dict already → return as-is
    - list of dicts with `name` field → dict by name
    - anything else → empty dict (unrecoverable)
    """
    if isinstance(attrs, dict):
        return attrs
    if isinstance(attrs, list):
        out = {}
        for i, a in enumerate(attrs):
            if not isinstance(a, dict):
                continue
            name = a.get("name") or a.get("caption") or f"attr_{i}"
            # Don't double-store the name field
            meta = {k: v for k, v in a.items() if k != "name"}
            out[name] = meta
        return out
    return {}


def _inline_objects(
    attrs,
    version: str,
    cache_dir,
    seen: set,
    depth: int,
    max_depth: int,
) -> dict:
    """Recursively expand object_type references in an attributes dict.

    Cycles: if we re-enter an object already in `seen`, substitute a stub
    (`{"_ref": name, "_note": "cycle"}`) instead of recursing forever.
    Depth cap: OCSF objects rarely nest >4 deep; cap to keep prompts small.
    """
    if depth > max_depth:
        return {"_truncated": f"max_depth={max_depth} exceeded"}

    # Defensive normalize — older OCSF versions return list-shaped attributes
    attrs = _normalize_attributes(attrs)

    out = {}
    for attr_name, attr_def in attrs.items():
        if not isinstance(attr_def, dict):
            out[attr_name] = attr_def
            continue

        new_def = dict(attr_def)
        obj_type = attr_def.get("object_type")
        if obj_type:
            if obj_type in seen:
                new_def["_ref"] = obj_type
                new_def["_note"] = "already expanded above (cycle avoided)"
            else:
                obj_schema = _load_object_file(version, obj_type, cache_dir)
                if obj_schema and "attributes" in obj_schema:
                    new_def["attributes"] = _inline_objects(
                        obj_schema["attributes"],
                        version, cache_dir,
                        seen | {obj_type},
                        depth + 1, max_depth,
                    )
                    new_def["_object_caption"] = obj_schema.get("caption", obj_type)

        # nested attrs already present (inline object) — recurse
        if "attributes" in new_def and not obj_type:
            new_def["attributes"] = _inline_objects(
                new_def["attributes"], version, cache_dir, seen, depth + 1, max_depth,
            )

        out[attr_name] = new_def
    return out


def resolve_class(
    version: str,
    class_uid: int,
    cache_dir: str | Path | None = None,
    max_depth: int = 4,
) -> dict:
    """Return the full class schema with all object_type refs inlined.

    This is what gets passed to the LLM so it sees e.g.
    vulnerabilities[].cve.cvss[].base_score without needing a separate
    objects dictionary.
    """
    entry = lookup_class(version, class_uid, cache_dir)
    raw = _load_class_file(version, entry["name"], cache_dir)

    attrs = raw.get("attributes", {})
    resolved_attrs = _inline_objects(attrs, version, cache_dir, set(), 0, max_depth)

    return {
        "uid": class_uid,
        "name": entry["name"],
        "caption": entry["caption"],
        "category_uid": entry.get("category_uid"),
        "category_name": entry.get("category_name"),
        "description": raw.get("description", entry.get("description", "")),
        "extends": raw.get("extends"),
        "attributes": resolved_attrs,
    }


# Keys we keep when compacting for prompt. Everything else is stripped.
# The generator needs: name, type, requirement, enum, and nested structure.
_KEEP_KEYS = {
    "type", "type_name", "requirement", "is_array", "enum",
    "object_type", "_object_caption", "_ref", "_note", "_truncated",
    "attributes",  # recurse into this
}


def _compact(node):
    """Recursively strip verbose OCSF metadata, keep only what the generator needs.

    Removes: description, caption, group, sibling, notes, references, profile,
    observable, deprecated, annotations, and any other ornament that inflates
    the prompt without helping the model pick a mapping expression.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _KEEP_KEYS:
                out[k] = _compact(v)
            # enums may be dicts of {id: {caption, ...}} — flatten to {id: caption}
        if "enum" in out and isinstance(out["enum"], dict):
            out["enum"] = {
                k: (v.get("caption", str(v)) if isinstance(v, dict) else v)
                for k, v in out["enum"].items()
            }
        return out
    if isinstance(node, list):
        return [_compact(x) for x in node]
    return node


def resolve_class_compact(
    version: str,
    class_uid: int,
    cache_dir: str | Path | None = None,
    max_depth: int = 3,
) -> dict:
    """Resolve + compact for LLM prompts. ~10x smaller than resolve_class()."""
    entry = lookup_class(version, class_uid, cache_dir)
    raw = _load_class_file(version, entry["name"], cache_dir)

    attrs = raw.get("attributes", {})
    resolved_attrs = _inline_objects(attrs, version, cache_dir, set(), 0, max_depth)

    compact_attrs = {
        name: _compact(defn) for name, defn in resolved_attrs.items()
    }

    return {
        "uid": class_uid,
        "name": entry["name"],
        "caption": entry["caption"],
        "category_uid": entry.get("category_uid"),
        "category_name": entry.get("category_name"),
        "attributes": compact_attrs,
    }


def summarize_cache(version: str, cache_dir: str | Path | None = None) -> str:
    check_version_available(version, cache_dir)
    v = version_dir(version, cache_dir)
    meta = json.loads((v / "_meta.json").read_text()) if (v / "_meta.json").exists() else {}
    idx = load_index(version, cache_dir)
    classes = list((v / "classes").glob("*.json"))
    objects = list((v / "objects").glob("*.json"))
    lines = [
        f"OCSF {version} cache at {v}",
        f"  fetched:  {meta.get('fetched_at', 'unknown')}",
        f"  classes:  {len(classes)} files, {len(idx)} indexed",
        f"  objects:  {len(objects)} files",
        f"  partial:  {meta.get('partial', False)}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    version = sys.argv[1] if len(sys.argv) > 1 else "1.8.0"
    if len(sys.argv) > 2:
        # resolve specific class
        uid = int(sys.argv[2])
        resolved = resolve_class(version, uid)
        print(json.dumps(resolved, indent=2)[:3000])
    else:
        print(summarize_cache(version))
        print("\nFirst 10 classes in index:")
        for c in list_classes(version)[:10]:
            print(f"  {c['uid']:>6}  {c['name']:<32}  {c['caption']}")
