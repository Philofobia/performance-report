"""Concrete, evidence-backed checks over HAR captures.

The report used to say *what kind* of problem a page has ("long tasks block
the main thread"). A ticket needs *which* request, *when*, in *how many runs*.
This module reads HARs — WebPageTest exports, whose extra fields carry the LCP
element, initiators, render-blocking status, layout shifts and the console, and
this project's own Playwright captures, which carry less — and answers one
narrow question per check with the runs and requests that prove it.

Pure: no config, no store, no model. Every check takes parsed runs and returns
a :class:`CheckResult`; a check whose fields the HAR does not carry says so
(``status="no_data"``) rather than guessing. HAR files carry cookies and
tokens, so nothing here keeps a header, and every URL that leaves this module
passes through :func:`store.artifacts.redact_url`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

from store.artifacts import redact_url

#: A majority of runs must show a problem for it to count as confirmed — one
#: slow run out of three is noise, two is a pattern.
MAJORITY = 0.5

#: Status values. ``confirmed``: the problem is in the capture. ``not_seen``:
#: the capture has what the check needs, and the problem is not there.
#: ``no_data``: the capture lacks what the check needs.
CONFIRMED, NOT_SEEN, NO_DATA = "confirmed", "not_seen", "no_data"


@dataclass(frozen=True)
class Request:
    """One network request, reduced to what the checks need."""

    url: str
    start_ms: float
    end_ms: float
    status: int
    content_type: str = ""
    initiator_type: Optional[str] = None
    initiator: Optional[str] = None
    priority: Optional[str] = None
    render_blocking: Optional[str] = None

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image")


@dataclass
class HarRun:
    """One page load inside a HAR (WebPageTest exports hold several)."""

    source: str
    index: int
    page_url: str
    requests: List[Request]
    lcp_ms: Optional[float] = None
    lcp_url: Optional[str] = None
    lcp_kind: Optional[str] = None
    cls: Optional[float] = None
    layout_shifts: List[Dict[str, Any]] = field(default_factory=list)
    console: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.source} run {self.index}"

    def request_for(self, url: Optional[str]) -> Optional[Request]:
        return next((r for r in self.requests if r.url == url), None) if url else None


@dataclass
class CheckResult:
    check: str
    status: str
    summary: str
    evidence: List[str] = field(default_factory=list)
    runs_matched: int = 0
    runs_total: int = 0


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _request(entry: Dict[str, Any], page_start: datetime) -> Request:
    started = _parse_time(entry["startedDateTime"])
    start = entry.get("_load_start")
    if start is None:
        start = (started - page_start).total_seconds() * 1000
    end = entry.get("_all_end")
    if end is None:
        end = start + max(float(entry.get("time") or 0), 0)
    status = entry.get("_responseCode", entry.get("response", {}).get("status", 0))
    content_type = (entry.get("_contentType")
                    or entry.get("response", {}).get("content", {}).get("mimeType") or "")
    return Request(
        url=entry["request"]["url"], start_ms=float(start), end_ms=float(end),
        status=int(status or 0), content_type=content_type.lower(),
        initiator_type=entry.get("_initiator_type"), initiator=entry.get("_initiator"),
        priority=entry.get("_priority"), render_blocking=entry.get("_renderBlocking"),
    )


def _shifts(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return [s for s in (raw or []) if isinstance(s, dict)]


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_har(path: Path) -> List[HarRun]:
    """Every page load in a HAR, oldest first, preflights excluded.

    A CORS preflight is not a download of anything the page uses; counting it
    would report the site's images twice.
    """
    log = json.loads(Path(path).read_text(encoding="utf-8"))["log"]
    pages = log.get("pages") or [{"id": None, "startedDateTime":
                                  log["entries"][0]["startedDateTime"], "title": ""}]
    runs: List[HarRun] = []
    for index, page in enumerate(pages, start=1):
        start = _parse_time(page["startedDateTime"])
        entries = [e for e in log["entries"]
                   if page["id"] is None or e.get("pageref") == page["id"]]
        requests = [_request(e, start) for e in entries
                    if e.get("_request_type") != "Preflight"
                    and e["request"].get("method", "GET") != "OPTIONS"]
        page_url = page.get("_URL") or (requests[0].url if requests else "")
        runs.append(HarRun(
            source=Path(path).name, index=index, page_url=page_url, requests=requests,
            lcp_ms=_number(page.get("_chromeUserTiming.LargestContentfulPaint")),
            lcp_url=page.get("_LargestContentfulPaintImageURL") or None,
            lcp_kind=page.get("_LargestContentfulPaintType"),
            cls=_number(page.get("_chromeUserTiming.CumulativeLayoutShift")),
            layout_shifts=_shifts(page.get("_LayoutShifts")),
            console=[c for c in page.get("_consoleLog") or [] if isinstance(c, dict)],
        ))
    return runs


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _short(url: str, limit: int = 110) -> str:
    """A redacted URL short enough for a list line, shortened in the middle.

    The host and the last path segment (with its query) are what identify a
    request - ``media.oakley.com/…/ww-empty-l1-hero-m.png`` - so those are
    kept, and the opaque middle of the path goes first.
    """
    text = redact_url(url)
    if len(text) <= limit:
        return text
    parts = urlsplit(text)
    segments = [s for s in parts.path.split("/") if s]
    query = f"?{parts.query}" if parts.query else ""
    # As many trailing segments as fit: several themes each ship a main.min.css,
    # and ".../theme-oakleyhome/css/main.min.css" is the one a developer can find.
    for keep in range(len(segments), 0, -1):
        short = f"{parts.hostname}/…/{'/'.join(segments[-keep:])}{query}"
        if len(short) <= limit:
            return short
    return short[:limit - 1] + "…"


def _asset(url: str) -> str:
    """The image a URL renders, whatever size was asked for."""
    return urlsplit(url).path


def _width(url: str) -> Optional[str]:
    values = parse_qs(urlsplit(url).query).get("width")
    return values[0] if values else None


def _verdict(check: str, runs: Sequence[HarRun], hits: List[HarRun],
             considered: List[HarRun], evidence: List[str],
             confirmed: str, not_seen: str, no_data: str) -> CheckResult:
    total = len(runs)
    if not considered:
        return CheckResult(check, NO_DATA, no_data, [], 0, total)
    status = CONFIRMED if len(hits) > len(considered) * MAJORITY else NOT_SEEN
    summary = (confirmed if status == CONFIRMED else not_seen).format(
        hits=len(hits), total=len(considered))
    return CheckResult(check, status, summary, evidence, len(hits), len(considered))


def _range(values: Sequence[float]) -> str:
    low, high = min(values), max(values)
    return f"{low:.0f} ms" if round(low) == round(high) else f"{low:.0f}–{high:.0f} ms"


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def lcp_render_delay(runs: Sequence[HarRun], *, min_gap_ms: float = 250,
                     script: Optional[str] = None) -> CheckResult:
    """The LCP image is downloaded well before it is painted.

    With ``script``, the paint must also wait for that script to finish —
    the signature of an image hidden by CSS until JavaScript reveals it.
    """
    considered, hits, gaps, evidence = [], [], [], []
    for run in runs:
        image = run.request_for(run.lcp_url)
        if run.lcp_ms is None or image is None:
            continue
        considered.append(run)
        gap = run.lcp_ms - image.end_ms
        line = (f"{run.label}: LCP image downloaded at {image.end_ms:.0f} ms, painted at "
                f"{run.lcp_ms:.0f} ms (+{gap:.0f} ms) — {_short(image.url, 90)}")
        waits = gap >= min_gap_ms
        if script is not None:
            gate = next((r for r in run.requests if script in r.url), None)
            if gate is None:
                waits = False
                line += f"; {script} not requested"
            else:
                waits = waits and image.end_ms <= gate.end_ms <= run.lcp_ms
                line += f"; {script} finished at {gate.end_ms:.0f} ms"
        if waits:
            hits.append(run)
            gaps.append(gap)
        evidence.append(line)
    gate = f", after {script} finishes," if script else ""
    return _verdict(
        "lcp_render_delay", runs, hits, considered, evidence,
        confirmed=(f"The LCP image is painted {_range(gaps) if gaps else ''} after it has "
                   f"downloaded{gate} in {{hits}} of {{total}} runs."),
        not_seen=(f"The LCP image is painted promptly after downloading ({{hits}} of "
                  f"{{total}} runs delayed{' by ' + script if script else ''})."),
        no_data="The captures carry no LCP image timing (a WebPageTest HAR does).",
    )


def preload_mismatch(runs: Sequence[HarRun]) -> CheckResult:
    """The LCP image is first fetched at one size, then painted from another.

    A ``<link rel=preload>`` whose URL differs from what ``<picture>`` picks
    downloads twice and wins nothing; Chrome also warns the preload was unused.
    """
    considered, hits, evidence = [], [], []
    for run in runs:
        if not run.lcp_url:
            continue
        same_image = [r for r in run.requests if _asset(r.url) == _asset(run.lcp_url)]
        if not same_image:
            continue
        considered.append(run)
        first = min(same_image, key=lambda r: r.start_ms)
        unused = [c for c in run.console if "preloaded using link preload but not used"
                  in str(c.get("text", ""))]
        if first.url != run.lcp_url:
            hits.append(run)
            evidence.append(
                f"{run.label}: first fetched as width={_width(first.url)} at "
                f"{first.start_ms:.0f} ms, painted from width={_width(run.lcp_url)} "
                f"(requested at {run.request_for(run.lcp_url).start_ms:.0f} ms, "
                f"priority {run.request_for(run.lcp_url).priority})"
                + ("; Chrome: preload not used" if unused else ""))
    return _verdict(
        "preload_mismatch", runs, hits, considered, evidence,
        confirmed="The LCP image is fetched at one size and painted from another in "
                  "{hits} of {total} runs — the early fetch is wasted.",
        not_seen="The first fetch of the LCP image is the one painted "
                 "({hits} of {total} runs mismatched).",
        no_data="The captures carry no LCP image URL (a WebPageTest HAR does).",
    )


def duplicate_downloads(runs: Sequence[HarRun]) -> CheckResult:
    """The same image URL downloaded more than once in one page load."""
    considered, hits, evidence = list(runs), [], []
    for run in runs:
        seen: Dict[str, List[Request]] = {}
        for request in run.requests:
            if request.is_image and 200 <= request.status < 300:
                seen.setdefault(request.url, []).append(request)
        twice = {url: reqs for url, reqs in seen.items() if len(reqs) > 1}
        if twice:
            hits.append(run)
            for url, reqs in list(twice.items())[:5]:
                times = ", ".join(f"{r.start_ms:.0f} ms" for r in reqs)
                evidence.append(f"{run.label}: ×{len(reqs)} at {times} — {_short(url, 90)}")
    return _verdict(
        "duplicate_downloads", runs, hits, considered, evidence,
        confirmed="The same image is downloaded twice in {hits} of {total} runs.",
        not_seen="No image is downloaded twice ({hits} of {total} runs affected).",
        no_data="No captures.",
    )


def multiple_renditions(runs: Sequence[HarRun]) -> CheckResult:
    """One image fetched at several widths in the same load."""
    considered, hits, evidence = list(runs), [], []
    for run in runs:
        widths: Dict[str, set] = {}
        for request in run.requests:
            if request.is_image and _width(request.url):
                widths.setdefault(_asset(request.url), set()).add(int(_width(request.url)))
        several = {asset: w for asset, w in widths.items() if len(w) > 1}
        if several:
            hits.append(run)
            if run.index == 1 or len(hits) == 1:
                for asset, w in list(several.items())[:5]:
                    evidence.append(f"{run.label}: {asset.rsplit('/', 1)[-1]} at widths "
                                    f"{', '.join(str(x) for x in sorted(w))}")
    return _verdict(
        "multiple_renditions", runs, hits, considered, evidence,
        confirmed="Images are downloaded at more than one width in {hits} of {total} runs.",
        not_seen="Each image is downloaded at one width ({hits} of {total} runs affected).",
        no_data="No captures.",
    )


def slide_in_shift(runs: Sequence[HarRun], *, min_steps: int = 4,
                   window_ms: float = 1500) -> CheckResult:
    """A run of small layout shifts of one region, frame after frame.

    An element animated in with layout properties (``left``, ``width``,
    ``margin``) instead of ``transform`` shifts on every frame: the same box,
    moving in steps, within a second or so.
    """
    considered, hits, evidence = [], [], []
    for run in runs:
        if not run.layout_shifts:
            if run.cls is not None:
                considered.append(run)
            continue
        considered.append(run)
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for shift in run.layout_shifts:
            rects = shift.get("rects") or []
            if rects:
                _x, y, _w, h = rects[0]
                groups.setdefault((y, h), []).append(shift)
        for (y, h), shifts in groups.items():
            shifts.sort(key=lambda s: s["time"])
            if (len(shifts) >= min_steps
                    and shifts[-1]["time"] - shifts[0]["time"] <= window_ms):
                score = sum(s["score"] for s in shifts)
                hits.append(run)
                evidence.append(
                    f"{run.label}: {len(shifts)} shifts of the band y={y}–{y + h}px between "
                    f"{shifts[0]['time']:.0f} and {shifts[-1]['time']:.0f} ms, "
                    f"score {score:.3f} of CLS {run.cls or 0:.3f}")
                break
    return _verdict(
        "slide_in_shift", runs, hits, considered, evidence,
        confirmed="An element slides in and shifts the layout frame by frame in "
                  "{hits} of {total} runs.",
        not_seen="No frame-by-frame slide-in shift ({hits} of {total} runs).",
        no_data="The captures carry no layout-shift detail (a WebPageTest HAR does).",
    )


def service_worker(runs: Sequence[HarRun]) -> CheckResult:
    """The service worker script is requested and answers."""
    considered, hits, evidence = [], [], []
    for run in runs:
        workers = [r for r in run.requests if re.search(r"service-worker[^/]*\.js|/sw\.js", r.url)]
        if not workers:
            continue
        considered.append(run)
        broken = [r for r in workers if not 200 <= r.status < 400]
        if broken:
            hits.append(run)
        if run.index == 1:
            evidence += [f"{run.label}: {r.status} {_short(r.url, 90)}" for r in workers]
    return _verdict(
        "service_worker", runs, hits, considered, evidence,
        confirmed="The service worker script fails in {hits} of {total} runs.",
        not_seen="The service worker script loads ({hits} of {total} runs failing).",
        no_data="No service worker script was requested in these captures.",
    )


def lcp_image_from_script(runs: Sequence[HarRun]) -> CheckResult:
    """The LCP image is requested by JavaScript rather than by the HTML."""
    considered, hits, evidence = [], [], []
    for run in runs:
        image = run.request_for(run.lcp_url)
        if image is None or image.initiator_type is None:
            continue
        considered.append(run)
        if image.initiator_type == "script":
            hits.append(run)
        evidence.append(f"{run.label}: requested by {image.initiator_type}"
                        f"{' (' + _short(image.initiator, 60) + ')' if image.initiator_type == 'script' and image.initiator else ''}"
                        f" at {image.start_ms:.0f} ms, priority {image.priority}")
    return _verdict(
        "lcp_image_from_script", runs, hits, considered, evidence,
        confirmed="The LCP image is requested by JavaScript in {hits} of {total} runs, "
                  "so the browser cannot discover it early.",
        not_seen="The LCP image is in the HTML and discovered by the parser "
                 "({hits} of {total} runs script-initiated).",
        no_data="The captures carry no initiator for the LCP image (a WebPageTest HAR does).",
    )


_ICONS = ("image/svg", "image/gif", "image/x-icon", "image/vnd.microsoft.icon")


def css_background_images(runs: Sequence[HarRun], *, match: str = "") -> CheckResult:
    """The images a ticket is about are requested by a stylesheet.

    ``match`` narrows to those images (a URL fragment, e.g. the product image
    path). Without it, every raster image counts — never icons: every site
    draws its menu arrows and spinners from CSS, and counting them confirmed
    "product tiles are CSS backgrounds" on a PLP whose tiles are ``<img>``.
    """
    considered, hits, evidence = [], [], []
    for run in runs:
        images = [r for r in run.requests if r.is_image and r.initiator_type
                  and not r.content_type.startswith(_ICONS)
                  and (not match or match in r.url)]
        if not images:
            continue
        considered.append(run)
        from_css = [r for r in images if r.initiator_type == "css"
                    or (r.initiator or "").split("?")[0].endswith(".css")]
        if len(from_css) > len(images) * MAJORITY:
            hits.append(run)
        if run.index == 1:
            kinds = sorted({r.initiator_type for r in images} - {None})
            evidence.append(f"{run.label}: {len(from_css)} of {len(images)} "
                            f"{'matching ' if match else ''}images requested by a stylesheet "
                            f"(initiators: {', '.join(kinds)})")
    return _verdict(
        "css_background_images", runs, hits, considered, evidence,
        confirmed="The images are loaded as CSS backgrounds in {hits} of {total} runs.",
        not_seen="The images are in the HTML, not CSS backgrounds "
                 "({hits} of {total} runs).",
        no_data="No matching images with initiators in these captures "
                "(a WebPageTest HAR carries initiators).",
    )


CHECKS: Dict[str, Callable[..., CheckResult]] = {
    "lcp_render_delay": lcp_render_delay,
    "preload_mismatch": preload_mismatch,
    "duplicate_downloads": duplicate_downloads,
    "multiple_renditions": multiple_renditions,
    "slide_in_shift": slide_in_shift,
    "service_worker": service_worker,
    "lcp_image_from_script": lcp_image_from_script,
    "css_background_images": css_background_images,
}


# --------------------------------------------------------------------------- #
# Findings no ticket asked for
# --------------------------------------------------------------------------- #
_HEADER_REJECTED = re.compile(r"Request header field (\S+) is not allowed", re.IGNORECASE)


def header_artifacts(runs: Sequence[HarRun],
                     header_names: Optional[Sequence[str]] = None) -> List[str]:
    """Hosts whose failures the *test* caused, by sending a custom header.

    A custom header on a cross-origin request forces a CORS preflight, and a
    host that does not allow it fails the request. Those failures belong to
    the capture, not the site, and must not read as the site's problems.
    With no ``header_names``, any header Chrome names as rejected counts.
    """
    names = [n.lower() for n in header_names] if header_names else None
    hosts: Dict[str, int] = {}
    for run in runs:
        for entry in run.console:
            text = str(entry.get("text", ""))
            rejected = _HEADER_REJECTED.search(text)
            if "CORS policy" not in text or not rejected:
                continue
            if names is not None and rejected.group(1).lower() not in names:
                continue
            match = re.search(r"at '(https?://[^/']+)", text)
            if match:
                host = urlsplit(match.group(1)).hostname or match.group(1)
                hosts[host] = hosts.get(host, 0) + 1
    return sorted(hosts)


def rejected_headers(runs: Sequence[HarRun]) -> List[str]:
    """The custom headers Chrome reported rejecting in a CORS preflight."""
    found = {m.group(1).lower() for run in runs for entry in run.console
             if (m := _HEADER_REJECTED.search(str(entry.get("text", ""))))}
    return sorted(found)


def untracked_findings(runs: Sequence[HarRun]) -> List[str]:
    """Concrete problems visible in the captures, whether or not a ticket names them."""
    if not runs:
        return []
    out: List[str] = []
    first = runs[0]

    lcps = [r.lcp_ms for r in runs if r.lcp_ms is not None]
    if lcps:
        mid = sorted(runs, key=lambda r: r.lcp_ms or 0)[len(runs) // 2]
        element = mid.lcp_kind or "unknown"
        image = mid.request_for(mid.lcp_url)
        document = mid.requests[0] if mid.requests else None
        line = f"LCP {_range(lcps)} across {len(lcps)} runs; element: {element}"
        if image and document:
            line += (f". Median run: document done {document.end_ms:.0f} ms, image requested "
                     f"{image.start_ms:.0f} ms, downloaded {image.end_ms:.0f} ms, painted "
                     f"{mid.lcp_ms:.0f} ms — {_short(image.url, 80)}")
        out.append(line)

        if image is not None:
            rivals = [r for r in mid.requests
                      if r.is_image and r.url != image.url
                      and (r.priority or "").lower() in ("high", "veryhigh")
                      and r.start_ms < image.end_ms]
            if rivals:
                out.append(f"{len(rivals)} other image(s) compete with the LCP image at "
                           f"high priority ({mid.label}):")
                out += [f"  {_short(r.url, 90)} {r.start_ms:.0f}–{r.end_ms:.0f} ms"
                        for r in rivals[:4]]

    blocking = [r for r in first.requests
                if r.render_blocking in ("blocking", "in_body_parser_blocking")]
    if blocking:
        slowest = sorted(blocking, key=lambda r: -(r.end_ms - r.start_ms))[:5]
        out.append(f"{len(blocking)} render-blocking requests ({first.label}); "
                   f"the slowest {len(slowest)}:")
        out += [f"  {_short(r.url, 90)} {r.end_ms - r.start_ms:.0f} ms" for r in slowest]

    late = [(run, s) for run in runs for s in run.layout_shifts
            if s.get("score", 0) >= 0.02]
    for run, shift in late[:3]:
        rect = (shift.get("rects") or [[None, None, None, None]])[0]
        out.append(f"Layout shift {shift['score']:.3f} at {shift['time']:.0f} ms "
                   f"({run.label}), region y={rect[1]} height={rect[3]}px")

    failed = [r for r in first.requests if r.status >= 400]
    if failed:
        out.append(f"{len(failed)} failed requests ({first.label}):")
        out += [f"  {r.status} {_short(r.url, 90)}" for r in failed[:5]]
    return out


def summarize_runs(runs: Sequence[HarRun]) -> Dict[str, Optional[float]]:
    """Median LCP and CLS across the captures, for the report's header line."""
    lcps = [r.lcp_ms for r in runs if r.lcp_ms is not None]
    cls = [r.cls for r in runs if r.cls is not None]
    return {"lcp_ms": median(lcps) if lcps else None, "cls": median(cls) if cls else None,
            "runs": float(len(runs))}
