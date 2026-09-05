"""
This is the Scrapy engine which controls the Scheduler, Downloader and Spider.

For more information see docs/topics/architecture.rst

"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import warnings
from enum import Enum
from functools import partial
from time import time
from traceback import format_exc
from typing import TYPE_CHECKING, Any

from twisted.internet.defer import CancelledError, Deferred, inlineCallbacks
from twisted.python.failure import Failure

from scrapy import signals
from scrapy.core.scheduler import BaseScheduler
from scrapy.core.scraper import Scraper
from scrapy.exceptions import (
    CloseSpider,
    DontCloseSpider,
    DownloadCancelledError,
    IgnoreRequest,
    ScrapyDeprecationWarning,
)
from scrapy.http import Request, Response
from scrapy.utils._stopmode import _max_stop_mode, _normalize_stop_mode, _StopMode
from scrapy.utils.asyncio import (
    AsyncioLoopingCall,
    create_looping_call,
    is_asyncio_available,
)
from scrapy.utils.defer import (
    _schedule_coro,
    deferred_from_coro,
    ensure_awaitable,
    maybe_deferred_to_future,
)
from scrapy.utils.deprecate import argument_is_required
from scrapy.utils.log import failure_to_exc_info, logformatter_adapter
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.python import global_object_name
from scrapy.utils.reactor import CallLaterOnce

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Generator

    from twisted.internet.task import LoopingCall

    from scrapy.core.downloader import Downloader
    from scrapy.crawler import Crawler
    from scrapy.logformatter import LogFormatter
    from scrapy.settings import BaseSettings, Settings
    from scrapy.signalmanager import SignalManager
    from scrapy.spiders import Spider


logger = logging.getLogger(__name__)


class EngineState(Enum):
    """The engine-level run state of :class:`ExecutionEngine`.

    .. versionadded:: VERSION
    """

    #: The engine has been created but :meth:`ExecutionEngine.start_async` has
    #: not been called yet.
    CREATED = "created"

    #: :meth:`ExecutionEngine.start_async` is running, the
    #: :signal:`engine_started` signal may be in flight.
    STARTING = "starting"

    #: The engine is running.
    RUNNING = "running"

    #: The engine is shutting down, the :signal:`engine_stopped` signal has
    #: not been sent yet.
    STOPPING = "stopping"

    #: The engine has been stopped and cannot be restarted.
    STOPPED = "stopped"


class SpiderState(Enum):
    """The spider lifecycle state of :class:`ExecutionEngine`.

    It is mostly independent from :class:`EngineState`: in a normal crawl the
    spider is opened before the engine is started, while :command:`shell`
    starts the engine first and opens a spider on the first ``fetch()``.

    .. versionadded:: VERSION
    """

    #: No spider has been opened yet.
    NONE = "none"

    #: :meth:`ExecutionEngine.open_spider_async` is running.
    OPENING = "opening"

    #: The spider is open.
    OPEN = "open"

    #: :meth:`ExecutionEngine.close_spider_async` is running.
    CLOSING = "closing"

    #: The spider has been closed; the engine is single-use, so no other
    #: spider can be opened.
    CLOSED = "closed"


_ENGINE_STATE_TRANSITIONS: dict[EngineState, frozenset[EngineState]] = {
    EngineState.CREATED: frozenset({EngineState.STARTING, EngineState.STOPPED}),
    EngineState.STARTING: frozenset({EngineState.RUNNING, EngineState.STOPPING}),
    EngineState.RUNNING: frozenset({EngineState.STOPPING}),
    EngineState.STOPPING: frozenset({EngineState.STOPPED}),
    EngineState.STOPPED: frozenset(),
}

_SPIDER_STATE_TRANSITIONS: dict[SpiderState, frozenset[SpiderState]] = {
    SpiderState.NONE: frozenset({SpiderState.OPENING}),
    SpiderState.OPENING: frozenset({SpiderState.OPEN, SpiderState.CLOSING}),
    SpiderState.OPEN: frozenset({SpiderState.CLOSING}),
    SpiderState.CLOSING: frozenset({SpiderState.CLOSED}),
    SpiderState.CLOSED: frozenset(),
}


class _Slot:
    def __init__(
        self,
        close_if_idle: bool,
        nextcall: CallLaterOnce[None],
        scheduler: BaseScheduler,
    ) -> None:
        self.closing: Deferred[None] | None = None
        self.inprogress: set[Request] = set()
        self.close_if_idle: bool = close_if_idle
        self.nextcall: CallLaterOnce[None] = nextcall
        self.scheduler: BaseScheduler = scheduler
        self.heartbeat: AsyncioLoopingCall | LoopingCall = create_looping_call(
            nextcall.schedule
        )

    def add_request(self, request: Request) -> None:
        self.inprogress.add(request)

    def remove_request(self, request: Request) -> None:
        self.inprogress.remove(request)
        self._maybe_fire_closing()

    async def close(self) -> None:
        self.closing = Deferred()
        self._maybe_fire_closing()
        await maybe_deferred_to_future(self.closing)

    def _maybe_fire_closing(self) -> None:
        if self.closing is not None and not self.inprogress:
            self.nextcall.cancel()
            if self.heartbeat.running:
                self.heartbeat.stop()
            self.closing.callback(None)


class ExecutionEngine:
    _SLOT_HEARTBEAT_INTERVAL: float = 5.0

    def __init__(
        self,
        crawler: Crawler,
        spider_closed_callback: Callable[
            [Spider], Coroutine[Any, Any, None] | Deferred[None] | None
        ],
    ) -> None:
        self.crawler: Crawler = crawler
        self.settings: Settings = crawler.settings
        self.signals: SignalManager = crawler.signals
        self.logformatter: LogFormatter = crawler.logformatter
        self._slot: _Slot | None = None
        self.spider: Spider | None = None
        self._engine_state: EngineState = EngineState.CREATED
        self._spider_state: SpiderState = SpiderState.NONE
        #: Reason for a spider close requested while the spider was still
        #: opening; consumed at the end of open_spider_async().
        self._pending_close_reason: str | None = None
        #: Whether a stop was requested while the spider was still opening or
        #: closing; consumed at the end of close_spider_async().
        self._pending_stop: bool = False
        #: How to stop, orthogonal to the lifecycle state: escalated by
        #: stop_async() and close_spider_async(), never lowered.
        self._stop_mode: _StopMode = "graceful"
        self._downloader_fast_stopped: bool = False
        self.paused: bool = False
        self._spider_closed_callback: Callable[
            [Spider], Coroutine[Any, Any, None] | Deferred[None] | None
        ] = spider_closed_callback
        self.start_time: float | None = None
        self._start: AsyncIterator[Any] | None = None
        # Whether Spider.start() raised, i.e. some start items or requests may
        # never have reached the engine.
        self._start_error: bool = False
        self._closewait: Deferred[None] | None = None
        self._start_request_processing_awaitable: (
            asyncio.Future[None] | Deferred[None] | None
        ) = None
        downloader_cls: type[Downloader] = load_object(self.settings["DOWNLOADER"])
        try:
            self.scheduler_cls: type[BaseScheduler] = self._get_scheduler_class(
                crawler.settings
            )
            self.downloader: Downloader = downloader_cls(crawler)
            self._downloader_fetch_needs_spider: bool = argument_is_required(
                self.downloader.fetch, "spider"
            )
            if self._downloader_fetch_needs_spider:
                warnings.warn(
                    f"The fetch() method of {global_object_name(downloader_cls)} requires a spider argument,"
                    f" this is deprecated and the argument will not be passed in future Scrapy versions.",
                    ScrapyDeprecationWarning,
                    stacklevel=2,
                )

            self.scraper: Scraper = Scraper(crawler)
        except Exception:
            if hasattr(self, "downloader"):
                self.downloader.close()
            raise

    def _get_scheduler_class(self, settings: BaseSettings) -> type[BaseScheduler]:
        scheduler_cls: type[BaseScheduler] = load_object(settings["SCHEDULER"])
        if not issubclass(scheduler_cls, BaseScheduler):
            raise TypeError(
                f"The provided scheduler class ({settings['SCHEDULER']})"
                " does not fully implement the scheduler interface"
            )
        return scheduler_cls

    @property
    def scheduler(self) -> BaseScheduler | None:
        """The scheduler in use, or ``None`` before the spider has started.

        .. versionadded:: 2.19.0
        """
        return self._slot.scheduler if self._slot is not None else None

    @property
    def state(self) -> EngineState:
        """The current engine run state, an :class:`EngineState` value.

        .. versionadded:: VERSION
        """
        return self._engine_state

    @property
    def spider_state(self) -> SpiderState:
        """The current spider lifecycle state, a :class:`SpiderState` value.

        .. versionadded:: VERSION
        """
        return self._spider_state

    @property
    def running(self) -> bool:
        """Whether the engine is in the :attr:`EngineState.RUNNING` state.

        .. versionchanged:: VERSION
            Became a read-only property derived from :attr:`state`; setting it
            is deprecated and has no effect.
        """
        return self._engine_state is EngineState.RUNNING

    @running.setter
    def running(self, value: bool) -> None:
        warnings.warn(
            "Setting ExecutionEngine.running is deprecated and has no effect,"
            " the engine state is managed internally.",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )

    def _transition_to(
        self,
        engine_state: EngineState | None = None,
        spider_state: SpiderState | None = None,
    ) -> None:
        """Set the engine and/or spider state, validating the transitions.

        An invalid transition is a bug: it is logged rather than raised, to
        avoid breaking a running crawl, and the state is set anyway.
        """
        if engine_state is not None:
            if engine_state not in _ENGINE_STATE_TRANSITIONS[self._engine_state]:
                logger.warning(
                    "Invalid engine state transition: %(old)s -> %(new)s",
                    {"old": self._engine_state.name, "new": engine_state.name},
                    stack_info=True,
                )
            self._engine_state = engine_state
        if spider_state is not None:
            if spider_state not in _SPIDER_STATE_TRANSITIONS[self._spider_state]:
                logger.warning(
                    "Invalid spider state transition: %(old)s -> %(new)s",
                    {"old": self._spider_state.name, "new": spider_state.name},
                    stack_info=True,
                )
            self._spider_state = spider_state

    def start(
        self, _start_request_processing: bool = True
    ) -> Deferred[None]:  # pragma: no cover
        warnings.warn(
            "ExecutionEngine.start() is deprecated, use start_async() instead",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return deferred_from_coro(
            self.start_async(_start_request_processing=_start_request_processing)
        )

    async def start_async(self, *, _start_request_processing: bool = True) -> None:
        """Start the execution engine and complete when it is stopped.

        Raise :exc:`RuntimeError` if the engine has already been started. If
        the spider has already been closed, or was never opened (and request
        processing was not disabled, as done by :command:`shell`), send the
        :signal:`engine_started` signal, finish the shutdown of the engine and
        return.

        .. versionadded:: 2.14
        """
        if self._engine_state is not EngineState.CREATED:
            raise RuntimeError("Engine already running")
        self.start_time = time()
        self._transition_to(engine_state=EngineState.STARTING)
        # Fired by _finish_stop(). Created before anything can stop the engine,
        # so that this method never completes before the engine is stopped,
        # whichever path the stop takes.
        self._closewait = Deferred()
        await self.signals.send_catch_log_async(signal=signals.engine_started)
        # An engine_started handler may have stopped the engine already.
        if self.state is EngineState.STARTING:
            if _start_request_processing and self._spider_state is not SpiderState.OPEN:
                # The spider was never opened, or was closed or is being closed
                # before the engine could start; finish the shutdown instead of
                # running.
                await self._stop()
            else:
                self._transition_to(engine_state=EngineState.RUNNING)
                if _start_request_processing:
                    coro = self._start_request_processing()
                    if is_asyncio_available():
                        # not wrapping in a Deferred here to avoid https://github.com/twisted/twisted/issues/12470
                        # (can happen when this is cancelled, e.g. in test_close_during_start_iteration())
                        self._start_request_processing_awaitable = (
                            asyncio.ensure_future(coro)
                        )
                    else:
                        self._start_request_processing_awaitable = (
                            Deferred.fromCoroutine(coro)
                        )
        with contextlib.suppress(asyncio.exceptions.CancelledError):
            await maybe_deferred_to_future(self._closewait)

    def stop(
        self, *, mode: _StopMode = "graceful"
    ) -> Deferred[None]:  # pragma: no cover
        warnings.warn(
            "ExecutionEngine.stop() is deprecated, use stop_async() instead",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return deferred_from_coro(self.stop_async(mode=mode))

    async def stop_async(self, *, mode: _StopMode = "graceful") -> None:
        """Gracefully stop the execution engine.

        Raise :exc:`RuntimeError` if the engine was never started. Return
        immediately if the engine is already stopping or stopped.

        If the spider is opening or closing, the stop is finished by
        :meth:`open_spider_async` or :meth:`close_spider_async`, so that
        :signal:`engine_stopped` is still sent after :signal:`spider_closed`,
        and this method returns without waiting for that. Awaiting
        :meth:`start_async` (or :meth:`Crawler.crawl_async
        <scrapy.crawler.Crawler.crawl_async>`) completes when the engine has
        stopped.

        .. versionadded:: 2.14

        .. versionchanged:: VERSION
            Calling it while the engine is stopping or stopped is no longer an
            error.
        """
        mode = _normalize_stop_mode(mode, allow_force=False)
        if self._engine_state is EngineState.CREATED:
            raise RuntimeError("Engine not running")
        self._stop_mode = _max_stop_mode(self._stop_mode, mode)
        if self._engine_state in (EngineState.STOPPING, EngineState.STOPPED):
            # The stop already under way is not waited for here (see _stop()),
            # but a fast stop can still drop the in-flight downloads that the
            # spider close in progress may be waiting for.
            if self._stop_mode == "fast" and self._spider_state in (
                SpiderState.OPEN,
                SpiderState.CLOSING,
            ):
                await self._fast_stop_downloader()
            return
        await self._stop()

    async def _stop(self) -> None:
        """Run the stop sequence, transitioning from STARTING or RUNNING to
        STOPPING and then, unless the spider lifecycle needs to finish first,
        to STOPPED."""
        self._transition_to(engine_state=EngineState.STOPPING)
        if self._start_request_processing_awaitable is not None:
            if (
                not is_asyncio_available()
                or self._start_request_processing_awaitable
                is not asyncio.current_task()
            ):
                # If using the asyncio loop and stop_async() was called from
                # start() itself, we can't cancel it, and _start_request_processing()
                # will exit via the self.running check.
                self._start_request_processing_awaitable.cancel()
            self._start_request_processing_awaitable = None
        if self._spider_state in (SpiderState.NONE, SpiderState.CLOSED):
            if self._spider_state is SpiderState.NONE:
                # No spider was ever opened (e.g. a spider-less start in
                # scrapy shell), so nothing closed the downloader.
                self.downloader.close()
            await self._finish_stop()
            return
        # The rest of the stop sequence must run after the spider is closed,
        # and is left to close_spider_async(): waiting here for a close that
        # is already in progress, or for the spider to finish opening so that
        # it can be closed, would deadlock when this method is called from
        # code that the close sequence itself awaits, e.g. a spider_closed
        # handler. If the spider is open, this call closes it and finishes the
        # stop; if it is opening or closing, this call returns right away.
        self._pending_stop = True
        await self.close_spider_async(reason="shutdown")

    async def _finish_stop(self) -> None:
        """Finish the stop sequence, transitioning from STOPPING to STOPPED."""
        self._pending_stop = False
        await self.signals.send_catch_log_async(signal=signals.engine_stopped)
        # Transition before firing _closewait: without a reactor-independent
        # scheduling hop, firing it resumes start_async() synchronously.
        self._transition_to(engine_state=EngineState.STOPPED)
        if self._closewait:
            self._closewait.callback(None)

    def close(self) -> Deferred[None]:  # pragma: no cover
        warnings.warn(
            "ExecutionEngine.close() is deprecated, use close_async() instead",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return deferred_from_coro(self.close_async())

    async def close_async(self, *, reason: str = "shutdown") -> None:
        """
        Gracefully close the execution engine.
        If it has already been started, stop it. In all cases, close the spider and the downloader.
        """
        if self._engine_state in (EngineState.STARTING, EngineState.RUNNING):
            await self.stop_async()  # will also close spider and downloader
            return
        if self._engine_state in (EngineState.STOPPING, EngineState.STOPPED):
            # Already being taken care of by stop_async().
            return
        # EngineState.CREATED: the engine was never started, so there is no
        # stop sequence to run; close the spider and/or the downloader directly.
        if self._spider_state in (SpiderState.OPENING, SpiderState.CLOSING):
            # The close is deferred until the spider is open, or is already in
            # progress; either way it will close the downloader. The engine
            # stays CREATED, so that a later start_async() (e.g. from
            # Crawler.crawl_async()) can still finish the shutdown.
            await self.close_spider_async(reason=reason)
            return
        if self._spider_state is SpiderState.OPEN:
            await self.close_spider_async(reason=reason)  # will also close downloader
        else:
            # SpiderState.NONE, or SpiderState.CLOSED (in which case the
            # downloader has already been closed, but closing it again is a
            # cheap no-op).
            self.downloader.close()
        self._transition_to(engine_state=EngineState.STOPPED)

    def pause(self) -> None:
        self.paused = True

    def unpause(self) -> None:
        self.paused = False

    async def _process_start_next(self) -> None:
        """Processes the next item or request from Spider.start().

        If a request, it is scheduled. If an item, it is sent to item
        pipelines.
        """
        assert self._start is not None
        try:
            item_or_request = await anext(self._start)
        except StopAsyncIteration:
            self._start = None
        except CloseSpider as exception:
            self._start = None
            _schedule_coro(
                self.close_spider_async(reason=exception.reason or "cancelled")
            )
        except Exception as exception:
            self._start = None
            self._start_error = True
            exception_traceback = format_exc()
            logger.error(
                f"Error while reading start items and requests: {exception}.\n{exception_traceback}",
                exc_info=True,
            )
            self.signals.send_catch_log(
                signal=signals.spider_error,
                failure=Failure(),
                response=None,
                spider=self.spider,
            )
            self.crawler.stats.inc_value("spider_exceptions/count")
            self.crawler.stats.inc_value(
                f"spider_exceptions/{type(exception).__name__}"
            )
        else:
            if not self.spider:
                return  # spider already closed
            if isinstance(item_or_request, Request):
                self.crawl(item_or_request)
            else:
                assert self._slot is not None
                self.scraper._start_itemproc_nowait(item_or_request)
                self._slot.nextcall.schedule()

    async def _start_request_processing(self) -> None:
        """Starts consuming Spider.start() output and sending scheduled
        requests."""
        # Starts the processing of scheduled requests, as well as a periodic
        # call to that processing method for scenarios where the scheduler
        # reports having pending requests but returns none.
        try:
            assert self._slot is not None  # typing
            self._slot.nextcall.schedule()
            self._slot.heartbeat.start(self._SLOT_HEARTBEAT_INTERVAL)

            while self._start and self.spider and self.running:
                await self._process_start_next()
                if not self.needs_backout():
                    # Give room for the outcome of self._process_start_next() to be
                    # processed before continuing with the next iteration.
                    self._slot.nextcall.schedule()
                    await self._slot.nextcall.wait()
        except (asyncio.exceptions.CancelledError, CancelledError):
            # self.stop_async() has cancelled us, nothing to do
            return
        except Exception:
            # an error happened, log it and stop the engine
            self._start_request_processing_awaitable = None
            logger.error(
                "Error while processing requests from start()",
                exc_info=True,
                extra={"spider": self.spider},
            )
            await self.stop_async()

    def _start_scheduled_requests(self) -> None:
        if self._spider_state is not SpiderState.OPEN or self.paused:
            return
        assert self._slot is not None  # typing

        while not self.needs_backout():
            if not self._start_scheduled_request():
                break

        if self.spider_is_idle() and self._slot.close_if_idle:
            self._spider_idle()

    def needs_backout(self) -> bool:
        """Returns ``True`` if no more requests can be sent at the moment, or
        ``False`` otherwise.

        See :ref:`start-requests-lazy` for an example.
        """
        assert self.scraper.slot is not None  # typing
        return (
            not self.running
            or not self._slot
            or bool(self._slot.closing)
            or self.downloader.needs_backout()
            or self.scraper.slot.needs_backout()
        )

    def _remove_request(self, _: Any, request: Request) -> None:
        assert self._slot
        self._slot.remove_request(request)

    def _start_scheduled_request(self) -> bool:
        assert self._slot is not None  # typing
        assert self.spider is not None  # typing

        request = self._slot.scheduler.next_request()
        if request is None:
            self.signals.send_catch_log(signals.scheduler_empty)
            return False

        d: Deferred[Response | Request] = self._download(request)
        d.addBoth(self._handle_downloader_output, request)
        d.addErrback(
            lambda f: logger.info(
                "Error while handling downloader output",
                exc_info=failure_to_exc_info(f),
                extra={"spider": self.spider},
            )
        )

        d2: Deferred[None] = d.addBoth(partial(self._remove_request, request=request))
        d2.addErrback(
            lambda f: logger.info(
                "Error while removing request from slot",
                exc_info=failure_to_exc_info(f),
                extra={"spider": self.spider},
            )
        )
        slot = self._slot
        d2.addBoth(lambda _: slot.nextcall.schedule())
        d2.addErrback(
            lambda f: logger.info(
                "Error while scheduling new request",
                exc_info=failure_to_exc_info(f),
                extra={"spider": self.spider},
            )
        )
        return True

    @inlineCallbacks
    def _handle_downloader_output(
        self, result: Request | Response | Failure, request: Request
    ) -> Generator[Deferred[Any], Any, None]:
        if (
            isinstance(result, Failure)
            and self._stop_mode == "fast"
            and result.check(
                DownloadCancelledError,
                CancelledError,
                asyncio.exceptions.CancelledError,
            )
        ):
            return

        # downloader middleware can return requests (for example, redirects)
        if isinstance(result, Request):
            self.crawl(result)
            return

        try:
            yield self.scraper.enqueue_scrape(result, request)
        except Exception:
            assert self.spider is not None
            logger.error(
                "Error while enqueuing scrape",
                exc_info=True,
                extra={"spider": self.spider},
            )

    def spider_is_idle(self) -> bool:
        if self._slot is None:
            raise RuntimeError("Engine slot not assigned")
        if not self.scraper.slot.is_idle():  # type: ignore[union-attr]
            return False
        if self.downloader.active:  # downloader has pending requests
            return False
        if self._start is not None:  # not all start requests are handled
            return False
        return not self._slot.scheduler.has_pending_requests()

    def crawl(self, request: Request) -> None:
        """Inject the request into the spider <-> downloader pipeline"""
        if self.spider is None:
            raise RuntimeError(f"No open spider to crawl: {request}")
        self._schedule_request(request)
        self._slot.nextcall.schedule()  # type: ignore[union-attr]

    def _schedule_request(self, request: Request) -> None:
        request_scheduled_result = self.signals.send_catch_log(
            signals.request_scheduled,
            request=request,
            spider=self.spider,
            dont_log=IgnoreRequest,
        )
        for _, result in request_scheduled_result:
            if isinstance(result, Failure) and isinstance(result.value, IgnoreRequest):
                return
        if not self._slot.scheduler.enqueue_request(request):  # type: ignore[union-attr]
            self.signals.send_catch_log(
                signals.request_dropped, request=request, spider=self.spider
            )

    def download(self, request: Request) -> Deferred[Response]:
        """Return a Deferred which fires with a Response as result, only downloader middlewares are applied"""
        warnings.warn(
            "ExecutionEngine.download() is deprecated, use download_async() instead",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return deferred_from_coro(self.download_async(request))

    async def download_async(self, request: Request) -> Response:
        """Return a coroutine which fires with a Response as result.

         Only downloader middlewares are applied.

        .. versionadded:: 2.14
        """
        if self.spider is None:
            raise RuntimeError(f"No open spider to crawl: {request}")
        while True:
            try:
                response_or_request = await maybe_deferred_to_future(
                    self._download(request)
                )
            finally:
                assert self._slot is not None
                self._slot.remove_request(request)
            if not isinstance(response_or_request, Request):
                return response_or_request
            request = response_or_request

    @inlineCallbacks
    def _download(
        self, request: Request
    ) -> Generator[Deferred[Any], Any, Response | Request]:
        assert self._slot is not None  # typing
        assert self.spider is not None

        self._slot.add_request(request)
        try:
            result: Response | Request
            if self._downloader_fetch_needs_spider:
                result = yield self.downloader.fetch(request, self.spider)
            else:
                result = yield self.downloader.fetch(request)
            if not isinstance(result, (Response, Request)):
                raise TypeError(
                    f"Incorrect type: expected Response or Request, got {type(result)}: {result!r}"
                )
            if isinstance(result, Response):
                if result.request is None:
                    result.request = request
                logkws = self.logformatter.crawled(result.request, result, self.spider)
                if logkws is not None:
                    logger.log(
                        *logformatter_adapter(logkws), extra={"spider": self.spider}
                    )
                self.signals.send_catch_log(
                    signal=signals.response_received,
                    response=result,
                    request=result.request,
                    spider=self.spider,
                )
            return result
        finally:
            self._slot.nextcall.schedule()

    def open_spider(
        self, spider: Spider, close_if_idle: bool = True
    ) -> Deferred[None]:  # pragma: no cover
        warnings.warn(
            "ExecutionEngine.open_spider() is deprecated, use open_spider_async() instead",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return deferred_from_coro(self.open_spider_async(close_if_idle=close_if_idle))

    async def open_spider_async(self, *, close_if_idle: bool = True) -> None:
        assert self.crawler.spider
        if self._spider_state is not SpiderState.NONE:
            raise RuntimeError(
                f"No free spider slot when opening {self.crawler.spider.name!r}"
            )
        if self._engine_state in (EngineState.STOPPING, EngineState.STOPPED):
            raise RuntimeError(
                f"Cannot open spider {self.crawler.spider.name!r}:"
                f" the engine has already been stopped"
            )
        # Build the scheduler before anything is opened: if that fails, there
        # is nothing to close, and the spider stays unopened.
        scheduler = build_from_crawler(self.scheduler_cls, self.crawler)
        self._transition_to(spider_state=SpiderState.OPENING)
        logger.info("Spider opened", extra={"spider": self.crawler.spider})
        self.spider = self.crawler.spider
        nextcall = CallLaterOnce(self._start_scheduled_requests)
        self._slot = _Slot(close_if_idle, nextcall, scheduler)
        try:
            # A component that fails to start can ask for the spider to be closed.
            # The rest of the startup runs anyway, so that components that are
            # started also get stopped, and the request is honored once the spider
            # is open.
            close_spider_exc: CloseSpider | None = None
            try:
                self._start = await self.scraper.spidermw.process_start()
                if hasattr(scheduler, "open") and (
                    d := scheduler.open(self.crawler.spider)
                ):
                    await maybe_deferred_to_future(d)
                await self.scraper.open_spider_async()
            except CloseSpider as exc:
                close_spider_exc = exc
            stats = self.crawler.stats
            if argument_is_required(stats.open_spider, "spider"):
                warnings.warn(
                    f"The open_spider() method of {global_object_name(type(stats))} requires a spider argument,"
                    f" this is deprecated and the argument will not be passed in future Scrapy versions.",
                    ScrapyDeprecationWarning,
                    stacklevel=2,
                )
                stats.open_spider(spider=self.crawler.spider)
            else:
                stats.open_spider()
            results = await self.signals.send_catch_log_async(
                signals.spider_opened, spider=self.crawler.spider, dont_log=CloseSpider
            )
            for _, result in results:
                if isinstance(result, CloseSpider):
                    close_spider_exc = close_spider_exc or result
            if close_spider_exc is not None:
                raise close_spider_exc
        except BaseException:
            # Consider the partially opened spider open, so that
            # close_spider_async() cleans it up (its per-step error handling
            # tolerates the parts that never got to open).
            self._transition_to(spider_state=SpiderState.OPEN)
            await self._close_spider_if_pending()
            raise
        self._transition_to(spider_state=SpiderState.OPEN)
        await self._close_spider_if_pending()

    async def _close_spider_if_pending(self) -> None:
        """Perform a close requested while the spider was opening."""
        if self._pending_close_reason is None:
            return
        reason = self._pending_close_reason
        self._pending_close_reason = None
        await self.close_spider_async(reason=reason)

    def _spider_idle(self) -> None:
        """
        Called when a spider gets idle, i.e. when there are no remaining requests to download or schedule.
        It can be called multiple times. If a handler for the spider_idle signal raises a DontCloseSpider
        exception, the spider is not closed until the next loop and this function is guaranteed to be called
        (at least) once again. A handler can raise CloseSpider to provide a custom closing reason.
        """
        assert self.spider is not None  # typing
        expected_ex = (DontCloseSpider, CloseSpider)
        res = self.signals.send_catch_log(
            signals.spider_idle, spider=self.spider, dont_log=expected_ex
        )
        detected_ex = {
            ex: x.value
            for _, x in res
            for ex in expected_ex
            if isinstance(x, Failure) and isinstance(x.value, ex)
        }
        if DontCloseSpider in detected_ex:
            return
        if self.spider_is_idle():
            default_reason = "start_error" if self._start_error else "finished"
            ex = detected_ex.get(CloseSpider, CloseSpider(reason=default_reason))
            assert isinstance(ex, CloseSpider)  # typing
            _schedule_coro(self.close_spider_async(reason=ex.reason))

    def close_spider(
        self,
        spider: Spider,
        reason: str = "cancelled",
        mode: _StopMode = "graceful",
    ) -> Deferred[None]:  # pragma: no cover
        warnings.warn(
            "ExecutionEngine.close_spider() is deprecated, use close_spider_async() instead",
            ScrapyDeprecationWarning,
            stacklevel=2,
        )
        return deferred_from_coro(self.close_spider_async(reason=reason, mode=mode))

    async def _fast_stop_downloader(self) -> None:
        if self._downloader_fast_stopped:
            return

        self._downloader_fast_stopped = True
        if not hasattr(self.downloader, "stop"):
            logger.warning(
                f"{type(self.downloader).__qualname__} does not implement "
                f"stop(), so pending downloads cannot be dropped and will be "
                f"finished before the spider closes",
                extra={"spider": self.spider},
            )
            return
        dropped_count = await self.downloader.stop()

        assert self.crawler.stats
        if dropped_count:
            self.crawler.stats.inc_value(
                "downloader/request_dropped_count", dropped_count
            )

        logger.info(
            "Fast shutdown dropped %(count)d downloader requests",
            {"count": dropped_count},
            extra={"spider": self.spider},
        )

    # pylint: disable=too-many-statements
    async def close_spider_async(  # noqa: PLR0912, PLR0915
        self,
        *,
        reason: str = "cancelled",
        mode: _StopMode = "graceful",
    ) -> None:
        """Close (cancel) spider and clear all its outstanding requests.

        Raise :exc:`RuntimeError` if no spider was ever opened. If the spider
        is already closed, or is already closing, return immediately. If the
        spider is still opening, ask :meth:`open_spider_async` to close it
        once it finishes opening, and return without waiting for that.

        .. versionadded:: 2.14

        .. versionchanged:: VERSION
            Calling it while the spider is closing, or after it has been
            closed, is no longer an error.
        """
        mode = _normalize_stop_mode(mode, allow_force=False)
        if self._spider_state is SpiderState.NONE:
            raise RuntimeError("Spider not opened")
        self._stop_mode = _max_stop_mode(self._stop_mode, mode)
        if self._spider_state is SpiderState.CLOSED:
            return
        if self._spider_state is SpiderState.CLOSING:
            # The close in progress is not waited for here (see _stop()), but
            # a fast stop can still drop the in-flight downloads that it may
            # be waiting for.
            if self._stop_mode == "fast":
                await self._fast_stop_downloader()
            return
        if self._spider_state is SpiderState.OPENING:
            # Cannot close the spider right now and cannot wait for it to
            # finish opening (open_spider_async() may be blocked on code that
            # awaits this method); ask open_spider_async() to close it.
            if self._pending_close_reason is None:
                self._pending_close_reason = reason
            return

        # SpiderState.OPEN
        if self._slot is None:
            raise RuntimeError("Engine slot not assigned")

        self._transition_to(spider_state=SpiderState.CLOSING)

        assert self.spider is not None  # typing
        spider = self.spider

        logger.info(
            "Closing spider (%(reason)s)", {"reason": reason}, extra={"spider": spider}
        )

        if self._stop_mode == "fast":
            await self._fast_stop_downloader()

        try:
            await self._slot.close()
        except Exception:
            logger.error("Slot close failure", exc_info=True, extra={"spider": spider})

        try:
            self.downloader.close()
        except Exception:
            logger.error(
                "Downloader close failure", exc_info=True, extra={"spider": spider}
            )

        try:
            await self.scraper.close_spider_async()
        except Exception:
            logger.error(
                "Scraper close failure", exc_info=True, extra={"spider": spider}
            )

        if hasattr(self._slot.scheduler, "close"):
            try:
                if (d := self._slot.scheduler.close(reason)) is not None:
                    await maybe_deferred_to_future(d)
            except Exception:
                logger.error(
                    "Scheduler close failure", exc_info=True, extra={"spider": spider}
                )

        try:
            await self.signals.send_catch_log_async(
                signal=signals.spider_closed,
                spider=spider,
                reason=reason,
            )
        except Exception:
            logger.error(
                "Error while sending spider_close signal",
                exc_info=True,
                extra={"spider": spider},
            )

        try:
            stats = self.crawler.stats
            if argument_is_required(stats.close_spider, "spider"):
                warnings.warn(
                    f"The close_spider() method of {global_object_name(type(stats))} requires a spider argument,"
                    f" this is deprecated and the argument will not be passed in future Scrapy versions.",
                    ScrapyDeprecationWarning,
                    stacklevel=2,
                )
                stats.close_spider(spider=self.crawler.spider, reason=reason)
            else:
                stats.close_spider(reason=reason)
        except Exception:
            logger.error("Stats close failure")

        logger.info(
            "Spider closed (%(reason)s)",
            {"reason": reason},
            extra={"spider": spider},
        )

        self._slot = None
        self.spider = None

        self._transition_to(spider_state=SpiderState.CLOSED)

        try:
            await ensure_awaitable(self._spider_closed_callback(spider))
        except Exception:
            logger.error("Error running spider_closed_callback")

        if self._pending_stop:
            # A stop was requested while the spider was opening or closing,
            # and left the rest of the stop sequence to this method, so that
            # engine_stopped is sent after spider_closed.
            await self._finish_stop()
