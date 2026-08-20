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
from store.vectordb import Document

MAX_TOP_ACTIONS = 3


@dataclass
class SimpleSummary:
    """The rule-based stand-in for ``LlmSummary``."""

    problem: str
    key_finding: str
    top_actions: List[str]


def load_runs(
    *,
    input_dir: Optional[Any] = None,
    from_store: Optional[Any] = None,
    pages: Optional[Sequence[str]] = None,
) -> List[Run]:
    """Load runs from a directory of normalized JSON, or from SQLite."""
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

    if pages:
        wanted = {p.strip() for p in pages if p.strip()}
        runs = [r for r in runs if r.page.name in wanted]

    if not runs:
        where = from_store if from_store is not None else input_dir
        raise FileNotFoundError(f"No runs found in {where}")
    return sorted(runs, key=lambda r: r.run_id)


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
    """
    settings = settings or load_settings()
    k = top_k or settings.rag.top_k
    chunks = knowledge.load_knowledge_dir(knowledge_dir)
    digest = knowledge.content_digest(chunks)

    analyses: List[PageAnalysis] = []
    for _page_name, page_runs in group_by_page(runs).items():
        primary = select_primary(page_runs)
        symptoms = retrieve.detect_symptoms(primary, settings.thresholds)

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
        ))

    summary: Any = rule_based_summary(analyses)
    if llm_client is not None and analyses and all(p.mode == "llm" for p in analyses):
        from analysis.llm import AnalysisError
        from rag.embeddings import EmbeddingError

        try:
            summary = _top_up_actions(
                llm_client.summarize(_summary_payload(analyses)), analyses
            )
        except (AnalysisError, EmbeddingError):
            summary = rule_based_summary(analyses)

    if page_analyses_out is not None:
        page_analyses_out.extend(analyses)

    project = runs[0].project.name if runs else "report"
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
        model=model, knowledge_digest=digest, trends=series,
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


def _build_live_clients(settings) -> tuple:
    """Build the real store and clients, or fall back to the rule-based path.

    A missing key is not an error here: it means this campaign is analysed by
    rules, which is a supported outcome.
    """
    from rag.embeddings import EmbeddingError, GoogleEmbeddingClient, resolve_api_key
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
    embed_client = GoogleEmbeddingClient(model=settings.models.embeddings)
    llm_client = GoogleAnalysisClient(model=settings.models.llm)
    return store, embed_client, llm_client


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
    p.add_argument("--output-dir", default=None,
                   help="Where to write <campaign-id>/report.json.")
    p.add_argument("--no-llm", action="store_true",
                   help="Force the rule-based path; make no model calls.")
    p.add_argument("--use-priors", action="store_true",
                   help="Ground analysis in findings from previous campaigns.")
    p.add_argument("--top-k", type=int, default=None,
                   help="Playbook chunks to retrieve per page.")
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
        )
    except FileNotFoundError as exc:
        print(f"No runs to analyse: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    store = embed_client = llm_client = None
    if not args.no_llm:
        store, embed_client, llm_client = _build_live_clients(settings)

    collected: List[PageAnalysis] = []
    report = run_analysis(
        runs, store=store, embed_client=embed_client, llm_client=llm_client,
        settings=settings, use_priors=args.use_priors, top_k=args.top_k,
        page_analyses_out=collected, llm_disabled=args.no_llm,
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
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
