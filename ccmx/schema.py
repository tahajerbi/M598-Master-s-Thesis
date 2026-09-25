"""Normalisation of raw ccHost rows into a stable node schema.

Field names and formats in this file were VERIFIED against real API responses
(see data/raw/explore/) rather than guessed. Notes on what was learned:

* Two date formats exist under overlapping key names:
    - upload_date_format (upload_list_wide, default) -> "Mon, Jul 20, 2026 @ 9:30 PM"
    - upload_date        (upload_page, info)         -> "Sat, Nov 6, 2004 @ 4:27 AM"
    - upload_date        (links_by)                  -> "2026-07-20 21:30:23"
  So the same key carries different formats depending on the dataview. Both are
  handled; anything unparseable stays None and is counted, never guessed.

* ccHost OMITS count keys when the value is zero. Verified: upload 71041 has
  upload_num_sources=1 and no upload_num_remixes key; upload 71040 has both.
  Therefore absent count == 0, and the count fields are defaulted to 0 rather
  than None. This is the one place we deliberately fill a missing value, and it
  only applies to the fields in ZERO_DEFAULT_FIELDS.

* upload_extra is a parsed dict in most dataviews but a PHP-serialised STRING in
  links_by. We only read it when it is already a dict.

* LEAKAGE WARNING: upload_tags concatenates three different tag sources -
  usertags (author-chosen), systags (format metadata) and ccud (site-derived
  status tags). ccud contains values like "in_remix" and "remix", which encode
  the outcome variable. Only `usertags` is safe as an RQ2 feature. The fields
  are kept separate here precisely so that cannot happen by accident.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# field -> candidate source keys, highest priority first
NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "upload_id": ("upload_id",),
    "user_name": ("user_name",),
    "user_id": ("user_id", "upload_user"),
    "user_real_name": ("user_real_name",),
    "name": ("upload_name", "upload_name_chop"),
    "date_raw": ("upload_date_format", "upload_date"),
    "license": ("license_name",),
    "license_url": ("license_url",),
    "num_scores": ("upload_num_scores",),
    "score": ("upload_score",),
    "num_playlists": ("upload_num_playlists",),
    "thumbs_up": ("thumbs_up",),
    "num_files": ("num_files",),
    "n_remixes": ("upload_num_remixes",),
    "n_sources": ("upload_num_sources",),
    "tags_all": ("upload_tags",),          # LEAKY - contains ccud status tags
    "page_url": ("file_page_url",),
    "year": ("year",),
}

# read out of the nested upload_extra dict
EXTRA_FIELDS: dict[str, str] = {
    "usertags": "usertags",     # author-chosen tags - the safe ones
    "systags": "systags",       # format metadata: mp3, 44k, stereo...
    "ccud": "ccud",             # site-derived status tags - LEAKY
    "bpm": "bpm",
    "num_reviews": "num_reviews",
    "featuring": "featuring",
    "nsfw": "nsfw",
    "ccplus": "ccplus",
}

INT_FIELDS = (
    "upload_id", "user_id", "num_scores", "score", "num_playlists",
    "thumbs_up", "num_files", "n_remixes", "n_sources", "year",
    "bpm", "num_reviews",
)

# ccHost omits these keys entirely when the value is zero (verified).
ZERO_DEFAULT_FIELDS = ("n_remixes", "n_sources")

_DATE_FORMATS = (
    "%a, %b %d, %Y @ %I:%M %p",   # "Mon, Jul 20, 2026 @ 9:30 PM"  (most rows)
    "%Y-%m-%d %H:%M:%S",          # "2026-07-20 21:30:23"          (links_by)
    "%a, %b %d, %Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _first(row: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in row and row[key] not in ("", None):
            return row[key]
    return None


def _to_int(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def parse_timestamp(date_raw) -> tuple[str | None, int | None]:
    """Return (iso8601, unix). Both None if unparseable - never guessed."""
    if not date_raw:
        return None, None
    text = re.sub(r"\s+", " ", str(date_raw)).strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # ccMixter renders local site time with no zone; treat as UTC and say so
        # in the thesis. Day-level resolution is what the temporal split needs.
        dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(), int(dt.timestamp())
    return None, None


def split_tags(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[,\s]+", str(value))
    return ",".join(p for p in (x.strip() for x in parts) if p) or None


def normalise_node(row: dict) -> dict[str, Any]:
    node = {f: _first(row, keys) for f, keys in NODE_FIELDS.items()}

    extra = row.get("upload_extra")
    if isinstance(extra, dict):
        for out_name, key in EXTRA_FIELDS.items():
            node[out_name] = extra.get(key)
    else:
        for out_name in EXTRA_FIELDS:
            node.setdefault(out_name, None)

    for field in INT_FIELDS:
        if field in node:
            node[field] = _to_int(node[field])
    for field in ZERO_DEFAULT_FIELDS:
        if node.get(field) is None:
            node[field] = 0

    node["date_iso"], node["date_unix"] = parse_timestamp(node.pop("date_raw"))
    for field in ("tags_all", "usertags", "systags", "ccud"):
        node[field] = split_tags(node.get(field))
    for field in ("nsfw", "ccplus"):
        node[field] = bool(node[field]) if node.get(field) is not None else None

    # Embedded lineage, truncated by the API. Kept raw here; edges are extracted
    # in phases.extract_embedded_edges so the completeness logic lives in one place.
    node["_remix_parents"] = row.get("remix_parents")
    node["_remix_children"] = row.get("remix_children")
    node["_parents_overflow"] = bool(row.get("more_parents_link"))
    node["_children_overflow"] = bool(row.get("children_overflow"))
    return node


def classify_link(entry: dict) -> tuple[str, int | None]:
    """Classify one remix_parents/remix_children entry.

    Returns (edge_type, id). On-site tracks carry upload_id; sample-pool items
    carry pool_item_id and point off-site (SoundCloud, personal sites, ...).
    """
    if entry.get("upload_id") is not None:
        return "local", _to_int(entry["upload_id"])
    if entry.get("pool_item_id") is not None:
        return "pool", _to_int(entry["pool_item_id"])
    return "unknown", None


def field_coverage(rows: list[dict]) -> dict[str, dict]:
    report: dict[str, dict] = {}
    for field, keys in NODE_FIELDS.items():
        found = {k: sum(1 for r in rows if r.get(k) not in ("", None)) for k in keys}
        chosen = next((k for k in keys if found.get(k)), None)
        report[field] = {
            "chosen_key": chosen,
            "n_present": found.get(chosen, 0) if chosen else 0,
            "candidates_seen": {k: v for k, v in found.items() if v},
        }
    return report
