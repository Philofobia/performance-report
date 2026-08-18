"""``python -m webui`` — serve the manual-entry form on loopback.

The binding rule is a refusal, not a warning. An unauthenticated endpoint that
writes files to disk, reachable from the network, is precisely the shape
SECURITY_PLAN.md exists to prevent, and a warning is a control that only works
on the people who were not going to make the mistake. Because there is no
remote reachability, there is nothing to authenticate — which is what makes
"no login" a decision here rather than an omission.

``wsgiref.simple_server`` is single-threaded and explicitly not a production
server. That is an accurate description of what this is.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, List, Optional

from webui.app import Application

#: Everything that resolves to this machine and nothing else.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_OUTPUT_DIR = "data/processed"


def _make_server(host: str, port: int, app: Application):
    from wsgiref.simple_server import make_server
    return make_server(host, port, app)


def _presets() -> tuple:
    """Device and network names, so the form offers what the runner accepts.

    A manual run recorded under an invented condition could never be compared
    against a measured one; it would sit in a bucket of its own forever.

    The defaults come from ``settings.run_defaults`` rather than from the
    first entry in each preset file: `networks.yaml` happens to list `online`
    first, and a form that started there would quietly file runs under a
    condition the operator never picked.
    """
    from config.load import load_devices, load_networks, load_settings
    devices = [d.name for d in load_devices().devices]
    networks = [n.name for n in load_networks().networks]
    run_defaults = load_settings().run_defaults
    defaults = {"device": run_defaults.device, "network": run_defaults.network}
    return devices, networks, defaults


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m webui",
        description="Serve the local manual-entry form (loopback only).",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"Loopback address to bind. One of: "
                        f"{', '.join(sorted(LOOPBACK_HOSTS))}.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"Port to listen on (default {DEFAULT_PORT}).")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                   help=f"Where run JSON is written (default {DEFAULT_OUTPUT_DIR}).")
    return p


def main(argv: Optional[List[str]] = None, *,
         server_factory: Optional[Callable] = None) -> int:
    """CLI entry point; returns a process exit code (0 = success)."""
    args = _build_parser().parse_args(argv)

    if args.host not in LOOPBACK_HOSTS:
        print(
            f"error: refusing to bind {args.host!r}. This form has no "
            f"authentication and writes files; it serves loopback only "
            f"({', '.join(sorted(LOOPBACK_HOSTS))}).",
            file=sys.stderr,
        )
        return 2

    try:
        devices, networks, defaults = _presets()
    except Exception as exc:  # ConfigError and friends
        print(f"error: could not read the device/network presets: {exc}",
              file=sys.stderr)
        return 2

    app = Application(output_dir=Path(args.output_dir), devices=devices,
                      networks=networks, defaults=defaults)
    server = (server_factory or _make_server)(args.host, args.port, app)

    print(f"Manual entry form: http://{args.host}:{args.port}/")
    print(f"Runs are written to {args.output_dir}. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
