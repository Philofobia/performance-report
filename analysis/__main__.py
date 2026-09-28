"""``python -m analysis`` — runs in, Report JSON out.

Reachable both directly and as ``python -m cli analyze``: the Phase 6 façade
forwards argv here verbatim rather than redeclaring these flags, so this
parser stays the single definition of the analysis stage's interface.

It never fails because a model was unavailable. Missing key, exhausted quota
or unusable model output all degrade to the rule-based path and the report
says so in ``meta.analysis_mode``. A non-zero exit means something a user can
fix: no runs found, unreadable input, conflicting flags.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from analysis import trends
from analysis.findings import PageAnalysis, analyze_page, select_primary
from analysis.reportmodel import Report, build_report, to_json
from config.load import load_settings
from normalize.schema import Run
from rag import knowledge, retrieve
from rag.budget import BudgetExhaustedError
from rag.embeddings import EmbeddingError
from store.vectordb import Document

MAX_TOP_ACTIONS = 3


@dataclass
class SimpleSummary:
    """The rule-based stand-in for ``LlmSummary``."""

    problem: str
    key_finding: str
    top_actions: List[str]


def _one_project(runs: List[Run], project: Optional[str], where: Any) -> List[Run]:
    """The runs of exactly one project — named, or the only one present.

    Never merged: a report takes its name, campaign id and history from its
    project, and a directory holding a CI campaign beside Oakley's produced a
    report named after whichever run happened to sort first.
    """
    counts: Dict[str, int] = {}
    for run in runs:
        counts[run.project.name] = counts.get(run.project.name, 0) + 1
    listing = ", ".join(f"{name} ({counts[name]})" for name in sorted(counts))

    if project is not None:
        if project not in counts:
            raise FileNotFoundError(
                f"No runs for project {project!r} in {where}; found: {listing}")
        return [r for r in runs if r.project.name == project]
    if len(counts) > 1:
        raise ValueError(
            f"Runs from more than one project in {where}: {listing}. "
            "Pass --project to choose one.")
    return runs


def _latest_per_condition(runs: List[Run]) -> List[Run]:
    """The newest run of each (page x device x network); say what was dropped.

    One run per condition is what a campaign writes, but the store holds every
    campaign ever measured, and a directory can hold files from before a
    rename. Taken together they were analysed as one campaign: August beside
    today, with the worse of the two chosen as the page's primary run.
    """
    latest: Dict[tuple, Run] = {}
    for run in runs:
        key = (run.page.name, run.condition.device, run.condition.network)
        held = latest.get(key)
        if held is None or (run.meta.created_at, run.run_id) > (
                held.meta.created_at, held.run_id):
            latest[key] = run
    superseded = len(runs) - len(latest)
    if superseded:
        noun = "run" if superseded == 1 else "runs"
        print(f"Analysing the newest run of each condition; {superseded} older "
              f"{noun} of the same conditions set aside.", file=sys.stderr)
    return list(latest.values())


def load_runs(
    *,
    input_dir: Optional[Any] = None,
    from_store: Optional[Any] = None,
    pages: Optional[Sequence[str]] = None,
    project: Optional[str] = None,
) -> List[Run]:
    """Load one campaign's runs from a directory of JSON, or from SQLite.

    One campaign means one project (``project``, or the only one present) and
    the newest run of each page x condition.
    """
    runs: List[Run] = []
    if from_store is not None:
        from store import sql

        # `sql.connect` creates what it opens, so a mistyped path would leave a
        # stray empty database behind and report "no runs found" — indis-
        # tinguishable from a store that is genuinely empty. `store/listing.py`
        # already refuses this; analysis has to as well.
        if not Path(from_store).is_file():
            raise FileNotFoundError(f"No run store at {from_store}")

        conn = sql.connect(from_store)
        sql.init_schema(conn)
        try:
            runs = sql.list_runs(conn)
        finally:
            conn.close()
    else:
        directory = Path(input_dir)
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Could not read run JSON {path}: {exc}") from exc
            runs.append(Run.model_validate(payload))

    where = from_store if from_store is not None else input_dir
    if runs:
        runs = _one_project(runs, project, where)

    if pages:
        wanted = {p.strip() for p in pages if p.strip()}
        runs = [r for r in runs if r.page.name in wanted]

    if not runs:
        raise FileNotFoundError(f"No runs found in {where}")
    return sorted(_latest_per_condition(runs), key=lambda r: r.run_id)


def group_by_page(runs: Sequence[Run]) -> Dict[str, List[Run]]:
    """Group runs by page name, page names sorted (§7.1)."""
    grouped: Dict[str, List[Run]] = {}
    for run in runs:
        grouped.setdefault(run.page.name, []).append(run)
    return {name: grouped[name] for name in sorted(grouped)}


def rule_based_summary(pages: Sequence[PageAnalysis]) -> SimpleSummary:
    """Executive summary without a model: state what the rules found."""
    failing = [p for p in pages if any(s.severity == "fail" for s in p.symptoms)]
    worst = failing[0] if failing else (pages[0] if pages else None)

    if worst is None:
        return SimpleSummary(
            problem="No runs were analysed.",
            key_finding="No measurements available.",
            top_actions=[],
        )

    page_word = "page" if len(pages) == 1 else "pages"
    problem = (
        f"{len(failing)} of {len(pages)} tested {page_word} exceed a Core Web "
        f"Vitals threshold." if failing else
        f"All {len(pages)} tested {page_word} are within their configured targets."
    )
    key_finding = worst.symptoms[0].text if worst.symptoms else worst.summary

    actions: List[str] = []
    for page in pages:
        for rec in page.recommendations:
            label = f"{rec.title} ({page.page_name})"
            if label not in actions:
                actions.append(label)
    return SimpleSummary(problem=problem, key_finding=key_finding,
                         top_actions=actions[:MAX_TOP_ACTIONS])


def load_har_evidence(har_args: Sequence[str],
                      tickets_path: Optional[str] = None) -> tuple:
    """``--har`` values -> (captures, ticket outcomes); both empty with no HAR.

    Tickets are evaluated only when a HAR was supplied: with nothing to check
    against, every ticket would read "no data", which says nothing.
    """
    from analysis import har_checks, tickets as tk

    captures = []
    for value in har_args:
        page, device, path = tk.parse_har_arg(value)
        runs = har_checks.load_har(path)
        if not runs:
            raise ValueError(f"{path} holds no page loads")
        captures.append(tk.Capture(page=page, device=device, runs=runs))
    if not captures:
        return [], []
    catalog = tk.load_catalog(Path(tickets_path) if tickets_path else tk.TICKETS_FILE)
    return captures, tk.evaluate(catalog, captures)


def _degradation_reason(exc: Exception) -> str:
    """The same reason names ``analyze_page`` gives a page that fell back.

    Most specific first: ``BudgetExhaustedError`` and ``QuotaExceededError``
    are both ``EmbeddingError``s, and "we chose not to spend" must not read as
    a bad response.
    """
    from analysis.llm import InvalidModelOutputError, LlmUnavailableError
    from rag.embeddings import MissingApiKeyError, QuotaExceededError

    for kind, reason in ((BudgetExhaustedError, "budget_exhausted"),
                         (QuotaExceededError, "quota_exhausted"),
                         (MissingApiKeyError, "no_api_key"),
                         (LlmUnavailableError, "model_unavailable"),
                         (InvalidModelOutputError, "invalid_model_output")):
        if isinstance(exc, kind):
            return reason
    return "invalid_model_output"


def _summary_payload(pages: Sequence[PageAnalysis]) -> str:
    """What the summary call sees: only text this system already produced.

    Neutralised anyway — it originated from a model that read untrusted
    context (design spec §12).
    """
    from rag.prompt import neutralize

    lines: List[str] = []
    for page in pages:
        lines.append(f"# {page.page_name}")
        lines.append(neutralize(page.summary))
        for finding in page.findings:
            lines.append(f"- {neutralize(finding.title)}")
        for rec in page.recommendations:
            lines.append(f"* action: {neutralize(rec.title)}")
    return "\n".join(lines)


def _top_up_actions(summary: Any, pages: Sequence[PageAnalysis]) -> Any:
    """Fill ``top_actions`` from the highest-projected recommendations.

    Never pads with invented actions: if the campaign has fewer than three
    recommendations, the list is simply shorter (design spec §5.4).
    """
    actions = list(summary.top_actions)
    for page in pages:
        if len(actions) >= MAX_TOP_ACTIONS:
            break
        for rec in page.recommendations:
            if len(actions) >= MAX_TOP_ACTIONS:
                break
            if rec.title not in actions:
                actions.append(rec.title)
    summary.top_actions = actions[:MAX_TOP_ACTIONS]
    return summary


def load_field_snapshot(settings: Any, project: str) -> Optional[Any]:
    """The latest stored field snapshot for ``project``, or ``None``.

    Mirrors ``load_runs``'s store-safety rule: ``sql.connect`` creates what it
    opens, so asking "is there field data yet?" for a campaign analysed from
    a directory would otherwise leave an empty database behind. Checking
    ``Path.is_file()`` first keeps that question free of side effects.

    Analysis must never fail because Grafana was down or no snapshot was ever
    ingested — the opposite of ``ingest field``, which exits non-zero on
    exactly those conditions. So every failure here — no store, an unreadable
    file, a corrupt row — degrades to ``None`` rather than raising, the same
    way a missing LLM API key degrades the analysis mode instead of aborting
    the report.
    """
    path = Path(settings.storage.sqlite_path)
    if not path.is_file():
        return None

    from store import sql

    try:
        conn = sql.connect(path)
    except Exception as exc:  # field data must never cost a report
        print(f"Field data unavailable: {exc}", file=sys.stderr)
        return None
    try:
        sql.init_schema(conn)
        return sql.get_latest_snapshot(conn, project)
    except Exception as exc:  # a corrupt snapshot degrades, it does not raise
        print(f"Field data unavailable: {exc}", file=sys.stderr)
        return None
    finally:
        conn.close()


def run_analysis(
    runs: Sequence[Run],
    *,
    store: Optional[Any] = None,
    embed_client: Optional[Any] = None,
    llm_client: Optional[Any] = None,
    settings: Optional[Any] = None,
    use_priors: bool = False,
    top_k: Optional[int] = None,
    knowledge_dir: str = "data/knowledge",
    generated_at: Optional[datetime] = None,
    page_analyses_out: Optional[List[PageAnalysis]] = None,
    llm_disabled: bool = False,
    history: Optional[Sequence[Any]] = None,
    field: Optional[Any] = None,
    no_field: bool = False,
    har_captures: Sequence[Any] = (),
    tickets: Sequence[Any] = (),
) -> Report:
    """Run the full analysis pipeline over a campaign's runs.

    ``llm_disabled`` records that the *user* turned the model off, so the
    report says "llm_disabled" rather than accusing the environment of a
    missing key.

    ``history`` is the trend input. Left None it is read from the configured
    run store — which happens whatever ``--input-dir``/``--from-store`` the
    current campaign came from, because the default path never touches the
    store and would otherwise have no history at all. Tests inject it, the way
    the LLM and embedding clients are already injected.

    ``field`` is the campaign's real-user snapshot. Left None (and
    ``no_field`` False) it is loaded from the configured store, the same way
    ``history`` is — tests inject it directly. A missing store, an unreadable
    snapshot, or a corrupt payload all degrade to no field data rather than
    failing the report: this stage never fails because Grafana was down.
    ``no_field`` lets a caller (the ``--no-field`` CLI flag) suppress a stored
    snapshot even when one exists.
    """
    settings = settings or load_settings()
    k = top_k or settings.rag.top_k
    chunks = knowledge.load_knowledge_dir(knowledge_dir)
    digest = knowledge.content_digest(chunks)

    project = runs[0].project.name if runs else "report"
    if field is None and not no_field:
        field = load_field_snapshot(settings, project)

    if store is not None and embed_client is not None:
        # Nothing else ever called `index_knowledge`, so a real store shipped
        # empty: retrieval found nothing, the model cited a playbook it had
        # invented, and the citation guard dropped every recommendation. The
        # pass is cheap to repeat — chunk ids are stable, so edited playbooks
        # replace their old chunks, and the embedding cache means unchanged
        # text costs no API calls.
        try:
            knowledge.index_knowledge(store, embed_client, chunks=chunks)
        except EmbeddingError as exc:
            # Includes BudgetExhaustedError. Retrieval will simply find
            # nothing and the pages degrade; indexing must not lose a report.
            print(f"Playbooks were not indexed: {exc}", file=sys.stderr)

    analyses: List[PageAnalysis] = []
    for page_name, page_runs in group_by_page(runs).items():
        primary = select_primary(page_runs)
        page_group = next(
            (g for g, name in settings.grafana.page_groups.items()
             if name == page_name),
            None,
        )
        symptoms = retrieve.detect_symptoms(primary, settings.thresholds)
        if field is not None:
            # Graded with the SAME thresholds as the report's top-level field
            # section (Task 10's `_field_block`). Detecting them here, where
            # `settings.thresholds` is already in scope, rather than inside
            # `analyze_page`, keeps the two gradings from ever drifting apart.
            symptoms = list(symptoms) + retrieve.detect_field_symptoms(
                field, page_group=page_group, thresholds=settings.thresholds
            )

        hits: List[Any] = []
        priors: List[Any] = []
        page_client = llm_client
        page_reason = "llm_disabled" if llm_disabled else "no_api_key"
        if store is not None and embed_client is not None:
            try:
                hits, _query = retrieve.retrieve_context(
                    primary, store, embed_client,
                    thresholds=settings.thresholds, top_k=k,
                )
                if use_priors:
                    priors = retrieve.retrieve_prior_findings(
                        primary, store, embed_client, thresholds=settings.thresholds
                    )
            except BudgetExhaustedError:
                # Retrieval is what grounds the model, and this system does not
                # ship ungrounded analysis — so a page that cannot afford its
                # embeddings is analysed by rules rather than by a model
                # working from nothing.
                hits, priors, page_client = [], [], None
                page_reason = "budget_exhausted"

        analyses.append(analyze_page(
            page_runs, hits=hits, symptoms=symptoms, client=page_client,
            prior_findings=priors, chunks=chunks,
            no_client_reason=page_reason,
            field=field, page_group=page_group,
        ))

    summary: Any = rule_based_summary(analyses)
    summary_degradation: Optional[str] = None
    if llm_client is not None and analyses and all(p.mode == "llm" for p in analyses):
        from analysis.llm import AnalysisError
        from rag.embeddings import EmbeddingError

        try:
            summary = _top_up_actions(
                llm_client.summarize(_summary_payload(analyses)), analyses
            )
        except (AnalysisError, EmbeddingError) as exc:
            summary = rule_based_summary(analyses)
            summary_degradation = _degradation_reason(exc)
            print(f"Executive summary: no model summary ({summary_degradation}) - "
                  "the report carries the rule-based one.", file=sys.stderr)

    if page_analyses_out is not None:
        page_analyses_out.extend(analyses)

    model = getattr(llm_client, "model", "none") if llm_client else "none"

    if history is None:
        history = trends.load_history(settings.storage.sqlite_path, project=project)
    series = trends.build_series(
        runs, history=history, thresholds=settings.thresholds,
        dead_band_pct=settings.trends.dead_band_pct,
        window=settings.trends.window,
    )

    return build_report(
        analyses, project=project, settings=settings, summary=summary,
        generated_at=generated_at or datetime.now(timezone.utc),
        model=model, knowledge_digest=digest, trends=series, field=field,
        summary_degradation=summary_degradation,
        har_captures=har_captures, tickets=tickets,
    )


def persist_findings(
    store: Any, embed_client: Any, report: Report, pages: Sequence[PageAnalysis]
) -> int:
    """Embed each page's findings so future runs can retrieve them (§5.1.2)."""
    documents: List[Document] = []
    for page in pages:
        body = [page.summary]
        body += [f"{f.title}. {f.detail}" for f in page.findings]
        documents.append(Document(
            doc_id=f"finding:{report.cover.campaign_id}:{page.page_name}",
            text="\n".join(part for part in body if part),
            kind="finding",
            source=f"{report.cover.project}/{page.page_name}",
            metadata={
                "campaign_id": report.cover.campaign_id,
                "page": page.page_name,
                "run_id": page.primary_run.run_id,
                "created_at": report.cover.generated_at.isoformat(),
                "symptom_codes": [s.code for s in page.symptoms],
            },
        ))
    if not documents:
        return 0
    vectors = embed_client.embed_documents([d.text for d in documents])
    store.add(documents, vectors, model=embed_client.model)
    return len(documents)


