"""Tickets nobody has filed yet, found in HAR captures.

``har_checks`` answers the question each open ticket asks. This module asks
its own: a fixed set of rules, each a problem a developer can pick up - a
script that holds the main thread, versioned assets cached for an hour, a
render-blocking stylesheet on someone else's CDN - with the runs, requests and
numbers that show it and the change that would fix it.

The rules keep the checks' discipline:

* **A majority of runs.** A request, host or message counts only when most
  runs of a capture show it; one slow run is noise.
* **Nothing the capture caused.** A request that failed because the capture
  sent its own header across origins (``har_checks.artifact_urls``) is never
  the site's problem, and neither is its console error.
* **Data or silence.** A rule whose fields the HAR does not carry - a
  Playwright capture has no CPU attribution - finds nothing, rather than
  guessing. WebPageTest exports carry everything used here.

Pure: no config, no store, no model. URLs pass through ``_short`` (redacted)
before they leave; headers are never read here at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from analysis import har_checks as hc
from analysis.har_checks import HarRun, Request, _short

#: Severity order, most severe first. A ticket takes its worst capture's.
SEVERITIES = ("high", "medium", "low")
_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

#: Evidence lines kept per capture. A ticket lists every capture it was seen
#: in; beyond this many lines a capture's list stops being read.
MAX_EVIDENCE_PER_CAPTURE = 6

#: Short page names for ticket titles, the house style of the open tickets
#: ("HP | ...", "PDP | ..."). A page not listed is upper-cased.
PAGE_ABBREVIATIONS = {"homepage": "HP", "plp": "PLP", "pdp": "PDP"}


@dataclass
class Finding:
    """What one rule found in one capture."""

    severity: str
    #: The problem in one line, specific to this capture - the ticket title
    #: is taken from the worst capture's.
    headline: str
    summary: str
    evidence: List[str] = field(default_factory=list)
    #: Orders captures within a severity (ms, bytes, count - rule's choice).
    magnitude: float = 0.0


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    detect: Callable[[Sequence[HarRun]], Optional[Finding]]
    #: What to change - the ticket's proposed fix.
    fix: str
    #: How to tell it is done, in terms of what the next capture shows.
    done_when: str
    #: ``har_checks`` checks whose open tickets touch the same problem.
    related_checks: Tuple[str, ...] = ()


@dataclass
class ProposedTicket:
    key: str
    title: str
    category: str
    severity: str
    summary: str
    fix: str
    done_when: str
    #: ``page/device`` of every capture the rule found it in.
    seen_on: List[str] = field(default_factory=list)
    captures_total: int = 0
    evidence: List[str] = field(default_factory=list)
    #: Open tickets on a related check - read them before filing.
    related: List[str] = field(default_factory=list)
    #: An open ticket that already tracks this rule (``tracks:`` in
    #: tickets.yaml); such a proposal is listed, not proposed.
    tracked_by: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _recurring(runs: Sequence[HarRun],
               per_run: Callable[[HarRun], Mapping[str, Any]]) -> Dict[str, List[Any]]:
    """Keys seen in a majority of runs -> what each run reported for them."""
    seen: Dict[str, List[Any]] = {}
    for run in runs:
        for key, detail in per_run(run).items():
            seen.setdefault(key, []).append(detail)
    return {key: details for key, details in seen.items()
            if len(details) > len(runs) * hc.MAJORITY}


def _path(url: str) -> str:
    """A URL without its query: ``main.js?v=2026-09-16`` and ``?v=2026-09-17``
    are the same script on consecutive days."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _name(url: str) -> str:
    """The last path segment - what a developer searches the codebase for."""
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or urlsplit(url).netloc


def _ms(value: float) -> str:
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms"


def _kb(value: float) -> str:
    return f"{value / 1024:.0f} KB"


def _range(values: Sequence[float], unit: Callable[[float], str] = _ms) -> str:
    low, high = min(values), max(values)
    return unit(low) if unit(low) == unit(high) else f"{unit(low)}–{unit(high)}"


