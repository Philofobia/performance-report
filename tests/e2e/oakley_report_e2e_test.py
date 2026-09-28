"""The real thing: a live Oakley campaign, all the way to a PDF, read back.

Every other e2e test proves a part — the runner against a fixture page, the
PDF writer against a hand-built report. This one proves the sequence against
the site the project exists to measure, driven through the same three commands
a person runs::

    python -m cli ingest auto  --pages homepage --runs 1
    python -m cli analyze      --no-llm --no-field
    python -m cli report       --skeleton-check

and then opens the PDF and checks it says what ``report.json`` says. A PDF can
be a valid, paginated, correctly-sized document and still carry the wrong
numbers, a blank appendix, or a bot-protection block page's measurements —
none of which the byte-level checks elsewhere would notice.

Why ``--no-llm`` and ``--no-field``: a test that spends free-tier quota every
time it runs, or fails when Grafana is down, is a test people stop running.
The skeleton is identical in every analysis mode — that is the point of it —
so the rule-based path proves the document; the model path is exercised by
running the pipeline by hand.

Opt-in by construction. It needs Chromium, the public network, and the Akamai
allowlist token in ``.env`` (``AKAMAI_BOT_TOKEN``) — without the token Akamai
answers 403 and there is nothing to measure. CI holds no token, so there it
skips; locally::

    pytest tests/e2e/oakley_report_e2e_test.py -v

Nothing is written to the repo's ``data/``. The storage paths in
``settings.yaml`` are relative to the working directory, so the whole run
happens in a temporary one with a copy of the playbook corpus, and
``--no-store`` keeps the campaign out of the run history.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.e2e

REPO = Path(__file__).resolve().parents[2]
PAGE = "homepage"
PAGE_URL = "https://www.oakley.com/en-us"
CONDITIONS = {("mid-mobile", "slow-4g"), ("desktop", "fast-3g")}

#: Headings the template always renders (report/template/report.html.j2). The
#: skeleton check proves the ``data-section`` structure; these prove the words
#: made it through Chromium's print pipeline into extractable text.
HEADINGS = (
    "What the measurements show",
    "What to do first",
    "What visitors actually experienced",
    "Every page, every condition",
    "How these numbers were produced",
    "What was captured",
)

#: An Akamai block page is a few requests and a few dozen DOM nodes. The real
#: homepage has measured 160-180 requests and thousands of nodes. The floors
#: sit far below the real page and far above the block page, so neither a
#: lighter redesign nor measurement noise trips them.
MIN_REQUESTS = 50
MIN_DOM_NODES = 300


@dataclass
class Campaign:
    workdir: Path
    runs: List[dict] = field(repr=False)
    report: dict = field(repr=False)
    report_dir: Path
    pdf: bytes = field(repr=False)
    pdf_text: str = field(repr=False)
    pdf_pages: int
    pdf_images: int
    skeleton_exit: int
    skeleton_output: str
    #: A credential. Kept out of the repr, because pytest prints the fixture
    #: value in every failure report involving it.
    token: str = field(repr=False)


def _run(argv: List[str]) -> tuple:
    """``python -m cli <argv>`` in-process; returns (exit code, stdout+stderr)."""
    import cli

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


def _pdf_contents(pdf_path: Path) -> tuple:
    """(text, page count, raster image count) of a PDF, via pypdf.

    Images are counted as ``/Image`` XObjects in each page's resources. Charts
    are inline SVG and produce none, so every image found is a screenshot.
    """
    pypdf = pytest.importorskip("pypdf", reason="pypdf not installed (test extra)")

    reader = pypdf.PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    images = set()
    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is None:
            continue
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            continue
        for ref in xobjects.get_object().values():
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Image":
                # The same image drawn on two pages is one XObject; key on the
                # indirect reference so it is counted once.
                images.add(getattr(ref, "idnum", id(obj)))
    return text, len(reader.pages), len(images)


@pytest.fixture(scope="module")
def campaign(tmp_path_factory) -> Campaign:
    from dotenv import dotenv_values

    # Read the repo's .env explicitly. A bare load_dotenv() searches from the
    # *calling* file's directory, and resolving the token must not depend on
    # where pytest was launched from.
    env_file = REPO / ".env"
    token = os.environ.get("AKAMAI_BOT_TOKEN") or (
        dotenv_values(env_file).get("AKAMAI_BOT_TOKEN") if env_file.is_file() else None
    )
    if not token:
        pytest.skip("AKAMAI_BOT_TOKEN is not set — Akamai would answer 403")
    pytest.importorskip("playwright.sync_api", reason="playwright not installed")

    workdir = tmp_path_factory.mktemp("oakley")
    shutil.copytree(REPO / "data" / "knowledge", workdir / "data" / "knowledge")
    processed = workdir / "processed"
    reports = workdir / "reports"

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("AKAMAI_BOT_TOKEN", token)
        mp.chdir(workdir)

        code, out = _run([
            "ingest", "auto", "--pages", PAGE, "--runs", "1", "--no-store",
            "--output-dir", str(processed),
            "--artifacts-root", str(workdir / "scratch"),
        ])
        if code == 3:  # ingest/automated.py: the target did not answer
            pytest.skip(f"oakley.com unreachable:\n{out}")
        assert code == 0, f"campaign failed (exit {code}):\n{out}"

        code, out = _run([
            "analyze", "--input-dir", str(processed), "--output-dir", str(reports),
            "--no-llm", "--no-field",
        ])
        assert code == 0, f"analysis failed (exit {code}):\n{out}"

        skeleton_exit, skeleton_output = _run([
            "report", "--reports-dir", str(reports), "--skeleton-check",
        ])

    runs = [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(processed.glob("*.json"))]
    report_json = next(reports.glob("*/report.json"))
    pdf_path = report_json.with_name("report.pdf")
    assert pdf_path.is_file(), f"no PDF was written:\n{skeleton_output}"
    text, pages, images = _pdf_contents(pdf_path)

    return Campaign(
        workdir=workdir,
        runs=runs,
        report=json.loads(report_json.read_text(encoding="utf-8")),
        report_dir=report_json.parent,
        pdf=pdf_path.read_bytes(),
        pdf_text=text,
        pdf_pages=pages,
        pdf_images=images,
        skeleton_exit=skeleton_exit,
        skeleton_output=skeleton_output,
        token=token,
    )


def _squash(text: str) -> str:
    """Collapse whitespace: PDF text extraction breaks lines wherever it likes."""
    return re.sub(r"\s+", " ", text)


# --------------------------------------------------------------------------- #
# the campaign measured the site, not Akamai's block page
# --------------------------------------------------------------------------- #
def test_one_run_per_configured_condition(campaign):
    assert {(r["condition"]["device"], r["condition"]["network"])
            for r in campaign.runs} == CONDITIONS
    assert all(r["page"]["url"] == PAGE_URL for r in campaign.runs)


def test_measurements_are_of_the_real_page_not_a_block_page(campaign):
    for run in campaign.runs:
        label = f"{run['condition']['device']}/{run['condition']['network']}"
        requests = run["metrics"]["network"]["request_count"]
        nodes = run["metrics"]["main_thread"]["dom_nodes"]
        assert requests and requests >= MIN_REQUESTS, (
            f"{label}: only {requests} requests — looks like a block page")
        assert nodes and nodes >= MIN_DOM_NODES, (
            f"{label}: only {nodes} DOM nodes — looks like a block page")


def test_the_main_document_answered_200(campaign):
    """The appendix lists each capture's requests; the document is among them."""
    for entry in campaign.report["appendix"]:
        document = [r for r in entry["requests"] if r["url"] == PAGE_URL]
        assert document, f"{entry['device']}: main document missing from the HAR"
        assert document[0]["status"] == 200


