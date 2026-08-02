---
category: javascript
metrics: tbt_ms, inp_ms, script_ms, total_transfer_kb
symptoms: tbt_high, inp_fail, inp_warn, script_heavy, dominant_script
expected_tbt_reduction_pct: 20, 60
effort: medium
---

# JavaScript cost and code splitting

JavaScript is the most expensive byte-for-byte resource: it must be
downloaded, parsed, compiled and executed. Long tasks block the main thread,
which is what Total Blocking Time measures and what makes INP poor.

## Split bundles by route

Load only the code the current route needs, with dynamic `import()` at route
boundaries. A single bundle serving every route makes the homepage pay for the
checkout flow.

Expected impact: 30-60% reduction in initial JavaScript on multi-route apps,
with a roughly proportional TBT reduction.

Trade-off: navigation then needs a chunk fetch; prefetch likely next routes on
idle to hide it.

## Break up long tasks

Any task over 50ms blocks input. Split work with `scheduler.yield()`, or
`await new Promise(r => setTimeout(r, 0))`, so the browser can respond
between chunks.

Expected impact: directly reduces TBT by the blocking portion of each task
(duration minus 50ms). Yielding usually improves INP more than trimming bytes.

Trade-off: yielding adds total wall-clock time even as responsiveness improves.

## Defer non-critical third-party scripts

Analytics, chat widgets and A/B tools rarely need to run before first paint.
Load with `defer`, or on interaction/idle.

Expected impact: 100-500ms TBT reduction per heavy third-party script.

Trade-off: A/B tools loaded late can cause visible flicker; measure before
deferring anything that renders.

## Remove unused code

Audit with coverage tooling. Drop polyfills for browsers you no longer
support, and prefer smaller libraries for single-purpose work such as date
formatting.

Expected impact: 10-40% bundle reduction, highly codebase-dependent.

Trade-off: requires real test coverage to change dependencies safely.
