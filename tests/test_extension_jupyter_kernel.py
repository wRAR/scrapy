from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scrapy.exceptions import NotConfigured
from scrapy.extensions.jupyter_kernel import JupyterKernelConsole
from scrapy.utils.test import get_crawler
from tests.utils import get_script_run_env

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from jupyter_client.blocking.client import BlockingKernelClient


SCRIPT = Path(__file__).parent / "JupyterKernelConsole" / "crawl.py"
# Guards against a hung subprocess or kernel.
TIMEOUT = 30


class TestFromCrawler:
    def test_disabled_by_default(self):
        crawler = get_crawler()
        with pytest.raises(NotConfigured) as excinfo:
            JupyterKernelConsole.from_crawler(crawler)
        assert not excinfo.value.args

    def test_disabled_explicitly(self):
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": False})
        with pytest.raises(NotConfigured) as excinfo:
            JupyterKernelConsole.from_crawler(crawler)
        assert not excinfo.value.args

    def test_no_asyncio(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.is_asyncio_available", lambda: False
        )
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": True})
        with pytest.raises(NotConfigured, match="asyncio"):
            JupyterKernelConsole.from_crawler(crawler)

    def test_missing_ipykernel(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.is_asyncio_available", lambda: True
        )
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.find_spec", lambda name: None
        )
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": True})
        with pytest.raises(NotConfigured, match="ipykernel"):
            JupyterKernelConsole.from_crawler(crawler)

    def test_enabled(self, monkeypatch):
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.is_asyncio_available", lambda: True
        )
        monkeypatch.setattr(
            "scrapy.extensions.jupyter_kernel.find_spec", lambda name: object()
        )
        crawler = get_crawler(settings_dict={"JUPYTER_KERNEL_ENABLED": True})
        console = JupyterKernelConsole.from_crawler(crawler)
        assert isinstance(console, JupyterKernelConsole)


