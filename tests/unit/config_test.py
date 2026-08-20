"""Unit tests for config/load.py (settings/devices/networks/targets loaders)."""
from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from config import load as cl

VALID_SETTINGS = {
    "run_defaults": {
        "runs": 3,
        "device": "mid-mobile",
        "network": "slow-4g",
        "mobile_device": "mid-mobile",
        "mobile_network": "slow-4g",
        "desktop_device": "desktop",
        "desktop_network": "fast-3g",
    },
    "thresholds": {
        "lcp_good_ms": 2500,
        "lcp_fail_ms": 4000,
        "cls_good": 0.1,
        "cls_fail": 0.25,
        "inp_good_ms": 200,
        "inp_fail_ms": 500,
        "fcp_good_ms": 1800,
        "ttfb_good_ms": 800,
    },
}

VALID_DEVICES = {
    "devices": [
        {
            "name": "mid-mobile",
            "viewport_width": 393,
            "viewport_height": 851,
            "device_scale_factor": 2.75,
            "mobile": True,
            "cpu_throttle": 4,
        },
        {
            "name": "desktop",
            "viewport_width": 1350,
            "viewport_height": 940,
            "mobile": False,
            "cpu_throttle": 1,
        },
    ]
}

VALID_NETWORKS = {
    "networks": [
        {"name": "online", "latency_ms": 0, "downlink_mbps": None, "uplink_mbps": None},
        {"name": "fast-3g", "latency_ms": 150, "downlink_mbps": 1.6, "uplink_mbps": 0.75},
        {"name": "slow-4g", "latency_ms": 170, "downlink_mbps": 4.0, "uplink_mbps": 3.0},
    ]
}

VALID_TARGETS = {
    "project": "storefront",
    "pages": [
        {
            "name": "homepage",
            "url": "https://example.com/",
            "tests": [
                {"device": "mid-mobile", "network": "slow-4g", "runs": 3},
                {"device": "desktop", "network": "fast-3g", "runs": 3},
            ],
        }
    ],
}


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def files(tmp_path):
    """Write four valid config files into a temp dir."""
    return {
        "settings": write(tmp_path / "settings.yaml", VALID_SETTINGS),
        "devices": write(tmp_path / "devices.yaml", VALID_DEVICES),
        "networks": write(tmp_path / "networks.yaml", VALID_NETWORKS),
        "targets": write(tmp_path / "targets.yaml", VALID_TARGETS),
    }


# ---- happy paths ---------------------------------------------------------- #
def test_load_real_settings_file():
    """The shipped config/settings.yaml parses."""
    s = cl.load_settings()
    assert s.run_defaults.runs == 3
    assert s.run_defaults.mobile_device == "mid-mobile"
    assert s.thresholds.lcp_good_ms == 2500


def test_trends_settings_come_from_the_shipped_file():
    trends = cl.load_settings().trends
    assert trends.dead_band_pct == 5.0
    assert trends.window == 5


def test_trends_defaults_apply_when_the_block_is_absent(tmp_path):
    path = write(tmp_path / "settings.yaml", {"run_defaults": {"runs": 3}})
    trends = cl.load_settings(path).trends
    assert trends.dead_band_pct == 5.0
    assert trends.window == 5


def test_a_negative_dead_band_is_rejected(tmp_path):
    path = write(tmp_path / "settings.yaml", {"trends": {"dead_band_pct": -1}})
    with pytest.raises(cl.ConfigError):
        cl.load_settings(path)


def test_a_window_of_one_is_rejected(tmp_path):
    # A window of 1 could never produce a direction; every series would render
    # as "new", which reads as missing data rather than as a misconfiguration.
    path = write(tmp_path / "settings.yaml", {"trends": {"window": 1}})
    with pytest.raises(cl.ConfigError):
        cl.load_settings(path)


def test_load_real_devices_file_has_mid_mobile_and_desktop():
    d = cl.load_devices()
    names = {dev.name for dev in d.devices}
    assert {"mid-mobile", "desktop", "high-mobile", "low-mobile"} <= names


def test_load_real_networks_file_has_devtools_tiers():
    n = cl.load_networks()
    names = {net.name for net in n.networks}
    assert {"online", "fast-3g", "slow-4g", "slow-3g", "offline"} <= names


def test_load_config_full_resolves(files):
    cfg = cl.load_config(**files)
    assert cfg.project == "storefront"
    assert set(cfg.devices.keys()) >= {"mid-mobile", "desktop"}
    assert "slow-4g" in cfg.networks
    assert len(cfg.pages) == 1
    assert [t.device for t in cfg.pages[0].tests] == ["mid-mobile", "desktop"]


def test_omitted_tests_get_defaults(files):
    files["targets"] = write(
        files["targets"], {"project": "s", "pages": [{"name": "home", "url": "https://x.com/"}]}
    )
    cfg = cl.load_config(**files)
    page = cfg.pages[0]
    assert len(page.tests) == 2
    assert (page.tests[0].device, page.tests[0].network) == ("mid-mobile", "slow-4g")
    assert (page.tests[1].device, page.tests[1].network) == ("desktop", "fast-3g")


# ---- error paths ---------------------------------------------------------- #
def test_missing_file_raises(files):
    with pytest.raises(cl.ConfigError, match="not found"):
        cl.load_yaml(files["settings"].with_name("nope.yaml"))


def test_malformed_yaml_raises(files):
    bad = files["settings"]
    bad.write_text("{{ not valid yaml :::", encoding="utf-8")
    with pytest.raises(cl.ConfigError, match="[Mm]alformed"):
        cl.load_settings(bad)


