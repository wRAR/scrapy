"""Tests for the ExecutionEngine state machine (#6916).

These cover the engine and spider lifecycles, including the race conditions
that the state machine is meant to make well-defined: closes and stops
requested while the engine is starting or the spider is opening, double
closes, and the inverted (scrapy shell) lifecycle where the engine starts
before a spider is opened.

All tests use deterministic synchronization (signals and Deferreds), not
timing, and run both with and without a reactor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from twisted.internet.defer import Deferred

from scrapy import signals
from scrapy.core.engine import EngineState, ExecutionEngine, SpiderState
from scrapy.core.scheduler import BaseScheduler
from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.utils.defer import (
    _schedule_coro,
    deferred_from_coro,
    maybe_deferred_to_future,
)
from scrapy.utils.spider import DefaultSpider
from scrapy.utils.test import get_crawler
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


class SignalRecorder:
    """Record lifecycle signals and the engine/spider states at the time each
    signal fired."""

    SIGNALS = ("engine_started", "engine_stopped", "spider_opened", "spider_closed")

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.calls: list[tuple[str, EngineState, SpiderState]] = []
        self.close_reasons: list[str] = []
        # Keep strong references to the handlers: signal connections are weak.
        self._handlers = [self._make_handler(name) for name in self.SIGNALS]
        for name, handler in zip(self.SIGNALS, self._handlers, strict=True):
            crawler.signals.connect(handler, getattr(signals, name))

    def _make_handler(self, name: str) -> Any:
        def handler(**kwargs: Any) -> None:
            assert self.crawler.engine is not None
            self.calls.append(
                (name, self.crawler.engine.state, self.crawler.engine.spider_state)
            )
            if name == "spider_closed":
                self.close_reasons.append(kwargs["reason"])

        return handler

    @property
    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def make_engine(crawler: Crawler) -> ExecutionEngine:
    """Create the engine of *crawler* as Crawler.crawl_async() (and scrapy
    shell) would, without starting anything."""
    crawler.spider = crawler._create_spider()
    engine = crawler.engine = crawler._create_engine()
    return engine


async def start_engine(
    engine: ExecutionEngine, *, _start_request_processing: bool = True
) -> Deferred[None]:
    """Start *engine* in the background and complete once it has been started.

    Return the Deferred of the ``start_async()`` call, which completes when
    the engine is stopped.
    """
    started: Deferred[None] = Deferred()

    def handler(**kwargs: Any) -> None:
        started.callback(None)

    engine.crawler.signals.connect(handler, signals.engine_started)
    dfd = deferred_from_coro(
        engine.start_async(_start_request_processing=_start_request_processing)
    )
    await maybe_deferred_to_future(started)
    engine.crawler.signals.disconnect(handler, signals.engine_started)
    return dfd


def assert_state(
    engine: ExecutionEngine,
    engine_state: EngineState | None = None,
    spider_state: SpiderState | None = None,
) -> None:
    if engine_state is not None:
        assert engine.state is engine_state, engine.state
    if spider_state is not None:
        assert engine.spider_state is spider_state, engine.spider_state


NORMAL_SIGNAL_ORDER = [
    "spider_opened",
    "engine_started",
    "spider_closed",
    "engine_stopped",
]


class TestNormalLifecycle:
    @coroutine_test
    async def test_state_progression(self) -> None:
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert not engine.running
        # In a normal crawl the spider opens before the engine starts.
        assert recorder.names == NORMAL_SIGNAL_ORDER
        assert recorder.calls[0][1:] == (EngineState.CREATED, SpiderState.OPENING)
        assert recorder.calls[1][1:] == (EngineState.STARTING, SpiderState.OPEN)
        assert recorder.calls[2][1:] == (EngineState.RUNNING, SpiderState.CLOSING)
        assert recorder.calls[3][1:] == (EngineState.STOPPING, SpiderState.CLOSED)
        assert crawler.stats
        assert crawler.stats.get_value("finish_reason") == "finished"

    @coroutine_test
    async def test_stop_idempotent(self) -> None:
        crawler = get_crawler(DefaultSpider)
        await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, EngineState.STOPPED)
        # None of these raises or hangs on a stopped engine.
        await engine.stop_async()
        await engine.close_async()
        await engine.close_spider_async()
        assert_state(engine, EngineState.STOPPED)


class TestShellLifecycle:
    """The inverted lifecycle used by scrapy shell: the engine starts without
    a spider, and the spider is opened while the engine is running."""

    @coroutine_test
    async def test_start_then_open(self) -> None:
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        start_dfd = await start_engine(engine, _start_request_processing=False)
        assert_state(engine, spider_state=SpiderState.NONE)
        await engine.open_spider_async(close_if_idle=False)
        assert_state(engine, EngineState.RUNNING, SpiderState.OPEN)
        # In the shell lifecycle the engine starts before the spider opens.
        assert recorder.names == ["engine_started", "spider_opened"]
        await engine.stop_async()
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == [
            "engine_started",
            "spider_opened",
            "spider_closed",
            "engine_stopped",
        ]
        await maybe_deferred_to_future(start_dfd)

    @coroutine_test
    async def test_spiderless_stop_closes_downloader(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = make_engine(crawler)
        engine.downloader.close = Mock(wraps=engine.downloader.close)  # type: ignore[method-assign]
        start_dfd = await start_engine(engine, _start_request_processing=False)
        await engine.stop_async()
        assert_state(engine, EngineState.STOPPED, SpiderState.NONE)
        # The downloader is closed even though no spider was ever opened.
        engine.downloader.close.assert_called()
        await maybe_deferred_to_future(start_dfd)


class TestEarlyClose:
    """Closes and stops requested before or while the engine starts."""

    @coroutine_test
    async def test_close_from_engine_started_handler(self) -> None:
        """A close triggered by an engine_started handler (race 1 in #6916)
        must leave the engine cleanly stopped instead of half-initialized."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)

        async def stop(**kwargs: Any) -> None:
            await crawler.stop_async()

        crawler.signals.connect(stop, signals.engine_started)
        await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == NORMAL_SIGNAL_ORDER
        assert recorder.close_reasons == ["shutdown"]

    @coroutine_test
    async def test_close_spider_before_start(self) -> None:
        """A spider close that completes before start_async() (race 2 in
        #6916, e.g. triggered by the CloseSpider or MemoryUsage extensions)
        must not leave start_async() hanging or the engine half-started."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        crawler.crawling = True  # as crawl_async() would set
        await engine.open_spider_async()
        await engine.close_spider_async(reason="early")
        assert_state(engine, EngineState.CREATED, SpiderState.CLOSED)
        assert not crawler.crawling  # the spider closed callback ran (race 3)
        # start_async() detects the closed spider and finishes the shutdown.
        await engine.start_async()
        assert_state(engine, EngineState.STOPPED)
        assert recorder.names == [
            "spider_opened",
            "spider_closed",
            "engine_started",
            "engine_stopped",
        ]
        assert recorder.close_reasons == ["early"]

    @coroutine_test
    async def test_spiderless_start(self) -> None:
        """start_async() without a spider (path 7 in #6916) must stop the
        engine cleanly instead of leaving it in an undefined state."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = ExecutionEngine(crawler, lambda _: None)
        crawler.engine = engine
        engine.downloader.close = Mock(wraps=engine.downloader.close)  # type: ignore[method-assign]
        await engine.start_async()
        assert_state(engine, EngineState.STOPPED, SpiderState.NONE)
        assert recorder.names == ["engine_started", "engine_stopped"]
        engine.downloader.close.assert_called()

    @coroutine_test
    async def test_close_in_progress_at_start(self) -> None:
        """A spider close that is still in progress when start_async() gets
        to check the spider (e.g. one scheduled by the CloseSpider extension
        while an engine_started handler was running) must not make
        start_async(), and therefore Crawler.crawl_async(), complete before
        the engine is stopped."""
        crawler = get_crawler(
            DefaultSpider, settings_dict={"SCHEDULER": BlockingScheduler}
        )
        recorder = SignalRecorder(crawler)
        close_in_progress: Deferred[None] = Deferred()
        crawl_done_at_engine_stopped: list[bool] = []
        schedulers: list[BlockingScheduler] = []

        async def engine_started(**kwargs: Any) -> None:
            engine = crawler.engine
            assert engine is not None
            scheduler = engine.scheduler
            assert isinstance(scheduler, BlockingScheduler)
            schedulers.append(scheduler)
            _schedule_coro(engine.close_spider_async(reason="early"))
            # Return once the close is under way, blocked in the scheduler.
            await maybe_deferred_to_future(scheduler.entered_close)
            close_in_progress.callback(None)

        def engine_stopped(**kwargs: Any) -> None:
            crawl_done_at_engine_stopped.append(crawl_dfd.called)

        crawler.signals.connect(engine_started, signals.engine_started)
        crawler.signals.connect(engine_stopped, signals.engine_stopped)
        crawl_dfd = deferred_from_coro(crawler.crawl_async())
        await maybe_deferred_to_future(close_in_progress)
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, spider_state=SpiderState.CLOSING)
        assert not crawl_dfd.called

        schedulers[0].unblock_close.callback(None)
        await maybe_deferred_to_future(crawl_dfd)
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == NORMAL_SIGNAL_ORDER
        assert recorder.close_reasons == ["early"]
        # The crawl completed only after the engine was stopped.
        assert crawl_done_at_engine_stopped == [False]

    @coroutine_test
    async def test_crawler_stop_before_start(self) -> None:
        """Crawler.stop_async() before the engine is started (e.g. from a
        spider_opened handler, or on Ctrl-C during a slow spider open) closes
        the spider as soon as it is open, and start_async() then finishes the
        shutdown; the crawl neither runs in full nor hangs."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)

        async def stop(**kwargs: Any) -> None:
            await crawler.stop_async()

        crawler.signals.connect(stop, signals.spider_opened)
        await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == [
            "spider_opened",
            "spider_closed",
            "engine_started",
            "engine_stopped",
        ]
        assert recorder.close_reasons == ["shutdown"]

    @coroutine_test
    async def test_crawler_stop_with_open_spider(self) -> None:
        """Crawler.stop_async() with an open spider and a never-started
        engine closes the spider."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        crawler.crawling = True  # as crawl_async() would set
        await engine.open_spider_async()
        await crawler.stop_async()
        assert_state(engine, EngineState.CREATED, SpiderState.CLOSED)
        assert recorder.names == ["spider_opened", "spider_closed"]
        assert recorder.close_reasons == ["shutdown"]
        await engine.start_async()
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)


