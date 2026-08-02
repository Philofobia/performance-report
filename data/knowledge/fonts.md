---
category: fonts
metrics: fcp_ms, cls, lcp_ms
symptoms: fcp_slow, cls_fail, cls_warn, dominant_font, render_blocking_css
expected_fcp_reduction_pct: 10, 30
effort: low
---

# Font loading

Web fonts block text rendering and cause layout shift when a fallback is
swapped for the web font at a different metric size.

## Use font-display: swap or optional

`font-display: swap` renders fallback text immediately and swaps when the web
font arrives. `optional` avoids the swap entirely on slow connections,
eliminating the shift at the cost of some visits not seeing the web font.

Expected impact: removes invisible-text delay, typically 200-800ms of blocked
text rendering on slow connections.

Trade-off: `swap` causes a visible flash of fallback text; `optional` means
inconsistent typography between visits.

## Preload the fonts used above the fold

`<link rel="preload" as="font" crossorigin>` for the one or two faces used in
the header and hero. Preloading every face wastes bandwidth and competes with
the LCP resource.

Expected impact: 100-300ms earlier text paint for preloaded faces.

Trade-off: over-preloading actively harms LCP by contending for bandwidth.

## Match fallback metrics

Use `size-adjust`, `ascent-override` and `descent-override` on the fallback
`@font-face` so the fallback occupies the same space as the web font.

Expected impact: reduces or eliminates font-swap layout shift, commonly worth
0.05-0.15 CLS on text-heavy pages.

Trade-off: requires per-font measurement to derive the override values.

## Subset and self-host

Subset to the glyphs actually used and self-host rather than relying on a
third-party origin, which costs an extra connection setup.

Expected impact: 40-70% smaller font files; removes one third-party
connection from the critical path.

Trade-off: self-hosting means owning cache headers and updates.
