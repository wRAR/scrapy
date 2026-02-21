from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin

import pytest
from testfixtures import LogCapture
from twisted.internet.defer import Deferred, succeed

from scrapy.core.scheduler import BaseScheduler
from scrapy.http import Request
from scrapy.spiders import Spider
from scrapy.utils.defer import ensure_awaitable
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.request import fingerprint
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from tests.mockserver.http import MockServer

PATHS = ["/a", "/b", "/c"]
URLS = [urljoin("https://example.org", p) for p in PATHS]


class MinimalScheduler:
    def __init__(self) -> None:
        self.requests: dict[bytes, Request] = {}

    def has_pending_requests(self) -> bool:
        return bool(self.requests)

    def enqueue_request(self, request: Request) -> bool:
        fp = fingerprint(request)
        if fp not in self.requests:
            self.requests[fp] = request
            return True
        return False

    def next_request(self) -> Request | None:
        if self.has_pending_requests():
            _, request = self.requests.popitem()
            return request
        return None


class SimpleScheduler(MinimalScheduler):
    def open(self, spider: Spider) -> Deferred:
        return succeed("open")

    def close(self, reason: str) -> Deferred:
        return succeed("close")

    def __len__(self) -> int:
        return len(self.requests)


class PathsSpider(Spider):
    name = "paths"

    def __init__(self, mockserver: MockServer, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.start_urls = [mockserver.url(p) for p in PATHS]

    def parse(self, response):
        return {"path": urlparse_cached(response).path}


class InterfaceCheckMixin:
    def test_scheduler_class(self, scheduler: BaseScheduler) -> None:
        assert isinstance(scheduler, BaseScheduler)
        assert issubclass(scheduler.__class__, BaseScheduler)


class TestBaseScheduler(InterfaceCheckMixin):
    @pytest.fixture
    def scheduler(self) -> BaseScheduler:
        return BaseScheduler()  # type: ignore[abstract]

    def test_methods(self, scheduler: BaseScheduler) -> None:
        assert scheduler.open(Spider("foo")) is None
        assert scheduler.close("finished") is None
        with pytest.raises(NotImplementedError):
            scheduler.has_pending_requests()
        with pytest.raises(NotImplementedError):
            scheduler.enqueue_request(Request("https://example.org"))
        with pytest.raises(NotImplementedError):
            scheduler.next_request()


class TestMinimalScheduler(InterfaceCheckMixin):
    @pytest.fixture
    def scheduler(self) -> BaseScheduler:
        return MinimalScheduler()  # type: ignore[return-value]

    def test_open_close(self, scheduler: BaseScheduler) -> None:
        with pytest.raises(AttributeError):
            scheduler.open(Spider("foo"))
        with pytest.raises(AttributeError):
            scheduler.close("finished")

    def test_len(self, scheduler: BaseScheduler) -> None:
        with pytest.raises(AttributeError):
            scheduler.__len__()  # type: ignore[attr-defined]
        with pytest.raises(TypeError):
            len(scheduler)  # type: ignore[arg-type]

    def test_enqueue_dequeue(self, scheduler: BaseScheduler) -> None:
        assert not scheduler.has_pending_requests()
        for url in URLS:
            assert scheduler.enqueue_request(Request(url))
            assert not scheduler.enqueue_request(Request(url))
        assert scheduler.has_pending_requests()

        dequeued = []
        while scheduler.has_pending_requests():
            request = scheduler.next_request()
            assert request
            dequeued.append(request.url)
        assert set(dequeued) == set(URLS)
        assert not scheduler.has_pending_requests()


class TestSimpleScheduler(InterfaceCheckMixin):
    @pytest.fixture
    def scheduler(self) -> BaseScheduler:
        return SimpleScheduler()  # type: ignore[return-value]

    @coroutine_test
    async def test_enqueue_dequeue(self, scheduler: BaseScheduler) -> None:
        open_result = await ensure_awaitable(scheduler.open(Spider("foo")))
        assert open_result == "open"
        assert not scheduler.has_pending_requests()

        for url in URLS:
            assert scheduler.enqueue_request(Request(url))
            assert not scheduler.enqueue_request(Request(url))

        assert scheduler.has_pending_requests()
        assert len(scheduler) == len(URLS)  # type: ignore[arg-type]

        dequeued = []
        while scheduler.has_pending_requests():
            request = scheduler.next_request()
            assert request
            dequeued.append(request.url)
        assert set(dequeued) == set(URLS)

        assert not scheduler.has_pending_requests()
        assert len(scheduler) == 0  # type: ignore[arg-type]

        close_result = await ensure_awaitable(scheduler.close(""))
        assert close_result == "close"


class TestMinimalSchedulerCrawl:
    scheduler_cls: ClassVar[type[BaseScheduler]] = MinimalScheduler  # type: ignore[assignment]

    @coroutine_test
    async def test_crawl(self, mockserver: MockServer) -> None:
        settings = {
            "SCHEDULER": self.scheduler_cls,
        }
        with LogCapture() as log:
            crawler = get_crawler(PathsSpider, settings)
            await crawler.crawl_async(mockserver)
        for path in PATHS:
            assert f"{{'path': '{path}'}}" in str(log)
        assert f"'item_scraped_count': {len(PATHS)}" in str(log)


class TestSimpleSchedulerCrawl(TestMinimalSchedulerCrawl):
    scheduler_cls = SimpleScheduler  # type: ignore[assignment]
