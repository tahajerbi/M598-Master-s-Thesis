# Predicting Creative Reuse in Online Music Communities

**Structure, Network Position and Source Recommendation**

Code, data and results for the M598 Master's thesis by **Taha Jerbi**
(Master of Data Science, AI, and Digital Business, Gisma University of Applied Sciences, Summer 2026).
Supervisor: Dr. Trung Nghia Duong.

This repository contains everything needed to reproduce every table, figure and
number in Chapters 4 and 5 of the thesis. **You do not need internet access or to
re-crawl anything:** the crawled data is included, and the notebooks run offline.

---

## 1. Quick start (≈ 20–25 minutes)

```bash
# 1. Get the code
git clone https://github.com/tahajerbi/M598-Master-s-Thesis.git
cd M598-Master-s-Thesis

# 2. Create an isolated Python 3.12 environment
python -m venv .venv
#   Windows:        .venv\Scripts\activate
#   macOS / Linux:  source .venv/bin/activate

# 3. Install the pinned packages
pip install -r requirements.txt

# 4. Check that the crawler and graph builder work (≈ 5 seconds, no network)
python tests/test_offline.py            # ends with "All checks passed."

# 5. Open the notebooks
jupyter lab                             # or: jupyter notebook
```

Then open and run the notebooks **in numerical order** (Section 3), using
*Run → Run All Cells*. Every notebook must be started from the repository root,
because all paths are relative to it (for example `data/processed/features.csv`).

To run everything non-interactively instead:

```bash
for nb in 01_eda_2 02_preprocess 03_features_split 04_modelling \
          05_rq3_graph 05b_rq3_hardening 06_rq3_recommend 07_rq3_embedding; do
  jupyter nbconvert --to notebook --execute --inplace \
          --ExecutePreprocessor.timeout=3000 "$nb.ipynb"
done
```

(On Windows PowerShell, run the `jupyter nbconvert ...` line once per notebook.)

All notebooks are committed **with their outputs**, so you can also read the
results directly on GitHub without running anything.

---

## 2. Requirements

| Item | Version used |
|---|---|
| Python | **3.12** (developed on Python 3.12.10 under Windows; also tested on Linux with Python 3.12) |
| pandas | 3.0.5 |
| numpy | 1.26.4 |
| scikit-learn | 1.9.1 |
| networkx | 3.6.1 |
| node2vec | 0.5.0 (with gensim 4.4.0) |
| matplotlib | 3.11.2 |
| JupyterLab | 4.6.3 |

All versions are pinned in `requirements.txt`. The only Windows-specific package
(`pywinpty`) is restricted to Windows, so the same file installs on macOS and Linux.

- **Use Python 3.12.** Python 3.13 or newer will not work, because the pinned numpy 1.26.4 has no build for it.
- **Disk space:** about 220 MB for the repository and about 750 MB for the environment.
- **Memory:** 8 GB RAM is sufficient.
- **GPU:** not needed.
- **Internet:** only needed for `git clone` and `pip install`.

---

## 3. What each notebook does and how long it takes

Timings were measured on a 2-core machine; a typical laptop is similar or faster. Each notebook reads the
outputs of the previous ones and writes its own outputs to disk.

