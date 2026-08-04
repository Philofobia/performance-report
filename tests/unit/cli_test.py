"""Unit tests for cli.py — the dispatching façade.

The façade's entire job is forwarding argv unchanged, so forwarding is what
these tests pin. A test asserting only "exit code 0" would pass happily while
the façade dropped every flag it was given.
"""
from __future__ import annotations

import pytest

import cli


@pytest.fixture()
def spy(monkeypatch):
    """Replace every delegate with a recorder returning a known exit code."""
    calls = {}

    def record(name):
        def delegate(argv):
            calls[name] = list(argv)
            return 7
        return delegate

    monkeypatch.setattr(
        cli, "_DELEGATES",
        {name: (lambda n=name: record(n)) for name in cli.COMMANDS},
    )
    return calls


# --- routing ----------------------------------------------------------------


@pytest.mark.parametrize("command", ["analyze", "report", "list-runs"])
def test_each_command_reaches_its_own_delegate(command, spy):
    cli.main([command])
    assert list(spy) == [command]


def test_ingest_modes_route_to_different_delegates(spy):
    cli.main(["ingest", "auto"])
    assert list(spy) == ["ingest auto"]
    spy.clear()
    cli.main(["ingest", "manual"])
    assert list(spy) == ["ingest manual"]


# --- forwarding -------------------------------------------------------------


def test_flags_reach_the_delegate_verbatim(spy):
    cli.main(["analyze", "--no-llm", "--pages", "homepage,plp", "--top-k", "5"])
    assert spy["analyze"] == ["--no-llm", "--pages", "homepage,plp", "--top-k", "5"]


def test_a_command_with_no_flags_forwards_an_empty_list(spy):
    cli.main(["report"])
    assert spy["report"] == []


def test_the_ingest_mode_token_is_consumed_not_forwarded(spy):
    cli.main(["ingest", "auto", "--dry-run"])
    assert spy["ingest auto"] == ["--dry-run"]


def test_help_is_forwarded_to_the_delegates_own_parser(spy):
    # Per-command help must be the real parser's help, never a copy that can
    # drift from it.
    cli.main(["report", "--help"])
    assert spy["report"] == ["--help"]


def test_a_flag_the_facade_does_not_know_is_still_forwarded(spy):
    # The point of verbatim forwarding: a stage can grow an option without
    # cli.py changing.
    cli.main(["analyze", "--some-future-flag", "x"])
    assert spy["analyze"] == ["--some-future-flag", "x"]


def test_flags_that_look_like_facade_options_are_not_intercepted(spy):
    cli.main(["report", "--help-me-out"])
    assert spy["report"] == ["--help-me-out"]


# --- exit codes -------------------------------------------------------------


def test_the_delegates_exit_code_is_returned_unchanged(spy):
    assert cli.main(["analyze"]) == 7


def test_an_unknown_command_exits_two(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "frobnicate" in capsys.readouterr().err


def test_ingest_without_a_mode_names_both_modes(capsys):
    assert cli.main(["ingest"]) == 2
    err = capsys.readouterr().err
    assert "auto" in err
    assert "manual" in err


def test_an_unknown_ingest_mode_exits_two(capsys):
    assert cli.main(["ingest", "sideways"]) == 2
    assert "sideways" in capsys.readouterr().err


# --- help -------------------------------------------------------------------


def test_no_arguments_prints_the_command_table(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for command in cli.COMMANDS:
        assert command in out


def test_help_lists_every_command_with_a_description(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for command, description in cli.COMMANDS.items():
        assert command in out
        assert description in out


# --- wiring -----------------------------------------------------------------


def test_every_command_has_a_real_delegate():
    # Guards the two tables drifting apart, which the spy fixture would hide.
    assert set(cli.COMMANDS) == set(cli._DELEGATES)


def test_delegates_are_imported_lazily():
    # `list-runs` must not pay for Playwright or matplotlib.
    import inspect
    for loader in cli._DELEGATES.values():
        assert "import" in inspect.getsource(loader)
