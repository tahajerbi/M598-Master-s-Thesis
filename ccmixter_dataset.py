#!/usr/bin/env python3
"""Build a remix-lineage dataset from the ccMixter Query API.

Typical run:

    python ccmixter_dataset.py explore --contact you@example.com
    # read data/raw/explore/explore_report.json, tighten ccmx/schema.py if needed
    python ccmixter_dataset.py crawl  --contact you@example.com
    python ccmixter_dataset.py edges  --contact you@example.com
    python ccmixter_dataset.py verify --contact you@example.com
    python ccmixter_dataset.py build

Every phase is cached and resumable; interrupt with Ctrl-C and rerun freely.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ccmx.build import build
from ccmx.client import CCMixterClient
from ccmx.phases import crawl_edges, crawl_nodes, explore, verify_edges

ROOT = Path(__file__).parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
CACHE = ROOT / "data" / "cache"


def make_client(args) -> CCMixterClient:
    return CCMixterClient(
        contact=args.contact,
        cache_dir=CACHE,
        min_interval=args.rate,
        offline=getattr(args, "offline", False),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rate", type=float, default=1.0, help="min seconds between requests")
    parser.add_argument("--contact", default="", help="your email, sent in the User-Agent")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("explore", help="probe dataviews and report real JSON keys")
    p.add_argument("--sample", type=int, default=5)

    p = sub.add_parser("crawl", help="walk the upload id space")
    p.add_argument("--dataview", default="upload_list_wide")
    p.add_argument("--out", default="nodes_graph.jsonl")
    p.add_argument("--chunk", type=int, default=50)
    p.add_argument("--max-id", type=int, default=None)
    p.add_argument("--start-id", type=int, default=1)

    p = sub.add_parser("edges", help="resolve parent edges via sources=")
    p.add_argument("--dataview", default="links_by")

    p = sub.add_parser("verify", help="cross-check edges against the remixes= direction")
    p.add_argument("--sample-size", type=int, default=200)

    p = sub.add_parser("build", help="assemble csv/graphml + summary")
    p.add_argument("--edge-types", default="local", help="comma-separated: local,pool_or_unknown")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.cmd == "build":
        summary = build(RAW, PROCESSED)
        print(json.dumps(summary, indent=2)[:3000])
        print(f"\nWrote nodes.csv, edges.csv, remix_graph_local.graphml, "
              f"remix_graph_all.graphml, summary.json to {PROCESSED}")
        return 0

    client = make_client(args)

    if args.cmd == "explore":
        report = explore(client, RAW / "explore", sample=args.sample)
        print(json.dumps(report, indent=2)[:6000])
        print(f"\nFull report + raw samples in {RAW / 'explore'}")
        print("Check that the 'chosen_key' for upload_id, date and the remix counts "
              "look right before running `crawl`.")

    elif args.cmd == "crawl":
        max_id = args.max_id
        if max_id is None:
            report_path = RAW / "explore" / "explore_report.json"
            if not report_path.exists():
                print("Run `explore` first, or pass --max-id.", file=sys.stderr)
                return 1
            max_id = json.loads(report_path.read_text())["max_upload_id"]
        n = crawl_nodes(client, RAW / args.out, max_id=max_id, dataview=args.dataview,
                        start_id=args.start_id, chunk=args.chunk)
        print(f"Wrote {n} new nodes to {RAW / args.out} (cache: {client.stats})")

    elif args.cmd == "edges":
        stats = crawl_edges(client, RAW / "nodes_graph.jsonl", RAW / "edges_query.jsonl")
        print(f"edges: {stats}")

    elif args.cmd == "verify":
        result = verify_edges(client, RAW / "nodes_graph.jsonl",
                              [RAW / "edges_embedded.jsonl", RAW / "edges_query.jsonl"],
                              RAW / "verify_report.json", sample_size=args.sample_size)
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
