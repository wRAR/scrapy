"""Signal dispatching machinery.

This module is internal Scrapy API; the public interface for working with
signals is :class:`scrapy.signalmanager.SignalManager`. It replaces the
previously used PyDispatcher library, keeping its dispatch semantics:
receivers are referenced weakly by default, any hashable object can be used
as a signal identity, and only the keyword arguments that a receiver accepts
are passed to it. Unlike PyDispatcher, the set of arguments accepted by a
receiver is computed once, when the receiver is connected, not on every send.
"""

from __future__ import annotations

import contextlib
import inspect
import weakref
from typing import TYPE_CHECKING, Final
from typing import Any as TypingAny

if TYPE_CHECKING:
    from collections.abc import Callable


class _Sentinel:
    """A named singleton for special sender/signal values."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


#: As a sender: connect to signals from any sender. As a signal: connect to
#: all signals.
Any: Final = _Sentinel("Any")

#: The default sender of :class:`~scrapy.signalmanager.SignalManager`,
#: used when no explicit sender is given.
Anonymous: Final = _Sentinel("Anonymous")


class Signal:
    """An object identifying a signal.

    Any hashable object can be used as a signal identity; instances of this
    class additionally carry a name, making them easier to recognize when
    debugging.
    """

    def __init__(self, name: str = "") -> None:
        self.name: str = name

    def __repr__(self) -> str:
        if self.name:
            return f"<signal {self.name}>"
        return super().__repr__()


class _StrongRef:
    """A callable that mimics :class:`weakref.ref` for a strong reference."""

    __slots__ = ("_obj",)

    def __init__(self, obj: TypingAny) -> None:
        self._obj = obj

    def __call__(self) -> TypingAny:
        return self._obj


def _accepted_arguments(receiver: Callable[..., TypingAny]) -> frozenset[str] | None:
    """Return the names of the keyword arguments that *receiver* accepts.

    ``None`` means all keyword arguments are accepted (the receiver takes
    ``**kwargs``, or its signature cannot be introspected).
    """
    try:
        sig = inspect.signature(receiver)
    except (TypeError, ValueError):
        return None
    accepted = set()
    for name, param in sig.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        if param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            accepted.add(name)
    return frozenset(accepted)


def _filtered_args(
    accepted_args: frozenset[str] | None, named: dict[str, TypingAny]
) -> dict[str, TypingAny]:
    """Return the subset of *named* that a receiver accepts."""
    if accepted_args is None:
        return named
    return {k: v for k, v in named.items() if k in accepted_args}


class _Receiver:
    """A single connection of a receiver to a signal."""

    __slots__ = ("accepted_args", "receiver_ref", "sender_ref")

    def __init__(
        self,
        receiver: Callable[..., TypingAny],
        sender: TypingAny,
        *,
        weak: bool,
        on_dead: Callable[[TypingAny], None],
    ) -> None:
        self.accepted_args: frozenset[str] | None = _accepted_arguments(receiver)
        self.receiver_ref: Callable[[], TypingAny]
        if not weak:
            self.receiver_ref = _StrongRef(receiver)
        elif inspect.ismethod(receiver):
            self.receiver_ref = weakref.WeakMethod(receiver, on_dead)
        else:
            try:
                self.receiver_ref = weakref.ref(receiver, on_dead)
            except TypeError:
                self.receiver_ref = _StrongRef(receiver)
        self.sender_ref: Callable[[], TypingAny]
        if isinstance(sender, _Sentinel) or sender is None:
            self.sender_ref = _StrongRef(sender)
        else:
            try:
                self.sender_ref = weakref.ref(sender, on_dead)
            except TypeError:
                self.sender_ref = _StrongRef(sender)

    def matches(self, receiver: Callable[..., TypingAny], sender: TypingAny) -> bool:
        """Whether this record connects *receiver* to exactly *sender*."""
        stored_receiver = self.receiver_ref()
        if stored_receiver is None or stored_receiver != receiver:
            return False
        return self.sender_ref() is sender

    def matches_sender(self, sender: TypingAny) -> bool:
        """Whether this record should receive a signal sent by *sender*."""
        stored = self.sender_ref()
        if stored is Any:
            return True
        if stored is None:
            # a dead sender weakref, unless None was connected explicitly
            return isinstance(self.sender_ref, _StrongRef) and sender is None
        return stored is sender


#: All connections, keyed by the signal object.
_registry: dict[TypingAny, list[_Receiver]] = {}


def _remove_record(
    signal: TypingAny, records: list[_Receiver], record: _Receiver
) -> None:
    with contextlib.suppress(ValueError):
        records.remove(record)
    if not records and _registry.get(signal) is records:
        _registry.pop(signal, None)


def connect(
    receiver: Callable[..., TypingAny],
    signal: TypingAny = Any,
    sender: TypingAny = Any,
    weak: bool = True,
) -> None:
    """Connect *receiver* to *signal* sent by *sender*.

    *signal* can be any hashable object; :data:`Any` means all signals.
    *sender* can be any object; :data:`Any` means any sender. If *weak* is
    true (default), the receiver (and the sender, when applicable) is
    referenced weakly and disconnected automatically when garbage collected.

    Connecting an already connected (receiver, signal, sender) combination
    has no effect.
    """
    if signal is None:
        raise TypeError(
            f"Signal cannot be None (receiver={receiver!r} sender={sender!r})"
        )
    records = _registry.get(signal)
    if records is None:
        records = _registry[signal] = []
    for record in tuple(records):
        if record.matches(receiver, sender):
            return
    # on_dead needs the record, which needs on_dead first; bind through a cell
    record_cell: list[_Receiver] = []

    def on_dead(_ref: TypingAny) -> None:
        if record_cell:
            _remove_record(signal, records, record_cell[0])

    new_record = _Receiver(receiver, sender, weak=weak, on_dead=on_dead)
    record_cell.append(new_record)
    records.append(new_record)


def disconnect(
    receiver: Callable[..., TypingAny],
    signal: TypingAny = Any,
    sender: TypingAny = Any,
    weak: bool = True,
) -> None:
    """Disconnect *receiver* from *signal* sent by *sender*.

    The (receiver, signal, sender) combination must match a previous
    :func:`connect` call; :exc:`KeyError` is raised otherwise. *weak* is
    accepted for backward compatibility and ignored.
    """
    records = _registry.get(signal, [])
    for record in tuple(records):
        if record.matches(receiver, sender):
            _remove_record(signal, records, record)
            return
    raise KeyError(
        f"No connection to receiver {receiver!r} for signal {signal!r}"
        f" from sender {sender!r}"
    )


def disconnect_all(signal: TypingAny = Any, sender: TypingAny = Any) -> None:
    """Disconnect all receivers of *signal*.

    If *sender* is :data:`Any` (default), all connections for the signal are
    removed; otherwise only the connections made with exactly that sender.
    """
    records = _registry.get(signal)
    if records is None:
        return
    if sender is Any:
        records.clear()
        if _registry.get(signal) is records:
            _registry.pop(signal, None)
        return
    for record in tuple(records):
        if record.sender_ref() is sender:
            _remove_record(signal, records, record)


def _live_receivers(
    signal: TypingAny, sender: TypingAny
) -> list[tuple[Callable[..., TypingAny], frozenset[str] | None]]:
    """Return the resolved live receivers that should receive *signal* from
    *sender*, with their accepted-argument sets, in connection order.

    Receivers connected to the signal itself come before receivers connected
    to :data:`Any`; a receiver connected more than once (e.g. both to a
    specific sender and to any sender) is returned only once.
    """
    result: list[tuple[Callable[..., TypingAny], frozenset[str] | None]] = []
    seen: set[TypingAny] = set()
    signal_keys = (signal, Any) if signal is not Any else (Any,)
    for signal_key in signal_keys:
        records = _registry.get(signal_key)
        if not records:
            continue
        for record in tuple(records):
            receiver = record.receiver_ref()
            if receiver is None:
                _remove_record(signal_key, records, record)
                continue
            if not record.matches_sender(sender):
                continue
            # id() alone cannot deduplicate bound methods, which are created
            # anew on every weakref resolution
            key = (
                (id(receiver.__self__), id(receiver.__func__))
                if inspect.ismethod(receiver)
                else id(receiver)
            )
            if key in seen:
                continue
            seen.add(key)
            result.append((receiver, record.accepted_args))
    return result