def _vendor(host: str) -> str:
    """Group a third party's hosts: ``cdn0.forter.com`` and
    ``5944d031151c.cdn4.forter.com`` are one vendor. Display only."""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) > 2 else host


def _third_party(run: HarRun) -> List[Request]:
    return [r for r in run.requests
            if urlsplit(r.url).hostname and not run.is_first_party(r)]


def _is_script(request: Request) -> bool:
    return request.request_type == "Script" or "javascript" in request.content_type


def _is_static(request: Request) -> bool:
    if request.request_type in ("Script", "Stylesheet", "Font", "Image"):
        return True
    return any(kind in request.content_type
               for kind in ("javascript", "css", "font", "image/"))


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
#: A script whose median CPU is below this is not worth its own line.
SCRIPT_CPU_MS = 100.0


def main_thread_scripts(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Scripts that keep the main thread busy during load.

    Scripts and the HTML document only: WebPageTest also attributes style
    parsing to stylesheets, which is not script and not what this ticket fixes.
    The document's time is parsing plus its inline scripts, and says so.
    """
    def per_run(run: HarRun) -> Dict[str, Tuple[float, str, bool]]:
        out: Dict[str, Tuple[float, str, bool]] = {}
        for index, request in enumerate(run.requests):
            if request.cpu_ms <= 0 or not (index == 0 or _is_script(request)):
                continue
            key = _path(request.url)
            cpu = out.get(key, (0.0, request.url, False))[0] + request.cpu_ms
            out[key] = (cpu, request.url, run.is_first_party(request))
        return out

    heavy = []
    for key, details in _recurring(runs, per_run).items():
        cpu = median(d[0] for d in details)
        if cpu >= SCRIPT_CPU_MS:
            heavy.append((cpu, details[0][1], details[0][2]))
    if not heavy:
        return None
    heavy.sort(key=lambda h: -h[0])
    top_cpu, top_url, _ = heavy[0]
    document = urlsplit(runs[0].page_url).path

    def label(url: str) -> str:
        return ("the HTML document (parsing and inline scripts)"
                if urlsplit(url).path == document else _short(url, 90))

    evidence = [f"{_ms(cpu)} median — {label(url)}{'' if first else ' (third party)'}"
                for cpu, url, first in heavy[:MAX_EVIDENCE_PER_CAPTURE - 1]]
    tbts = [r.tbt_ms for r in runs if r.tbt_ms is not None]
    longest = [max(b - a for a, b in r.long_tasks) for r in runs if r.long_tasks]
    if tbts or longest:
        evidence.append(
            (f"Total Blocking Time {_range(tbts)}" if tbts else "")
            + ("; " if tbts and longest else "")
            + (f"longest task {_range(longest)}" if longest else "")
            + f" across {len(runs)} runs")
    culprit = ("the HTML document" if urlsplit(top_url).path == document
               else _name(top_url))
    return Finding(
        severity="high" if top_cpu >= 500 else "medium",
        headline=f"{culprit} runs {_ms(top_cpu)} of main-thread script during load",
        summary=(f"{len(heavy)} script(s) each run ≥{SCRIPT_CPU_MS:.0f} ms of main-thread "
                 f"time in most runs; {culprit} alone runs {_ms(top_cpu)}."),
        evidence=evidence, magnitude=top_cpu)


#: Third-party cost worth a ticket: main-thread time, or bytes.
THIRD_PARTY_CPU_MS = 100.0
THIRD_PARTY_BYTES = 150 * 1024


def third_party_cost(runs: Sequence[HarRun]) -> Optional[Finding]:
    """What third parties cost the page: requests, bytes, main-thread time."""
    def per_run(run: HarRun) -> Dict[str, Tuple[int, int, float]]:
        vendors: Dict[str, List[float]] = {}
        for request in _third_party(run):
            totals = vendors.setdefault(_vendor(request.host), [0, 0, 0.0])
            totals[0] += 1
            totals[1] += request.bytes_in
            totals[2] += request.cpu_ms
        return {vendor: tuple(t) for vendor, t in vendors.items()}

    vendors = {vendor: (median(d[0] for d in details), median(d[1] for d in details),
                        median(d[2] for d in details))
               for vendor, details in _recurring(runs, per_run).items()}
    if not vendors:
        return None
    requests = sum(v[0] for v in vendors.values())
    size = sum(v[1] for v in vendors.values())
    cpu = sum(v[2] for v in vendors.values())
    if cpu < THIRD_PARTY_CPU_MS and size < THIRD_PARTY_BYTES:
        return None
    ranked = sorted(vendors.items(), key=lambda kv: (-kv[1][2], -kv[1][1]))
    evidence = [f"{vendor}: {n:.0f} requests, {_kb(b)}, {_ms(c)} CPU (medians)"
                for vendor, (n, b, c) in ranked[:MAX_EVIDENCE_PER_CAPTURE]]
    return Finding(
        severity="high" if cpu >= 250 or size >= 500 * 1024 else "medium",
        headline=f"Third parties add {_kb(size)} and {_ms(cpu)} of main-thread time",
        summary=(f"{len(vendors)} third-party vendors load in most runs: {requests:.0f} "
                 f"requests, {_kb(size)}, {_ms(cpu)} of CPU. Measure the page without "
                 "them with `ingest auto --block-third-party`."),
        evidence=evidence, magnitude=cpu)


_BLOCKING = ("blocking", "in_body_parser_blocking")


def third_party_render_blocking(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Render-blocking requests to hosts outside the site."""
    def per_run(run: HarRun) -> Dict[str, Request]:
        return {_path(r.url): r for r in _third_party(run) if r.render_blocking in _BLOCKING}

    found = _recurring(runs, per_run)
    if not found:
        return None
    evidence = []
    for key, requests in sorted(found.items()):
        durations = [r.end_ms - r.start_ms for r in requests]
        ends = [r.end_ms for r in requests]
        evidence.append(f"{_short(requests[0].url, 90)} — {_range(durations)} "
                        f"to download, done at {_range(ends)}")
    names = ", ".join(sorted({_name(r[0].url) for r in found.values()}))
    return Finding(
        severity="high",
        headline=f"First paint waits for third-party {names}",
        summary=(f"{len(found)} render-blocking request(s) go to other origins: each "
                 "needs its own DNS, TLS and connection before the page can paint."),
        evidence=evidence, magnitude=float(len(found)))


def redirected_subresources(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Sub-resources requested at a URL that only redirects."""
    def per_run(run: HarRun) -> Dict[str, Request]:
        return {_path(r.url): r for r in run.requests[1:] if 300 <= r.status < 400}

    found = _recurring(runs, per_run)
    if not found:
        return None
    evidence = []
    for requests in found.values():
        first = requests[0]
        target = f" → {_short(first.redirect_url, 70)}" if first.redirect_url else ""
        evidence.append(f"{first.status} {_short(first.url, 80)}{target} "
                        f"({_range([r.end_ms - r.start_ms for r in requests])})")
    return Finding(
        severity="medium",
        headline=f"{len(found)} sub-resource(s) are requested at a URL that redirects",
        summary=("Each redirect is a full round trip before the real download starts; "
                 "request the final URL directly."),
        evidence=evidence[:MAX_EVIDENCE_PER_CAPTURE], magnitude=float(len(found)))


_VERSIONED = re.compile(r"/(?P<lib>@?[a-z0-9][\w.-]*?)@(?P<version>\d[\w.-]*)/", re.I)


def duplicate_libraries(runs: Sequence[HarRun]) -> Optional[Finding]:
    """One library loaded at more than one major version."""
    def per_run(run: HarRun) -> Dict[str, Dict[str, str]]:
        majors: Dict[str, Dict[str, str]] = {}
        for request in run.requests:
            match = _VERSIONED.search(urlsplit(request.url).path)
            if match:
                major = match.group("version").split(".")[0]
                majors.setdefault(match.group("lib").lower(), {}).setdefault(
                    major, request.url)
        return {lib: versions for lib, versions in majors.items() if len(versions) > 1}

    found = _recurring(runs, per_run)
    if not found:
        return None
    evidence = [f"{lib} {' and '.join(sorted(versions[0]))}: "
                + ", ".join(_short(u, 70) for u in versions[0].values())
                for lib, versions in sorted(found.items())]
    libs = ", ".join(f"{lib} {' and '.join(sorted(v[0]))}" for lib, v in sorted(found.items()))
    return Finding(
        severity="medium",
        headline=f"Two versions of the same library load: {libs}",
        summary=("The page downloads, parses and runs more than one major version of "
                 "the same library."),
        evidence=evidence, magnitude=float(len(found)))


#: Static assets cached for less than this are re-downloaded (or revalidated)
#: by a returning visitor within the week.
MIN_STATIC_MAX_AGE_S = 7 * 24 * 3600
_MAX_AGE = re.compile(r"max-age=(\d+)")


def _cache_problem(request: Request) -> Optional[str]:
    policy = request.cache_control
    if not policy:
        return "no Cache-Control"
    if "no-store" in policy or "no-cache" in policy:
        return policy
    match = _MAX_AGE.search(policy)
    if match is None or int(match.group(1)) < MIN_STATIC_MAX_AGE_S or "private" in policy:
        return policy
    return None


def _age(policy: str) -> str:
    match = _MAX_AGE.search(policy)
    if not match:
        return policy
    seconds = int(match.group(1))
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{seconds} s"


def _policy_label(policy: str) -> str:
    """A policy with its ages humanised: ``private, max-age=281269`` and
    ``private, max-age=281280`` are one policy - the CDN counts down to a
    fixed expiry - so they must not be two lines of evidence."""
    return re.sub(r"(s-maxage|max-age)=(\d+)",
                  lambda m: f"{m.group(1)} {_age('max-age=' + m.group(2))}", policy)


def short_static_cache(runs: Sequence[HarRun]) -> Optional[Finding]:
    """The site's own static assets cached for less than a week, or privately."""
    def per_run(run: HarRun) -> Dict[str, Tuple[str, str]]:
        return {_path(r.url): (problem, r.url) for r in run.requests[1:]
                if r.ok and _is_static(r) and run.is_first_party(r)
                and (problem := _cache_problem(r))}

    found = _recurring(runs, per_run)
    if not found:
        return None
    by_policy: Dict[str, List[str]] = {}
    for details in found.values():
        by_policy.setdefault(_policy_label(details[0][0]), []).append(details[0][1])
    ranked = sorted(by_policy.items(), key=lambda kv: -len(kv[1]))
    versioned = sum(1 for d in found.values() if urlsplit(d[0][1]).query)
    evidence = [f"{policy} on {len(urls)} asset(s), e.g. "
                + ", ".join(_name(u) for u in urls[:3])
                for policy, urls in ranked[:MAX_EVIDENCE_PER_CAPTURE]]
    policy, urls = ranked[0]
    return Finding(
        severity="high" if len(found) >= 50 else "medium",
        headline=(f"{len(found)} static assets are cached for under a week "
                  f"({len(urls)} with {policy})"),
        summary=(f"{len(found)} of the site's own scripts, styles, fonts and images are "
                 f"cached for under a week or marked private; {versioned} of them carry a "
                 "version in the URL, so they could be cached for a year."),
        evidence=evidence, magnitude=float(len(found)))


_TEXT_TYPES = ("javascript", "css", "html", "json", "svg", "xml", "text/plain")
#: Below one TCP packet compression saves nothing measurable.
MIN_COMPRESSIBLE_BYTES = 1400


def uncompressed_text(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Text responses served without gzip, Brotli or zstd."""
    def per_run(run: HarRun) -> Dict[str, Request]:
        return {_path(r.url): r for r in run.requests
                if r.ok and not r.encoding and r.bytes_in >= MIN_COMPRESSIBLE_BYTES
                and any(t in r.content_type for t in _TEXT_TYPES)}

    found = _recurring(runs, per_run)
    if not found:
        return None
    ranked = sorted(found.values(), key=lambda rs: -rs[0].bytes_in)
    total = sum(rs[0].bytes_in for rs in ranked)
    return Finding(
        severity="medium" if total >= 50 * 1024 else "low",
        headline=f"{len(found)} text response(s) are served uncompressed ({_kb(total)})",
        summary="Text compresses to a fraction of its size; these arrive raw.",
        evidence=[f"{_kb(rs[0].bytes_in)} {rs[0].content_type} — {_short(rs[0].url, 80)}"
                  for rs in ranked[:MAX_EVIDENCE_PER_CAPTURE]],
        magnitude=float(total))


def font_display(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Web fonts whose text stays invisible until they arrive."""
    def per_run(run: HarRun) -> Dict[str, str]:
        return {f"{f.get('family')} {f.get('weight', '')}".strip(): str(f.get("display"))
                for f in run.fonts
                if f.get("status") == "loaded" and f.get("display") in ("auto", "block")}

    found = _recurring(runs, per_run)
    if not found:
        return None
    families = sorted({key.rsplit(" ", 1)[0] if " " in key else key for key in found})
    return Finding(
        severity="medium",
        headline=f"{', '.join(families)} hide text until the font arrives (font-display: auto)",
        summary=(f"{len(found)} loaded font face(s) use font-display auto or block: text "
                 "in them is invisible for up to 3 s while the file downloads."),
        evidence=[f"{key}: font-display {details[0]}" for key, details in sorted(found.items())],
        magnitude=float(len(found)))


#: DOM size at which style and layout work visibly grows (Chrome's audit).
DOM_ELEMENTS_WARN = 1500
DOM_ELEMENTS_HIGH = 3000


def dom_size(runs: Sequence[HarRun]) -> Optional[Finding]:
    counts = [r.dom_elements for r in runs if r.dom_elements is not None]
    if not counts or median(counts) < DOM_ELEMENTS_WARN:
        return None
    size = median(counts)
    return Finding(
        severity="high" if size >= DOM_ELEMENTS_HIGH else "medium",
        headline=f"The page builds {size:,.0f} DOM elements",
        summary=(f"{size:,.0f} elements at load (median of {len(counts)} runs; "
                 f"{DOM_ELEMENTS_WARN:,} is where style and layout cost grows): every "
                 "style recalculation and layout walks all of them."),
        evidence=[f"{_range(counts, lambda n: f'{n:,.0f}')} elements across "
                  f"{len(counts)} runs"],
        magnitude=float(size))


_HIGH_PRIORITY = ("high", "veryhigh", "highest")


def lcp_priority(runs: Sequence[HarRun]) -> Optional[Finding]:
    """The LCP image is fetched at low priority."""
    considered, low = [], []
    for run in runs:
        image = run.request_for(run.lcp_url)
        if image is None or image.priority is None:
            continue
        considered.append(run)
        if image.priority.lower() not in _HIGH_PRIORITY:
            low.append((run, image))
    if not low or len(low) <= len(considered) * hc.MAJORITY:
        return None
    return Finding(
        severity="high",
        headline="The LCP image is fetched at Low priority",
        summary=(f"In {len(low)} of {len(considered)} runs the browser fetches the largest "
                 "image at low priority, behind scripts and styles."),
        evidence=[f"{run.label}: priority {image.priority}, requested at "
                  f"{image.start_ms:.0f} ms, painted at {run.lcp_ms or 0:.0f} ms — "
                  f"{_short(image.url, 80)}" for run, image in low],
        magnitude=float(len(low)))


def failed_requests(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Requests the site makes that fail - not those the capture broke."""
    caused = hc.artifact_urls(runs)

    def per_run(run: HarRun) -> Dict[str, Request]:
        return {_path(r.url): r for r in run.requests
                if r.status >= 400 and r.url not in caused}

    found = _recurring(runs, per_run)
    if not found:
        return None
    ranked = sorted(found.values(),
                    key=lambda rs: (not runs[0].is_first_party(rs[0]), rs[0].url))
    first_party = [rs for rs in ranked if runs[0].is_first_party(rs[0])]
    return Finding(
        severity="medium" if first_party else "low",
        headline=f"{len(found)} request(s) fail on every load",
        summary=(f"{len(found)} request(s) answer an error in most runs "
                 f"({len(first_party)} to the site's own hosts)."),
        evidence=[f"{rs[0].status} {_short(rs[0].url, 90)}"
                  for rs in ranked[:MAX_EVIDENCE_PER_CAPTURE]],
        magnitude=float(len(found)))


_JS_ERROR = re.compile(r"\b(TypeError|ReferenceError|SyntaxError|RangeError|Uncaught)\b")
_URL = re.compile(r"https?://[^\s'\")]+")


def console_errors(runs: Sequence[HarRun]) -> Optional[Finding]:
    """JavaScript errors in the console, less those the capture caused.

    Network failures are ``failed_requests``' and service-worker refusals the
    ``service_worker`` check's; CORS errors on hosts the capture's own header
    broke are dropped.
    """
    artifact_hosts = set(hc.header_artifacts(runs))

    def key_of(text: str) -> str:
        return re.sub(r"\d+", "N", _URL.sub("<url>", text))[:160]

    def per_run(run: HarRun) -> Dict[str, str]:
        out = {}
        for entry in run.console:
            text = str(entry.get("text", ""))
            if "serviceworker" in text.lower() or text.startswith("Failed to load resource"):
                continue
            if "CORS policy" in text:
                hosts = {urlsplit(u).hostname for u in _URL.findall(text)}
                if hosts & artifact_hosts or run.cross_origin_headers:
                    continue
            is_error = entry.get("level") == "error" and entry.get("source") == "javascript"
            if is_error or _JS_ERROR.search(text):
                out[key_of(text)] = _URL.sub(lambda m: _short(m.group(0), 60), text)
        return out

    found = _recurring(runs, per_run)
    if not found:
        return None
    return Finding(
        severity="medium",
        headline=f"{len(found)} JavaScript error(s) on every load",
        summary=f"{len(found)} distinct error(s) appear in the console in most runs.",
        evidence=[details[0][:200] for details in list(found.values())
                  [:MAX_EVIDENCE_PER_CAPTURE]],
        magnitude=float(len(found)))


def vulnerable_libraries(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Libraries with published advisories, as WebPageTest detects them."""
    def per_run(run: HarRun) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for vuln in run.js_vulns:
            out.setdefault(f"{vuln.get('name')} {vuln.get('version')}", []).append(vuln)
        return out

    found = _recurring(runs, per_run)
    if not found:
        return None
    rows = []
    for lib, details in sorted(found.items()):
        advisories = details[0]
        worst = min((str(a.get("severity", "low")) for a in advisories),
                    key=lambda s: _RANK.get(s, len(SEVERITIES)))
        rows.append((worst, lib, advisories))
    rows.sort(key=lambda r: _RANK.get(r[0], len(SEVERITIES)))
    return Finding(
        severity="high" if rows[0][0] == "high" else "medium",
        headline="Outdated libraries with known advisories: "
                 + ", ".join(lib for _, lib, _ in rows),
        summary=f"{len(rows)} library version(s) on the page have published advisories; "
                "old versions are also the heaviest.",
        evidence=[f"{lib}: {len(adv)} advisory(ies), worst {worst} — "
                  f"{adv[0].get('url', '')}" for worst, lib, adv in rows],
        magnitude=float(len(rows)))


#: An image is oversized when its file is this many times wider than the
#: pixels it fills, and wastes at least this many pixels of width.
OVERSIZE_RATIO = 1.5
OVERSIZE_MIN_EXTRA_PX = 200


def oversized_images(runs: Sequence[HarRun]) -> Optional[Finding]:
    """Raster images downloaded much wider than they are displayed."""
    def per_run(run: HarRun) -> Dict[str, Tuple[int, int, float]]:
        out = {}
        for image in run.images:
            url, shown, natural = (str(image.get("url", "")), image.get("width") or 0,
                                   image.get("naturalWidth") or 0)
            if not url.startswith("http") or urlsplit(url).path.endswith(".svg"):
                continue
            needed = shown * run.dpr
            if shown and natural > needed * OVERSIZE_RATIO \
                    and natural - needed >= OVERSIZE_MIN_EXTRA_PX:
                out[url] = (shown, natural, run.dpr)
        return out

    found = _recurring(runs, per_run)
    if not found:
        return None
    ranked = sorted(found.items(), key=lambda kv: -(kv[1][0][1] / (kv[1][0][0] * kv[1][0][2])))
    evidence = [f"{natural} px wide in a {shown} px slot at {dpr:g}x — {_short(url, 80)}"
                for url, ((shown, natural, dpr), *_rest) in ranked[:MAX_EVIDENCE_PER_CAPTURE]]
    return Finding(
        severity="medium",
        headline=f"{len(found)} image(s) are downloaded far wider than they are shown",
        summary=(f"{len(found)} images are at least {OVERSIZE_RATIO:g}x wider than the "
                 "pixels they fill, even at the device's pixel ratio."),
        evidence=evidence, magnitude=float(len(found)))


RULES: Tuple[Rule, ...] = (
    Rule("main_thread_scripts", "JavaScript", main_thread_scripts,
         fix=("Profile the named scripts in a DevTools Performance trace. Defer or "
              "lazy-load what the first view does not need, split the rest by route, "
              "and break long tasks up (scheduler.yield()) so input can run between them."),
         done_when="No script runs over 100 ms of main-thread time during load in a new "
                   "WebPageTest capture, and Total Blocking Time is under 200 ms."),
    Rule("third_party_cost", "Third parties", third_party_cost,
         fix=("Review each vendor with its owner: load non-essential tags after the "
              "page is interactive, or through a tag manager with a consent gate; drop "
              "the ones nobody reads."),
         done_when="Third-party CPU under 100 ms and bytes under 150 KB in a new capture."),
    Rule("third_party_render_blocking", "Third parties", third_party_render_blocking,
         fix=("Self-host these files with the site's own CSS/JS, or load them without "
              "blocking (media=print swap for CSS, defer/async for scripts)."),
         done_when="No render-blocking request goes to a host outside the site."),
    Rule("redirected_subresources", "Network", redirected_subresources,
         fix="Reference the final URL (for unpkg, the exact version, e.g. react@17.0.2).",
         done_when="No sub-resource answers 3xx in a new capture."),
    Rule("duplicate_libraries", "JavaScript", duplicate_libraries,
         fix=("Move every consumer onto one version and serve it from the site's own "
              "bundle rather than a public CDN."),
         done_when="Each library loads at one version."),
    Rule("short_static_cache", "Caching", short_static_cache,
         fix=("Serve versioned static assets with Cache-Control: public, "
              "max-age=31536000, immutable; drop `private` from static images."),
         done_when="Every versioned script, style, font and image on the site's hosts "
                   "carries a max-age of at least a week (a year when versioned)."),
    Rule("uncompressed_text", "Network", uncompressed_text,
         fix="Enable Brotli (or gzip) for these content types at the CDN or origin.",
         done_when="Every text response over 1.4 KB carries a Content-Encoding."),
    Rule("font_display", "Fonts", font_display,
         fix="Add font-display: swap (or optional) to these @font-face rules.",
         done_when="No loaded font face reports font-display auto or block."),
    Rule("dom_size", "Rendering", dom_size,
         fix=("Render below-the-fold sections, mega-menu panels and off-screen "
              "carousels on demand instead of in the initial HTML."),
         done_when="Under 1,500 DOM elements at load."),
    Rule("lcp_priority", "Images", lcp_priority,
         fix=('Add fetchpriority="high" to the LCP <img> (and to its preload), and '
              "make sure it is not lazy-loaded."),
         done_when="The LCP image is fetched at High priority in every run.",
         related_checks=("preload_mismatch", "lcp_render_delay")),
    Rule("oversized_images", "Images", oversized_images,
         fix="Request the width the slot needs (srcset/sizes, or the image CDN's width "
             "parameter matched to the layout).",
         done_when="No raster image is more than 1.5x wider than its slot at the "
                   "device's pixel ratio.",
         related_checks=("preload_mismatch", "multiple_renditions")),
    Rule("failed_requests", "Errors", failed_requests,
         fix="Remove the references, or restore the resources they point to.",
         done_when="No request answers 4xx/5xx in a capture taken with the header "
                   "scoped to the site.",
         related_checks=("service_worker",)),
    Rule("console_errors", "Errors", console_errors,
         fix="Fix each error at its source; an uncaught error can stop the rest of "
             "the script it is in.",
         done_when="The console of a new capture shows none of these errors."),
    Rule("vulnerable_libraries", "Maintenance", vulnerable_libraries,
         fix="Upgrade the libraries (or remove them where nothing uses them).",
         done_when="WebPageTest flags no library with a known advisory."),
)

RULE_IDS = tuple(rule.id for rule in RULES)


# --------------------------------------------------------------------------- #
# Across captures
# --------------------------------------------------------------------------- #
def _title(pages: Sequence[str], all_pages: Sequence[str], headline: str) -> str:
    if len(all_pages) > 1 and set(pages) >= set(all_pages):
        scope = "All pages"
    else:
        scope = " / ".join(PAGE_ABBREVIATIONS.get(p, p.upper()) for p in pages)
    return f"{scope} | {headline}"


def discover(captures: Sequence[Any], catalog: Optional[Any] = None) -> List[ProposedTicket]:
    """Every rule over every capture, one proposed ticket per rule found.

    ``captures`` are ``analysis.tickets.Capture``; ``catalog`` a
    ``TicketCatalog`` (optional), used to name related and tracking tickets.
    Sorted most severe first, then by how many captures show it.
    """
    open_tickets = list(getattr(catalog, "tickets", []) or [])
    all_pages = sorted({c.page for c in captures})
    proposals: List[ProposedTicket] = []
    for rule in RULES:
        hits = [(c, f) for c in captures if c.runs and (f := rule.detect(c.runs))]
        if not hits:
            continue
        worst = min(hits, key=lambda cf: (_RANK[cf[1].severity], -cf[1].magnitude))[1]
        evidence: List[str] = []
        for capture, found in hits:
            evidence.append(f"{capture.label}: {found.headline}")
            evidence += [f"  {line}" for line in found.evidence[:MAX_EVIDENCE_PER_CAPTURE]]
        pages = sorted({c.page for c, _ in hits}, key=all_pages.index)
        proposals.append(ProposedTicket(
            key=rule.id, title=_title(pages, all_pages, worst.headline),
            category=rule.category, severity=worst.severity, summary=worst.summary,
            fix=rule.fix, done_when=rule.done_when,
            seen_on=[f"{c.page}/{c.device}" for c, _ in hits],
            captures_total=len(captures), evidence=evidence,
            related=[t.id for t in open_tickets
                     if t.check in rule.related_checks],
            tracked_by=next((t.id for t in open_tickets
                             if rule.id in (getattr(t, "tracks", None) or [])), None),
        ))
    proposals.sort(key=lambda p: (_RANK[p.severity], -len(p.seen_on), p.key))
    return proposals
