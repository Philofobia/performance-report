# Phase 6 — CLI Orchestration and Skeleton Drift Guard

**Date:** 2026-08-04
**Status:** Approved design
**Covers:** PROJECT_SPEC.md §10 Phase 6 — `cli.py`, `store/listing.py`,
`report/skeleton.py`, `report/__main__.py`

---

## 1. Purpose

Every stage of the pipeline works, and every stage has its own front door. To go
from a browser campaign to a PDF today you invoke three different modules and
remember which flag belongs to which. Phase 6 gives the system one entry point,
and turns the skeleton promise from a property the test suite checks into a
property the CLI enforces against a committed baseline.

Two deliverables, one theme: **the pipeline gets a face, and the guarantee gets
teeth.**

## 2. Scope

**In:**

- `cli.py` — a dispatching façade: `ingest auto`, `ingest manual`, `analyze`,
  `report`, `list-runs`
- `store/listing.py` — `list-runs`, the only genuinely new capability
- `report/skeleton.py` — baseline load/save and a drift diff
- `report/__main__.py` — `--skeleton-check` and `--update-baseline`
- `report/skeleton.baseline.json` — the committed canonical section list
- README and PROJECT_SPEC updates

**Out (deferred, with reason):**

- A `run` subcommand chaining ingest → analyze → report. The stages have
  genuinely different failure modes and cadences — a campaign takes minutes and
  hits the network, analysis costs API quota, rendering is instant and free.
  Chaining them hides which one failed. Revisit if the three-command sequence
  proves annoying in practice.
- Prior-run trends, screenshot appendix, web UI. Phase 7.
- Moving any stage's argparse definition into `cli.py`. See §4.

## 3. Location and invocation

`cli.py` lives at the repository root and is invoked as `python -m cli`.

PROJECT_SPEC §10 says `src/cli.py`. There is no `src/` package — the repo is
organised as top-level packages (`ingest/`, `analysis/`, `report/`, `store/`,
`rag/`, `normalize/`, `config/`). Inventing a `src/` for one module would put
the entry point somewhere no other code lives. The spec line is corrected
rather than followed.

## 4. Architecture — dispatch, not subparsers

`cli.py` consumes only the command token(s) and forwards the **remaining argv
verbatim** to the delegate's existing `main(argv) -> int`, returning its exit
code untouched.

| Command | Delegate |
|---|---|
| `ingest auto` | `ingest.automated.main` |
| `ingest manual` | `ingest.manual.main` |
| `analyze` | `analysis.__main__.main` |
| `report` | `report.__main__.main` |
| `list-runs` | `store.listing.main` |

**Why not `argparse` subparsers.** Registering the stages as subparsers means
redeclaring every flag they own — around thirty for `ingest manual` alone — in a
second place. That duplication is wrong the first time a stage grows an option,
and nothing fails when it does: the façade simply cannot pass the new flag
through. Verbatim forwarding has no such surface.

**Consequences of forwarding, all desirable:**

- `python -m cli report --help` forwards `--help` to the report parser, so
  per-command help is the real parser's help and cannot go stale.
- `python -m cli --help` prints the command table with one-line descriptions;
  the table is the single place `cli.py` describes the stages at all.
- Existing entry points keep working. `python -m analysis --no-llm` is still
  valid, and every existing CLI test stays valid with it.
- Exit codes propagate unchanged, so `--dry-run` semantics, error codes and
  quiet-success behaviour are the stages' business, not the façade's.

**What `cli.py` owns:** the command table, the dispatch, `--help`, and the exit
code for an unrecognised command (2, matching argparse convention). Nothing
else. It computes nothing and validates nothing.

`ingest` with no mode, or an unknown mode, prints the two valid modes and exits
2 rather than guessing.

## 5. The skeleton drift guard

### 5.1 Where the flags live

`--skeleton-check` and `--update-baseline` are added to **`report/__main__.py`**,
not to `cli.py`. Because the façade forwards verbatim, a flag declared in
`cli.py` could never reach the report stage; declaring it there would also mean
`python -m report --skeleton-check` does not exist, splitting the report
interface across two modules. The flag belongs to the stage that renders.

### 5.2 The baseline

`report/skeleton.baseline.json` is committed and holds the canonical section
list:

```json
{
  "version": 1,
  "sections": ["cover", "summary", "methodology", "page[]", "page.metrics", "..."]
}
```

The `version` field exists so a future change to the fingerprint *algorithm*
(rather than to the template) can be detected as such instead of surfacing as
mass drift.

### 5.3 Behaviour

