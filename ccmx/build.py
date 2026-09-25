"""Assemble the analysis dataset from the crawl, and REPORT (not choose) the
candidate outcome variables so the definition is made from evidence.

Inputs (data/raw/):
  nodes_graph.jsonl        authoritative for structure + most attributes
  edges_embedded.jsonl     parent->child edges harvested from embedded lists
  edges_query.jsonl        parent->child edges from sources= for overflow tracks
  nodes_meta.jsonl         OPTIONAL. If present, left-joins num_playlists only;
                           every other field comes from the graph pass. Absent
                           is fine and expected.

Outputs (data/processed/):
  nodes.csv                one row per upload + outcome columns + has_meta flag
  edges.csv                deduped union, typed local/pool, dated on both ends
  remix_graph_local.graphml
  remix_graph_all.graphml  (local + pool)
  summary.json             degree + cascade-depth distributions for BOTH edge
                           definitions, outcome base rates, data-quality flags

No feature that mixes time periods is computed here. Author-history and
network-position features for RQ2 are built downstream, each recomputed at its
own track's timestamp - not globally - which is why the raw dates are carried on
every edge.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path

import networkx as nx
import pandas as pd

from .phases import read_jsonl

log = logging.getLogger(__name__)

NODE_COLUMNS = [
    "upload_id", "user_name", "user_id", "user_real_name", "name",
    "date_iso", "date_unix", "year", "license", "license_url",
    "score", "num_scores", "num_playlists", "thumbs_up", "num_files",
    "n_remixes", "n_sources", "usertags", "systags", "page_url",
]

# tags_all and ccud are deliberately EXCLUDED from the feature-facing node table:
# ccud carries site-derived status tags (e.g. "in_remix") that leak the outcome.
# usertags (author-chosen) is the safe tag column and is kept.


def _load_nodes(raw_dir: Path) -> pd.DataFrame:
    nodes = pd.DataFrame(read_jsonl(raw_dir / "nodes_graph.jsonl"))
    if nodes.empty:
        raise SystemExit("nodes_graph.jsonl is empty - run the crawl first")
    nodes = nodes.drop_duplicates("upload_id").sort_values("upload_id")

    meta_path = raw_dir / "nodes_meta.jsonl"
    nodes["has_meta"] = False
    if meta_path.exists():
        meta = pd.DataFrame(read_jsonl(meta_path)).drop_duplicates("upload_id")
        if not meta.empty and "num_playlists" in meta:
            playlists = meta.set_index("upload_id")["num_playlists"]
            nodes["num_playlists"] = nodes["upload_id"].map(playlists).where(
                lambda s: s.notna(), nodes.get("num_playlists")
            )
            nodes["has_meta"] = nodes["upload_id"].isin(meta["upload_id"])
            log.info("merged num_playlists for %d nodes from meta pass", len(meta))

    for col in NODE_COLUMNS + ["has_meta"]:
        if col not in nodes:
            nodes[col] = pd.NA
    return nodes[NODE_COLUMNS + ["has_meta"]]


def _load_edges(raw_dir: Path, node_ids: set[int]) -> pd.DataFrame:
    frames = []
    for fname in ("edges_embedded.jsonl", "edges_query.jsonl"):
        rows = list(read_jsonl(raw_dir / fname))
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return pd.DataFrame(columns=["parent_id", "child_id", "edge_type", "source"])
    edges = pd.concat(frames, ignore_index=True)

    # An edge may be seen both embedded and via query; keep one, prefer 'query'
    # since that side is authoritative for overflow tracks. Dedup on the pair.
    edges["_pref"] = (edges["source"] == "query").astype(int)
    edges = (
        edges.sort_values("_pref", ascending=False)
        .drop_duplicates(["parent_id", "child_id"])
        .drop(columns="_pref")
    )

    # Normalise edge_type to the two categories the analysis uses. Anything whose
    # parent isn't an on-site node is 'pool' (off-site sample reuse or unknown).
    def canon(row) -> str:
        if row["edge_type"] in ("local",):
            return "local" if row["parent_id"] in node_ids else "pool"
        if row["edge_type"] in ("pool", "pool_or_unknown"):
            return "pool"
        return "local" if row["parent_id"] in node_ids else "pool"

    edges["edge_type"] = edges.apply(canon, axis=1)
    return edges


def build(raw_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    nodes = _load_nodes(raw_dir)
    node_ids = set(int(x) for x in nodes["upload_id"])
    edges = _load_edges(raw_dir, node_ids)

    dates = nodes.set_index("upload_id")["date_unix"]
    edges["child_date_unix"] = edges["child_id"].map(dates)
    edges["parent_date_unix"] = edges["parent_id"].map(dates)
    both = edges["child_date_unix"].notna() & edges["parent_date_unix"].notna()
    edges["time_inverted"] = both & (edges["child_date_unix"] < edges["parent_date_unix"])

    # ---- cycle resolution ------------------------------------------------
    # A remix lineage must flow forward in time, so a directed cycle is
    # impossible in the real world and is either a data error or a genuine
    # mutual-sampling relationship the crawl flattened into two edges. We remove
    # the offending edges using the one ground truth we trust - upload dates -
    # and record exactly what was removed so the step is auditable.
    #
    # Rule, applied in order:
    #   1. Drop time-inverted edges (parent posted AFTER child). These are
    #      unambiguously wrong: you cannot remix a track that does not yet exist.
    #   2. If any cycles remain among same-day or undated edges, drop the edge
    #      that closes each cycle (the within-cycle edge whose child is oldest),
    #      which is the minimal, deterministic way to restore acyclicity.
    edges["_removed_reason"] = ""
    edges.loc[edges["time_inverted"], "_removed_reason"] = "time_inverted"

    kept = edges[edges["_removed_reason"] == ""].copy()
    resolved = _break_remaining_cycles(kept, dates)
    removed_cycle_ids = resolved["removed_edge_index"]
    edges.loc[edges.index.isin(removed_cycle_ids), "_removed_reason"] = "cycle_same_day"

    edges_kept = edges[edges["_removed_reason"] == ""].drop(columns="_removed_reason")
    edges_removed = edges[edges["_removed_reason"] != ""]

    nodes.to_csv(out_dir / "nodes.csv", index=False)
    edges_kept.to_csv(out_dir / "edges.csv", index=False)
    if len(edges_removed):
        edges_removed.to_csv(out_dir / "edges_removed.csv", index=False)

    edges = edges_kept  # everything downstream uses the acyclic set

    # ---- two graphs: local-only, and local+pool -------------------------
    summary: dict = {"data_quality_flags": {}, "outcomes": {}, "variants": {}}
    q = summary["data_quality_flags"]
    q["n_nodes"] = int(len(nodes))
    q["n_nodes_missing_date"] = int(nodes["date_iso"].isna().sum())
    q["n_nodes_missing_user"] = int(nodes["user_name"].isna().sum())
    q["n_edges_before_cycle_fix"] = int(len(edges_kept) + len(edges_removed))
    q["n_edges_kept"] = int(len(edges_kept))
    q["n_edges_removed_time_inverted"] = int((edges_removed["_removed_reason"] == "time_inverted").sum())
    q["n_edges_removed_cycle_same_day"] = int((edges_removed["_removed_reason"] == "cycle_same_day").sum())
    q["edge_type_counts"] = {k: int(v) for k, v in edges["edge_type"].value_counts().items()}
    q["edge_source_counts"] = {k: int(v) for k, v in edges["source"].value_counts().items()}
    q["edges_pool_parent_offsite"] = int((edges["edge_type"] == "pool").sum())

    graph_local = _make_graph(nodes, edges[edges["edge_type"] == "local"], node_ids)
    graph_all = _make_graph(nodes, edges, node_ids)
    nx.write_graphml(graph_local, out_dir / "remix_graph_local.graphml")
    nx.write_graphml(graph_all, out_dir / "remix_graph_all.graphml")

    for name, g in (("local", graph_local), ("all", graph_all)):
        summary["variants"][name] = _structural_summary(g)

    # ---- candidate outcome variables, REPORTED for both edge sets -------
    for name, g in (("local", graph_local), ("all", graph_all)):
        out_deg = dict(g.out_degree())
        received = pd.Series(out_deg, dtype="int64")
        n = len(received)
        remixed_once = int((received > 0).sum())

        # "spawned a further remix": a track with a child that itself has a child
        grandparent = sum(
            1 for node in g.nodes
            if any(g.out_degree(c) > 0 for c in g.successors(node))
        )
        summary["outcomes"][name] = {
            "n_nodes": n,
            "remixed_at_least_once": remixed_once,
            "base_rate_remixed_once": round(remixed_once / max(n, 1), 4),
            "spawned_further_remix": grandparent,
            "base_rate_spawned_further": round(grandparent / max(n, 1), 4),
            "remixes_received_histogram": _hist(received),
        }

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _break_remaining_cycles(edges: pd.DataFrame, dates: pd.Series) -> dict:
    """Break any directed cycles left after dropping time-inverted edges.

    Returns the DataFrame indices of edges to remove. Deterministic: for each
    cycle found, remove the single edge whose child upload_id is smallest (the
    oldest track in the cycle), which reliably opens the loop. Repeats until the
    graph is acyclic. Same-day mutual remixes are the expected cause, so this
    should touch very few edges; if it touches many, that's a signal worth
    reporting rather than hiding.
    """
    g = nx.DiGraph()
    edge_index = {}
    for idx, r in edges.iterrows():
        p, c = int(r["parent_id"]), int(r["child_id"])
        g.add_edge(p, c)
        edge_index[(p, c)] = idx

    removed = []
    guard = 0
    while not nx.is_directed_acyclic_graph(g):
        guard += 1
        if guard > 100000:
            log.error("cycle-breaking exceeded guard; stopping")
            break
        try:
            cycle = next(iter(nx.simple_cycles(g)))
        except StopIteration:
            break
        # edges of this cycle: (cycle[i] -> cycle[i+1]) ... -> back to cycle[0]
        pairs = [(cycle[i], cycle[(i + 1) % len(cycle)]) for i in range(len(cycle))]
        # remove the edge whose child (the arrowhead) is the oldest id present
        victim = min(pairs, key=lambda pc: pc[1])
        g.remove_edge(*victim)
        if victim in edge_index:
            removed.append(edge_index[victim])

    return {"removed_edge_index": set(removed)}


def _make_graph(nodes: pd.DataFrame, edges: pd.DataFrame, node_ids: set[int]) -> nx.DiGraph:
    g = nx.DiGraph()
    for row in nodes.itertuples(index=False):
        g.add_node(
            int(row.upload_id),
            user=str(row.user_name) if pd.notna(row.user_name) else "",
            date_unix=int(row.date_unix) if pd.notna(row.date_unix) else -1,
        )
    for r in edges.itertuples(index=False):
        p, c = int(r.parent_id), int(r.child_id)
        # pool parents are off-site: add them as nodes so the edge exists, but
        # flag them so they're distinguishable from crawled on-site tracks.
        if p not in g:
            g.add_node(p, user="", date_unix=-1, offsite=True)
        if c in g:
            g.add_edge(p, c)
    return g


def _structural_summary(g: nx.DiGraph) -> dict:
    out_deg = dict(g.out_degree())
    in_deg = dict(g.in_degree())
    s = {
        "n_nodes": g.number_of_nodes(),
        "n_edges": g.number_of_edges(),
        "remixes_received": _dist(out_deg),
        "sources_used": _dist(in_deg),
        "is_dag": nx.is_directed_acyclic_graph(g),
    }
    if s["is_dag"]:
        depth = _depth(g)
        s["cascade_depth_distribution"] = dict(sorted(Counter(depth.values()).items()))
        s["max_cascade_depth"] = max(depth.values(), default=0)
    else:
        s["cascade_depth_distribution"] = None
        s["cycles_example"] = [list(c) for c in list(nx.simple_cycles(g))[:3]]

    weak = sorted((len(c) for c in nx.weakly_connected_components(g)), reverse=True)
    s["n_components"] = len(weak)
    s["largest_component"] = weak[0] if weak else 0
    return s


def _depth(g: nx.DiGraph) -> dict[int, int]:
    depth: dict[int, int] = {}
    for node in nx.topological_sort(g):
        preds = list(g.predecessors(node))
        depth[node] = 0 if not preds else 1 + max(depth[p] for p in preds)
    return depth


def _dist(degrees: dict) -> dict:
    counts = Counter(degrees.values())
    total = sum(counts.values()) or 1
    return {
        "counts_head": dict(sorted(counts.items())[:15]),
        "max": max(counts) if counts else 0,
        "mean": round(sum(k * v for k, v in counts.items()) / total, 4),
        "share_zero": round(counts.get(0, 0) / total, 4),
    }


def _hist(series: pd.Series) -> dict:
    buckets = {"0": 0, "1": 0, "2": 0, "3-5": 0, "6-10": 0, "11-25": 0, "26+": 0}
    for v in series:
        if v == 0: buckets["0"] += 1
        elif v == 1: buckets["1"] += 1
        elif v == 2: buckets["2"] += 1
        elif v <= 5: buckets["3-5"] += 1
        elif v <= 10: buckets["6-10"] += 1
        elif v <= 25: buckets["11-25"] += 1
        else: buckets["26+"] += 1
    return buckets
