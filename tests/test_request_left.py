from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scrapy.signals import request_left_downloader
from scrapy.spiders import Spider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import inline_callbacks_test

if TYPE_CHECKING:
    from collections.abc import Generator

    from twisted.internet.defer import Deferred

    from tests.mockserver.http import MockServer


class SignalCatcherSpider(Spider):
    name = "signal_catcher"

    def __init__(self, crawler, url, *args, **kwargs):
        super().__init__(*args, **kwargs)
        crawler.signals.connect(self.on_request_left, signal=request_left_downloader)
        self.caught_times = 0
        self.start_urls = [url]

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        return cls(crawler, *args, **kwargs)

    def on_request_left(self, request, spider):
        self.caught_times += 1


class TestCatching:
    @inline_callbacks_test
    def test_success(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        crawler = get_crawler(SignalCatcherSpider)
        yield crawler.crawl(mockserver.url("/status?n=200"))
        assert isinstance(crawler.spider, SignalCatcherSpider)
        assert crawler.spider.caught_times == 1

    @inline_callbacks_test
    def test_timeout(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        crawler = get_crawler(SignalCatcherSpider, {"DOWNLOAD_TIMEOUT": 0.1})
        yield crawler.crawl(mockserver.url("/delay?n=0.2"))
        assert isinstance(crawler.spider, SignalCatcherSpider)
        assert crawler.spider.caught_times == 1

    @inline_callbacks_test
    def test_disconnect(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        crawler = get_crawler(SignalCatcherSpider)
        yield crawler.crawl(mockserver.url("/drop"))
        assert isinstance(crawler.spider, SignalCatcherSpider)
        assert crawler.spider.caught_times == 1

    @inline_callbacks_test
    def test_noconnect(self):
        crawler = get_crawler(SignalCatcherSpider)
        yield crawler.crawl("http://thereisdefinetelynosuchdomain.com")
        assert isinstance(crawler.spider, SignalCatcherSpider)
        assert crawler.spider.caught_times == 1
