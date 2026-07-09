"""Starlette ASGI app that replaces the Twisted-based mock HTTP resources.

It is served by Hypercorn (see http.py), which can speak HTTP/1.1, HTTP/2
and, in the future, HTTP/3 from a single code base, unlike Twisted.

Most endpoints are plain ``async def f(request) -> Response`` handlers, but
some need direct control over ``http.response.body`` framing (sending fewer
bytes than the advertised Content-Length, truncating a chunked stream) or
over the connection lifetime (never responding), so they are ASGI-callable
class instances: ``__call__`` on an instance isn't a ``function``/``method``,
so Starlette's Route plumbs them through as raw ASGI apps without the
Request/Response wrapping (and without the exception-to-500 conversion,
which would repair the very framing breakage those endpoints exist to
produce).

Two endpoints need the actual OS socket, which the ASGI contract doesn't
expose. Hypercorn's per-connection StreamWriter is stashed in a ContextVar
by http.py (Hypercorn creates the ASGI task inside a TaskGroup, which
inherits the contextvars of the connection handler):

* ``/drop?abort=1`` sets SO_LINGER and closes the socket fd to force a TCP
  RST, mirroring Twisted's ``abortConnection()``.
* ``/raw`` writes its argument verbatim as the whole HTTP response (status
  line included) and closes the connection, so tests can produce responses
  with arbitrary or broken framing, mirroring Twisted's
  ``startedWriting = 1`` trick. HTTP/1.1 over plain TCP only.

Bodies that tests compare byte-for-byte (the ``redirectTo()`` HTML of
``/redirect``, the 404 page of ``/static``) reproduce the Twisted output
exactly; see tests/test_engine.py::TestEngineBase._assert_bytes_received.
"""

from __future__ import annotations

import asyncio
import contextvars
import gzip
import json
import mimetypes
import os
import random
import socket
import struct
from email.utils import formatdate
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlencode

from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from tests import tests_datadir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request
    from starlette.types import Receive, Scope, Send

# Populated by http.py so /drop?abort=1 and /raw can access the TCP socket.
current_writer: contextvars.ContextVar[asyncio.StreamWriter | None] = (
    contextvars.ContextVar("current_writer", default=None)
)


_HTML = {"content-type": "text/html"}
_TEXT = {"content-type": "text/plain"}
# what twisted.web.util.redirectTo() sets
_REDIRECT_HTML = {"content-type": "text/html; charset=utf-8"}

_STATIC_ROOT = Path(tests_datadir, "test_site")

_NOT_FOUND_BODY = b"""
<html>
  <head><title>404 - No Such Resource</title></head>
  <body>
    <h1>No Such Resource</h1>
    <p>File not found.</p>
  </body>
</html>
"""


def _redirect_body(url: str) -> bytes:
    # the exact HTML produced by twisted.web.util.redirectTo()
    return f"""
<html>
    <head>
        <meta http-equiv=\"refresh\" content=\"0;URL={url}\">
    </head>
    <body bgcolor=\"#FFFFFF\" text=\"#000000\">
    <a href=\"{url}\">click here</a>
    </body>
</html>
""".encode()


def _title_case(name: str) -> str:
    # ASGI lowercases header names; Twisted's getAllRawHeaders() returns them
    # in canonical case, and /echo's tests compare against names like
    # "Accept-Charset" and "X-Custom-Header".
    return "-".join(part.capitalize() for part in name.split("-"))


# --- Request-based endpoints ---


async def root(request: Request) -> Response:
    return Response(b"Scrapy mock HTTP server\n", headers=_HTML)


async def text(request: Request) -> Response:
    return Response(b"Works", headers=_TEXT)


async def html(request: Request) -> Response:
    return Response(
        b"<body><p class='one'>Works</p><p class='two'>World</p></body>",
        headers=_HTML,
    )


async def enc_gb18030(request: Request) -> Response:
    return Response(
        b"<p>gb18030 encoding</p>",
        headers={"content-type": "text/html; charset=gb18030"},
    )


async def redirect(request: Request) -> Response:
    return Response(
        _redirect_body("/redirected"),
        status_code=302,
        headers={**_REDIRECT_HTML, "location": "/redirected"},
    )


async def redirect_no_meta_refresh(request: Request) -> Response:
    body = _redirect_body("/redirected").replace(
        b'http-equiv="refresh"', b'http-no-equiv="do-not-refresh-me"'
    )
    return Response(
        body, status_code=302, headers={**_REDIRECT_HTML, "location": "/redirected"}
    )


async def redirected(request: Request) -> Response:
    return Response(b"Redirected here", headers=_TEXT)


async def redirect_to(request: Request) -> Response:
    goto = request.query_params.get("goto", "/")
    return Response(
        b"redirecting...",
        status_code=302,
        headers={**_REDIRECT_HTML, "location": goto},
    )


