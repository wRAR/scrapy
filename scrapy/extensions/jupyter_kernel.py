"""
Embedded Jupyter kernel extension (experimental).

Runs an IPython kernel inside the Scrapy process, on the crawler's asyncio
event loop, so Jupyter clients (``jupyter console --existing``, qtconsole,
VS Code) can inspect a live crawl with rich completion, magics and top-level
``await``.

Code executed from a client runs in the crawler's event loop: touching the
engine is safe, ``await`` works on engine APIs directly, and the crawl is
paused while a cell runs (the same semantics as the telnet console).
"""

from __future__ import annotations

import functools
import logging
import pprint
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.extensions.telnet import update_telnet_vars
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.engine import print_engine_status
from scrapy.utils.trackref import print_live_refs

if TYPE_CHECKING:
    from ipykernel.kernelapp import IPKernelApp

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy.crawler import Crawler


logger = logging.getLogger(__name__)

# At most one embedded kernel per process: ipykernel and IPython use
# process-wide singletons (IPKernelApp, the kernel and the shell classes).
_kernel_running = False


@functools.cache
def _get_app_class() -> type[IPKernelApp]:
    from ipykernel.ipkernel import IPythonKernel  # noqa: PLC0415
    from ipykernel.kernelapp import IPKernelApp  # noqa: PLC0415

    class EmbeddedKernel(IPythonKernel):
        async def shutdown_request(self, stream: Any, ident: Any, parent: Any) -> None:
            # The stock implementation stops the current event loop and
            # terminates all child processes of this process.
            if self.session:
                self.session.send(
                    stream,
                    "shutdown_reply",
                    {"status": "ok", "restart": False},
                    parent,
                    ident=ident,
                )

    class EmbeddedKernelApp(IPKernelApp):
        kernel_class = EmbeddedKernel

        def init_signal(self) -> None:
            # The stock implementation sets SIGINT to SIG_IGN process-wide,
            # which would disable Ctrl-C for the crawl.
            pass

    return EmbeddedKernelApp


class JupyterKernelConsole:
    """Embed a Jupyter (IPython) kernel in the crawler's asyncio event loop."""

    def __init__(self, crawler: Crawler):
        self.crawler: Crawler = crawler
        self.connection_file: str = (
            crawler.settings.get("JUPYTER_KERNEL_CONNECTION_FILE") or ""
        )
        self._app: IPKernelApp | None = None
        crawler.signals.connect(self.start_kernel, signals.engine_started)
        crawler.signals.connect(self.stop_kernel, signals.engine_stopped)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        if not crawler.settings.getbool("JUPYTER_KERNEL_ENABLED"):
            raise NotConfigured
        if not is_asyncio_available():
            raise NotConfigured(
                f"{cls.__name__} requires the asyncio support. Make"
                f" sure that you have either enabled the asyncio Twisted"
                f" reactor in the TWISTED_REACTOR setting or disabled the"
                f" TWISTED_REACTOR_ENABLED setting. See the asyncio documentation"
                f" of Scrapy for more information."
            )
        if find_spec("ipykernel") is None:
            raise NotConfigured(
                f"{cls.__name__} requires the ipykernel library to be installed."
            )
        return cls(crawler)

    def start_kernel(self) -> None:
        global _kernel_running  # noqa: PLW0603
        if _kernel_running:
            logger.warning(
                "Only one embedded Jupyter kernel per process is supported;"
                " not starting another one.",
                extra={"crawler": self.crawler},
            )
            return
        kwargs: dict[str, Any] = {
            "capture_fd_output": False,
        }
        if self.connection_file:
            kwargs["connection_file"] = self.connection_file
        self._app = app = _get_app_class().instance(**kwargs)
        app.initialize([])  # type: ignore[no-untyped-call]
        assert app.shell
        # Merge into the shell's own namespace instead of replacing
        # kernel.user_ns: the latter leaves user_module pointing elsewhere, so
        # cells would run with globals() != locals() and imports or top-level
        # names would be invisible inside functions defined in later cells.
        app.shell.user_ns.update(self._get_console_vars())
        app.shell.set_completer_frame()
        app.kernel.start()
        _kernel_running = True
        logger.info(
            f"Jupyter kernel: connect with e.g. 'jupyter console --existing {app.abs_connection_file}'",
            extra={"crawler": self.crawler},
        )

    def stop_kernel(self) -> None:
        global _kernel_running  # noqa: PLW0603
        app = self._app
        if app is None:
            return
        self._app = None
        # Tell the connected clients that the kernel is going away, like a
        # stock kernel does at shutdown. jupyter console shows the stream
        # message only with include_other_output enabled and ignores
        # shutdown_reply; qtconsole closes (or offers to) on shutdown_reply.
        kernel = app.kernel
        kernel.session.send(
            kernel.iopub_socket,
            "stream",
            {
                "name": "stderr",
                "text": "Scrapy engine stopped; shutting down the kernel.\n",
            },
            ident=kernel._topic("stream"),
        )
        kernel.session.send(
            kernel.iopub_socket,
            "shutdown_reply",
            {"status": "ok", "restart": False},
            ident=kernel._topic("shutdown"),
        )
        app.cleanup_connection_file()  # type: ignore[no-untyped-call]
        app.close()  # type: ignore[no-untyped-call]
        # Clear the singletons so that a later crawl can start a fresh kernel.
        if app.shell:
            app.shell.__class__.clear_instance()
        app.kernel.__class__.clear_instance()
        app.__class__.clear_instance()
        _kernel_running = False

    def _get_console_vars(self) -> dict[str, Any]:
        # The same namespace as TelnetConsole._get_telnet_vars(), except
        # "help": IPython provides its own help system.
        assert self.crawler.engine
        console_vars: dict[str, Any] = {
            "engine": self.crawler.engine,
            "spider": self.crawler.engine.spider,
            "crawler": self.crawler,
            "extensions": self.crawler.extensions,
            "stats": self.crawler.stats,
            "settings": self.crawler.settings,
            "est": lambda: print_engine_status(self.crawler.engine),
            "p": pprint.pprint,
            "prefs": print_live_refs,
        }
        self.crawler.signals.send_catch_log(
            update_telnet_vars, telnet_vars=console_vars
        )
        return console_vars
