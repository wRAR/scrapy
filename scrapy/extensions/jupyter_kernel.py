from __future__ import annotations

import asyncio
import atexit
import functools
import logging
import os
import pprint
import signal
import sys
import threading
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.extensions.telnet import update_telnet_vars
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.engine import print_engine_status
from scrapy.utils.trackref import print_live_refs

if TYPE_CHECKING:
    from collections.abc import Callable

    # typing.Self requires Python 3.11
    from typing import Self

    from ipykernel.kernelapp import IPKernelApp

    from scrapy.crawler import Crawler


logger = logging.getLogger(__name__)
# ipykernel logs through this logger instead of its own stderr handler.
_kernel_logger = logging.getLogger(f"{__name__}.kernel")

# At most one embedded kernel per process: ipykernel and IPython use
# process-wide singletons (IPKernelApp, the kernel and the shell classes).
_kernel_running = False


@functools.cache
def _get_app_class() -> type[IPKernelApp]:
    from ipykernel.ipkernel import IPythonKernel  # noqa: PLC0415
    from ipykernel.kernelapp import IPKernelApp  # noqa: PLC0415
    from ipykernel.zmqshell import ZMQInteractiveShell  # noqa: PLC0415

    class EmbeddedShell(ZMQInteractiveShell):
        def ask_exit(self) -> None:
            # The stock implementation stops the event loop, i.e. the crawl.
            # Only tell the client to exit and to leave the kernel running.
            assert self.payload_manager
            self.payload_manager.write_payload(
                {"source": "ask_exit", "keepkernel": True}
            )

    class EmbeddedKernel(IPythonKernel):
        shell_class = EmbeddedShell
        # The number of cells being executed, and a callback to run once
        # there are none left.
        _executing = 0
        _when_idle: Callable[[], None] | None = None

        async def execute_request(self, stream: Any, ident: Any, parent: Any) -> None:
            self._executing += 1
            try:
                await super().execute_request(stream, ident, parent)  # type: ignore[no-untyped-call]
            finally:
                self._executing -= 1
                if not self._executing and self._when_idle is not None:
                    # Run it after the caller has published the idle status,
                    # which clients wait for.
                    asyncio.get_running_loop().call_soon(self._when_idle)
                    self._when_idle = None

        async def interrupt_request(self, stream: Any, ident: Any, parent: Any) -> None:
            # The stock implementation sends SIGINT to the whole process, or
            # to its process group, which stops the crawl if no cell is
            # running and also reaches other processes of the group. Signal
            # only the thread that runs the cells, and only while one runs.
            # Like the stock implementation, do nothing on Windows.
            if self._executing and hasattr(signal, "pthread_kill"):
                thread_id = threading.main_thread().ident
                assert thread_id is not None
                signal.pthread_kill(thread_id, signal.SIGINT)
            assert self.session
            self.session.send(
                stream, "interrupt_reply", {"status": "ok"}, parent, ident=ident
            )

        async def shutdown_request(self, stream: Any, ident: Any, parent: Any) -> None:
            # The stock implementation stops the current event loop and
            # terminates all child processes of this process.
            assert self.session
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
            # The stock implementation sets SIGINT to SIG_IGN, which would
            # disable Ctrl-C for the crawl.
            pass

        def init_crash_handler(self) -> None:
            # The stock implementation replaces sys.excepthook.
            pass

        def init_pdb(self) -> None:
            # The stock implementation replaces pdb.Pdb and pdb.set_trace.
            pass

        def patch_io(self) -> None:
            # The stock implementation patches faulthandler.
            pass

        def init_io(self) -> None:
            # Keep sys.stderr, where Scrapy logs to: logging handlers created
            # while the kernel is running, e.g. for another crawler, would
            # otherwise send log messages to the clients.
            stderr = sys.stderr
            super().init_io()  # type: ignore[no-untyped-call]
            sys.stderr = stderr

        def log_connection_info(self) -> None:
            # The stock implementation prints instructions for a standalone
            # kernel to the standard output. Keep only what the kernel needs.
            self._ports = {
                "shell": self.shell_port,
                "iopub": self.iopub_port,
                "stdin": self.stdin_port,
                "hb": self.hb_port,
                "control": self.control_port,
            }

    return EmbeddedKernelApp


