"""The main mock HTTP server, serving the app from starlette_app.py.

Run as a subprocess via the MockServer context manager; the server prints
the listen URLs (http, https and, with --listen-h3, HTTP/3) to stdout, one
per line, in that order.

Hypercorn is used instead of Twisted so that the same app can be served
over HTTP/1.1 and HTTP/2 (negotiated via ALPN on the TLS port), and over
HTTP/3 in the future (Hypercorn binds a UDP QUIC socket when quic_bind is
set and hypercorn[h3] is installed).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hypercorn.app_wrappers import ASGIWrapper
from hypercorn.asyncio.run import worker_serve
from hypercorn.asyncio.tcp_server import TCPServer
from hypercorn.config import Config

from .http_base import BaseMockServer
from .starlette_app import app, current_writer

if TYPE_CHECKING:
    from hypercorn.typing import ASGIFramework


class MockServer(BaseMockServer):
    module_name = "tests.mockserver.http"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-h3", action="store_true")
    args = parser.parse_args()

    # Expose the per-connection writer to ASGI handlers via a contextvar, so
    # /drop?abort=1 and /raw can write to or abort the TCP socket. The ASGI
    # task is created inside a TaskGroup, which inherits the context.
    _original_run = TCPServer.run

    async def _run_with_writer(self: TCPServer) -> None:
        current_writer.set(self.writer)
        await _original_run(self)

    TCPServer.run = _run_with_writer  # type: ignore[method-assign]

    config = Config()
    config.accesslog = None
    config.errorlog = None
    config.graceful_timeout = 0.5
    # starlette_app adds Date itself, to avoid duplicates when a handler
    # sets an explicit Date header
    config.include_date_header = False

    keys_dir = Path(__file__).parent.parent / "keys"
    config.certfile = str(keys_dir / "localhost.crt")
    config.keyfile = str(keys_dir / "localhost.key")

    config.insecure_bind = ["127.0.0.1:0"]
    config.bind = ["127.0.0.1:0"]
    config.quic_bind = ["127.0.0.1:0"] if args.listen_h3 else []
    config.alpn_protocols = ["h2", "http/1.1"]

    sockets = config.create_sockets()

    # BaseMockServer reads addresses in this exact order: http, https, h3.
    for sock in sockets.insecure_sockets:
        sock_host, sock_port = sock.getsockname()[:2]
        print(f"http://{sock_host}:{sock_port}", flush=True)
    for sock in sockets.secure_sockets:
        sock_host, sock_port = sock.getsockname()[:2]
        print(f"https://{sock_host}:{sock_port}", flush=True)
    for sock in sockets.quic_sockets:
        sock_host, sock_port = sock.getsockname()[:2]
        print(f"https+h3://{sock_host}:{sock_port}", flush=True)

    def _exception_handler(
        loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        # /drop?abort=1 and /raw close the socket fd behind asyncio's back,
        # so the transport cleanup fails with EBADF; don't log that.
        exc = context.get("exception")
        if isinstance(exc, OSError) and exc.errno == errno.EBADF:
            return
        loop.default_exception_handler(context)

    async def _run() -> None:
        asyncio.get_running_loop().set_exception_handler(_exception_handler)
        await worker_serve(
            ASGIWrapper(cast("ASGIFramework", app)),
            config,
            sockets=sockets,
        )

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