| # | Notebook | Thesis section | What it does | Main outputs | Time |
|---|---|---|---|---|---|
| 1 | `01_eda_2.ipynb` | 4.3, 4.4, 4.5 | Exploratory analysis and data-quality audit: missingness, label source, DAG check, cascade depth, components, right-censoring, removed edges | `figures/*.png` | < 1 min |
| 2 | `02_preprocess.ipynb` | 4.5 | Drops uninformative columns, builds the outcome labels from the lineage graph | `data/processed/nodes_clean.csv` | < 1 min |
| 3 | `03_features_split.ipynb` | 4.6, 4.7 | Horizon labels, temporal split with embargo, the 51 leakage-safe features, automated leakage self-checks | `data/processed/features.csv` | < 1 min |
| 4 | `04_modelling.ipynb` | 4.8, 5.1, 5.2, 5.3 | **RQ1 & RQ2:** tuning on a temporal validation slice, baselines vs gradient boosting at 6/12/24 months, bootstrap CIs, calibration, (grouped) permutation importance, robustness checks | `results_summary.csv`, `results_grouped_importance.csv`, `fig_04_summary.png` | ≈ 1–2 min |
| 5 | `05_rq3_graph.ipynb` | 4.9, 5.4 | **RQ3a:** yearly as-of graph snapshots, structural features, node vs graph vs combined | `fig_05_rq3.png` | ≈ 1–2 min |
| 6 | `05b_rq3_hardening.ipynb` | 4.9, 5.4 | **RQ3a:** half-year snapshots and node2vec summaries | `fig_05b_rq3_hardened.png` | ≈ 3–4 min |
| 7 | `06_rq3_recommend.ipynb` | 4.10, 5.5 | **RQ3b:** source recommendation under random / recent / age-matched negatives; freezes the evaluation candidate sets | `results_rq3_recommend.csv`, `data/processed/rq3_eval_sets.pkl`, `fig_06_rq3_recommend.png` | ≈ 3–4 min |
| 8 | `07_rq3_embedding.ipynb` | 5.6 | **RQ3b:** adds node2vec taste vectors to the ranker, evaluated on the frozen candidate sets | `results_rq3_embedding.csv`, `fig_07_rq3_embedding.png` | ≈ 5–6 min |

`01_eda.ipynb` is an earlier draft of the exploratory analysis, kept for the
record. **`01_eda_2.ipynb` is the version used in the thesis.**

Notebook 07 depends on `data/processed/rq3_eval_sets.pkl`, which notebook 06
writes. Run 06 before 07.

---

## 4. Where to find the numbers reported in the thesis

| Thesis item | File / notebook cell |
|---|---|
| Table 4.1, Section 4.2 (crawl evidence) | `data/raw/verify_report.json`, `data/raw/nodes_graph_failures.jsonl`, `data/raw/edges_query.done.jsonl` |
| Sections 4.3–4.5 (dataset statistics, label source) | `data/processed/summary.json`, `01_eda_2.ipynb` |
| Table 4.2, Figure 4.1 (temporal split) | `03_features_split.ipynb`, Section 2 |
| Table 4.3 (features) | `03_features_split.ipynb`, Sections 3–4 |
| Table 5.1, Figure 5.1 (RQ2) | `results_summary.csv`, `04_modelling.ipynb` Section 2 |
| Figure 5.2 (calibration) | `04_modelling.ipynb` Section 3 |
| Figure 5.3 (RQ1, grouped importance) | `results_grouped_importance.csv`, `04_modelling.ipynb` Section 4b |
| Section 5.3 (robustness) | `04_modelling.ipynb` Sections 5 and 7 |
| Table 5.2 (RQ3a) | `05_rq3_graph.ipynb`, `05b_rq3_hardening.ipynb` |
| Table 5.3, Figure 5.4 (RQ3b) | `results_rq3_recommend.csv`, `06_rq3_recommend.ipynb` |
| Section 5.6 (embeddings) | `results_rq3_embedding.csv`, `07_rq3_embedding.ipynb` |

---

## 5. Reproducibility: what to expect

All random seeds are fixed. Re-running the notebooks gives:

- **Identical results** for the dataset statistics, the temporal split, all RQ1
  and RQ2 results (Table 5.1, Figures 5.2–5.3, Section 5.3) and the yearly RQ3a
  experiment (`05_rq3_graph.ipynb`).
- **Small differences, usually in the third or fourth decimal place,** in three notebooks:
  - `05b_rq3_hardening.ipynb` and `07_rq3_embedding.ipynb`: node2vec is fitted
    with 4 worker threads, which is not bit-for-bit deterministic (Section 4.11 of the thesis).
  - `06_rq3_recommend.ipynb`: candidate sampling depends on the order of tracks
    that share the same upload timestamp, and that order can differ between
    operating systems. Individual MRR values can move by about ±0.01. Every
    comparison and conclusion in Section 5.5 is unchanged.