`report --skeleton-check` renders the located campaign as it normally would,
fingerprints the HTML, and compares against the baseline. Match: the report is
written and the run exits 0, printing a one-line confirmation with the section
count. Drift: the comparison is printed and the run exits 1.

```
$ python -m cli report --skeleton-check
skeleton drift vs report/skeleton.baseline.json:
  - page.lcp_breakdown   (expected at index 7)
  + page.waterfall       (found at index 7)
exit 1
```

The diff is produced with `difflib.SequenceMatcher` over the two lists, so a
section that moved reads as a removal plus an addition rather than as a
wholesale mismatch from that index onward.

**Drift still writes the report.** The rendered output is evidence for
diagnosing the drift; suppressing it would mean the one command that detected a
problem also destroys the artifact needed to understand it. The non-zero exit is
what CI reads.

`report --update-baseline` writes the current fingerprint to the baseline file
and exits 0, printing the section count. Regenerating a baseline is therefore
always a deliberate act that shows up as a reviewable diff in a commit — which
is the entire mechanism by which drift becomes visible. The two flags are
mutually exclusive; passing both is an argparse error.

### 5.4 The guard that needs no data

A unit test renders a synthetic `Report` and asserts its fingerprint equals the
committed baseline. This is what makes the baseline honest: without it, a
template change plus a forgotten `--update-baseline` leaves a stale baseline in
the repo that nobody notices until someone happens to run `--skeleton-check` on
a real campaign. The test runs offline, needs no campaign, and fails in CI the
moment the template's structure changes without the baseline moving with it.

This is why the design needs no separate `skeleton-check` subcommand: the
data-free guard is a test, and the flag guards real output.

## 6. `list-runs`

`store/listing.py` exposes:

- `format_run_table(runs) -> str` — pure, no I/O, no database
- `main(argv) -> int` — opens the store, queries, prints

Both `python -m cli list-runs` and `python -m store.listing` work, and the
façade has no special case for the one command whose logic is new.

**Columns:** run id, page, device, network, LCP (ms), CLS, INP (ms). Newest
first — `store.sql.list_runs` already orders by `created_at DESC`.

A metric the run does not carry prints `—`, never `0` or an empty cell. A run
with no INP entry (the documented no-handler case) must not read as an INP of
zero.

**Flags:** `--db` (default from `settings.storage.sqlite_path`), `--pages`,
`--device`, `--network`, `--limit` (default 20). `--pages` accepts the same
comma-separated form as the other stages; `store.sql.list_runs` filters one page
at a time, so multiple names are queried per name and concatenated, preserving
newest-first order.

An empty result prints `no runs stored` and exits 0 — an empty store is a fact,
not an error. A missing database file exits 1 with the path, because a typo'd
`--db` should not read as "you have no runs".

Column widths are computed from the data so a long run id does not shift the
table; the header is always printed.

## 7. Error handling

The façade adds no error handling. Every stage already prints to stderr and
returns an int; the façade returns what it is given. The only errors `cli.py`
originates are "unknown command" and "missing ingest mode", both exit 2.

`store/listing.py` follows the existing convention: `StoreError` and `OSError`
become a stderr message plus exit 1.

## 8. Testing

| File | Covers |
|---|---|
| `tests/unit/cli_test.py` | Routing to each delegate with exact forwarded argv, exit-code propagation, `--help`, unknown command → 2, missing ingest mode → 2, `--help` forwarded to a delegate |
| `tests/unit/listing_test.py` | Table formatting, missing metrics as `—`, column alignment, filters, limit, empty store, missing database |
| `tests/unit/skeleton_test.py` (extended) | Baseline round-trip, drift diff for added / removed / reordered / identical, and the committed baseline matching a rendered synthetic report |
| `tests/integration/cli_test.py` | `analyze --no-llm` → `report --no-pdf --skeleton-check` driven through `cli.main`, on a temporary campaign, offline |

Routing is tested by monkeypatching the delegates and asserting the argv they
received — the façade's whole job is forwarding, so forwarding is what the test
must pin. A test that only asserted "exit code 0" would pass while the façade
dropped every flag.

The offline suite stays browser-free, network-free and key-free.

## 9. Documentation

- README: `python -m cli` leads the "Running it" section, with the per-stage
  invocations retained as the equivalent longhand; Phase 6 marked Done in the
  roadmap; the "Missing" gap table reduced to Phase 7 items; test count
  refreshed.
- PROJECT_SPEC §10 Phase 6 checkboxes ticked, and the `src/cli.py` reference
  corrected to `cli.py`.
- The Phase 6 note in `report/skeleton.py`'s module docstring updated from a
  forward reference to a description of what was built.
