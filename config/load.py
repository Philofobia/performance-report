"""Pydantic models + YAML loaders for config/settings|devices|networks|targets.yaml.

Raises :class:`ConfigError` with clean messages on:
  * missing/malformed config files,
  * empty page lists,
  * test conditions referencing unknown device/network names.

The default "one mobile + one desktop per page" conditions (settings.yaml) are
filled in for any page whose ``tests`` are omitted/empty (PROJECT_SPEC §4.4).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, ValidationError

CONFIG_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
DEVICES_FILE = CONFIG_DIR / "devices.yaml"
NETWORKS_FILE = CONFIG_DIR / "networks.yaml"
TARGETS_FILE = CONFIG_DIR / "targets.yaml"


class ConfigError(Exception):
    """Clean, user-facing error for any config loading/validation failure."""


# --------------------------------------------------------------------------- #
# optional custom request headers
# --------------------------------------------------------------------------- #
# `${VAR}` references, resolved from the environment at use time.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# RFC 7230 header field-name token characters.
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def resolve_headers(
    raw: Optional[Mapping[str, Any]],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Resolve a header mapping, expanding ``${VAR}`` references from ``env``.

    Header *names* are committed in config (they document which sites need
    which header); header *values* reference environment variables so secrets
    such as a bot-allowlist token never enter git.

    Returns ``{}`` for ``None``/empty input — the common case, since headers are
    opt-in. Raises :class:`ConfigError`, naming the header and the missing
    variable but **never** a resolved value, on:

    * a referenced variable that is unset or empty (an exported-but-blank token
      would silently disable the allowlist instead of failing),
    * a header name that is not a valid RFC 7230 token,
    * CR/LF anywhere in a resolved value (header injection — a token carrying
      newlines could smuggle an extra header onto every request).
    """
    if not raw:
        return {}
    environ = os.environ if env is None else env

    resolved: Dict[str, str] = {}
    for name, value in raw.items():
        name = "" if name is None else str(name)
        if not _HEADER_NAME.match(name.strip()) or name != name.strip():
            raise ConfigError(
                f"Invalid HTTP header name in config: {name!r}. "
                "Names must be a single RFC 7230 token (no spaces or separators)."
            )
        if not isinstance(value, str):
            raise ConfigError(
                f"Header {name!r} must be a string, got {type(value).__name__}. "
                "Use a literal or a ${ENV_VAR} reference."
            )

        def _expand(match: re.Match) -> str:
            var = match.group(1)
            got = environ.get(var)
            if not got:
                raise ConfigError(
                    f"Header {name!r} references environment variable "
                    f"${{{var}}}, which is unset or empty. Set it in .env "
                    "(gitignored) or the environment, or remove the header."
                )
            return got

        expanded = _ENV_REF.sub(_expand, value)
        if "\r" in expanded or "\n" in expanded:
            # Do not echo the value — it is a secret.
            raise ConfigError(
                f"Resolved value for header {name!r} contains CR/LF, which "
                "would allow header injection. Check the source variable."
            )
        resolved[name] = expanded
    return resolved


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
class Device(BaseModel):
    name: str = Field(min_length=1)
    label: str = Field(default="")
    viewport_width: int = Field(ge=1)
    viewport_height: int = Field(ge=1)
    device_scale_factor: float = Field(default=1.0, gt=0)
    mobile: bool = False
    cpu_throttle: float = Field(default=1.0, ge=1)
    user_agent: Optional[str] = None
    playwright_device: Optional[str] = None


class Network(BaseModel):
    name: str = Field(min_length=1)
    label: str = Field(default="")
    latency_ms: float = Field(default=0, ge=0)
    downlink_mbps: Optional[float] = Field(default=None, ge=0)
    uplink_mbps: Optional[float] = Field(default=None, ge=0)
    offline: bool = False


class PageTest(BaseModel):
    device: str = Field(min_length=1)
    network: str = Field(min_length=1)
    runs: int = Field(default=3, ge=1)


class PageTarget(BaseModel):
    name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    tests: List[PageTest] = Field(default_factory=list)
    # None = inherit the project headers; {} = explicitly send none.
    headers: Optional[Dict[str, str]] = None