class KernelCrawl:
    """A crawl with the kernel enabled, running crawl.py in a subprocess, and
    a client of its kernel."""

    def __init__(
        self, tmp_path: Path, mode: str, *args: str, connection_file: Path | None
    ):
        self.log_path = tmp_path / "log.txt"
        env = get_script_run_env()
        # Keep the IPython profile and the Jupyter files of the user out of
        # the tests.
        env["IPYTHONDIR"] = str(tmp_path / "ipython")
        env["JUPYTER_RUNTIME_DIR"] = str(tmp_path / "runtime")
        with self.log_path.open("w") as log:
            self.process = subprocess.Popen(
                [sys.executable, str(SCRIPT), mode, str(connection_file or ""), *args],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
        self.connection_file = connection_file or (
            tmp_path / "runtime" / f"kernel-{self.process.pid}.json"
        )
        self._client: BlockingKernelClient | None = None

    @property
    def client(self) -> BlockingKernelClient:
        assert self._client
        return self._client

    @property
    def log(self) -> str:
        return self.log_path.read_text()

    def connect(self, previous_key: str | None = None) -> None:
        """Connect to the kernel, once its connection file exists and has a
        key different from *previous_key*."""
        from jupyter_client.blocking.client import BlockingKernelClient  # noqa: PLC0415

        self.disconnect()
        deadline = time.monotonic() + TIMEOUT
        while True:
            assert time.monotonic() < deadline, self.log
            assert self.process.poll() is None, self.log
            try:
                key = json.loads(self.connection_file.read_text())["key"]
            except (OSError, ValueError, KeyError):
                pass
            else:
                if key != previous_key:
                    break
            time.sleep(0.1)
        client = BlockingKernelClient(connection_file=str(self.connection_file))
        client.load_connection_file()
        client.start_channels()
        client.wait_for_ready(timeout=TIMEOUT)
        self._client = client

    def disconnect(self) -> None:
        if self._client:
            self._client.stop_channels()
            self._client = None

    def execute(self, code: str) -> tuple[dict[str, Any], str]:
        """Run *code*, return its reply and its output."""
        output: list[str] = []

        def output_hook(msg: dict[str, Any]) -> None:
            content = msg["content"]
            if msg["msg_type"] == "stream":
                output.append(content["text"])
            elif msg["msg_type"] == "execute_result":
                output.append(content["data"]["text/plain"])

        reply = self.client.execute_interactive(
            code, timeout=TIMEOUT, output_hook=output_hook
        )
        return reply["content"], "".join(output).strip()

    def run(self, code: str) -> str:
        """Run *code*, check that it succeeded and return its output."""
        reply, output = self.execute(code)
        assert reply["status"] == "ok", reply
        return output

    def start_cell(self, code: str) -> str:
        """Start running *code*, which must print "started" first, and return
        its message ID once it does."""
        msg_id: str = self.client.execute(code)
        while True:
            msg = self.client.get_iopub_msg(timeout=TIMEOUT)
            if (
                msg["parent_header"].get("msg_id") == msg_id
                and msg["msg_type"] == "stream"
                and "started" in msg["content"]["text"]
            ):
                return msg_id

    def get_reply(self, msg_id: str) -> dict[str, Any]:
        while True:
            msg: dict[str, Any] = self.client.get_shell_msg(timeout=TIMEOUT)
            if msg["parent_header"].get("msg_id") == msg_id:
                content: dict[str, Any] = msg["content"]
                return content

    def request(self, msg_type: str, content: dict[str, Any]) -> dict[str, Any]:
        """Send a control request, return its reply."""
        msg = self.client.session.msg(msg_type, content)
        self.client.control_channel.send(msg)
        while True:
            reply: dict[str, Any] = self.client.control_channel.get_msg(timeout=TIMEOUT)
            if reply["parent_header"].get("msg_id") == msg["header"]["msg_id"]:
                reply_content: dict[str, Any] = reply["content"]
                return reply_content

    def wait(self) -> int:
        try:
            return self.process.wait(TIMEOUT)
        except subprocess.TimeoutExpired:
            pytest.fail(f"The crawl did not finish:\n{self.log}")

    def finish(self) -> int:
        """Let the crawl finish, return its exit code."""
        self.run("spider.done = True")
        return self.wait()

    def close(self) -> None:
        self.disconnect()
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()


@pytest.fixture
def start_crawl(tmp_path: Path) -> Iterator[Callable[..., KernelCrawl]]:
    crawls: list[KernelCrawl] = []

    def start(
        mode: str = "reactorless", *args: str, connection_file: Path | None = None
    ) -> KernelCrawl:
        crawl = KernelCrawl(
            tmp_path,
            mode,
            *args,
            connection_file=connection_file,
        )
        crawls.append(crawl)
        return crawl

    yield start
    for crawl in crawls:
        crawl.close()


skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32", reason="Interrupting cells relies on POSIX signals"
)


