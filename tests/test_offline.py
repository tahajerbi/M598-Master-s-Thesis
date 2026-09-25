"""Offline smoke test: runs the whole pipeline against a fake ccMixter.

Does not touch the network. Verifies that id-chunk crawling, edge resolution,
resumability and the graph build all behave, and that a known cascade depth is
recovered exactly.

    python tests/test_offline.py
"""

from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ccmx.build import build
from ccmx.phases import crawl_edges, crawl_nodes, read_jsonl

TMP = Path(__file__).parent / "_tmp"


class FakeAPI:
    """Synthetic site: 300 uploads, a seeded remix DAG, ccHost-ish field names."""

    def __init__(self, n: int = 300, seed: int = 7) -> None:
        rng = random.Random(seed)
        self.n = n
        self.parents: dict[int, list[int]] = {}
        for uid in range(1, n + 1):
            if uid > 20 and rng.random() < 0.35:
                k = rng.choice([1, 1, 1, 2])
                self.parents[uid] = rng.sample(range(1, uid), k)
        self.children: dict[int, list[int]] = {}
        for child, ps in self.parents.items():
            for p in ps:
                self.children.setdefault(p, []).append(child)
        self.calls = 0

    def row(self, uid: int) -> dict:
        return {
            "upload_id": str(uid),
            "user_name": f"artist{uid % 25}",
            "upload_name": f"track {uid}",
            "upload_date_format": "2011-03-05 14:22:01",
            "license_name": "BY-NC",
            "upload_num_scores": str(uid % 7),
            "upload_num_remixes": str(len(self.children.get(uid, []))),
            "upload_num_sources": str(len(self.parents.get(uid, []))),
            "upload_tags": "ambient electronic",
        }

    def get_json(self, **params):
        self.calls += 1
        if "ids" in params:
            ids = [int(i) for i in str(params["ids"]).split(",")]
            return [self.row(i) for i in ids if 1 <= i <= self.n]
        if "sources" in params:
            return [self.row(p) for p in self.parents.get(int(params["sources"]), [])]
        if "remixes" in params:
            return [self.row(c) for c in self.children.get(int(params["remixes"]), [])]
        return [self.row(i) for i in range(1, min(self.n, int(params.get("limit", 10))) + 1)]


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)
    raw, processed = TMP / "raw", TMP / "processed"
    api = FakeAPI()

    print("crawl")
    n = crawl_nodes(api, raw / "nodes_graph.jsonl", max_id=api.n)
    check("all uploads captured", n == api.n, f"{n}/{api.n}")

    calls_before = api.calls
    n2 = crawl_nodes(api, raw / "nodes_graph.jsonl", max_id=api.n)
    check("rerun is a no-op (resumable)", n2 == 0 and api.calls == calls_before)

    nodes = list(read_jsonl(raw / "nodes_graph.jsonl"))
    check("dates parsed", all(x["date_unix"] for x in nodes))
    check("ints parsed", all(isinstance(x["n_remixes"], int) for x in nodes))
    check("tags normalised", nodes[0]["tags_all"] == "ambient,electronic", nodes[0]["tags_all"])

    print("edges")
    e = crawl_edges(api, raw / "nodes_graph.jsonl", raw / "edges_query.jsonl")
    n_e = e["embedded"] + e["queried"]
    expected = sum(len(v) for v in api.parents.values())
    check("edge count matches ground truth", n_e == expected, f"{n_e}/{expected}")
    calls_before = api.calls
    e2 = crawl_edges(api, raw / "nodes_graph.jsonl", raw / "edges_query.jsonl")
    check("edge rerun is a no-op",
          e2["embedded"] + e2["queried"] == 0 and api.calls == calls_before)

    edges = list(read_jsonl(raw / "edges_embedded.jsonl")) + \
            list(read_jsonl(raw / "edges_query.jsonl"))
    truth = {(p, c) for c, ps in api.parents.items() for p in ps}
    check("edge set is exactly right",
          {(x["parent_id"], x["child_id"]) for x in edges} == truth)
    check("all edges typed local", all(x["edge_type"] == "local" for x in edges))

    print("build")
    summary = build(raw, processed)
    local = summary["variants"]["local"]
    check("node count", local["n_nodes"] == api.n)
    check("edge count", local["n_edges"] == expected)
    check("graph is a DAG", local["is_dag"] is True)

    # independent recomputation of cascade depth
    depth: dict[int, int] = {}
    for uid in range(1, api.n + 1):
        ps = api.parents.get(uid, [])
        depth[uid] = 0 if not ps else 1 + max(depth[p] for p in ps)
    expected_dist = {}
    for d in depth.values():
        expected_dist[d] = expected_dist.get(d, 0) + 1
    got = {int(k): v for k, v in local["cascade_depth_distribution"].items()}
    check("cascade depth distribution", got == expected_dist, json.dumps(got))

    n_remixed = sum(1 for uid in range(1, api.n + 1) if api.children.get(uid))
    check("base rate of being remixed",
          summary["outcomes"]["local"]["remixed_at_least_once"] == n_remixed)

    for name in ("nodes.csv", "edges.csv", "remix_graph_local.graphml", "summary.json"):
        check(f"wrote {name}", (processed / name).exists())

    shutil.rmtree(TMP)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()