def test_core_web_vitals_are_present(campaign):
    for run in campaign.runs:
        cwp = run["metrics"]["cwp"]
        for metric in ("lcp_ms", "fcp_ms", "ttfb_ms"):
            assert cwp[metric] and cwp[metric] > 0, f"{metric} missing"
        assert cwp["cls"] is not None
        assert cwp["inp_ms"] is not None


# --------------------------------------------------------------------------- #
# report.json
# --------------------------------------------------------------------------- #
def test_report_describes_this_campaign(campaign):
    report = campaign.report
    assert report["cover"]["project"] == "oakley"
    assert report["cover"]["pages"] == [PAGE]
    assert report["cover"]["verdict"] in {"pass", "warn", "fail"}
    assert report["meta"]["analysis_mode"] == "rule_based"
    assert report["meta"]["field_mode"] == "unavailable"


def test_every_capture_reached_the_appendix(campaign):
    report = campaign.report
    assert len(report["appendix"]) == len(CONDITIONS)
    assert report["meta"]["degraded_appendix_entries"] == 0
    for entry in report["appendix"]:
        assert entry["screenshot"] and entry["har"]
        assert entry["total_requests"] >= MIN_REQUESTS


def test_rules_produced_a_plan(campaign):
    """The playbook corpus was found, so the report has something to say."""
    report = campaign.report
    assert report["meta"]["knowledge_digest"], "no playbooks were loaded"
    assert report["pages"][0]["recommendations"]
    assert report["action_plan"]


