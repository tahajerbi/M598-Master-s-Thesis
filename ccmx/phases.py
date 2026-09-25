"""Crawl phases: explore -> crawl -> edges -> verify.

Revised after inspecting real API responses. Two changes matter:

1. `upload_list_wide` is the graph dataview - it is the only one carrying
   upload_num_remixes / upload_num_sources AND both remix_parents and
   remix_children. `upload_page` is crawled as a second pass for the fields
   upload_list_wide lacks (upload_score, upload_num_playlists, user_id,
   upload_tags). They merge on upload_id.

2. Lineage arrives embedded in the node rows, but TRUNCATED (verified: upload
   71040 declares 18 sources and lists 3, flagged by more_parents_link). So we
   harvest embedded edges for free and issue a `sources=` request only where the
   embedded list is provably incomplete. Most remixes have one or two parents,
   so this avoids the large majority of edge requests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Iterator

from .client import CCMixterClient, MalformedResponse 
from .schema import classify_link, field_coverage, normalise_node

log = logging.getLogger(__name__)

CANDIDATE_DATAVIEWS = ("upload_page", "info", "upload_list_wide", "links_by", "default")

GRAPH_DATAVIEW = "upload_list_wide"
META_DATAVIEW = "upload_page"


# ---------------------------------------------------------------- io helpers


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("a", encoding="utf-8")

    def write(self, obj: dict) -> None:
        self.fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.fh.flush()

    def close(self) -> None:
        self.fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def chunked(seq: Iterable, size: int) -> Iterator[list]:
    batch: list = []
    for item in seq:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# -------------------------------------------------------------------- explore


def explore(client: CCMixterClient, out_dir: Path, sample: int = 5) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"max_upload_id": None, "dataviews": {}}

    newest = client.get_json(sort="id", ord="DESC", limit=1, dataview="links_by")
    if newest:
        report["max_upload_id"] = max(
            (normalise_node(r).get("upload_id") or 0) for r in newest
        )

    for view in CANDIDATE_DATAVIEWS:
        try:
            rows = client.get_json(dataview=view, limit=sample, sort="date", ord="DESC")
        except RuntimeError as exc:
            report["dataviews"][view] = {"error": str(exc)}
            continue
        (out_dir / f"sample_{view}.json").write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        report["dataviews"][view] = {
            "n_rows": len(rows),
            "keys": sorted({k for r in rows for k in r}),
            "coverage": field_coverage(rows),
        }

    try:
        remixes = client.get_json(dataview="links_by", limit=1, sort="date",
                                  ord="DESC", remixmin=1)
        probe_id = normalise_node(remixes[0]).get("upload_id") if remixes else None
        report["edge_probe_upload_id"] = probe_id
        for direction in ("sources", "remixes"):
            if probe_id is None:
                break
            try:
                rows = client.get_json(**{direction: probe_id},
                                       dataview="links_by", limit=100)
            except RuntimeError as exc:
                report.setdefault("edges", {})[direction] = {"error": str(exc)}
                continue
            (out_dir / f"sample_edge_{direction}.json").write_text(
                json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            report.setdefault("edges", {})[direction] = {
                "n_rows": len(rows),
                "keys": sorted({k for r in rows for k in r}),
            }
    except RuntimeError as exc:
        report.setdefault("edges", {})["probe_error"] = str(exc)

    (out_dir / "explore_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------------- crawl


def _fetch_ids(client, ids: list[int], dataview: str, failures: "JsonlWriter") -> list[dict]:
    """Fetch a batch, bisecting around records the server can't serialise.

    ccMixter returns byte-identical malformed JSON for certain ids, so a failed
    batch is split until the culprit is isolated to a single id. That id is
    logged and skipped; the rest of the batch is salvaged. Worst case this costs
    ~2x requests for an affected chunk, which is far cheaper than losing 50
    tracks or hand-nursing the crawl.
    """
    try:
        return client.get_json(ids=",".join(map(str, ids)), limit=len(ids),
                               dataview=dataview)
    except MalformedResponse as exc:
        if len(ids) == 1:
            log.error("unparseable record, skipping upload_id=%d", ids[0])
            failures.write({"upload_id": ids[0], "error": str(exc)})
            return []
        mid = len(ids) // 2
        log.warning("malformed batch of %d, bisecting", len(ids))
        return (_fetch_ids(client, ids[:mid], dataview, failures)
                + _fetch_ids(client, ids[mid:], dataview, failures))


def crawl_nodes(
    client: CCMixterClient,
    out_path: Path,
    max_id: int,
    dataview: str = GRAPH_DATAVIEW,
    start_id: int = 1,
    chunk: int = 50,
) -> int:
    seen = {row.get("upload_id") for row in read_jsonl(out_path)}
    done_chunks = {i // chunk for i in seen if i}
    log.info("resuming %s: %d nodes already stored", out_path.name, len(seen))

    written = 0
    failures_path = out_path.with_name(out_path.stem + "_failures.jsonl")
    with JsonlWriter(out_path) as writer, JsonlWriter(failures_path) as failures:
        for batch in chunked(range(start_id, max_id + 1), chunk):
            if batch[0] // chunk in done_chunks:
                continue
            rows = _fetch_ids(client, batch, dataview, failures)
            for row in rows:
                node = normalise_node(row)
                if node.get("upload_id") is None or node["upload_id"] in seen:
                    continue
                node["_dataview"] = dataview
                writer.write(node)
                seen.add(node["upload_id"])
                written += 1
            log.info("ids %d-%d -> %d rows (total %d)",
                     batch[0], batch[-1], len(rows), len(seen))
    return written


# ---------------------------------------------------------------------- edges


def extract_embedded_edges(node: dict) -> tuple[list[dict], bool]:
    """Pull edges out of one node's embedded remix_parents list.

    Returns (edges, needs_query). needs_query is True when the embedded list is
    provably incomplete: either the API set more_parents_link, or fewer parents
    are listed than upload_num_sources declares.

    Note we trust `sources=` over the embedded list when we do query, and we
    keep provenance on every edge so the two can be compared later.
    """
    child_id = node["upload_id"]
    listed = node.get("_remix_parents") or []
    declared = node.get("n_sources") or 0

    edges = []
    for entry in listed:
        edge_type, pid = classify_link(entry)
        if pid is None or pid == child_id:
            continue
        edges.append({
            "parent_id": pid,
            "child_id": child_id,
            "parent_user": entry.get("user_real_name"),
            "edge_type": edge_type,
            "source": "embedded",
        })

    needs_query = bool(node.get("_parents_overflow")) or len(edges) < declared
    return edges, needs_query


def crawl_edges(
    client: CCMixterClient,
    nodes_path: Path,
    out_path: Path,
    dataview: str = "links_by",
) -> dict:
    """Harvest embedded parent edges, then query `sources=` only where needed."""
    nodes = [n for n in read_jsonl(nodes_path) if n.get("upload_id") is not None]
    node_ids = {n["upload_id"] for n in nodes}

    embedded_path = out_path.with_name("edges_embedded.jsonl")
    to_query: list[int] = []
    n_embedded = 0

    if not embedded_path.exists():
        with JsonlWriter(embedded_path) as writer:
            for node in nodes:
                edges, needs_query = extract_embedded_edges(node)
                for edge in edges:
                    writer.write(edge)
                    n_embedded += 1
                if needs_query:
                    to_query.append(node["upload_id"])
    else:
        n_embedded = sum(1 for _ in read_jsonl(embedded_path))
        for node in nodes:
            if extract_embedded_edges(node)[1]:
                to_query.append(node["upload_id"])

    # If nothing at all was derivative, the schema mapping is broken - say so
    # loudly rather than quietly producing an empty graph.
    if n_embedded == 0 and not to_query:
        log.error("no embedded edges and nothing to query - check the schema mapping "
                  "against data/raw/explore/sample_upload_list_wide.json")

    done_path = out_path.with_name("edges_query.done.jsonl")
    done = {row["child_id"] for row in read_jsonl(done_path)}
    log.info("%d embedded edges; %d tracks need a sources= query, %d already done",
             n_embedded, len(to_query), len(done))

    n_queried = 0
    with JsonlWriter(out_path) as writer, JsonlWriter(done_path) as marker:
        for i, child_id in enumerate(to_query, 1):
            if child_id in done:
                continue
            try:
                rows = client.get_json(sources=child_id, dataview=dataview, limit=200)
            except MalformedResponse as exc:
                log.error("unparseable sources= for child %d, skipping: %s", child_id, exc)
                marker.write({"child_id": child_id})
                continue
            for row in rows:
                parent = normalise_node(row)
                pid = parent.get("upload_id")
                if pid is None or pid == child_id:
                    continue
                writer.write({
                    "parent_id": pid,
                    "child_id": child_id,
                    "parent_user": parent.get("user_name"),
                    "edge_type": "local" if pid in node_ids else "pool_or_unknown",
                    "source": "query",
                })
                n_queried += 1
            marker.write({"child_id": child_id})
            if i % 200 == 0:
                log.info("edges: %d/%d queried children, %d edges",
                         i, len(to_query), n_queried)

    return {"embedded": n_embedded, "queried": n_queried, "n_query_targets": len(to_query)}


# --------------------------------------------------------------------- verify


def verify_edges(
    client: CCMixterClient,
    nodes_path: Path,
    edges_paths: list[Path],
    out_path: Path,
    sample_size: int = 200,
    seed: int = 42,
) -> dict:
    """Cross-check the parent-side edge set against the `remixes=` direction.

    If the two disagree, the graph is not what you think it is, and cascade
    depth and link-prediction labels all inherit the error.
    """
    import random

    nodes = [n for n in read_jsonl(nodes_path) if n.get("upload_id") is not None]
    have = set()
    for path in edges_paths:
        for edge in read_jsonl(path):
            have.add((edge["parent_id"], edge["child_id"]))

    parents = [n for n in nodes if (n.get("n_remixes") or 0) > 0]
    if not parents:
        return {"error": "no node has n_remixes > 0 - schema mapping is wrong"}

    rng = random.Random(seed)
    sample = rng.sample(parents, min(sample_size, len(parents)))

    missing, extra, checked = [], 0, 0
    for node in sample:
        pid = node["upload_id"]
        rows = client.get_json(remixes=pid, dataview="links_by", limit=200)
        children = {normalise_node(r).get("upload_id") for r in rows} - {None}
        checked += len(children)
        for cid in children:
            if (pid, cid) not in have:
                missing.append({"parent_id": pid, "child_id": cid})
        extra += len({c for (p, c) in have if p == pid} - children)

    result = {
        "sampled_parents": len(sample),
        "edges_seen_from_remixes_side": checked,
        "missing_from_parent_side": len(missing),
        "present_only_on_parent_side": extra,
        "agreement_rate": (1 - len(missing) / checked) if checked else None,
        "examples_missing": missing[:20],
    }
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
