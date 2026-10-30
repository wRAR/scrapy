from __future__ import annotations

import asyncio
import functools
import warnings
from typing import TYPE_CHECKING, Any

import pytest
from twisted.internet import defer
from twisted.python.failure import Failure

from scrapy.exceptions import ScrapyDeprecationWarning
from scrapy.signalmanager import SignalManager
from scrapy.utils.asyncio import call_later
from scrapy.utils.defer import deferred_from_coro, ensure_awaitable
from scrapy.utils.signal import (
    send_catch_log,
    send_catch_log_async,
    send_catch_log_deferred,
)
from scrapy.utils.test import get_from_asyncio_queue
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable


class TestSendCatchLog:
    # whether the function being tested returns exceptions or failures
    returns_exceptions: bool = False

    @coroutine_test
    async def test_send_catch_log(self, caplog: pytest.LogCaptureFixture) -> None:
        test_signal = object()
        signal_manager = SignalManager()
        handlers_called: set[Callable[..., None]] = set()

        signal_manager.connect(self.error_handler, signal=test_signal)
        signal_manager.connect(self.ok_handler, signal=test_signal)
        caplog.clear()
        result = await ensure_awaitable(
            self._get_result(test_signal, arg="test", handlers_called=handlers_called)
        )

        assert self.error_handler in handlers_called
        assert self.ok_handler in handlers_called
        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert "error_handler" in record.getMessage()
        assert record.levelname == "ERROR"
        assert result[0][0] == self.error_handler  # pylint: disable=comparison-with-callable
        assert isinstance(
            result[0][1], Exception if self.returns_exceptions else Failure
        )
        assert result[1] == (self.ok_handler, "OK")

        signal_manager.disconnect(self.error_handler, signal=test_signal)
        signal_manager.disconnect(self.ok_handler, signal=test_signal)

    @coroutine_test
    async def test_send_catch_log_dont_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        test_signal = object()
        signal_manager = SignalManager()
        handlers_called: set[Callable[..., None]] = set()

        signal_manager.connect(self.error_handler, signal=test_signal)
        caplog.clear()
        result = await ensure_awaitable(
            self._get_result(
                test_signal,
                arg="test",
                handlers_called=handlers_called,
                dont_log=ZeroDivisionError,
            )
        )

        assert self.error_handler in handlers_called
        assert not caplog.records
        assert isinstance(
            result[0][1], Exception if self.returns_exceptions else Failure
        )

        signal_manager.disconnect(self.error_handler, signal=test_signal)

    def _get_result(self, signal: Any, *a: Any, **kw: Any) -> Any:
        return send_catch_log(signal, *a, **kw)

    def error_handler(self, arg, handlers_called):
        handlers_called.add(self.error_handler)
        1 / 0

    def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        return "OK"


@pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
class TestSendCatchLogDeferred(TestSendCatchLog):
    def _get_result(self, signal: Any, *a: Any, **kw: Any) -> Any:
        return send_catch_log_deferred(signal, *a, **kw)


class TestSendCatchLogDeferred2(TestSendCatchLogDeferred):
    def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        d: defer.Deferred[str] = defer.Deferred()
        call_later(0, d.callback, "OK")
        return d


class TestSendCatchLogDeferredAsyncDef(TestSendCatchLogDeferred):
    async def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        await defer.succeed(42)
        return "OK"


@pytest.mark.only_asyncio
class TestSendCatchLogDeferredAsyncio(TestSendCatchLogDeferred):
    async def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        await asyncio.sleep(0.2)
        return await get_from_asyncio_queue("OK")


class TestSendCatchLogAsync(TestSendCatchLog):
    returns_exceptions = True

    def _get_result(self, signal, *a, **kw):
        return deferred_from_coro(send_catch_log_async(signal, *a, **kw))


