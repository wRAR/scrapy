from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scrapy.utils.test import get_crawler
from tests.spiders import (
    ErrorSpider,
    FollowAllSpider,
    ItemSpider,
    MaxItemsAndRequestsSpider,
    MetaSpider,
    SlowSpider,
)
from tests.utils.decorators import inline_callbacks_test

if TYPE_CHECKING:
    from collections.abc import Generator

    from twisted.internet.defer import Deferred

    from tests.mockserver.http import MockServer


class TestCloseSpider:
    @inline_callbacks_test
    def test_closespider_itemcount(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        close_on = 5
        crawler = get_crawler(ItemSpider, {"CLOSESPIDER_ITEMCOUNT": close_on})
        yield crawler.crawl(mockserver=mockserver)
        assert isinstance(crawler.spider, MetaSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_itemcount"
        assert crawler.stats
        itemcount = crawler.stats.get_value("item_scraped_count")
        assert itemcount >= close_on

    @inline_callbacks_test
    def test_closespider_pagecount(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        close_on = 5
        crawler = get_crawler(FollowAllSpider, {"CLOSESPIDER_PAGECOUNT": close_on})
        yield crawler.crawl(mockserver=mockserver)
        assert isinstance(crawler.spider, MetaSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_pagecount"
        assert crawler.stats
        pagecount = crawler.stats.get_value("response_received_count")
        assert pagecount >= close_on

    @inline_callbacks_test
    def test_closespider_pagecount_no_item(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        close_on = 5
        max_items = 5
        max_requests = close_on + max_items
        crawler = get_crawler(
            MaxItemsAndRequestsSpider,
            {
                "CLOSESPIDER_PAGECOUNT_NO_ITEM": close_on,
            },
        )
        yield crawler.crawl(
            max_items=max_items, max_requests=max_requests, mockserver=mockserver
        )
        assert isinstance(crawler.spider, MetaSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_pagecount_no_item"
        assert crawler.stats
        pagecount = crawler.stats.get_value("response_received_count")
        itemcount = crawler.stats.get_value("item_scraped_count")
        assert pagecount <= close_on + itemcount

    @inline_callbacks_test
    def test_closespider_pagecount_no_item_with_pagecount(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        close_on_pagecount_no_item = 5
        close_on_pagecount = 20
        crawler = get_crawler(
            FollowAllSpider,
            {
                "CLOSESPIDER_PAGECOUNT_NO_ITEM": close_on_pagecount_no_item,
                "CLOSESPIDER_PAGECOUNT": close_on_pagecount,
            },
        )
        yield crawler.crawl(mockserver=mockserver)
        assert isinstance(crawler.spider, MetaSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_pagecount_no_item"
        assert crawler.stats
        pagecount = crawler.stats.get_value("response_received_count")
        assert pagecount < close_on_pagecount

    @inline_callbacks_test
    def test_closespider_errorcount(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        close_on = 5
        crawler = get_crawler(ErrorSpider, {"CLOSESPIDER_ERRORCOUNT": close_on})
        yield crawler.crawl(total=1000000, mockserver=mockserver)
        assert isinstance(crawler.spider, ErrorSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_errorcount"
        key = f"spider_exceptions/{crawler.spider.exception_cls.__name__}"
        assert crawler.stats
        errorcount = crawler.stats.get_value(key)
        assert crawler.stats.get_value("spider_exceptions/count") >= close_on
        assert errorcount >= close_on

    @inline_callbacks_test
    def test_closespider_timeout(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        close_on = 0.1
        crawler = get_crawler(FollowAllSpider, {"CLOSESPIDER_TIMEOUT": close_on})
        yield crawler.crawl(total=1000000, mockserver=mockserver)
        assert isinstance(crawler.spider, MetaSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_timeout"
        assert crawler.stats
        total_seconds = crawler.stats.get_value("elapsed_time_seconds")
        assert total_seconds >= close_on

    @inline_callbacks_test
    def test_closespider_timeout_no_item(
        self, mockserver: MockServer
    ) -> Generator[Deferred[Any], Any, None]:
        timeout = 1
        crawler = get_crawler(SlowSpider, {"CLOSESPIDER_TIMEOUT_NO_ITEM": timeout})
        yield crawler.crawl(n=3, mockserver=mockserver)
        assert isinstance(crawler.spider, MetaSpider)
        reason = crawler.spider.meta["close_reason"]
        assert reason == "closespider_timeout_no_item"
        assert crawler.stats
        total_seconds = crawler.stats.get_value("elapsed_time_seconds")
        assert total_seconds >= timeout
