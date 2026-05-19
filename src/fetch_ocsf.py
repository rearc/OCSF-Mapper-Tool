"""Fetch and cache the full OCSF schema for a given version.

Pulls:
  - /api/{version}/classes              → list of all classes (for index)
  - /api/{version}/classes/{name}       → full attrs for each class
  - /api/{version}/objects              → list of all objects
  - /api/{version}/objects/{name}       → full attrs for each object

Writes:
  ocsf_schema_cache/{version}/
    _index.json                         # class_uid → {name, caption, category}
    _objects_index.json                 # object_name → file path
    classes/{name}.json
    objects/{name}.json

Usage:
    python fetch_ocsf.py 1.8.0
    python fetch_ocsf.py 1.8.0 --cache-dir /path/to/cache
    python fetch_ocsf.py 1.8.0 --only-classes 2002,5020    # partial fetch

Run this ONCE per OCSF version from an environment with network access to
schema.ocsf.io (Databricks driver nodes typically work; locked-down sandboxes
may not). The resulting cache is committed to the repo and used offline by
generator.py and validator.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

BASE = "https://schema.ocsf.io/api/{version}"
REQUEST_DELAY = 0.15  # be polite to schema.ocsf.io


def _get(url: str, retries: int = 3, allow_404: bool = False) -> dict | None:
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
            if allow_404 and r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            # don't retry 404s
            if hasattr(e, "response") and e.response is not None and e.response.status_code == 404:
                if allow_404:
                    return None
                raise RuntimeError(f"404 Not Found: {url}") from e
            last_err = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def _normalize_catalog(raw) -> dict:
    """OCSF API returns catalogs as either a list of {name, ...} dicts or a
    {name: {...}} mapping. Normalize both shapes to the dict form that the
    rest of this module expects."""
    if isinstance(raw, dict):
        # already a {name: {...}} map, or wrapped like {"classes": [...]}
        if "classes" in raw and isinstance(raw["classes"], list):
            raw = raw["classes"]
        elif "objects" in raw and isinstance(raw["objects"], list):
            raw = raw["objects"]
        else:
            return raw  # already the expected shape
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("type") or item.get("caption")
            if name:
                out[name] = item
        return out
    raise TypeError(f"unexpected catalog shape: {type(raw).__name__}")


def fetch_classes_list(version: str) -> dict:
    """Returns the full classes catalog: {class_name: {uid, caption, category_name, ...}}."""
    return _normalize_catalog(_get(f"{BASE.format(version=version)}/classes"))


def fetch_objects_list(version: str) -> dict:
    return _normalize_catalog(_get(f"{BASE.format(version=version)}/objects"))


def fetch_class(version: str, name: str) -> dict | None:
    return _get(f"{BASE.format(version=version)}/classes/{name}", allow_404=True)


def fetch_object(version: str, name: str) -> dict | None:
    return _get(f"{BASE.format(version=version)}/objects/{name}", allow_404=True)


def build_index(classes_catalog: dict) -> dict:
    """class_uid → summary used by generator for class lookup."""
    index = {}
    for name, meta in classes_catalog.items():
        uid = meta.get("uid")
        if uid is None:
            continue
        index[str(uid)] = {
            "name": name,
            "caption": meta.get("caption", name),
            "category_uid": meta.get("category_uid"),
            "category_name": meta.get("category_name"),
            "description": meta.get("description", ""),
            "extends": meta.get("extends"),
        }
    return index


def fetch_all(
    version: str,
    cache_dir: Path,
    only_classes: list[str] | None = None,
) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    classes_dir = cache_dir / "classes"
    objects_dir = cache_dir / "objects"
    classes_dir.mkdir(exist_ok=True)
    objects_dir.mkdir(exist_ok=True)

    print(f"[1/4] Fetching class catalog for OCSF {version}...", flush=True)
    classes_catalog = fetch_classes_list(version)
    print(f"      found {len(classes_catalog)} classes")

    print(f"[2/4] Fetching object catalog...", flush=True)
    objects_catalog = fetch_objects_list(version)
    print(f"      found {len(objects_catalog)} objects")

    # filter if --only-classes given
    target_classes = list(classes_catalog.keys())
    if only_classes:
        wanted_uids = set(only_classes)
        target_classes = [
            name for name, meta in classes_catalog.items()
            if str(meta.get("uid")) in wanted_uids
        ]
        print(f"      filtered to {len(target_classes)} classes: {target_classes}")

    # fetch classes
    print(f"[3/4] Fetching {len(target_classes)} class definitions...", flush=True)
    skipped_classes = []
    for i, name in enumerate(target_classes, 1):
        out_path = classes_dir / f"{name}.json"
        if out_path.exists():
            print(f"      [{i}/{len(target_classes)}] {name} (cached)")
            continue
        print(f"      [{i}/{len(target_classes)}] {name}")
        data = fetch_class(version, name)
        if data is None:
            print(f"                  ⚠ 404 — skipping (class listed in catalog but no detail endpoint)")
            skipped_classes.append(name)
            continue
        out_path.write_text(json.dumps(data, indent=2))
        time.sleep(REQUEST_DELAY)

    # fetch objects — always fetch all, since classes reference them
    target_objects = list(objects_catalog.keys())
    print(f"[4/4] Fetching {len(target_objects)} object definitions...", flush=True)
    skipped_objects = []
    for i, name in enumerate(target_objects, 1):
        out_path = objects_dir / f"{name}.json"
        if out_path.exists():
            if i % 25 == 0:
                print(f"      [{i}/{len(target_objects)}] ...")
            continue
        if i % 25 == 0 or i <= 3:
            print(f"      [{i}/{len(target_objects)}] {name}")
        data = fetch_object(version, name)
        if data is None:
            skipped_objects.append(name)
            continue
        out_path.write_text(json.dumps(data, indent=2))
        time.sleep(REQUEST_DELAY)

    if skipped_classes:
        print(f"\n⚠ {len(skipped_classes)} classes had no detail endpoint (404): {skipped_classes}")
    if skipped_objects:
        print(f"⚠ {len(skipped_objects)} objects had no detail endpoint (404): {skipped_objects[:10]}{'...' if len(skipped_objects) > 10 else ''}")

    # write indices — only include classes that actually have a cached file
    class_index = build_index(classes_catalog)
    fetched_names = {p.stem for p in classes_dir.glob("*.json")}
    class_index = {uid: meta for uid, meta in class_index.items() if meta["name"] in fetched_names}
    (cache_dir / "_index.json").write_text(json.dumps(class_index, indent=2, sort_keys=True))
    (cache_dir / "_objects_index.json").write_text(
        json.dumps(sorted(objects_catalog.keys()), indent=2)
    )
    (cache_dir / "_meta.json").write_text(json.dumps({
        "ocsf_version": version,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "class_count": len(target_classes),
        "object_count": len(target_objects),
        "partial": bool(only_classes),
    }, indent=2))

    print(f"\nDone. Cache at: {cache_dir}")
    print(f"  classes: {len(target_classes)}")
    print(f"  objects: {len(target_objects)}")
    print(f"  index:   {cache_dir}/_index.json")

    return class_index


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="OCSF version, e.g. 1.8.0")
    ap.add_argument("--cache-dir", default=None,
                    help="cache root (default: ./ocsf_schema_cache)")
    ap.add_argument("--only-classes", default=None,
                    help="comma-separated class_uids to fetch (skips others); objects are always fully fetched")
    args = ap.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else (
        Path(__file__).parent / "ocsf_schema_cache"
    )
    cache_dir = cache_dir / args.version

    only_classes = args.only_classes.split(",") if args.only_classes else None

    try:
        fetch_all(args.version, cache_dir, only_classes=only_classes)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
