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


def test_the_shipped_settings_file_parses_its_appendix_block():
    # The shipped value (20) deliberately differs from AppendixConfig's
    # model default (15, asserted above via cl.Settings()) so this test can
    # actually distinguish "parsed from config/settings.yaml" from "pydantic
    # defaults fired because the block was never read" — a bare `>= 1` check
    # passes either way and proves nothing.
    settings = cl.load_settings()
    assert settings.report.appendix.top_requests == 20


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
