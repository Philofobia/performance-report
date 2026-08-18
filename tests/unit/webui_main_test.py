"""Unit tests for the manual-entry server's entry point.

The binding rule is the security control this phase turns on, so it is tested
the way a control should be: by proving the socket is never created, not by
proving a warning was printed. `server_factory` is injected for exactly that —
the offline suite binds no port.
"""
from __future__ import annotations

import pytest

from webui.__main__ import LOOPBACK_HOSTS, main


class RecordingFactory:
    """Stands in for wsgiref's make_server; records that it was called."""

    def __init__(self):
        self.calls = []

    def __call__(self, host, port, app):
        self.calls.append((host, port, app))
        return self

    def serve_forever(self):      # the server the factory returns
        return None

    def server_close(self):
        return None


@pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
def test_loopback_hosts_are_accepted(host, tmp_path):
    factory = RecordingFactory()
    code = main(["--host", host, "--output-dir", str(tmp_path)],
                server_factory=factory)
    assert code == 0
    assert factory.calls[0][0] == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::", "example.com"])
def test_a_non_loopback_host_is_refused_before_any_socket_exists(host, tmp_path, capsys):
    factory = RecordingFactory()
    code = main(["--host", host, "--output-dir", str(tmp_path)],
                server_factory=factory)
    assert code == 2
    assert factory.calls == []
    assert host in capsys.readouterr().err


def test_the_port_is_passed_through(tmp_path):
    factory = RecordingFactory()
    main(["--port", "8123", "--output-dir", str(tmp_path)], server_factory=factory)
    assert factory.calls[0][1] == 8123


def test_the_default_host_is_loopback(tmp_path):
    factory = RecordingFactory()
    main(["--output-dir", str(tmp_path)], server_factory=factory)
    assert factory.calls[0][0] == "127.0.0.1"


def test_a_keyboard_interrupt_is_a_clean_exit(tmp_path):
    class Interrupting(RecordingFactory):
        def serve_forever(self):
            raise KeyboardInterrupt

    assert main(["--output-dir", str(tmp_path)], server_factory=Interrupting()) == 0
