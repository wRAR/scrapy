from __future__ import annotations

import functools
import gc
from typing import TYPE_CHECKING
from typing import Any as TypingAny

import pytest

from scrapy import _dispatcher
from scrapy._dispatcher import Anonymous, Any, Signal, _registry
from scrapy.signalmanager import SignalManager
from tests.utils.decorators import coroutine_test

if TYPE_CHECKING:
    from collections.abc import Callable


class _ArgumentReceiver:
    def handle(self, spider: str) -> str:
        return spider

    def __call__(self, spider: str) -> str:
        return spider


class _SlottedReceiver:
    __slots__ = ()

    def handle(self, spider: str) -> str:
        return spider

    def __call__(self, spider: str) -> str:
        return spider


class TestSignal:
    def test_repr(self):
        assert repr(Signal("spider_opened")) == "<signal spider_opened>"

    def test_repr_unnamed(self):
        assert "Signal object at" in repr(Signal())

    def test_sentinel_reprs(self):
        assert repr(Any) == "Any"
        assert repr(Anonymous) == "Anonymous"


@pytest.fixture
def signal_manager() -> SignalManager:
    return SignalManager()


class TestDispatch:
    @pytest.mark.parametrize("asynchronous", [False, True])
    @pytest.mark.parametrize(
        ("receiver", "expected"),
        [
            pytest.param(
                lambda spider, reason=None: (spider, reason),
                ("foo", "done"),
                id="plain",
            ),
            pytest.param(
                lambda spider, *, reason: (spider, reason),
                ("foo", "done"),
                id="keyword-only",
            ),
            pytest.param(
                lambda a="default", /, spider=None: (a, spider),
                ("default", "foo"),
                id="positional-only",
            ),
            pytest.param(lambda: "no args", "no args", id="no-args"),
            pytest.param(_ArgumentReceiver().handle, "foo", id="bound-method"),
            pytest.param(
                functools.partial(lambda extra, spider: (extra, spider), "extra"),
                ("extra", "foo"),
                id="partial",
            ),
            pytest.param(_ArgumentReceiver(), "foo", id="callable-instance"),
            pytest.param(_SlottedReceiver(), "foo", id="non-weakrefable-instance"),
            pytest.param(_SlottedReceiver().handle, "foo", id="non-weakrefable-method"),
        ],
    )
    @coroutine_test
    async def test_receiver_arguments(
        self,
        signal_manager: SignalManager,
        receiver: Callable[..., TypingAny],
        expected: TypingAny,
        *,
        asynchronous: bool,
    ) -> None:
        signal = object()
        signal_manager.connect(receiver, signal)
        if asynchronous:
            result = await signal_manager.send_catch_log_async(
                signal, spider="foo", reason="done", a="ignored", extra="ignored"
            )
        else:
            result = signal_manager.send_catch_log(
                signal, spider="foo", reason="done", a="ignored", extra="ignored"
            )
        assert result == [(receiver, expected)]
        signal_manager.disconnect_all(signal)

    @pytest.mark.parametrize("asynchronous", [False, True])
    @coroutine_test
    async def test_uninspectable_receiver(
        self, signal_manager: SignalManager, *, asynchronous: bool
    ) -> None:
        signal = object()
        signal_manager.connect(dict, signal)
        if asynchronous:
            result = await signal_manager.send_catch_log_async(signal, value=42)
        else:
            result = signal_manager.send_catch_log(signal, value=42)
        assert result == [
            (dict, {"value": 42, "signal": signal, "sender": signal_manager.sender})
        ]
        signal_manager.disconnect_all(signal)

    @pytest.mark.parametrize("asynchronous", [False, True])
    @coroutine_test
    async def test_builtin_bound_method(
        self, signal_manager: SignalManager, *, asynchronous: bool
    ) -> None:
        signal = object()
        values = [1]
        signal_manager.connect(values.clear, signal)
        signal_manager.connect(values.clear, signal, sender=Any)
        gc.collect()
        if asynchronous:
            result = await signal_manager.send_catch_log_async(signal, ignored=42)
        else:
            result = signal_manager.send_catch_log(signal, ignored=42)
        assert values == []
        assert result == [(values.clear, None)]
        signal_manager.disconnect(values.clear, signal)
        signal_manager.disconnect(values.clear, signal, sender=Any)
        assert signal_manager.send_catch_log(signal) == []

    def test_argument_filtering(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        def partial_handler(spider):
            received.append(("partial", spider))

        def full_handler(**kwargs):
            received.append(("full", kwargs))

        signal_manager.connect(partial_handler, signal)
        signal_manager.connect(full_handler, signal)
        signal_manager.send_catch_log(signal, spider="foo", reason="done")
        assert received[0] == ("partial", "foo")
        name, kwargs = received[1]
        assert name == "full"
        assert kwargs == {
            "spider": "foo",
            "reason": "done",
            "signal": signal,
            "sender": Anonymous,
        }

    def test_connection_order(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []
        handlers = []
        for i in range(10):

            def handler(i=i):
                received.append(i)

            handlers.append(handler)
            signal_manager.connect(handler, signal)
        signal_manager.send_catch_log(signal)
        assert received == list(range(10))

    def test_reconnect_is_noop(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        def handler():
            received.append(1)

        signal_manager.connect(handler, signal)
        signal_manager.connect(handler, signal)
        signal_manager.send_catch_log(signal)
        assert received == [1]

    def test_sender_filtering(self, signal_manager: SignalManager):
        signal = Signal("test")
        sender1, sender2 = object(), object()
        received = []

        def for_sender1(sender):
            received.append(("sender1", sender))

        def for_all(sender):
            received.append(("all", sender))

        signal_manager.connect(for_sender1, signal, sender=sender1)
        signal_manager.connect(for_all, signal, sender=Any)
        signal_manager.send_catch_log(signal, sender=sender1)
        signal_manager.send_catch_log(signal, sender=sender2)
        assert received == [
            ("sender1", sender1),
            ("all", sender1),
            ("all", sender2),
        ]

    def test_anonymous_sender(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        def handler():
            received.append(1)

        signal_manager.connect(handler, signal, sender=Anonymous)
        signal_manager.send_catch_log(signal)  # sends with sender=Anonymous
        signal_manager.send_catch_log(signal, sender=object())
        assert received == [1]

    def test_receiver_connected_twice_called_once(self, signal_manager: SignalManager):
        signal = Signal("test")
        sender = object()
        received = []

        def handler():
            received.append(1)

        signal_manager.connect(handler, signal, sender=sender)
        signal_manager.connect(handler, signal, sender=Any)
        signal_manager.send_catch_log(signal, sender=sender)
        assert received == [1]

    def test_any_signal(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        def handler(signal):
            received.append(signal)

        signal_manager.connect(handler, Any, sender=Any)
        signal_manager.send_catch_log(signal)
        assert received == [signal]
        signal_manager.disconnect(handler, Any, sender=Any)

    def test_bare_object_signal(self, signal_manager: SignalManager):
        signal = object()
        received = []

        def handler():
            received.append(1)

        signal_manager.connect(handler, signal)
        signal_manager.send_catch_log(signal)
        assert received == [1]

    def test_weak_receiver_garbage_collected(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        class Handler:
            def handle(self):
                received.append(1)

        handler = Handler()
        signal_manager.connect(handler.handle, signal)
        signal_manager.send_catch_log(signal)
        assert received == [1]
        del handler
        gc.collect()
        signal_manager.send_catch_log(signal)
        assert received == [1]
        # the record and the then-empty signal entry were removed
        assert signal not in _registry

    def test_strong_receiver(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        class Handler:
            def handle(self):
                received.append(1)

        handler = Handler()
        signal_manager.connect(handler.handle, signal, weak=False)
        del handler
        gc.collect()
        signal_manager.send_catch_log(signal)
        assert received == [1]
        signal_manager.disconnect_all(signal)

    def test_sender_dies_during_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Sender:
            pass

        dying: list[Sender] = []

        class Registry(dict[object, TypingAny]):
            def setdefault(self, *args: TypingAny) -> TypingAny:
                result = super().setdefault(*args)
                # simulates garbage collection running right after the lookup
                dying.clear()
                return result

        monkeypatch.setattr(_dispatcher, "_registry", Registry())
        signal = object()
        receiver = _ArgumentReceiver()
        other_sender = Sender()
        SignalManager(other_sender).connect(receiver.handle, signal)
        dying.append(other_sender)
        del other_sender
        signal_manager = SignalManager(Sender())
        # the death of the only other sender of the signal unregisters the
        # dict that connect() has just fetched
        signal_manager.connect(receiver, signal)
        assert signal_manager.send_catch_log(signal, spider="foo") == [
            (receiver, "foo")
        ]

    def test_weak_sender_garbage_collected(self, signal_manager: SignalManager):
        signal = Signal("test")

        class Sender:
            pass

        sender = Sender()  # plain object() instances are not weakrefable
        received = []

        def handler():
            received.append(1)

        signal_manager.connect(handler, signal, sender=sender)
        del sender
        gc.collect()
        assert signal not in _registry

    def test_disconnect(self, signal_manager: SignalManager):
        signal = Signal("test")
        received = []

        class Handler:
            def handle(self):
                received.append(1)

        handler = Handler()
        signal_manager.connect(handler.handle, signal)
        # a fresh, equal bound method object must match the connection
        signal_manager.disconnect(handler.handle, signal)
        signal_manager.send_catch_log(signal)
        assert received == []

    def test_disconnect_unknown_raises(self, signal_manager: SignalManager):
        signal = Signal("test")

        def handler():
            pass

        with pytest.raises(KeyError):
            signal_manager.disconnect(handler, signal)

    def test_connect_none_signal_raises(self, signal_manager: SignalManager):
        def handler():
            pass

        with pytest.raises(TypeError):
            signal_manager.connect(handler, None)

    def test_disconnect_all_specific_sender(self, signal_manager: SignalManager):
        signal = Signal("test")
        sender = object()
        received = []

        def for_sender(sender):
            received.append(("sender", sender))

        def for_all(sender):
            received.append(("all", sender))

        signal_manager.connect(for_sender, signal, sender=sender)
        signal_manager.connect(for_all, signal, sender=Any)
        signal_manager.disconnect_all(signal, sender=sender)
        signal_manager.send_catch_log(signal, sender=sender)
        assert received == [("all", sender)]
        signal_manager.disconnect_all(signal, sender=Any)
        signal_manager.send_catch_log(signal, sender=sender)
        assert received == [("all", sender)]
        assert signal not in _registry

    def test_self_disconnecting_receiver(self, signal_manager: SignalManager):
        # the pattern used by SignalManager.wait_for()
        signal = Signal("test")
        received = []

        def first():
            signal_manager.disconnect(first, signal)
            received.append("first")

        def second():
            received.append("second")

        signal_manager.connect(first, signal)
        signal_manager.connect(second, signal)
        signal_manager.send_catch_log(signal)
        # disconnecting during dispatch does not affect the ongoing dispatch
        assert received == ["first", "second"]
        signal_manager.send_catch_log(signal)
        assert received == ["first", "second", "second"]
        signal_manager.disconnect_all(signal)

    def test_signal_and_sender_arguments(self, signal_manager: SignalManager):
        signal = Signal("test")
        sender = object()
        received = []

        def handler(signal, sender):
            received.append((signal, sender))

        signal_manager.connect(handler, signal, sender=sender)
        signal_manager.send_catch_log(signal, sender=sender)
        assert received == [(signal, sender)]

    @pytest.mark.parametrize("sender", [None, Any, Anonymous])
    @pytest.mark.parametrize("signal", [object(), Any])
    def test_special_sender(self, sender: object, signal: object) -> None:
        signal_manager = SignalManager(sender)

        def handler(sender: object) -> object:
            return sender

        signal_manager.connect(handler, signal)
        assert signal_manager.send_catch_log(signal) == [(handler, sender)]
        signal_manager.disconnect(handler, signal)

    def test_disconnect_all_unknown(self, signal_manager: SignalManager) -> None:
        signal_manager.disconnect_all(object())

    def test_disconnect_keeps_other_receivers(
        self, signal_manager: SignalManager
    ) -> None:
        signal = object()
        first = _ArgumentReceiver()
        second = _ArgumentReceiver()
        signal_manager.connect(first.handle, signal)
        signal_manager.connect(second.handle, signal)
        signal_manager.disconnect(second.handle, signal)
        assert signal_manager.send_catch_log(signal, spider="foo") == [
            (first.handle, "foo")
        ]

    def test_bound_method_deduplicated(self, signal_manager: SignalManager) -> None:
        signal = object()
        handler = _ArgumentReceiver()
        signal_manager.connect(handler.handle, Any, sender=Any)
        signal_manager.connect(handler.handle, Any)
        signal_manager.connect(handler.handle, signal, sender=Any)
        signal_manager.connect(handler.handle, signal)
        assert signal_manager.send_catch_log(signal, spider="foo") == [
            (handler.handle, "foo")
        ]
        signal_manager.disconnect_all(signal)
        signal_manager.disconnect_all(Any, sender=Any)

    def test_disconnected_receiver_callback(
        self, signal_manager: SignalManager
    ) -> None:
        signal = object()
        first = _ArgumentReceiver()
        second = _ArgumentReceiver()
        signal_manager.connect(first.handle, signal)
        signal_manager.disconnect(first.handle, signal)
        signal_manager.connect(second.handle, signal)
        del first
        gc.collect()
        assert signal_manager.send_catch_log(signal, spider="foo") == [
            (second.handle, "foo")
        ]
