---
category: layout-stability
metrics: cls
symptoms: cls_fail, cls_warn
expected_cls_reduction_pct: 50, 95
effort: low
---

# Layout stability (CLS)

Cumulative Layout Shift measures content moving after it is visible. It is
usually caused by content inserted without reserved space.

## Reserve space for all replaced elements

Images, videos, iframes and embeds need explicit dimensions or an
`aspect-ratio`, so the browser reserves the box before content arrives.

Expected impact: typically removes the largest single CLS contributor.

Trade-off: none.

## Reserve space for injected content

Banners, cookie notices and ad slots inserted at the top of the document push
everything down. Reserve a fixed-height container, or overlay rather than
insert.

Expected impact: eliminates shifts commonly worth 0.1-0.3 CLS.

Trade-off: a reserved slot shows empty space when nothing loads.

## Avoid inserting above existing content

Never insert content above what the user is already reading unless it is a
response to their own interaction. Shifts within 500ms of a user input are
excluded from CLS precisely because they are expected.

Expected impact: removes the most user-visible category of shift.

Trade-off: may require rethinking notification placement.

## Use transform for animation

Animate with `transform` and `opacity`, which run on the compositor, rather
than animating `top`, `left`, `width` or `height`.

Expected impact: removes animation-caused shift entirely and lowers main
thread work.

Trade-off: some effects need restructuring to express as transforms.
