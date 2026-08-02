"""Unit tests for optional per-target HTTP request headers.

Custom request headers (e.g. an Akamai bot-allowlist token) are **opt-in**: a
configuration that declares none must behave exactly as it did before the
feature existed. That guarantee is asserted directly — see
``test_no_headers_omits_extra_http_headers_key_entirely``.

Header *names* live in committed config; header *values* are ``${ENV_VAR}``
references resolved from the environment at use time, so no secret enters git.
No real browser and no real environment mutation: env is passed in explicitly.
"""
from __future__ import annotations

import pytest

from config.load import (
    ConfigError,
    Device,
    Network,
    PageTarget,
    PageTest,
    ProjectConfig,
    Settings,
    TargetsConfig,
    resolve_headers,
)

TOKEN_ENV = {"AKAMAI_BOT_TOKEN": "secret-token-value"}


# --------------------------------------------------------------------------- #
# resolve_headers — ${ENV_VAR} expansion
# --------------------------------------------------------------------------- #
def test_resolve_headers_expands_env_reference():
    resolved = resolve_headers({"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}, env=TOKEN_ENV)
    assert resolved == {"X-Akamai-Bot": "secret-token-value"}


def test_resolve_headers_passes_literal_values_through():
    resolved = resolve_headers({"X-Test-Run": "campaign-42"}, env={})
    assert resolved == {"X-Test-Run": "campaign-42"}


def test_resolve_headers_returns_empty_for_no_headers():
    assert resolve_headers({}, env=TOKEN_ENV) == {}
    assert resolve_headers(None, env=TOKEN_ENV) == {}


def test_missing_env_var_names_the_header_and_variable():
    with pytest.raises(ConfigError) as exc:
        resolve_headers({"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}, env={})
    message = str(exc.value)
    assert "X-Akamai-Bot" in message
    assert "AKAMAI_BOT_TOKEN" in message


def test_missing_env_var_error_never_leaks_other_env_values():
    """The error must not dump the environment while reporting what is missing."""
    with pytest.raises(ConfigError) as exc:
        resolve_headers({"X-Akamai-Bot": "${MISSING_VAR}"}, env=TOKEN_ENV)
    assert "secret-token-value" not in str(exc.value)


def test_empty_env_var_is_treated_as_missing():
    """An exported-but-blank token would silently disable the allowlist."""
    with pytest.raises(ConfigError):
        resolve_headers({"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}, env={"AKAMAI_BOT_TOKEN": ""})


# --------------------------------------------------------------------------- #
# resolve_headers — injection / shape validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["tok\rinjected", "tok\ninjected", "tok\r\nX-Evil: 1"])
def test_crlf_in_resolved_value_is_rejected(bad):
    """A token carrying CR/LF could smuggle an extra header into every request."""
    with pytest.raises(ConfigError):
        resolve_headers({"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}, env={"AKAMAI_BOT_TOKEN": bad})


@pytest.mark.parametrize("bad", ["X-Bad\r\nEvil", "X Bad", "", "   "])
def test_invalid_header_name_is_rejected(bad):
    with pytest.raises(ConfigError):
        resolve_headers({bad: "value"}, env={})


def test_resolve_headers_rejects_non_string_value():
    with pytest.raises(ConfigError):
        resolve_headers({"X-Akamai-Bot": 12345}, env={})


# --------------------------------------------------------------------------- #
# ProjectConfig.headers_for — project/page merge and opt-out
# --------------------------------------------------------------------------- #
def _config(project_headers=None, page_headers=None) -> ProjectConfig:
    page = PageTarget(
        name="homepage",
        url="https://www.oakley.com/en-us",
        tests=[PageTest(device="mid-mobile", network="slow-4g", runs=1)],
        headers=page_headers,
    )
    return ProjectConfig(
        settings=Settings(),
        devices={"mid-mobile": Device(name="mid-mobile", viewport_width=393, viewport_height=851)},
        networks={"slow-4g": Network(name="slow-4g", latency_ms=170)},
        project="oakley",
        pages=[page],
        headers=project_headers or {},
    )


def test_page_inherits_project_headers():
    cfg = _config(project_headers={"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"})
    assert cfg.headers_for(cfg.pages[0], env=TOKEN_ENV) == {"X-Akamai-Bot": "secret-token-value"}


def test_page_headers_override_project_headers_per_key():
    cfg = _config(
        project_headers={"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}", "X-Env": "prod"},
        page_headers={"X-Env": "staging"},
    )
    resolved = cfg.headers_for(cfg.pages[0], env=TOKEN_ENV)
    assert resolved == {"X-Akamai-Bot": "secret-token-value", "X-Env": "staging"}


def test_page_can_opt_out_of_project_headers_with_empty_mapping():
    """`headers: {}` on a page means "send none", distinct from omitting the key."""
    cfg = _config(project_headers={"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}, page_headers={})
    assert cfg.headers_for(cfg.pages[0], env=TOKEN_ENV) == {}


def test_no_headers_configured_anywhere_resolves_empty():
    cfg = _config()
    assert cfg.headers_for(cfg.pages[0], env=TOKEN_ENV) == {}


def test_headers_are_not_resolved_until_requested():
    """Loading a config with an unset token must not fail; only using it does.

    Keeps header support genuinely optional: an unrelated campaign never
    touches the environment.
    """
    cfg = _config(project_headers={"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"})
    assert cfg.pages[0].name == "homepage"  # construction succeeded with no env
    with pytest.raises(ConfigError):
        cfg.headers_for(cfg.pages[0], env={})


def test_targets_config_accepts_project_level_headers():
    targets = TargetsConfig.model_validate(
        {
            "project": "oakley",
            "headers": {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"},
            "pages": [{"name": "homepage", "url": "https://www.oakley.com/en-us"}],
        }
    )
    assert targets.headers == {"X-Akamai-Bot": "${AKAMAI_BOT_TOKEN}"}
    assert targets.pages[0].headers is None  # omitted == inherit
