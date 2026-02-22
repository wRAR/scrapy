from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from subprocess import PIPE, Popen
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import pytest

from scrapy.http import Request
from scrapy.utils.test import get_crawler
from tests.spiders import SimpleSpider, SingleRequestSpider
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Generator

    from tests.mockserver.http import MockServer


class MitmProxy:
    auth_user = "scrapy"
    auth_pass = "scrapy"

    def start(self):
        script = """
import sys
from mitmproxy.tools.main import mitmdump
sys.argv[0] = "mitmdump"
sys.exit(mitmdump())
        """
        cert_path = Path(__file__).parent.resolve() / "keys"
        self.proc = Popen(
            [
                sys.executable,
                "-u",
                "-c",
                script,
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                "0",
                "--proxyauth",
                f"{self.auth_user}:{self.auth_pass}",
                "--set",
                f"confdir={cert_path}",
                "--ssl-insecure",
            ],
            stdout=PIPE,
        )
        line = self.proc.stdout.readline().decode("utf-8")
        host_port = re.search(r"listening at (?:http://)?([^:]+:\d+)", line).group(1)
        return f"http://{self.auth_user}:{self.auth_pass}@{host_port}"

    def stop(self):
        self.proc.kill()
        self.proc.communicate()


def _wrong_credentials(proxy_url):
    bad_auth_proxy = list(urlsplit(proxy_url))
    bad_auth_proxy[1] = bad_auth_proxy[1].replace("scrapy:scrapy@", "wrong:wronger@")
    return urlunsplit(bad_auth_proxy)


@pytest.mark.requires_mitmproxy
class TestProxyConnect:
    @pytest.fixture(autouse=True)
    def proxy(self) -> Generator[None]:
        self._oldenv = os.environ.copy()
        self._proxy = MitmProxy()
        proxy_url = self._proxy.start()
        os.environ["https_proxy"] = proxy_url
        os.environ["http_proxy"] = proxy_url
        try:
            yield
        finally:
            self._proxy.stop()
            os.environ = self._oldenv

    @coroutine_test
    async def test_https_connect_tunnel(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer
    ) -> None:
        crawler = get_crawler(SimpleSpider)
        with caplog.at_level("DEBUG"):
            await crawler.crawl_async(mockserver.url("/status?n=200", is_secure=True))
        self._assert_got_response_code(200, caplog.text)

    @coroutine_test
    async def test_https_tunnel_auth_error(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer
    ) -> None:
        os.environ["https_proxy"] = _wrong_credentials(os.environ["https_proxy"])
        crawler = get_crawler(SimpleSpider)
        with caplog.at_level("DEBUG"):
            await crawler.crawl_async(mockserver.url("/status?n=200", is_secure=True))
        # The proxy returns a 407 error code but it does not reach the client;
        # he just sees a TunnelError.
        self._assert_got_tunnel_error(caplog.text)

    @coroutine_test
    async def test_https_tunnel_without_leak_proxy_authorization_header(
        self, caplog: pytest.LogCaptureFixture, mockserver: MockServer
    ) -> None:
        request = Request(mockserver.url("/echo", is_secure=True))
        crawler = get_crawler(SingleRequestSpider)
        with caplog.at_level("DEBUG"):
            await crawler.crawl_async(seed=request)
        assert isinstance(crawler.spider, SingleRequestSpider)
        self._assert_got_response_code(200, caplog.text)
        echo = json.loads(crawler.spider.meta["responses"][0].text)
        assert "Proxy-Authorization" not in echo["headers"]

    @staticmethod
    def _assert_got_response_code(code: int, log: str) -> None:
        assert log.count(f"Crawled ({code})") == 1

    @staticmethod
    def _assert_got_tunnel_error(log: str) -> None:
        assert "TunnelError" in log