_NUMBERS = b"".join(str(x).encode("utf8") for x in range(2**18))


async def numbers(request: Request) -> Response:
    return Response(_NUMBERS, headers=_TEXT)


async def status(request: Request) -> Response:
    n = int(request.query_params.get("n", 200))
    return Response(b"", status_code=n, headers=_HTML)


async def delay(request: Request) -> Response:
    n = float(request.query_params.get("n", 1))
    b = int(request.query_params.get("b", 1))
    body = f"Response delayed for {n:.3f} seconds\n".encode()
    if not b:
        await asyncio.sleep(n)

    async def gen() -> AsyncIterator[bytes]:
        if b:
            # headers were sent immediately; delay the body
            await asyncio.sleep(n)
        yield body

    return StreamingResponse(gen(), headers=_HTML)


async def follow(request: Request) -> Response:
    args: dict[str, list[str]] = {
        k: request.query_params.getlist(k) for k in request.query_params
    }
    total = int(args.get("total", ["100"])[0])
    show = int(args.get("show", ["1"])[0])
    order = args.get("order", ["desc"])[0]
    maxlatency = float(args.get("maxlatency", ["0"])[0])
    n = int(args.get("n", [str(total)])[0])
    nlist: list[int] | range
    if order == "rand":
        nlist = [random.randint(1, total) for _ in range(show)]
    else:  # order == "desc"
        nlist = range(n, max(n - show, 0), -1)

    await asyncio.sleep(random.random() * maxlatency)

    s = """<html> <head></head> <body>"""
    for nl in nlist:
        args["n"] = [str(nl)]
        argstr = urlencode(args, doseq=True)
        s += f"<a href='/follow?{argstr}'>follow {nl}</a><br>"
    s += """</body>"""
    return Response(s.encode(), headers=_HTML)


async def host(request: Request) -> Response:
    return Response(request.headers.get("host", "").encode(), headers=_HTML)


async def client_ip(request: Request) -> Response:
    client = request.client
    ip = client.host.encode() if client and client.host else b""
    return Response(ip, headers=_HTML)


async def content_length_header(request: Request) -> Response:
    return Response(request.headers.get("content-length", "").encode(), headers=_HTML)


async def empty_content_type(request: Request) -> Response:
    body = await request.body()
    # Twisted sends a truly empty Content-Type value here; Hypercorn rejects
    # an empty header value, and a single space is effectively empty for
    # Scrapy's response-class detection.
    return Response(body, headers={"content-type": " "})


async def echo(request: Request) -> Response:
    body = await request.body()
    headers: dict[str, list[str]] = {}
    for name, value in request.scope["headers"]:
        headers.setdefault(_title_case(name.decode()), []).append(value.decode())
    output = {"headers": headers, "body": body.decode()}
    return Response(json.dumps(output).encode(), headers=_HTML)


async def payload(request: Request) -> Response:
    body = await request.body()
    content_length = request.headers.get("content-length")
    if len(body) != 100 or content_length is None or int(content_length) != 100:
        return Response(b"ERROR", headers=_HTML)
    return Response(body, headers=_HTML)


async def alpayload(request: Request) -> Response:
    body = await request.body()
    return Response(body, headers=_HTML)


async def response_headers(request: Request) -> Response:
    body = json.loads((await request.body()).decode())
    response = Response(json.dumps(body).encode(), headers=_HTML)
    for name, value in body.items():
        if name.lower() == "set-cookie":
            response.headers.append(name, value)
        else:
            response.headers[name] = value
    return response


async def compress(request: Request) -> Response:
    data = request.query_params.get("data", "")
    if request.headers.get("accept-encoding") == "gzip":
        return Response(
            gzip.compress(data.encode()),
            headers={**_HTML, "content-encoding": "gzip"},
        )
    # just set this to trigger a test failure if no valid accept-encoding
    # header was set
    return Response(
        b"Did not receive a valid accept-encoding header",
        status_code=500,
        headers=_HTML,
    )


async def duplicate_header(request: Request) -> Response:
    response = Response(b"", headers=_HTML)
    response.headers.append("set-cookie", "a=b")
    response.headers.append("set-cookie", "c=d")
    return response


async def set_cookie(request: Request) -> Response:
    response = Response(b"", headers=_HTML)
    for name, value in request.query_params.multi_items():
        response.headers.append("set-cookie", f"{name}={value}")
    return response


async def uri_echo(request: Request) -> Response:
    # the raw (still percent-encoded) request target, like Twisted's
    # request.uri; used by verbatim_url tests
    raw_path = request.scope.get("raw_path") or request.url.path.encode()
    query_string = request.scope.get("query_string", b"")
    uri = raw_path + (b"?" + query_string if query_string else b"")
    return Response(uri, headers=_HTML)


