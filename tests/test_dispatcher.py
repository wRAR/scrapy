"""Tests for scrapy._dispatcher, the PyDispatcher replacement."""

from __future__ import annotations

import functools
import gc

import pytest

from scrapy._dispatcher import (
    Anonymous,
    Any,
    Signal,
    _accepted_arguments,
    _registry,
    connect,
    disconnect,
    disconnect_all,
)
from scrapy.utils.signal import send_catch_log


class TestSignal:
    def test_repr(self):
        assert repr(Signal("spider_opened")) == "<signal spider_opened>"

    def test_repr_unnamed(self):
        assert "Signal object at" in repr(Signal())

    def test_sentinel_reprs(self):
        assert repr(Any) == "Any"
        assert repr(Anonymous) == "Anonymous"


class TestAcceptedArguments:
    def test_plain(self):
        def f(spider, reason=None):
            pass

        assert _accepted_arguments(f) == frozenset({"spider", "reason"})

    def test_keyword_only(self):
        def f(spider, *, reason):
            pass

        assert _accepted_arguments(f) == frozenset({"spider", "reason"})

    def test_var_keyword(self):
        def f(spider, **kwargs):
            pass

        assert _accepted_arguments(f) is None

    def test_positional_only(self):
        def f(a, /, spider):
            pass

        assert _accepted_arguments(f) == frozenset({"spider"})

    def test_no_args(self):
        def f():
            pass

        assert _accepted_arguments(f) == frozenset()

    def test_bound_method(self):
        class Handler:
            def handle(self, spider):
                pass

        assert _accepted_arguments(Handler().handle) == frozenset({"spider"})

    def test_partial(self):
        def f(a, spider=None):
            pass

        assert _accepted_arguments(functools.partial(f, 1)) == frozenset({"spider"})

    def test_callable_instance(self):
        class Handler:
            def __call__(self, spider):
                pass

        assert _accepted_arguments(Handler()) == frozenset({"spider"})

    def test_not_introspectable(self):
        assert _accepted_arguments(type) is None