class TestCloseDuringOpen:
    @coroutine_test
    async def test_close_spider_during_open(self) -> None:
        """A close requested while the spider is opening is deferred until
        the open finishes, keeping the spider_opened/spider_closed order."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        state_after_close_call: list[SpiderState] = []

        async def close(**kwargs: Any) -> None:
            await engine.close_spider_async(reason="early")
            state_after_close_call.append(engine.spider_state)

        crawler.signals.connect(close, signals.spider_opened)
        await engine.open_spider_async()
        # The close was deferred, not performed inline in the handler.
        assert state_after_close_call == [SpiderState.OPENING]
        assert_state(engine, spider_state=SpiderState.CLOSED)
        assert recorder.names == ["spider_opened", "spider_closed"]
        assert recorder.close_reasons == ["early"]

    @coroutine_test
    async def test_stop_during_open(self) -> None:
        """A stop requested while the spider is opening is finished once the
        spider has been opened and closed, so engine_stopped stays last."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        start_dfd = await start_engine(engine, _start_request_processing=False)

        async def stop(**kwargs: Any) -> None:
            await engine.stop_async()

        crawler.signals.connect(stop, signals.spider_opened)
        await engine.open_spider_async(close_if_idle=False)
        await maybe_deferred_to_future(start_dfd)
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == [
            "engine_started",
            "spider_opened",
            "spider_closed",
            "engine_stopped",
        ]
        assert recorder.close_reasons == ["shutdown"]

    @coroutine_test
    async def test_close_engine_during_open(self) -> None:
        """close_async() while the spider is opening defers the spider close;
        a later start_async() finishes the shutdown."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)

        async def close(**kwargs: Any) -> None:
            await engine.close_async()

        crawler.signals.connect(close, signals.spider_opened)
        await engine.open_spider_async()
        assert_state(engine, EngineState.CREATED, SpiderState.CLOSED)
        await engine.start_async()
        assert_state(engine, EngineState.STOPPED)
        assert recorder.names == [
            "spider_opened",
            "spider_closed",
            "engine_started",
            "engine_stopped",
        ]


class BlockingScheduler(BaseScheduler):
    """A scheduler whose close() blocks until unblocked, to keep the spider
    deterministically in the CLOSING state."""

    def __init__(self) -> None:
        self.entered_close: Deferred[None] = Deferred()
        self.unblock_close: Deferred[None] = Deferred()

    def has_pending_requests(self) -> bool:
        return False

    def enqueue_request(self, request: Any) -> bool:
        return True

    def next_request(self) -> Any:
        return None

    def close(self, reason: str) -> Deferred[None]:
        self.entered_close.callback(None)
        return self.unblock_close


class TestDoubleClose:
    @coroutine_test
    async def test_close_after_close(self) -> None:
        """close_spider_async() after the spider has been closed (races 4 and
        6 in #6916) returns instead of raising RuntimeError."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        await engine.close_spider_async(reason="first")
        assert_state(engine, spider_state=SpiderState.CLOSED)
        await engine.close_spider_async(reason="second")
        assert recorder.close_reasons == ["first"]

    @coroutine_test
    async def test_close_while_closing(self) -> None:
        """A concurrent close_spider_async() returns instead of raising or
        starting a second close sequence."""
        crawler = get_crawler(
            DefaultSpider, settings_dict={"SCHEDULER": BlockingScheduler}
        )
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        assert engine._slot is not None
        scheduler = engine._slot.scheduler
        assert isinstance(scheduler, BlockingScheduler)

        first = deferred_from_coro(engine.close_spider_async(reason="first"))
        # Wait until the first close is underway, blocked on scheduler close.
        await maybe_deferred_to_future(scheduler.entered_close)
        assert_state(engine, spider_state=SpiderState.CLOSING)

        await engine.close_spider_async(reason="second")
        assert_state(engine, spider_state=SpiderState.CLOSING)  # still the first close

        scheduler.unblock_close.callback(None)
        await maybe_deferred_to_future(first)
        assert_state(engine, spider_state=SpiderState.CLOSED)
        assert recorder.close_reasons == ["first"]

    @coroutine_test
    async def test_stop_while_closing(self) -> None:
        """A stop requested while the spider is closing does not wait for the
        close, but the close finishes the stop, so that engine_stopped is
        still sent after spider_closed."""
        crawler = get_crawler(
            DefaultSpider, settings_dict={"SCHEDULER": BlockingScheduler}
        )
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        start_dfd = await start_engine(engine, _start_request_processing=False)
        assert engine._slot is not None
        scheduler = engine._slot.scheduler
        assert isinstance(scheduler, BlockingScheduler)

        close_dfd = deferred_from_coro(engine.close_spider_async(reason="first"))
        await maybe_deferred_to_future(scheduler.entered_close)
        assert_state(engine, spider_state=SpiderState.CLOSING)

        await engine.stop_async()  # returns without waiting for the close
        assert_state(engine, EngineState.STOPPING, SpiderState.CLOSING)
        assert "engine_stopped" not in recorder.names

        scheduler.unblock_close.callback(None)
        await maybe_deferred_to_future(close_dfd)
        await maybe_deferred_to_future(start_dfd)
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == [
            "spider_opened",
            "engine_started",
            "spider_closed",
            "engine_stopped",
        ]
        assert recorder.close_reasons == ["first"]

    @coroutine_test
    async def test_stop_from_spider_closed_handler(self) -> None:
        """Stopping the crawler from a spider_closed handler, i.e. from code
        that the close sequence awaits, must not deadlock."""
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)

        async def stop(**kwargs: Any) -> None:
            await crawler.stop_async()

        crawler.signals.connect(stop, signals.spider_closed)
        await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == NORMAL_SIGNAL_ORDER