async def chunked(request: Request) -> Response:
    async def gen() -> AsyncIterator[bytes]:
        yield b"chunked "
        yield b"content\n"

    return StreamingResponse(gen(), headers=_HTML)


async def large_chunked_file(request: Request) -> Response:
    async def gen() -> AsyncIterator[bytes]:
        chunk = b"x" * 1024
        for _ in range(1024):
            yield chunk

    return StreamingResponse(gen(), headers=_HTML)


# --- Static file serving (replaces twisted.web.static.File) ---


def _file_row(name: str, path: Path) -> str:
    if path.is_dir():
        return (
            f'<tr><td><a href="{quote(name)}/">{name}/</a></td>'
            f"<td></td><td>[Directory]</td><td></td></tr>"
        )
    size = path.stat().st_size
    content_type = mimetypes.guess_type(name)[0] or "text/html"
    return (
        f'<tr><td><a href="{quote(name)}">{name}</a></td>'
        f"<td>{size}B</td><td>{content_type}</td><td></td></tr>"
    )


def _directory_listing(url_path: str, directory: Path) -> Response:
    # the table structure (thead with a "Filename" header) mimics Twisted's
    # DirectoryLister; tests/test_pipeline_crawl.py extracts hrefs with the
    # XPath //table[thead/tr/th="Filename"]/tbody//a/@href
    rows = "\n".join(
        _file_row(entry.name, entry) for entry in sorted(directory.iterdir())
    )
    body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Directory listing for {url_path}</title>
</head>
<body>
<h1>Directory listing for {url_path}</h1>
<table>
    <thead>
        <tr>
            <th>Filename</th>
            <th>Size</th>
            <th>Content type</th>
            <th>Content encoding</th>
        </tr>
    </thead>
    <tbody>
{rows}
    </tbody>
</table>
</body>
</html>
"""
    return Response(body.encode(), headers=_HTML)


def _not_found() -> Response:
    # the exact HTML produced by Twisted for a 404
    return Response(_NOT_FOUND_BODY, status_code=404, headers=_HTML)


async def static_serve(request: Request) -> Response:
    root = _STATIC_ROOT.resolve()
    subpath = request.path_params.get("rest", "")
    try:
        target = (root / subpath).resolve() if subpath else root
    except (OSError, ValueError):
        return _not_found()
    if not target.is_relative_to(root) or not target.exists():
        return _not_found()
    if target.is_dir():
        if not request.url.path.endswith("/"):
            location = request.url.path + "/"
            return Response(
                _redirect_body(location),
                status_code=302,
                headers={**_REDIRECT_HTML, "location": location},
            )
        index = target / "index.html"
        if index.is_file():
            target = index
        else:
            return _directory_listing(request.url.path, target)
    content_type = mimetypes.guess_type(target.name)[0] or "text/html"
    return Response(target.read_bytes(), headers={"content-type": content_type})


# Catch-all for unknown paths (mirrors Twisted's Root.getChild returning self).
async def catchall(request: Request) -> Response:
    return Response(b"Scrapy mock HTTP server\n", headers=_HTML)


# --- Raw-ASGI endpoints (class instances so Starlette's Route plumbs them
#     through as ASGI apps without the Request/Response wrapping) ---


async def _wait_disconnect(receive: Receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


class _Wait:
    """Never respond; the client should hit its download timeout."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await _wait_disconnect(receive)


class _HangAfterHeaders:
    """Send the response start and some body bytes, then hang."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"some bytes", "more_body": True}
        )
        await _wait_disconnect(receive)


class _Partial:
    """Advertise Content-Length: 1024 but send only 16 bytes and close."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html"),
                    (b"content-length", b"1024"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"partial content\n",
                "more_body": True,
            }
        )
        raise RuntimeError("partial: forced close")


class _Broken:
    """Advertise Content-Length: 20 but send only 7 bytes and close."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html"),
                    (b"content-length", b"20"),
                ],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"partial", "more_body": True}
        )
        raise RuntimeError("broken: forced close")


class _BrokenChunked:
    r"""Send a chunked HTTP/1.1 response without the terminating chunk.

    Without a Content-Length, Hypercorn picks chunked transfer encoding on
    HTTP/1.1; raising after two chunks closes the connection before the
    terminating ``0\r\n\r\n`` is sent, so the client sees a truncated
    chunked stream, like with the Twisted implementation. (On HTTP/2 this
    produces RST_STREAM instead; the affected tests are HTTP/1.1-only.)
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/html")],
            }
        )
        await send(
            {"type": "http.response.body", "body": b"chunked ", "more_body": True}
        )
        await send(
            {"type": "http.response.body", "body": b"content\n", "more_body": True}
        )
        raise RuntimeError("broken-chunked: forced close")


