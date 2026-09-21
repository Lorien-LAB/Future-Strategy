# Calibration performance: measured costs and unchanged results

This work optimizes execution of the corrected v2 engine. It does not change MR500/Trend96 grids, selection rules, costs, chronology, account arithmetic, or reported strategy returns.

## Why the real run was slow

The original Haipu run used 69 products and annual calibration from 2021 through 2026. Each eligible product/year runs 500 MR candidates; the first two years can also evaluate 96 Trend candidates. Every candidate replayed the same contract selection and price history, including pre-training warmup. Calibration was serial despite 40 available CPUs.

An isolated benchmark used the original engine at `abbbd964c544f24b39225186dc57529617ce7e4a`, the actual Haipu-adapted RB daily Parquet, the original 2023 six-year training window, and the user's 10m equity/retrospective average-margin research inputs. The running baseline was not modified. Attaching py-spy to that process was denied by the container's process-inspection permissions, so cProfile ran in a separate process using the same code and real input.

The diagnostic grid retained all ten sigma and ten target values with one stop value (100 candidates). This diagnostic was only for attribution; acceptance benchmarks below use complete grids.

| Hot path | Cumulative profile seconds | Share of 86.15s fit | What was repeated |
|---|---:|---:|---|
| `Ledger.assert_reconciled` | 23.98 | 27.8% | Every active day rescanned all previous journal events: 95,566,694 event visits |
| `PairSelector.update` | 14.88 | 17.3% | The same OI rankings, confirmation state and contract dates for each candidate |
| `Features.signal_id` | 11.19 | 13.0% | Serialization and SHA-256 of an immutable signal identity at every use |
| `Model.model_id` | 5.48 | 6.4% | Serialization and SHA-256 of an unchanged immutable model |
| `PairHistory.snapshot` | 9.08 | 10.5% | The same window statistics and entry features across target/stop candidates |
| Cold `MarketData.prefix_hash` | 3.14 | 3.6% | Recursive dataclass copying of primitive Bar records |

The first one-candidate profile was dominated by cold prefix hashing (3.28 of 4.29s); that is **not** the sustained 500-grid bottleneck. The larger profile distinguishes once-per-fit work from repeated replay work. Profiled times include profiler overhead and are not used as wall-time speedup claims.

## Changes

1. Immutable Model/Features identifiers are memoized per instance. Their dataclass payloads, equality, replacement semantics and exact identifier strings remain unchanged; caches are not serialized dataclass fields.
2. The engine's append-only daily journal check reconciles only new events against a cached reconciled prefix. It still checks equity each day, and performs a complete journal audit before reporting success. Calling `Ledger.assert_reconciled()` without arguments still performs the original full audit. Historical record mutation is checked by this full audit; the engine does not mutate booked event dictionaries.
3. One `PreparedHistory` per product/year/family fit reuses the original selector and feature functions. Read-only sequence prefixes expose only each observation day's history, including empty-product dates and pre-source warmup. Snapshot keys include every model field read by the existing snapshot function; target/stop candidates retain their own Model and Signal identities.
4. Primitive Bar records avoid recursive deepcopy when building the same historical digest. No identity algorithm, keys, observation ordering or value coercion changes.

Caches are scoped to one fit and are not returned with the model. They do not become a second market-data store or another ledger. The ordinary Engine replay remains available for differential verification.

## Optional process parallelism

`calibrate` and `run` accept `--workers N`, default 1. Library `fit_schedule`, `calibrate` and `run` accept the same keyword-only runtime setting. Positive integer validation is explicit. This setting is outside Config and model/calibration identities.

For each year, spawned processes evaluate independent products; each product's family order and parameter order stay unchanged. Product-only payloads retain the original **whole-market observation calendar**, including empty-product days and pre-source warmup. The parent consumes results in original product order, then applies the unchanged annual VR pooling. Causally ordered cost-threshold fitting and ledger events remain sequential. Worker exceptions preserve partial Engine state and fills for the existing failure writer.

The process count is at most the requested count and number of products. Startup and serialization can outweigh gains for tiny jobs, so parallelism is opt-in. Product-boundary progress is printed in both modes; quiet mode remains quiet. This is not checkpoint/resume support.