class TestInvalidTransitions:
    @coroutine_test
    async def test_stop_not_started(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = ExecutionEngine(crawler, lambda _: None)
        with pytest.raises(RuntimeError, match="Engine not running"):
            await engine.stop_async()
        engine.downloader.close()  # cleanup

    @coroutine_test
    async def test_double_open(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        with pytest.raises(RuntimeError, match="No free spider slot"):
            await engine.open_spider_async()
        await engine.close_spider_async()

    @coroutine_test
    async def test_open_after_stop(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = make_engine(crawler)
        await engine.close_async()
        assert_state(engine, EngineState.STOPPED)
        with pytest.raises(RuntimeError, match="engine has already been stopped"):
            await engine.open_spider_async()

    @coroutine_test
    async def test_close_spider_never_opened(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = ExecutionEngine(crawler, lambda _: None)
        with pytest.raises(RuntimeError, match="Spider not opened"):
            await engine.close_spider_async()
        engine.downloader.close()  # cleanup


class BrokenScheduler(BlockingScheduler):
    @classmethod
    def from_crawler(cls, crawler: Crawler) -> BrokenScheduler:
        raise ValueError("broken scheduler")


class TestOpenFailure:
    @coroutine_test
    async def test_scheduler_creation_error(self) -> None:
        """An error while building the scheduler is reported as is, instead
        of being masked by the error of closing a spider that never got a
        slot, and leaves the spider unopened."""
        crawler = get_crawler(
            DefaultSpider, settings_dict={"SCHEDULER": BrokenScheduler}
        )
        recorder = SignalRecorder(crawler)
        with pytest.raises(ValueError, match="broken scheduler"):
            await crawler.crawl_async()
        engine = crawler.engine
        assert engine is not None
        assert_state(engine, EngineState.STOPPED, SpiderState.NONE)
        assert recorder.names == []


class TestCloseAsync:
    """close_async() must work, and clean everything up, in every state."""

    @coroutine_test
    async def test_created_no_spider(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = ExecutionEngine(crawler, lambda _: None)
        engine.downloader.close = Mock(wraps=engine.downloader.close)  # type: ignore[method-assign]
        await engine.close_async()
        assert_state(engine, EngineState.STOPPED)
        engine.downloader.close.assert_called()

    @coroutine_test
    async def test_created_open_spider(self) -> None:
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        await engine.close_async()
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == ["spider_opened", "spider_closed"]
        assert recorder.close_reasons == ["shutdown"]

    @coroutine_test
    async def test_created_closed_spider(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        await engine.close_spider_async(reason="early")
        await engine.close_async()
        assert_state(engine, EngineState.STOPPED)

    @coroutine_test
    async def test_created_closing_spider(self) -> None:
        """close_async() while the spider is closing leaves the cleanup to
        that close instead of waiting for it."""
        crawler = get_crawler(
            DefaultSpider, settings_dict={"SCHEDULER": BlockingScheduler}
        )
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        assert engine._slot is not None
        scheduler = engine._slot.scheduler
        assert isinstance(scheduler, BlockingScheduler)

        close_dfd = deferred_from_coro(engine.close_spider_async(reason="first"))
        await maybe_deferred_to_future(scheduler.entered_close)
        await engine.close_async()
        assert_state(engine, spider_state=SpiderState.CLOSING)

        scheduler.unblock_close.callback(None)
        await maybe_deferred_to_future(close_dfd)
        assert_state(engine, spider_state=SpiderState.CLOSED)
        assert recorder.close_reasons == ["first"]

    @coroutine_test
    async def test_running(self) -> None:
        crawler = get_crawler(DefaultSpider)
        recorder = SignalRecorder(crawler)
        engine = make_engine(crawler)
        await engine.open_spider_async()
        start_dfd = await start_engine(engine)
        await engine.close_async()
        assert_state(engine, EngineState.STOPPED, SpiderState.CLOSED)
        assert recorder.names == NORMAL_SIGNAL_ORDER
        assert recorder.close_reasons == ["shutdown"]
        await maybe_deferred_to_future(start_dfd)


class TestDeprecated:
    @coroutine_test
    async def test_running_setter(self) -> None:
        crawler = get_crawler(DefaultSpider)
        engine = ExecutionEngine(crawler, lambda _: None)
        with pytest.warns(
            ScrapyDeprecationWarning,
            match="Setting ExecutionEngine.running is deprecated",
        ):
            engine.running = True
        assert engine.running is False  # setting it has no effect
        engine.downloader.close()  # cleanup
