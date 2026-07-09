"""Twisted resources for the mock servers that remain Twisted-based
(proxy_echo.py, simple_https.py) and for tests that run an in-process
Twisted site (tests/test_core_downloader.py, tests/test_http2_client_protocol.py).

The resources used by the main mock HTTP server were replaced with ASGI
handlers in starlette_app.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ParamSpec, TypeVar

from twisted.internet.task import deferLater
from twisted.web import resource
from twisted.web.server import NOT_DONE_YET

if TYPE_CHECKING:
    from collections.abc import Callable

    from twisted.internet.defer import Deferred
    from twisted.web.http import Request


_T = TypeVar("_T")
_P = ParamSpec("_P")


def getarg(request, name, default=None, type_=None):
    if name in request.args:
        value = request.args[name][0]
        if type_ is not None:
            value = type_(value)
        return value
    return default


class PayloadResource(resource.Resource):
    """
    A testing resource which renders itself as the contents of the request body
    as long as the request body is 100 bytes long, otherwise which renders
    itself as C{"ERROR"}.
    """

    def render(self, request):
        data = request.content.read()
        contentLength = request.requestHeaders.getRawHeaders(b"content-length")[0]
        if len(data) != 100 or int(contentLength) != 100:
            return b"ERROR"
        return data


class LeafResource(resource.Resource):
    isLeaf = True

    def deferRequest(
        self,
        request: Request,
        delay: float,
        f: Callable[_P, _T],
        *a: _P.args,
        **kw: _P.kwargs,
    ) -> Deferred[_T]:
        from twisted.internet import reactor

        def _cancelrequest(_):
            # silence CancelledError
            d.addErrback(lambda _: None)
            d.cancel()

        d = deferLater(reactor, delay, f, *a, **kw)
        request.notifyFinish().addErrback(_cancelrequest)
        return d


class Status(LeafResource):
    def render_GET(self, request):
        n = getarg(request, b"n", 200, type_=int)
        request.setResponseCode(n)
        return b""


class UriResource(resource.Resource):
    """Return the full uri that was requested"""

    def getChild(self, path, request):
        return self

    def render(self, request):
        # Note: this is an ugly hack for CONNECT request timeout test.
        #       Returning some data here fail SSL/TLS handshake
        # ToDo: implement proper HTTPS proxy tests, not faking them.
        if request.method != b"CONNECT":
            return request.uri
        request.transport.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        return NOT_DONE_YET