class _Drop:
    """Close the connection mid-response.

    With ``abort=1``, force a TCP RST (like Twisted's ``abortConnection()``)
    via SO_LINGER with a zero timeout; closing the socket fd directly
    bypasses the graceful shutdown(2) that asyncio's transport would do,
    which would send a FIN and invalidate SO_LINGER. Without ``abort``,
    send a partial response and close gracefully (FIN), like Twisted's
    ``loseConnection()``.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        query = parse_qs((scope.get("query_string") or b"").decode())
        abort = int(query.get("abort", ["0"])[0])
        if abort:
            writer = current_writer.get()
            assert writer is not None
            sock = writer.get_extra_info("socket")
            assert sock is not None
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            os.close(sock.fileno())
            raise RuntimeError("drop: aborted")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html"),
                    (b"content-length", b"1024"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"this connection will be dropped\n",
                "more_body": True,
            }
        )
        raise RuntimeError("drop: forced close")


class _Raw:
    """Send the ``raw`` query argument as the whole response, verbatim.

    This lets tests produce responses with arbitrary framing (e.g. a
    Content-Length-less response terminated by connection close) or broken
    status lines, which cannot be expressed through the ASGI contract, so
    the bytes are written directly to the socket via the writer that
    http.py stashes in ``current_writer``. Only meaningful for HTTP/1.1
    over plain TCP.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        query = parse_qs((scope.get("query_string") or b"").decode())
        raw = query.get("raw", ["HTTP 1.1 200 OK\n"])[0].encode()
        writer = current_writer.get()
        assert writer is not None
        writer.write(raw)
        await writer.drain()
        writer.close()
        # Raising prevents Hypercorn from responding; whatever it tries to
        # write to the already-closed transport is discarded.
        raise RuntimeError("raw: forced close")


class _DateHeader:
    """Add a Date header to responses that don't set one.

    Twisted adds Date to every response and tests check for its presence,
    but Hypercorn's equivalent (``include_date_header``) adds the header
    unconditionally, which would produce a duplicate when a handler sets
    its own Date (e.g. /response-headers), so it is disabled in http.py in
    favor of this middleware.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_date(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(name.lower() == b"date" for name, _ in headers):
                    headers.append((b"date", formatdate(usegmt=True).encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_date)


_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"]

routes = [
    Route("/", root, methods=_ALL_METHODS),
    Route("/text", text, methods=_ALL_METHODS),
    Route("/html", html, methods=_ALL_METHODS),
    Route("/enc-gb18030", enc_gb18030, methods=_ALL_METHODS),
    Route("/redirect", redirect, methods=_ALL_METHODS),
    Route("/redirect-no-meta-refresh", redirect_no_meta_refresh, methods=_ALL_METHODS),
    Route("/redirected", redirected, methods=_ALL_METHODS),
    Route("/redirect-to", redirect_to, methods=_ALL_METHODS),
    Route("/numbers", numbers, methods=_ALL_METHODS),
    Route("/status", status, methods=_ALL_METHODS),
    Route("/delay", delay, methods=_ALL_METHODS),
    Route("/follow", follow, methods=_ALL_METHODS),
    Route("/host", host, methods=_ALL_METHODS),
    Route("/client-ip", client_ip, methods=_ALL_METHODS),
    Route("/contentlength", content_length_header, methods=_ALL_METHODS),
    Route("/nocontenttype", empty_content_type, methods=_ALL_METHODS),
    Route("/echo", echo, methods=_ALL_METHODS),
    Route("/payload", payload, methods=_ALL_METHODS),
    Route("/alpayload", alpayload, methods=_ALL_METHODS),
    Route("/response-headers", response_headers, methods=_ALL_METHODS),
    Route("/compress", compress, methods=_ALL_METHODS),
    Route("/duplicate-header", duplicate_header, methods=_ALL_METHODS),
    Route("/set-cookie", set_cookie, methods=_ALL_METHODS),
    Route("/uri", uri_echo, methods=_ALL_METHODS),
    Route("/uri/{rest:path}", uri_echo, methods=_ALL_METHODS),
    Route("/chunked", chunked, methods=_ALL_METHODS),
    Route("/largechunkedfile", large_chunked_file, methods=_ALL_METHODS),
    Route("/static", static_serve, methods=_ALL_METHODS),
    Route("/static/{rest:path}", static_serve, methods=_ALL_METHODS),
    Route("/wait", _Wait()),
    Route("/hang-after-headers", _HangAfterHeaders()),
    Route("/partial", _Partial()),
    Route("/broken", _Broken()),
    Route("/broken-chunked", _BrokenChunked()),
    Route("/drop", _Drop()),
    Route("/raw", _Raw()),
    Route("/{rest:path}", catchall, methods=_ALL_METHODS),
]


app = _DateHeader(Starlette(routes=routes))
