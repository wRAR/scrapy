from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

import pytest

from scrapy import signals
from scrapy.signalmanager import SignalManager
from scrapy.utils.asyncio import call_later
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable


def test_disconnect_all() -> None:
    signal = object()
    sender = object()
    sm = SignalManager(sender)

    calls = []

    def handler():
        calls.append(1)

    sm.connect(handler, signal)
    sm.send_catch_log(signal)
    assert calls == [1]

    sm.disconnect_all(signal)
    sm.send_catch_log(signal)
    assert calls == [1]  # handler no longer called after disconnect_all


class TestUnknownArgumentWarning:
    @pytest.mark.filterwarnings("error")
    def test_warning_as_error_leaves_receiver_disconnected(self) -> None:
        signal_manager = SignalManager(object())

        def handler(cheese: Any = None) -> None:
            pass

        with pytest.raises(UserWarning, match="declares cheese"):
            signal_manager.connect(handler, signals.spider_opened)
        assert signal_manager.send_catch_log(signals.spider_opened) == []

    def test_warns(self) -> None:
        def handler(spider: Any, cheese: Any = None, *, ham: Any = None) -> None:
            pass

        with pytest.warns(
            UserWarning,
            match=r"handler declares cheese, ham, which scrapy\.signals\.spider_opened does not send",
        ) as record:
            SignalManager().connect(handler, signals.spider_opened)
        assert record[0].filename == __file__

    def test_callable_instance(self) -> None:
        class Handler:
            def __call__(self, cheese: Any = None) -> None:
                pass

        with pytest.warns(UserWarning, match=r"Handler object at .* declares cheese"):
            SignalManager().connect(Handler(), signals.spider_opened)

    @pytest.mark.filterwarnings("error")
    @pytest.mark.parametrize(
        ("handler", "signal"),
        [
            (lambda spider, signal, sender: None, signals.spider_opened),
            (lambda cheese, **kwargs: None, signals.spider_opened),
            (lambda cheese: None, object()),
            (type, signals.spider_opened),
        ],
    )
    def test_no_warning(self, handler: Callable[..., Any], signal: object) -> None:
        SignalManager().connect(handler, signal)


def test_every_signal_declares_its_arguments() -> None:
    defined = {v for k, v in vars(signals).items() if not k.startswith("_")}
    assert defined == set(signals._signal_args)


def test_signature_inspected_once() -> None:
    class Handler:
        def __init__(self) -> None:
            self.lookups = 0

        @property
        def __signature__(self) -> inspect.Signature:
            self.lookups += 1
            return inspect.signature(self.__call__)

        def __call__(self, spider: str) -> str:
            return spider

    handler = Handler()
    signal_manager = SignalManager(object())
    signal_manager.connect(handler, signals.spider_opened)
    assert signal_manager.send_catch_log(signals.spider_opened, spider="foo") == [
        (handler, "foo")
    ]
    assert handler.lookups == 1


@coroutine_test
async def test_wait_for() -> None:
    signal = object()
    signal_manager = SignalManager()
    call_later(0, signal_manager.send_catch_log, signal)
    await signal_manager.wait_for(signal)
    assert signal_manager.send_catch_log(signal) == []