def _build_live_clients(settings, budget=None) -> tuple:
    """Build the real store and clients, or fall back to the rule-based path.

    A missing key is not an error here: it means this campaign is analysed by
    rules, which is a supported outcome.
    """
    from rag.embeddings import (
        EmbeddingCache,
        GoogleEmbeddingClient,
        resolve_api_key,
    )
    from store import sql
    from store.vectordb import SqliteVectorStore

    from analysis.llm import GoogleAnalysisClient

    try:
        resolve_api_key()
    except EmbeddingError as exc:
        print(f"Running rule-based: {exc}", file=sys.stderr)
        return None, None, None

    conn = sql.connect(settings.storage.sqlite_path)
    store = SqliteVectorStore(conn)
    # The cache is what makes re-indexing the corpus every run free: it is
    # keyed by content, so unchanged playbooks cost no API calls at all.
    embed_client = GoogleEmbeddingClient(
        model=settings.models.embeddings, budget=budget,
        cache=EmbeddingCache(conn))
    llm_client = GoogleAnalysisClient(model=settings.models.llm, budget=budget)
    return store, embed_client, llm_client


def _budget_from_args(args, settings) -> Optional[Any]:
    """Build this run's budget from settings plus command-line overrides.

    Overrides are applied to a copy: a flag that changes one run must not
    change the settings object every later stage reads.
    """
    from rag.budget import build_budget

    if getattr(args, "no_budget", False):
        return None

    supplied = {
        key: value
        for key, value in (
            ("daily_requests", args.daily_requests),
            ("daily_input_tokens", args.daily_input_tokens),
            ("daily_output_tokens", args.daily_output_tokens),
            ("max_output_tokens_per_call", args.max_output_tokens),
        )
        if value is not None
    }
    if supplied:
        settings = settings.model_copy(deep=True)
        settings.budget.llm = settings.budget.llm.model_copy(update=supplied)

    # A run that will not spend anything has no business creating the store:
    # `sql.connect` creates what it opens, and `--budget-status` on a fresh
    # checkout would otherwise leave an empty database behind.
    spends = not (getattr(args, "budget_status", False) or getattr(args, "no_llm", False))
    conn = None
    if spends or Path(settings.storage.sqlite_path).is_file():
        try:
            from store import sql

            conn = sql.connect(settings.storage.sqlite_path)
        except Exception as exc:  # bookkeeping must never cost a report
            print(f"Token ledger unavailable, counting in memory only: {exc}",
                  file=sys.stderr)
    return build_budget(settings, conn=conn)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m analysis",
        description="Analyse stored runs and emit the Report JSON.",
    )
    p.add_argument("--input-dir", default=None,
                   help="Directory of normalized run JSON (default data/processed).")
    p.add_argument("--from-store", default=None,
                   help="Read runs from this SQLite database instead of a directory.")
    p.add_argument("--pages", default=None,
                   help="Comma-separated page names to analyse.")
    p.add_argument("--har", action="append", default=[], metavar="PAGE/DEVICE=PATH",
                   help="A HAR for one page and condition, e.g. "
                        "homepage/mobile=HOMEMOB.har (WebPageTest exports carry the "
                        "most). Repeat per file. Its findings and the tickets in "
                        "--tickets are checked against it; the file is read, never "
                        "copied.")
    p.add_argument("--tickets", default=None,
                   help="Ticket catalog (default config/tickets.yaml).")
    p.add_argument("--project", default=None,
                   help="Project to analyse, when the input holds more than one.")
    p.add_argument("--output-dir", default=None,
                   help="Where to write <campaign-id>/report.json.")
    p.add_argument("--no-llm", action="store_true",
                   help="Force the rule-based path; make no model calls.")
    p.add_argument("--use-priors", action="store_true",
                   help="Ground analysis in findings from previous campaigns.")
    p.add_argument("--no-field", action="store_true",
                   help="Ignore any stored field data; analyse lab metrics only.")
    p.add_argument("--top-k", type=int, default=None,
                   help="Playbook chunks to retrieve per page.")
    p.add_argument("--no-budget", action="store_true",
                   help="Spend freely: make no daily token or request checks.")
    p.add_argument("--budget-status", action="store_true",
                   help="Print today's spend against the budget and exit, "
                        "making no API calls.")
    p.add_argument("--daily-requests", type=int, default=None,
                   help="Override budget.llm.daily_requests for this run.")
    p.add_argument("--daily-input-tokens", type=int, default=None,
                   help="Override budget.llm.daily_input_tokens for this run.")
    p.add_argument("--daily-output-tokens", type=int, default=None,
                   help="Override budget.llm.daily_output_tokens for this run.")
    p.add_argument("--max-output-tokens", type=int, default=None,
                   help="Override budget.llm.max_output_tokens_per_call.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # Load .env (gitignored) before anything resolves the API key. Without
    # this the key is only visible when the caller has exported it, so a
    # correctly-configured project would silently analyse every campaign
    # rule-based and report a missing key. `ingest/automated.py` does the same.
    try:
        from dotenv import load_dotenv

        load_dotenv(override=False)
    except ImportError:  # pragma: no cover - python-dotenv is a pinned dependency
        pass

    args = _build_parser().parse_args(argv)

    if args.input_dir and args.from_store:
        print("--input-dir and --from-store are mutually exclusive.", file=sys.stderr)
        return 2

    settings = load_settings()
    budget = _budget_from_args(args, settings)

    if args.budget_status:
        # Answerable from the ledger alone: no key, no network, no run needed.
        print("budget: disabled" if budget is None else budget.summary_line())
        return 0

    output_dir = Path(args.output_dir or settings.report.output_dir)
    pages = args.pages.split(",") if args.pages else None

    try:
        runs = load_runs(
            input_dir=(
                args.input_dir
                or (None if args.from_store else "data/processed")
            ),
            from_store=args.from_store,
            pages=pages,
            project=args.project,
        )
    except FileNotFoundError as exc:
        print(f"No runs to analyse: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        captures, outcomes = load_har_evidence(args.har, args.tickets)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Could not use the HAR captures: {exc}", file=sys.stderr)
        return 1

    store = embed_client = llm_client = None
    if not args.no_llm:
        store, embed_client, llm_client = _build_live_clients(settings, budget)

    collected: List[PageAnalysis] = []
    report = run_analysis(
        runs, store=store, embed_client=embed_client, llm_client=llm_client,
        settings=settings, use_priors=args.use_priors, top_k=args.top_k,
        page_analyses_out=collected, llm_disabled=args.no_llm,
        no_field=args.no_field, har_captures=captures, tickets=outcomes,
    )

    target = output_dir / report.cover.campaign_id
    try:
        target.mkdir(parents=True, exist_ok=True)
        destination = target / "report.json"
        destination.write_text(to_json(report), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write the report: {exc}", file=sys.stderr)
        return 1

    if (
        store is not None
        and embed_client is not None
        and report.meta.analysis_mode == "llm"
    ):
        try:
            persist_findings(store, embed_client, report, collected)
        except Exception as exc:  # persistence must never lose the report
            print(f"Findings were not persisted: {exc}", file=sys.stderr)

    print(destination)
    print(
        f"{len(report.pages)} page(s), verdict={report.cover.verdict}, "
        f"mode={report.meta.analysis_mode}"
    )
    if budget is not None and llm_client is not None:
        print(budget.summary_line(), file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