class TargetsConfig(BaseModel):
    project: str = Field(min_length=1)
    pages: List[PageTarget]
    # Project-wide request headers, applied to every page unless overridden.
    headers: Dict[str, str] = Field(default_factory=dict)

    @field_validator("pages")
    @classmethod
    def _pages_not_empty(cls, v: List[PageTarget]) -> List[PageTarget]:
        if not v:
            raise ValueError("targets.yaml must define at least one page")
        return v

    @field_validator("pages")
    @classmethod
    def _pages_unique(cls, v: List[PageTarget]) -> List[PageTarget]:
        names = [p.name for p in v]
        if len(names) != len(set(names)):
            raise ValueError("page names must be unique")
        return v


class DevicesConfig(BaseModel):
    devices: List[Device]

    @field_validator("devices")
    @classmethod
    def _unique(cls, v: List[Device]) -> List[Device]:
        names = [d.name for d in v]
        if len(names) != len(set(names)):
            raise ValueError("device names must be unique")
        return v


class NetworksConfig(BaseModel):
    networks: List[Network]

    @field_validator("networks")
    @classmethod
    def _unique(cls, v: List[Network]) -> List[Network]:
        names = [n.name for n in v]
        if len(names) != len(set(names)):
            raise ValueError("network names must be unique")
        return v


class RunDefaults(BaseModel):
    runs: int = Field(default=3, ge=1)
    device: str = "mid-mobile"
    network: str = "slow-4g"
    mobile_device: str = "mid-mobile"
    mobile_network: str = "slow-4g"
    desktop_device: str = "desktop"
    desktop_network: str = "fast-3g"


class Thresholds(BaseModel):
    lcp_good_ms: int = 2500
    lcp_fail_ms: int = 4000
    cls_good: float = 0.1
    cls_fail: float = 0.25
    inp_good_ms: int = 200
    inp_fail_ms: int = 500
    fcp_good_ms: int = 1800
    ttfb_good_ms: int = 800


class ModelsConfig(BaseModel):
    embeddings: str = "text-embedding-004"
    llm: str = "gemini-2.0-flash"
    embed_dimensions: int = 768


class RagConfig(BaseModel):
    top_k: int = Field(default=5, ge=1)


class StorageConfig(BaseModel):
    sqlite_path: str = "data/processed/runs.sqlite"
    vector_dir: str = "data/vector"
    raw_dir: str = "data/raw"


class AppendixConfig(BaseModel):
    """The capture appendix (PROJECT_SPEC §10 Phase 7B).

    ``screenshot_max_height_px`` exists because a full-page mobile capture runs
    to tens of thousands of pixels. Scaling one to fit a page renders an
    unreadable smear, so beyond this height the image is cropped from the top
    and the crop is stated in the caption.
    """

    top_requests: int = Field(default=15, ge=1)
    screenshot_width_px: int = Field(default=720, ge=64)
    screenshot_max_height_px: int = Field(default=1600, ge=64)


class ReportConfig(BaseModel):
    output_dir: str = "data/reports"
    appendix: AppendixConfig = Field(default_factory=AppendixConfig)


class TrendsConfig(BaseModel):
    """Campaign-over-campaign comparison (PROJECT_SPEC §10 Phase 7).

    ``dead_band_pct`` exists because emulated throttling has real run-to-run
    variance: without it a 3% wobble reads as a regression every campaign and
    the trend section becomes noise the reader learns to skip.

    ``window`` is at least 2 — a window of 1 could never produce a direction,
    and every series silently rendering as "new" would look like missing data
    rather than a misconfiguration.
    """

    dead_band_pct: float = Field(default=5.0, ge=0)
    window: int = Field(default=5, ge=2)


class TimeoutsConfig(BaseModel):
    """Per-navigation budgets for the browser runner.

    The defaults suit a typical page. A heavy commerce page measured under CPU
    *and* network throttling while HAR and trace recording are active can take
    well over 30s to fire `load` — so these are configuration, not constants.

    ``navigation_ms`` failing is a real failure: the page never loaded, and
    there is nothing to measure. ``network_idle_ms`` is only a settle window
    and its expiry is non-fatal (see ``ingest/browser/runner.py``).
    """

    navigation_ms: int = Field(default=30_000, ge=1_000)
    network_idle_ms: int = Field(default=5_000, ge=0)
    lcp_ms: int = Field(default=3_000, ge=0)
    inp_ms: int = Field(default=1_000, ge=0)


class Settings(BaseModel):
    run_defaults: RunDefaults = Field(default_factory=RunDefaults)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    trends: TrendsConfig = Field(default_factory=TrendsConfig)