@pytest.mark.filterwarnings("ignore::scrapy.exceptions.ScrapyDeprecationWarning")
class TestSendCatchLogAsync2(TestSendCatchLogAsync):
    def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        d: defer.Deferred[str] = defer.Deferred()
        call_later(0, d.callback, "OK")
        return d


class TestSendCatchLogAsyncAsyncDef(TestSendCatchLogAsync):
    async def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        await defer.succeed(42)
        return "OK"


@pytest.mark.only_asyncio
class TestSendCatchLogAsyncAsyncio(TestSendCatchLogAsync):
    async def ok_handler(self, arg, handlers_called):
        handlers_called.add(self.ok_handler)
        assert arg == "test"
        await asyncio.sleep(0.2)
        return await get_from_asyncio_queue("OK")


class TestSendCatchLog2:
    def test_error_logged_if_deferred_not_supported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def test_handler():
            return defer.Deferred()

        test_signal = object()
        signal_manager = SignalManager()
        signal_manager.connect(test_handler, test_signal)
        send_catch_log(test_signal)
        assert len(caplog.records) == 1
        assert "Cannot return deferreds from signal handler" in caplog.text
        signal_manager.disconnect(test_handler, test_signal)


@pytest.mark.parametrize("method", ["send_catch_log_async", "send_catch_log_deferred"])
@pytest.mark.parametrize("result_kind", ["value", "coroutine", "deferred"])
@pytest.mark.parametrize(
    ("receiver_kind", "name"),
    [
        ("function", "function"),
        ("bound_method", "Handler.handle"),
        ("partial", "functools.partial"),
        ("callable_instance", "Handler object"),
    ],
)
@coroutine_test
async def test_receiver_kinds(
    method: str,
    result_kind: str,
    receiver_kind: str,
    name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def coroutine(value: int) -> int:
        return value

    def function(value: int) -> Any:
        if result_kind == "coroutine":
            return coroutine(value)
        if result_kind == "deferred":
            return defer.succeed(value)
        return value

    class Handler:
        def handle(self, value: int) -> Any:
            return function(value)

        def __call__(self, value: int) -> Any:
            return function(value)

    owner = Handler()
    receivers: dict[str, Callable[..., Any]] = {
        "function": function,
        "bound_method": owner.handle,
        "partial": functools.partial(function, value=42),
        "callable_instance": owner,
    }
    receiver = receivers[receiver_kind]

    def second() -> str:
        return "second"

    signal = object()
    signal_manager = SignalManager()
    signal_manager.connect(receiver, signal)
    signal_manager.connect(second, signal)
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always", ScrapyDeprecationWarning)
        results = await ensure_awaitable(
            getattr(signal_manager, method)(signal, value=42, ignored=True)
        )
    assert results == [(receiver, 42), (second, "second")]
    assert not caplog.records
    return_warnings = [
        str(record.message)
        for record in records
        if "returned a Deferred" in str(record.message)
    ]
    if result_kind == "deferred":
        assert len(return_warnings) == 1
        assert name in return_warnings[0]
    else:
        assert return_warnings == []


@coroutine_test
async def test_callable_receiver_failure(caplog: pytest.LogCaptureFixture) -> None:
    error = ValueError("handler failed")

    class Handler:
        def __call__(self) -> Failure:
            return Failure(error)

    receiver = Handler()

    def second() -> str:
        return "second"

    signal = object()
    signal_manager = SignalManager()
    signal_manager.connect(receiver, signal)
    signal_manager.connect(second, signal)
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter("always", ScrapyDeprecationWarning)
        results = await ensure_awaitable(
            signal_manager.send_catch_log_deferred(signal, dont_log=ValueError)
        )
    assert results[0][0] is receiver
    assert isinstance(results[0][1], Failure)
    assert results[0][1].value is error
    assert results[1] == (second, "second")
    assert any(
        "Handler object" in str(record.message)
        and "returned a Failure" in str(record.message)
        for record in records
    )
    assert not caplog.records
