"""Open tickets, checked against HAR captures.

``config/tickets.yaml`` names each ticket, the pages it applies to and the
check in :mod:`analysis.har_checks` that can confirm it. This module runs
those checks over the captures supplied for the campaign and says, per
ticket, what the captures show — with the runs and requests that prove it.

A ticket is never dropped: one with no applicable capture, or that no HAR can
answer, is reported with the reason. A report that silently omits a ticket
reads as though it was checked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from analysis import har_checks as hc
from analysis.discovery import RULE_IDS

TICKETS_FILE = Path(__file__).resolve().parents[1] / "config" / "tickets.yaml"

#: Evidence lines kept per capture: enough to act on, few enough to read.
MAX_EVIDENCE_PER_CAPTURE = 3

NOT_CHECKABLE = "not_checkable"


class TicketSpec(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    sites: List[str] = Field(default_factory=list)
    pages: List[str] = Field(default_factory=lambda: ["*"])
    check: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    #: ``analysis.discovery`` rules this ticket already covers: a problem a
    #: rule finds is then listed as filed here rather than proposed again.
    tracks: List[str] = Field(default_factory=list)


class TicketCatalog(BaseModel):
    site_hosts: Dict[str, List[str]] = Field(default_factory=dict)
    tickets: List[TicketSpec] = Field(default_factory=list)


def load_catalog(path: Path = TICKETS_FILE) -> TicketCatalog:
    """Load and validate the catalog; an unknown check is an error, not a skip."""
    catalog = TicketCatalog.model_validate(
        yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    for ticket in catalog.tickets:
        if ticket.check is not None and ticket.check not in hc.CHECKS:
            raise ValueError(f"{ticket.id}: unknown check {ticket.check!r} "
                             f"(known: {', '.join(sorted(hc.CHECKS))})")
        if ticket.check is None and not ticket.reason:
            raise ValueError(f"{ticket.id}: a ticket with no check must give a reason")
        unknown = sorted(set(ticket.tracks) - set(RULE_IDS))
        if unknown:
            raise ValueError(f"{ticket.id}: unknown discovery rule(s) {', '.join(unknown)} "
                             f"(known: {', '.join(RULE_IDS)})")
    return catalog


@dataclass
class Capture:
    """One HAR for one page under one condition."""

    page: str
    device: str
    runs: List[hc.HarRun]

    @property
    def label(self) -> str:
        source = self.runs[0].source if self.runs else "?"
        return f"{self.page} / {self.device} ({source}, {len(self.runs)} runs)"


@dataclass
class TicketOutcome:
    id: str
    title: str
    sites: List[str]
    pages: List[str]
    status: str
    summary: str
    evidence: List[str] = field(default_factory=list)
    observed_on: List[str] = field(default_factory=list)


def site_of(url: str, site_hosts: Mapping[str, Sequence[str]]) -> Optional[str]:
    host = (urlsplit(url).hostname or "").lower()
    return next((site for site, hosts in site_hosts.items() if host in hosts), None)


def evaluate(catalog: TicketCatalog, captures: Sequence[Capture]) -> List[TicketOutcome]:
    """Every ticket in the catalog, in catalog order, with what the captures show."""
    outcomes: List[TicketOutcome] = []
    for ticket in catalog.tickets:
        applicable = [c for c in captures
                      if "*" in ticket.pages or c.page in ticket.pages]
        base = dict(id=ticket.id, title=ticket.title, sites=list(ticket.sites),
                    pages=list(ticket.pages))
        if ticket.check is None:
            outcomes.append(TicketOutcome(**base, status=NOT_CHECKABLE,
                                          summary=ticket.reason))
            continue
        if not applicable:
            outcomes.append(TicketOutcome(
                **base, status=hc.NO_DATA,
                summary=f"No capture was supplied for {', '.join(ticket.pages)}."))
            continue

        results: List[Tuple[Capture, hc.CheckResult]] = [
            (c, hc.CHECKS[ticket.check](c.runs, **ticket.params)) for c in applicable]
        confirmed = [(c, r) for c, r in results if r.status == hc.CONFIRMED]
        answered = [(c, r) for c, r in results if r.status != hc.NO_DATA]
        status = (hc.CONFIRMED if confirmed else hc.NOT_SEEN if answered else hc.NO_DATA)

        evidence: List[str] = []
        for capture, result in results:
            evidence.append(f"{capture.label}: {result.summary}")
            if result.status == hc.CONFIRMED:
                evidence += [f"  {line}" for line in
                             result.evidence[:MAX_EVIDENCE_PER_CAPTURE]]
        sites = sorted({site for c, _ in answered
                        if (site := site_of(c.runs[0].page_url, catalog.site_hosts))})
        shown = confirmed or answered or results
        outcomes.append(TicketOutcome(
            **base, status=status, evidence=evidence, observed_on=sites,
            summary=_headline(status, shown, len(results))))
    return outcomes


def _headline(status: str, shown: Sequence[Tuple[Capture, hc.CheckResult]],
              total: int) -> str:
    where = ", ".join(f"{c.page}/{c.device}" for c, _ in shown)
    if status == hc.CONFIRMED:
        return f"Confirmed on {where} ({len(shown)} of {total} captures)."
    if status == hc.NOT_SEEN:
        return f"Not seen in {total} capture(s) that can show it."
    return shown[0][1].summary if shown else "No capture can show it."


def parse_har_arg(value: str) -> Tuple[str, str, Path]:
    """``page/device=path`` -> (page, device, path), as ``--har`` takes it."""
    target, sep, path = value.partition("=")
    page, slash, device = target.partition("/")
    if not sep or not slash or not page or not device or not path:
        raise ValueError(f"--har expects page/device=path, got {value!r}")
    return page.strip(), device.strip(), Path(path.strip())
