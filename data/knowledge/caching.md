---
category: caching
metrics: ttfb_ms, lcp_ms
symptoms: ttfb_slow, many_requests
expected_ttfb_reduction_pct: 30, 80
effort: medium
---

# Caching and server response time

Time to First Byte gates every other metric: nothing can paint before the
first byte arrives. It is dominated by server work, network latency and
whether a cache was hit.

## Serve static assets from a CDN with long max-age

Immutable, content-hashed assets should carry
`Cache-Control: public, max-age=31536000, immutable`.

Expected impact: near-zero latency for repeat visits; often 200-600ms TTFB
reduction on first visits by serving from a nearby edge.

Trade-off: requires content hashing in the build so URLs change on deploy.

## Cache HTML at the edge

Even a short edge TTL with stale-while-revalidate removes origin round-trips
for most visitors.

Expected impact: 100-500ms TTFB reduction depending on origin distance.

Trade-off: personalized pages need care — cache the shell and hydrate
per-user data separately, or vary correctly.

## Fix slow origin work

Profile the server. The usual causes are N+1 queries, missing database
indexes and synchronous third-party calls in the request path.

Expected impact: highly variable; removing an N+1 query commonly cuts
server time by 50-90%.

Trade-off: needs backend profiling, not a frontend change.

## Reduce request count

Fewer requests means fewer connection setups on high-latency links. HTTP/2
and HTTP/3 reduce but do not remove this cost.

Expected impact: 10-30% load-time reduction on high-latency connections when
request counts are very high.

Trade-off: do not bundle so aggressively that caching granularity is lost.