def test_empty_pages_list_raises(files):
    files["targets"] = write(files["targets"], {"project": "s", "pages": []})
    with pytest.raises(cl.ConfigError, match="at least one page"):
        cl.load_config(**files)


def test_unknown_device_raises(files):
    files["targets"] = write(
        files["targets"],
        {
            "project": "s",
            "pages": [
                {
                    "name": "home",
                    "url": "https://x.com/",
                    "tests": [{"device": "nope-device", "network": "online"}],
                }
            ],
        },
    )
    with pytest.raises(cl.ConfigError, match="Unknown device"):
        cl.load_config(**files)


def test_unknown_network_raises(files):
    files["targets"] = write(
        files["targets"],
        {
            "project": "s",
            "pages": [
                {
                    "name": "home",
                    "url": "https://x.com/",
                    "tests": [{"device": "mid-mobile", "network": "nope-net"}],
                }
            ],
        },
    )
    with pytest.raises(cl.ConfigError, match="Unknown network"):
        cl.load_config(**files)


def test_appendix_settings_have_working_defaults():
    settings = cl.Settings()
    assert settings.report.appendix.top_requests == 15
    assert settings.report.appendix.screenshot_width_px == 720
    assert settings.report.appendix.screenshot_max_height_px == 1600


def test_a_zero_top_requests_is_rejected_at_load_time():
    with pytest.raises(ValidationError):
        cl.AppendixConfig(top_requests=0)


def test_the_shipped_settings_files_appendix_keys_match_the_model():
    # A value comparison here would be a tautology: the shipped YAML's
    # appendix values are identical to AppendixConfig's own defaults, so a
    # load_settings() bug that silently fell back to defaults (block renamed,
    # field typo'd, block deleted) would produce the exact same numbers as a
    # correct load and the test would prove nothing either way.
    #
    # Compare key *names* instead. pydantic's `extra="ignore"` means a
    # renamed block (`appendix:` -> `appendixx:`) or a typo'd field
    # (`top_requests:` -> `top_request:`) is swallowed silently — the model
    # just falls back to defaults with no error. Reading the raw YAML
    # ourselves and checking its key set against AppendixConfig's declared
    # fields catches exactly that failure mode, independent of what any
    # value is.
    #
    # Equality, not a subset check, in either direction: every field on
    # AppendixConfig documents a deliberate choice for the shipped report
    # (see the comments in settings.yaml), so the file is expected to name
    # all of them and none besides. A subset check would miss a field
    # silently dropped from the file (it'd still pass as "subset"), and a
    # superset check would miss an extra/misspelled key sitting alongside
    # the real ones. Equality catches both.
    #
    # Blind spot: this only proves the file's appendix block names the right
    # keys, not that load_settings() correctly threads their values into the
    # model. It also means any future optional AppendixConfig field must be
    # spelled out in the shipped file even if its default would do — a
    # deliberate tradeoff given this file already documents every field by
    # hand.
    with open(cl.SETTINGS_FILE, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw_appendix_keys = set((raw.get("report") or {}).get("appendix") or {})
    assert raw_appendix_keys == set(cl.AppendixConfig.model_fields.keys())


def test_duplicate_page_names_raise(files):
    files["targets"] = write(
        files["targets"],
        {
            "project": "s",
            "pages": [
                {"name": "home", "url": "https://a.com/"},
                {"name": "home", "url": "https://b.com/"},
            ],
        },
    )
    with pytest.raises(cl.ConfigError, match="unique"):
        cl.load_config(**files)


def test_ci_targets_file_resolves_to_two_single_run_conditions():
    """The campaign CI runs must load and cross-validate like any other.

    Pinned deliberately: a device or network renamed in the presets would
    otherwise break CI at browser-launch time on a pull request, rather than
    in the offline suite that is meant to catch it.
    """
    from config.load import CONFIG_DIR, load_config

    cfg = load_config(targets=CONFIG_DIR / "ci-targets.yaml")

    assert cfg.project == "ci-smoke"
    assert [p.name for p in cfg.pages] == ["homepage"]
    page = cfg.pages[0]
    assert page.url.startswith("https://")
    assert [(t.device, t.network, t.runs) for t in page.tests] == [
        ("mid-mobile", "slow-4g", 1),
        ("desktop", "fast-3g", 1),
    ]
    # No bot-allowlist header: CI has no such secret and needs none.
    assert not cfg.headers


def test_ci_target_url_passes_the_ssrf_gate():
    """The CI campaign is subject to the same gate as any other target."""
    from config.load import CONFIG_DIR, load_config
    from normalize.url_safety import validate_url

    cfg = load_config(targets=CONFIG_DIR / "ci-targets.yaml")
    for page in cfg.pages:
        assert validate_url(page.url, resolve=False) == page.url


def test_budget_defaults_fit_one_report():
    """The shipped budget is sized so a day's first report always completes."""
    budget = cl.load_settings().budget

    assert budget.enabled is True
    assert budget.llm.daily_requests == 60
    assert budget.llm.daily_input_tokens == 250000
    assert budget.llm.daily_output_tokens == 60000
    assert budget.llm.max_output_tokens_per_call == 8192
    assert budget.embeddings.daily_requests == 100
    assert budget.embeddings.daily_input_tokens == 100000


def test_budget_defaults_apply_when_the_block_is_absent(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("rag:\n  top_k: 2\n", encoding="utf-8")

    assert cl.load_settings(path).budget.llm.daily_requests == 60


def test_budget_rejects_negative_limits():
    with pytest.raises(ValidationError):
        cl.ServiceBudget(daily_requests=-1)


def test_budget_zero_is_allowed_and_means_no_budget():
    """Zero is a configuration, not a mistake: spend nothing on this service."""
    assert cl.ServiceBudget(daily_requests=0).daily_requests == 0