# --------------------------------------------------------------------------- #
# the PDF
# --------------------------------------------------------------------------- #
def test_skeleton_matches_the_committed_baseline(campaign):
    assert campaign.skeleton_exit == 0, campaign.skeleton_output
    assert "skeleton ok" in campaign.skeleton_output


def test_pdf_is_a_real_multi_page_document(campaign):
    assert campaign.pdf.startswith(b"%PDF")
    assert campaign.pdf_pages >= 3, f"only {campaign.pdf_pages} page(s)"


def test_pdf_text_carries_every_section(campaign):
    text = _squash(campaign.pdf_text)
    missing = [h for h in HEADINGS if h not in text]
    assert not missing, f"headings not in the PDF text: {missing}"


def test_pdf_names_the_campaign_and_the_page(campaign):
    text = _squash(campaign.pdf_text)
    assert campaign.report["cover"]["campaign_id"] in text
    assert PAGE_URL in text
    assert campaign.report["cover"]["verdict"].upper() in text.upper()


@pytest.mark.xfail(
    os.name == "nt",
    strict=True,
    reason=(
        "Known defect: on Windows the report's faces (Corbel, Constantia) have "
        "old-style default figures, and body's `font-variant-numeric: "
        "tabular-nums lining-nums` swaps each digit for an alternate glyph with "
        "no cmap entry. Chromium's PDF writer then has no Unicode for it, so "
        "digits extract as U+0000 — visible, but not searchable, copyable or "
        "readable by a screen reader. Linux CI falls back to fonts without the "
        "alternates and never sees it. Remove this marker with the fix."
    ),
)
def test_pdf_numbers_match_report_json(campaign):
    """The comparison table prints each condition's LCP rounded to the ms.

    The same number must appear in the PDF — the check that the document a
    person reads says what the data says, not merely that it rendered.
    """
    text = _squash(campaign.pdf_text)
    for row in campaign.report["comparison"]:
        lcp = str(int(round(row["lcp_ms"])))
        assert re.search(rf"\b{lcp}\s?ms\b", text), (
            f"LCP {lcp} ms for {row['device']}/{row['network']} not in the PDF")


def test_pdf_embeds_a_screenshot_per_capture(campaign):
    assert campaign.pdf_images >= len(campaign.report["appendix"]), (
        f"{campaign.pdf_images} image(s) in the PDF for "
        f"{len(campaign.report['appendix'])} captures")


def test_the_bot_token_leaks_nowhere(campaign):
    """The allowlist token is a credential; the PDF gets emailed.

    Checked in every artifact the pipeline wrote, not only the PDF: the HAR
    is scrubbed on its way into the store, and this is the proof it was.
    """
    token = campaign.token.encode()
    assert token not in campaign.pdf
    leaked = [
        str(path.relative_to(campaign.workdir))
        for path in campaign.workdir.rglob("*")
        if path.is_file() and path.suffix in {".json", ".har", ".html", ".md"}
        and token in path.read_bytes()
    ]
    assert not leaked, f"AKAMAI_BOT_TOKEN found in: {leaked}"
