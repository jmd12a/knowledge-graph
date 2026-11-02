"""Exercise the API restart port probe with real loopback sockets, without databases or models."""

from __future__ import annotations

import errno
import json
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.evidence import CaseFailure  # noqa: E402
from evals.runtime import assert_api_port_available  # noqa: E402


def time_wait_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            server, _ = listener.accept()
            with server:
                server.settimeout(2)
                server.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b"", "Server must close first to own TIME_WAIT"
                client.shutdown(socket.SHUT_WR)
                assert server.recv(1) == b""
    rows = Path("/proc/net/tcp").read_text("ascii").splitlines()[1:]
    assert any(row.split()[1].split(":")[1] == f"{port:04X}" and row.split()[3] == "06" for row in rows), (
        "The real connection did not enter TIME_WAIT on the listener's port"
    )
    return port


def main():
    port = time_wait_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as old_probe:
        try:
            old_probe.bind(("127.0.0.1", port))
        except OSError as exc:
            assert exc.errno == errno.EADDRINUSE
        else:
            raise AssertionError("Legacy plain bind unexpectedly accepted the TIME_WAIT port")
    assert_api_port_available(port)
    checked = []
    for reuse in (False, True):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if reuse:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            occupied = listener.getsockname()[1]
            try:
                assert_api_port_available(occupied)
            except CaseFailure:
                pass
            else:
                raise AssertionError("The probe accepted a real listening socket")
            # The probe must leave the existing listener intact and usable.
            with socket.create_connection(("127.0.0.1", occupied), timeout=2):
                accepted, _ = listener.accept()
                accepted.close()
            checked.append(
                {"reuseaddr": reuse, "live_listener_rejected": True, "listener_remains_usable": True}
            )
    print(
        json.dumps(
            {
                "status": "passed",
                "TIME_WAIT_observed": True,
                "legacy_plain_bind_reproduced_failure": True,
                "restart_probe_accepts_TIME_WAIT": True,
                "listeners": checked,
                "database_calls": 0,
                "model_calls": 0,
                "processes_stopped": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