@pytest.mark.skipif(find_spec("ipykernel") is None, reason="Requires ipykernel")
class TestKernel:
    @pytest.mark.parametrize("mode", ["reactor", "reactorless"])
    def test_execute(self, start_crawl, mode):
        crawl = start_crawl(mode)
        crawl.connect()
        assert (
            crawl.run(
                "import asyncio, threading\n"
                "(asyncio.get_running_loop() is spider.loop,"
                " threading.current_thread() is threading.main_thread())"
            )
            == "(True, True)"
        )
        assert (
            crawl.run(
                "from scrapy import Request\n"
                "(await engine.download_async(Request('data:,a'))).text"
            )
            == "'a'"
        )
        assert crawl.finish() == 0

    def test_namespace(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        names = [
            "crawler",
            "engine",
            "est",
            "extensions",
            "p",
            "prefs",
            "settings",
            "spider",
            "stats",
        ]
        assert crawl.run(f"[name in globals() for name in {names}]") == str(
            [True] * len(names)
        )
        # Imports and definitions of a cell are visible in functions defined in
        # later cells.
        crawl.run("import json")
        crawl.run("def f():\n    return json.dumps(1)")
        assert crawl.run("f()") == "'1'"
        assert crawl.finish() == 0

    def test_completion(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        reply = crawl.client.complete("engine.dow", reply=True, timeout=TIMEOUT)
        assert "download_async" in reply["content"]["matches"]
        assert crawl.finish() == 0

    def test_close(self, start_crawl, tmp_path):
        crawl = start_crawl(connection_file=tmp_path / "kernel.json")
        crawl.connect()
        crawl.run("spider.done = True")
        while True:
            msg = crawl.client.get_iopub_msg(timeout=TIMEOUT)
            if msg["msg_type"] == "shutdown_reply":
                break
        assert crawl.wait() == 0
        assert not crawl.connection_file.exists()
        assert "Changed process state: []" in crawl.log
        assert "Traceback" not in crawl.log
        assert "NOTE: When using the `ipython kernel` entry point" not in crawl.log

    def test_exit(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        reply, _ = crawl.execute("exit")
        assert reply["payload"] == [{"source": "ask_exit", "keepkernel": True}]
        assert crawl.run("engine.running") == "True"
        assert crawl.finish() == 0

    def test_shutdown_request(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        assert crawl.request("shutdown_request", {"restart": False}) == {
            "status": "ok",
            "restart": False,
        }
        assert crawl.run("engine.running") == "True"
        assert crawl.finish() == 0

    def test_interrupt_idle(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        assert crawl.request("interrupt_request", {}) == {"status": "ok"}
        time.sleep(1)
        assert crawl.run("engine.running") == "True"
        assert crawl.finish() == 0
        assert "Received SIGINT" not in crawl.log

    @skip_on_windows
    @pytest.mark.parametrize(
        ("code", "error"),
        [
            ("import asyncio\nawait asyncio.sleep(60)", "CancelledError"),
            ("import time\ntime.sleep(60)", "KeyboardInterrupt"),
        ],
    )
    def test_interrupt_cell(self, start_crawl, code, error):
        crawl = start_crawl()
        crawl.connect()
        msg_id = crawl.start_cell(f"print('started')\n{code}")
        assert crawl.request("interrupt_request", {}) == {"status": "ok"}
        reply = crawl.get_reply(msg_id)
        assert reply["status"] == "error"
        assert reply["ename"] == error
        assert crawl.run("engine.running") == "True"
        assert crawl.finish() == 0

    @skip_on_windows
    def test_sigint_cell(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        msg_id = crawl.start_cell(
            "import asyncio\nprint('started')\nawait asyncio.sleep(60)"
        )
        os.kill(crawl.process.pid, signal.SIGINT)
        assert crawl.get_reply(msg_id)["ename"] == "CancelledError"
        assert crawl.run("engine.running") == "True"
        assert crawl.finish() == 0

    @skip_on_windows
    def test_sigint_idle(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        os.kill(crawl.process.pid, signal.SIGINT)
        assert crawl.wait() == 0
        assert "Received SIGINT, shutting down gracefully" in crawl.log

    def test_stop_from_cell(self, start_crawl):
        crawl = start_crawl()
        crawl.connect()
        assert crawl.run("await crawler.stop_async()") == ""
        assert crawl.wait() == 0
        assert not crawl.connection_file.exists()
        assert "Changed process state: []" in crawl.log
        assert "Traceback" not in crawl.log

    def test_sequential_crawls(self, start_crawl):
        crawl = start_crawl("sequential")
        crawl.connect()
        key = json.loads(crawl.connection_file.read_text())["key"]
        crawl.run("spider.done = True")
        crawl.connect(previous_key=key)
        assert crawl.finish() == 0

    def test_concurrent_crawls(self, start_crawl):
        crawl = start_crawl("concurrent")
        crawl.connect()
        message = "Not starting a Jupyter kernel, the one of another crawler"
        deadline = time.monotonic() + TIMEOUT
        while message not in crawl.log:
            assert time.monotonic() < deadline, crawl.log
            time.sleep(0.1)
        assert crawl.finish() == 0

    def test_thread(self, start_crawl):
        crawl = start_crawl("thread", "done")
        assert crawl.wait() == 0
        assert "requires the event loop of the crawler to run in the main" in (
            crawl.log
        )
        assert not crawl.connection_file.exists()

    def test_start_error(self, start_crawl, tmp_path):
        # The directory of the connection file cannot be created.
        (tmp_path / "file").touch()
        crawl = start_crawl(
            "reactorless", "done", connection_file=tmp_path / "file" / "kernel.json"
        )
        assert crawl.wait() == 0
        assert "Could not start a Jupyter kernel" in crawl.log
        assert "Changed process state: []" in crawl.log