Repeating the same 100-candidate profile after these changes reduced total function calls from 189.47 million to 23.72 million. Full/daily reconciliation cumulative time fell from 23.98s to 0.58s, and the entire profiled fit from 86.15s to 16.96s. This confirms removal of the measured repeated work; it is separate from the unprofiled timing comparison.

## Complete-grid real-data measurements

Linux, Python 3.11.16, same machine and input files, separate processes, unprofiled fit time. Data loading is measured separately by the benchmark utility.

| Exact work | Original | Optimized single process | Speedup |
|---|---:|---:|---:|
| RB 2023, complete MR500 | 142.470s | 26.647s | 5.35x |
| RB 2021, complete Trend96 | 19.083s | 4.479s | 4.26x |
| CU 2023, complete MR500 | 154.054s | 28.414s | 5.42x |

For RB/CU/AU/AG together in 2023, all **2,000 MR candidates** and the ordered annual model/VR aggregation were compared:

| Execution | Fit seconds | Versus original |
|---|---:|---:|
| Original, one process | 631.645 | 1.00x |
| Optimized, one process | 119.463 | 5.29x |
| Optimized, four spawned processes | 35.853 | 17.62x |

All three complete model dictionaries and ordered training audit rows are exactly equal, including annual VR thresholds. The four-process gain versus optimized one-process execution is 3.33x, not 17.62x; the larger figure combines redundant-work removal and parallelism. Loading times were 5.18s, 4.86s and 4.55s respectively and are excluded from fit time. These are individual controlled runs, not a confidence interval or a verified 69-product end-to-end speedup.

The RB comparisons match the full returned results exactly, including best-candidate audits containing 81 and 53 real training entries respectively. Both RB fixtures fail the original worst-year quality gate, so their returned models and feature lists are empty. A separate full CU500 fit **passes** the original quality gate: its selected model, all **136 nonempty real entry-feature rows**, and entire training audit match exactly. These fixtures verify execution equivalence; they are not an independent profitability evaluation.

The original and optimized nonempty synthetic end-to-end workflows also match models apart from the required code-provenance hash. All 39 persisted account/trade CSV/JSON/JSONL outputs across 0/1/2bp are byte-identical, including daily equity, fills, orders, ledger, gate decisions, candidates, allocation events, open positions and final state.

The synthetic cache measurement retained 1,304 global days using about 0.65MB before lazy snapshots and 4.55MB after all ten MR sigma/day combinations. This bounds a fit-local example, not a guarantee for every market history. Original Bar objects remain slotted.

## Regression validation

- Full upstream suite: 113 passed on Windows/Python 3.11 with UTF-8 mode and 113 passed in 22.37s on the Linux research host.
- Real process tests compare complete serial/parallel calibration bundles, sparse calendars, mixed MR/Trend families, equal-score ties, progress/quiet behavior and runtime-only worker identity.
- Missing held-contract marks preserve the worker's actual partial fills/state in the original failure outputs; invalid workers fail explicitly.
- Independent scoped reviews approved the causal-history/incremental-audit change and the process-parallel change without unresolved findings.

## Reproduce the measurements

Run the same utility against separate baseline and candidate checkouts and unique output directories:

```bash
python tools/benchmark_training.py --source /path/to/engine \
  --market /path/to/canonical/RB.parquet --specs /path/to/research_specs.csv \
  --config /path/to/config_10m.json --output /path/to/new-benchmark \
  --product RB --year 2023
```

Use `--family trend_following --year 2021` for Trend96. Use `--one-stop --profile` for the 100-candidate diagnostic; do not label it a complete-grid result. Use `--schedule --products RB,CU,AU,AG --market /path/to/canonical --workers 4` for a complete one-year four-product schedule. The utility writes result.json, timing.json, and optionally profile.pstats/profile.txt. Compare result.json exactly between revisions. Actual market inputs remain outside Git.

## Boundaries

- This is mechanical performance work, not evidence of alpha or a reproduction of the old 43.08% return.
- The new source hash intentionally invalidates old model bundles. Recalibrate with the new source; never rewrite a bundle's identity to force reuse.
- Single-product timings do not establish the full 69-product runtime. Full-market throughput requires its own completed run.
- The already-running original baseline and its pinned source are preserved; this PR does not hot-patch it.
