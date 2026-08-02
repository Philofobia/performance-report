---
category: images
metrics: lcp_ms, total_transfer_kb, fcp_ms
symptoms: lcp_fail, lcp_warn, page_weight, dominant_img, dominant_image
expected_lcp_reduction_pct: 15, 40
effort: low
---

# Image optimization

Images are the most common Largest Contentful Paint element and usually the
largest share of transferred bytes. Fixing them is typically the highest
ratio of impact to effort on a content-heavy page.

## Serve modern formats

Encode as AVIF with a WebP fallback and JPEG as a last resort. AVIF is
typically 30-50% smaller than JPEG at equivalent perceived quality, WebP
25-35% smaller.

Expected impact: 20-40% reduction in image bytes. When the LCP element is an
image, this usually moves LCP by 10-25%.

Trade-off: AVIF encoding is slower at build time, and very old browsers need
the fallback chain. Use `<picture>` with `type` attributes.

## Size images to their display size

Serve dimensions matched to the layout box and device pixel ratio via
`srcset` and `sizes`. Shipping a 2000px-wide image into a 400px slot wastes
roughly 96% of its pixels.

Expected impact: 30-60% reduction in bytes for oversized images.

Trade-off: requires a resizing pipeline or an image CDN.

## Prioritize the LCP image, lazy-load the rest

Add `fetchpriority="high"` to the LCP image and preload it. Apply
`loading="lazy"` only to below-the-fold images — lazy-loading the LCP element
delays it and makes LCP worse.

Expected impact: 5-20% LCP reduction from prioritization alone, with no byte
savings required.

Trade-off: mis-identifying the LCP element regresses the metric. Verify
against a real measurement rather than assuming.

## Always set width and height

Set explicit `width` and `height` attributes (or `aspect-ratio` in CSS) so the
browser reserves space before the image loads.

Expected impact: eliminates image-caused layout shift, often the single
largest contributor to CLS.

Trade-off: none. This is close to a free fix.