Re-running 06 overwrites `data/processed/rq3_eval_sets.pkl`. To reproduce
Section 5.6 against the exact candidate sets used in the thesis, run only
notebook 07 on the committed `rq3_eval_sets.pkl`, or restore it with
`git checkout data/processed/rq3_eval_sets.pkl`.

---

## 6. Repository structure

```
.
├── README.md                    this file
├── requirements.txt             pinned Python environment
├── ccmixter_dataset.py          command-line entry point for the crawler
├── ccmx/                        crawler package
│   ├── client.py                rate-limited, cached HTTP client (≥ 1 s between requests)
│   ├── phases.py                explore → crawl → edges → verify
│   ├── schema.py                normalises raw API records into the node schema
│   └── build.py                 builds node/edge tables, removes time-inverted edges, checks the DAG
├── tests/
│   └── test_offline.py          runs the whole crawl + build against a synthetic site
├── 01_eda.ipynb                 earlier EDA draft (not used in the thesis)
├── 01_eda_2.ipynb … 07_rq3_embedding.ipynb   analysis notebooks (Section 3)
├── results_*.csv                result tables written by notebooks 04, 06, 07
├── fig_*.png, figures/          figures written by the notebooks
└── data/
    ├── raw/                     crawled records (JSON Lines), verification report, API samples
    ├── processed/               node/edge tables, features, graphs, frozen RQ3b candidate sets
    ├── cache/                   gzipped API responses keyed by URL (makes the crawl resumable)
    └── bad_payloads/            the malformed API responses isolated during the crawl
```

Key data files:

| File | Contents |
|---|---|
| `data/processed/nodes.csv` | 51,486 tracks as crawled (one row per upload) |
| `data/processed/edges.csv` | 62,300 source → derivative edges (59,186 local, 3,114 pool) |
| `data/processed/edges_removed.csv` | the 684 time-inverted edges removed during cleaning |
| `data/processed/nodes_clean.csv` | cleaned node table with outcome labels (from notebook 02) |
| `data/processed/features.csv` | 51 features, horizon labels and split (from notebook 03) |
| `data/processed/summary.json` | dataset statistics used as self-checks by later notebooks |
| `data/processed/remix_graph_local.graphml` | the local remix DAG |

---

## 7. Optional: re-creating the dataset

**This is not needed to reproduce the results.** The data was crawled from the
public ccMixter Query API 2.0 (<https://ccmixter.org/query-api>) in July 2026,
covering uploads from 28 October 2004 to 20 July 2026.

Rebuild the processed tables from the committed raw crawl (offline, about 15 seconds):

```bash
python ccmixter_dataset.py build
```

Re-crawl from scratch (needs internet; sends a contact email in the User-Agent;
rate-limited to one request per second, so it takes roughly two hours):

```bash
python ccmixter_dataset.py explore --contact you@example.com
python ccmixter_dataset.py crawl   --contact you@example.com
python ccmixter_dataset.py edges   --contact you@example.com
python ccmixter_dataset.py verify  --contact you@example.com
python ccmixter_dataset.py build
```

Every response is cached in `data/cache/`, so an interrupted crawl can be
resumed at no extra cost. A new crawl will differ from the thesis data, because
the platform has changed since July 2026.

---

## 8. Data, licence and ethics

- The dataset contains only public track **metadata** (IDs, titles, usernames,
  dates, licences, tags and declared remix links). No audio was downloaded.
- The tracks themselves are published on ccMixter under various Creative Commons
  licences, about three quarters of them non-commercial. This repository is for
  academic, non-commercial research only.
- No human participants were involved.

---

## 9. Citation

> Jerbi, T. (2026) *Predicting Creative Reuse in Online Music Communities: Structure,
> Network Position and Source Recommendation*. Master's thesis, Gisma University of
> Applied Sciences.