class TestDispatch:
    def test_argument_filtering(self):
        signal = Signal("test")
        received = []

        def partial_handler(spider):
            received.append(("partial", spider))

        def full_handler(**kwargs):
            received.append(("full", kwargs))

        connect(partial_handler, signal)
        connect(full_handler, signal)
        send_catch_log(signal, spider="foo", reason="done")
        assert received[0] == ("partial", "foo")
        name, kwargs = received[1]
        assert name == "full"
        assert kwargs == {
            "spider": "foo",
            "reason": "done",
            "signal": signal,
            "sender": Anonymous,
        }

    def test_keyword_only_receiver(self):
        # broken in released PyDispatcher 2.0.7
        signal = Signal("test")
        received = []

        def handler(spider, *, reason):
            received.append((spider, reason))

        connect(handler, signal)
        results = send_catch_log(signal, spider="foo", reason="done")
        assert received == [("foo", "done")]
        assert results == [(handler, None)]

    def test_partial_receiver(self):
        # raised ValueError in PyDispatcher
        signal = Signal("test")
        received = []

        def handler(extra, spider):
            received.append((extra, spider))

        bound = functools.partial(handler, "extra")
        connect(bound, signal)
        send_catch_log(signal, spider="foo")
        assert received == [("extra", "foo")]

    def test_connection_order(self):
        signal = Signal("test")
        received = []
        handlers = []
        for i in range(10):

            def handler(i=i):
                received.append(i)

            handlers.append(handler)
            connect(handler, signal)
        send_catch_log(signal)
        assert received == list(range(10))

    def test_reconnect_is_noop(self):
        signal = Signal("test")
        received = []

        def handler():
            received.append(1)

        connect(handler, signal)
        connect(handler, signal)
        send_catch_log(signal)
        assert received == [1]

    def test_sender_filtering(self):
        signal = Signal("test")
        sender1, sender2 = object(), object()
        received = []

        def for_sender1(sender):
            received.append(("sender1", sender))

        def for_all(sender):
            received.append(("all", sender))

        connect(for_sender1, signal, sender=sender1)
        connect(for_all, signal, sender=Any)
        send_catch_log(signal, sender=sender1)
        send_catch_log(signal, sender=sender2)
        assert received == [
            ("sender1", sender1),
            ("all", sender1),
            ("all", sender2),
        ]

    def test_anonymous_sender(self):
        signal = Signal("test")
        received = []

        def handler():
            received.append(1)

        connect(handler, signal, sender=Anonymous)
        send_catch_log(signal)  # sends with sender=Anonymous
        send_catch_log(signal, sender=object())
        assert received == [1]

    def test_receiver_connected_twice_called_once(self):
        signal = Signal("test")
        sender = object()
        received = []

        def handler():
            received.append(1)

        connect(handler, signal, sender=sender)
        connect(handler, signal, sender=Any)
        send_catch_log(signal, sender=sender)
        assert received == [1]

    def test_any_signal(self):
        signal = Signal("test")
        received = []

        def handler(signal):
            received.append(signal)

        connect(handler, Any)
        send_catch_log(signal)
        assert received == [signal]
        disconnect(handler, Any)

    def test_bare_object_signal(self):
        signal = object()
        received = []

        def handler():
            received.append(1)

        connect(handler, signal)
        send_catch_log(signal)
        assert received == [1]

    def test_weak_receiver_garbage_collected(self):
        signal = Signal("test")
        received = []

        class Handler:
            def handle(self):
                received.append(1)

        handler = Handler()
        connect(handler.handle, signal)
        send_catch_log(signal)
        assert received == [1]
        del handler
        gc.collect()
        send_catch_log(signal)
        assert received == [1]
        # the record and the then-empty signal entry were removed
        assert signal not in _registry

    def test_strong_receiver(self):
        signal = Signal("test")
        received = []

        class Handler:
            def handle(self):
                received.append(1)

        handler = Handler()
        connect(handler.handle, signal, weak=False)
        del handler
        gc.collect()
        send_catch_log(signal)
        assert received == [1]
        disconnect_all(signal)

    def test_weak_sender_garbage_collected(self):
        signal = Signal("test")

        class Sender:
            pass

        sender = Sender()  # plain object() instances are not weakrefable
        received = []

        def handler():
            received.append(1)

        connect(handler, signal, sender=sender)
        del sender
        gc.collect()
        assert signal not in _registry

    def test_disconnect(self):
        signal = Signal("test")
        received = []

        class Handler:
            def handle(self):
                received.append(1)

        handler = Handler()
        connect(handler.handle, signal)
        # a fresh, equal bound method object must match the connection
        disconnect(handler.handle, signal)
        send_catch_log(signal)
        assert received == []

    def test_disconnect_unknown_raises(self):
        signal = Signal("test")

        def handler():
            pass

        with pytest.raises(KeyError):
            disconnect(handler, signal)

    def test_connect_none_signal_raises(self):
        def handler():
            pass

        with pytest.raises(TypeError):
            connect(handler, None)

    def test_disconnect_all_specific_sender(self):
        signal = Signal("test")
        sender = object()
        received = []

        def for_sender(sender):
            received.append(("sender", sender))

        def for_all(sender):
            received.append(("all", sender))

        connect(for_sender, signal, sender=sender)
        connect(for_all, signal, sender=Any)
        disconnect_all(signal, sender=sender)
        send_catch_log(signal, sender=sender)
        assert received == [("all", sender)]
        disconnect_all(signal)
        send_catch_log(signal, sender=sender)
        assert received == [("all", sender)]
        assert signal not in _registry

    def test_self_disconnecting_receiver(self):
        # the pattern used by SignalManager.wait_for()
        signal = Signal("test")
        received = []

        def first():
            disconnect(first, signal)
            received.append("first")

        def second():
            received.append("second")

        connect(first, signal)
        connect(second, signal)
        send_catch_log(signal)
        # disconnecting during dispatch does not affect the ongoing dispatch
        assert received == ["first", "second"]
        send_catch_log(signal)
        assert received == ["first", "second", "second"]
        disconnect_all(signal)

    def test_signal_and_sender_arguments(self):
        signal = Signal("test")
        sender = object()
        received = []

        def handler(signal, sender):
            received.append((signal, sender))

        connect(handler, signal, sender=sender)
        send_catch_log(signal, sender=sender)
        assert received == [(signal, sender)]