def _close_app(app: IPKernelApp) -> None:
    global _kernel_running  # noqa: PLW0603
    app.cleanup_connection_file()  # type: ignore[no-untyped-call]
    app.close()  # type: ignore[no-untyped-call]
    # Both are registered by initialize(), and keep the app alive until exit.
    atexit.unregister(app.cleanup_connection_file)
    atexit.unregister(app.close)
    # Clear the singletons so that a later crawl can start a fresh kernel.
    if app.shell:
        app.shell.__class__.clear_instance()
    if app.kernel:
        app.kernel.__class__.clear_instance()
    app.__class__.clear_instance()
    _kernel_running = False


class JupyterKernelConsole:
    """Run an IPython kernel in the crawler's asyncio event loop, so that
    Jupyter clients can connect to it to inspect and control a running crawl.

    See :ref:`jupyter-kernel`.
    """

    def __init__(self, crawler: Crawler):
        self.crawler: Crawler = crawler
        self._connection_file: str = (
            crawler.settings.get("JUPYTER_KERNEL_CONNECTION_FILE") or ""
        )
        self._app: IPKernelApp | None = None
        crawler.signals.connect(self._start_kernel, signals.engine_started)
        crawler.signals.connect(self._stop_kernel, signals.engine_stopped)

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

    def _start_kernel(self) -> None:
        global _kernel_running  # noqa: PLW0603
        if _kernel_running:
            logger.info(
                "Not starting a Jupyter kernel, the one of another crawler is"
                " already running.",
                extra={"crawler": self.crawler},
            )
            return
        if threading.current_thread() is not threading.main_thread():
            # Running cells relies on signal handlers, which Python only
            # allows in the main thread.
            logger.error(
                "Not starting a Jupyter kernel, it requires the event loop of"
                " the crawler to run in the main thread.",
                extra={"crawler": self.crawler},
            )
            return
        kwargs: dict[str, Any] = {
            "capture_fd_output": False,
            "ignore_cwd": True,
            "log": _kernel_logger,
        }
        if self._connection_file:
            kwargs["connection_file"] = self._connection_file
        # Hide ipykernel's INFO messages, e.g. about every failing cell.
        # Scrapy's logging setup resets the level of existing loggers.
        _kernel_logger.setLevel(logging.WARNING)
        app = _get_app_class().instance(**kwargs)
        mplbackend = os.environ.get("MPLBACKEND")
        try:
            app.initialize([])  # type: ignore[no-untyped-call]
            assert app.shell
            # Merge into the shell's own namespace instead of replacing
            # kernel.user_ns: the latter leaves user_module pointing elsewhere,
            # so cells would run with globals() != locals() and imports or
            # top-level names would be invisible inside functions defined in
            # later cells.
            app.shell.user_ns.update(self._get_console_vars())
            app.shell.set_completer_frame()
            app.kernel.start()
        except Exception:
            logger.exception(
                "Could not start a Jupyter kernel.", extra={"crawler": self.crawler}
            )
            _close_app(app)
            return
        finally:
            # initialize() sets this for inline plots in clients, but child
            # processes would inherit it.
            if mplbackend is None:
                os.environ.pop("MPLBACKEND", None)
        self._app = app
        _kernel_running = True
        logger.info(
            f"Jupyter kernel: connect with e.g. 'jupyter console --existing {app.abs_connection_file}'",
            extra={"crawler": self.crawler},
        )

    def _stop_kernel(self) -> None:
        app = self._app
        if app is None:
            return
        self._app = None
        if app.kernel._executing:
            # The engine was stopped from a cell, e.g. with
            # crawler.stop_async(). Closing the kernel now would drop the
            # reply to that cell.
            app.kernel._when_idle = functools.partial(self._close_kernel, app)
            return
        self._close_kernel(app)

    def _close_kernel(self, app: IPKernelApp) -> None:
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
        _close_app(app)

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