# --------------------------------------------------------------------------- #
# loaders
# --------------------------------------------------------------------------- #
def load_yaml(path: Path) -> Dict[str, Any]:
    """Load a config file, raising ConfigError on missing/malformed/mis-shaped."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Malformed YAML in {p}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"Config root must be a mapping in {p}, got {type(data).__name__}"
        )
    return data


def load_settings(path: Path = SETTINGS_FILE) -> Settings:
    try:
        return Settings.model_validate(load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid settings.yaml ({path}): {exc}") from exc


def load_devices(path: Path = DEVICES_FILE) -> DevicesConfig:
    try:
        return DevicesConfig.model_validate(load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid devices.yaml ({path}): {exc}") from exc


def load_networks(path: Path = NETWORKS_FILE) -> NetworksConfig:
    try:
        return NetworksConfig.model_validate(load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid networks.yaml ({path}): {exc}") from exc


def load_targets(path: Path = TARGETS_FILE) -> TargetsConfig:
    try:
        return TargetsConfig.model_validate(load_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Invalid targets.yaml ({path}): {exc}") from exc


# --------------------------------------------------------------------------- #
# combined / resolved config
# --------------------------------------------------------------------------- #
@dataclass
class ProjectConfig:
    """Fully loaded + cross-validated configuration."""

    settings: Settings
    devices: Dict[str, Device]
    networks: Dict[str, Network]
    project: str
    pages: List[PageTarget]  # pages with default conditions already resolved
    headers: Dict[str, str] = field(default_factory=dict)  # unresolved ${VAR} refs

    def headers_for(
        self,
        page: PageTarget,
        *,
        env: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, str]:
        """Resolve the effective request headers for one page.

        Project headers with the page's own merged over them per key; a page
        declaring an explicitly empty ``headers: {}`` sends none. Resolution is
        **lazy** — done here rather than at load time — so a config that
        declares headers can still be loaded and used by campaigns that do not
        need them (e.g. under ``--no-headers``, or with the token unset).
        """
        if page.headers is None:
            merged = dict(self.headers)  # omitted -> inherit
        elif not page.headers:
            merged = {}  # explicit `headers: {}` -> opt out
        else:
            merged = {**self.headers, **page.headers}
        return resolve_headers(merged, env=env)

    def default_tests(self) -> List[PageTest]:
        d = self.settings.run_defaults
        return [
            PageTest(device=d.mobile_device, network=d.mobile_network, runs=d.runs),
            PageTest(device=d.desktop_device, network=d.desktop_network, runs=d.runs),
        ]


def _resolve_tests(page: PageTarget, defaults: List[PageTest]) -> List[PageTest]:
    return list(page.tests) if page.tests else list(defaults)


def load_config(
    settings: Path = SETTINGS_FILE,
    devices: Path = DEVICES_FILE,
    networks: Path = NETWORKS_FILE,
    targets: Path = TARGETS_FILE,
) -> ProjectConfig:
    """Load all four config files and cross-validate the test matrix."""
    settings_obj = load_settings(settings)
    devices_obj = load_devices(devices)
    networks_obj = load_networks(networks)
    targets_obj = load_targets(targets)

    device_map = {d.name: d for d in devices_obj.devices}
    network_map = {n.name: n for n in networks_obj.networks}
    defaults = ProjectConfig(
        settings=settings_obj,
        devices=device_map,
        networks=network_map,
        project=targets_obj.project,
        pages=[],
    ).default_tests()

    unknown_devices: List[str] = []
    unknown_networks: List[str] = []
    for page in targets_obj.pages:
        page.tests = _resolve_tests(page, defaults)
        for t in page.tests:
            if t.device not in device_map:
                unknown_devices.append(f"{page.name}:{t.device}")
            if t.network not in network_map:
                unknown_networks.append(f"{page.name}:{t.network}")

    if unknown_devices:
        raise ConfigError(
            "Unknown device name(s) in targets.yaml: "
            + ", ".join(sorted(set(unknown_devices)))
        )
    if unknown_networks:
        raise ConfigError(
            "Unknown network name(s) in targets.yaml: "
            + ", ".join(sorted(set(unknown_networks)))
        )

    return ProjectConfig(
        settings=settings_obj,
        devices=device_map,
        networks=network_map,
        project=targets_obj.project,
        pages=targets_obj.pages,
        headers=targets_obj.headers,
    )

