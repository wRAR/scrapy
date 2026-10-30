from __future__ import annotations

import inspect
import weakref
from itertools import chain
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
    return {k: named[k] for k in accepted_args if k in named}


class _Receiver:
    """A single connection of a receiver to a signal."""

    __slots__ = ("accepted_args", "receiver_ref", "sender_ref")

    def __init__(
        self,
        receiver: Callable[..., TypingAny],
        sender: TypingAny,
        accepted_args: frozenset[str] | None,
        *,
        weak: bool,
        on_dead: Callable[[TypingAny], None],
    ) -> None:
        self.accepted_args = accepted_args
        self.receiver_ref: Callable[[], TypingAny]
        # Bound methods of built-in types are temporary objects, like other
        # bound methods, but WeakMethod does not support them, so keep them
        # alive for the duration of the connection.
        if not weak or inspect.isbuiltin(receiver):
            self.receiver_ref = _StrongRef(receiver)
        else:
            try:
                self.receiver_ref = (
                    weakref.WeakMethod(receiver, on_dead)
                    if inspect.ismethod(receiver)
                    else weakref.ref(receiver, on_dead)
                )
            except TypeError:
                self.receiver_ref = _StrongRef(receiver)
        self.sender_ref: Callable[[], TypingAny]
        try:
            self.sender_ref = weakref.ref(sender, on_dead)
        except TypeError:
            # Any, Anonymous, None and other objects without weakref support
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


#: All connections, keyed by the signal object and then by the id() of the
#: sender, so that a send only looks at the records of its own sender and of
#: :data:`Any`. Records still check their sender by identity, so a record left
#: behind by a dead sender never matches a new sender that reuses its id.
_registry: dict[TypingAny, dict[int, list[_Receiver]]] = {}

_ANY_ID = id(Any)


def _find_record(
    receiver: Callable[..., TypingAny], signal: TypingAny, sender: TypingAny
) -> _Receiver | None:
    # iterate over a copy: resolving weak references can run garbage
    # collection, whose callbacks remove records from the list
    for record in tuple(_registry.get(signal, {}).get(id(sender), ())):
        if record.matches(receiver, sender):
            return record
    return None


def _remove_record(signal: TypingAny, sender_key: int, record: _Receiver) -> None:
    """Remove *record* from the registry, together with the containers it
    leaves empty."""
    buckets = _registry.get(signal, {})
    records = buckets.get(sender_key, [])
    try:
        records.remove(record)
    except ValueError:
        return
    if not records:
        del buckets[sender_key]
        if not buckets:
            del _registry[signal]


def connect(
    receiver: Callable[..., TypingAny],
    signal: TypingAny,
    accepted_args: frozenset[str] | None,
    sender: TypingAny = Any,
    *,
    weak: bool = True,
) -> None:
    """Connect *receiver* to *signal* sent by *sender*.

    *signal* can be any hashable object; :data:`Any` means all signals.
    *sender* can be any object; :data:`Any` means any sender. If *weak* is
    true (default), the receiver (and the sender, when applicable) is
    referenced weakly and disconnected automatically when garbage collected.

    *accepted_args* is the result of :func:`_accepted_arguments` for
    *receiver*, used to filter the keyword arguments of each send.

    Connecting an already connected (receiver, signal, sender) combination
    has no effect.
    """
    if signal is None:
        raise TypeError(
            f"Signal cannot be None (receiver={receiver!r} sender={sender!r})"
        )
    if _find_record(receiver, signal, sender) is not None:
        return
    sender_key = id(sender)

    def on_dead(_ref: TypingAny) -> None:
        _remove_record(signal, sender_key, new_record)

    new_record = _Receiver(receiver, sender, accepted_args, weak=weak, on_dead=on_dead)
    # Garbage collection can run between any two of these steps, and its
    # weakref callbacks unregister the containers they leave empty, which can
    # include the ones fetched here before the record is added to them. Once
    # a registered list holds the record, it stays registered.
    while True:
        records = _registry.setdefault(signal, {}).setdefault(sender_key, [])
        records.append(new_record)
        if _registry.get(signal, {}).get(sender_key) is records:
            return


def disconnect(
    receiver: Callable[..., TypingAny],
    signal: TypingAny,
    sender: TypingAny = Any,
    *,
    weak: bool = True,
) -> None:
    """Disconnect *receiver* from *signal* sent by *sender*.

    The (receiver, signal, sender) combination must match a previous
    :func:`connect` call; :exc:`KeyError` is raised otherwise. *weak* is
    accepted for backward compatibility and ignored.
    """
    record = _find_record(receiver, signal, sender)
    if record is None:
        raise KeyError(
            f"No connection to receiver {receiver!r} for signal {signal!r}"
            f" from sender {sender!r}"
        )
    _remove_record(signal, id(sender), record)


def disconnect_all(signal: TypingAny = Any, sender: TypingAny = Any) -> None:
    """Disconnect all receivers of *signal*.

    If *sender* is :data:`Any` (default), all connections for the signal are
    removed; otherwise only the connections made with exactly that sender.
    """
    if sender is Any:
        _registry.pop(signal, None)
        return
    buckets = _registry.get(signal, {})
    if buckets.pop(id(sender), None) is not None and not buckets:
        del _registry[signal]


def _live_receivers(
    signal: TypingAny, sender: TypingAny
) -> list[tuple[Callable[..., TypingAny], frozenset[str] | None]]:
    """Return the resolved live receivers that should receive *signal* from
    *sender*, with their accepted-argument sets.

    Receivers connected to *signal* come before receivers connected to all
    signals (:data:`Any`); within each, receivers connected to *sender* come
    before receivers connected to any sender, in connection order. A receiver
    connected more than once (e.g. both to a specific sender and to any
    sender) is returned only once.
    """
    signal_keys = (signal, Any) if signal is not Any else (Any,)
    sender_keys = (id(sender), _ANY_ID) if sender is not Any else (_ANY_ID,)
    sources: list[tuple[_Receiver, ...]] = []
    for signal_key in signal_keys:
        buckets = _registry.get(signal_key)
        if buckets:
            for sender_key in sender_keys:
                records = buckets.get(sender_key)
                if records:
                    sources.append(tuple(records))
    if not sources:
        return []
    seen: set[object] | None = set() if len(sources) > 1 else None
    result: list[tuple[Callable[..., TypingAny], frozenset[str] | None]] = []
    for record in chain.from_iterable(sources):
        receiver = record.receiver_ref()
        # Weakref callbacks remove the records of dead receivers and senders;
        # this skips records whose callback has not run yet.
        if receiver is None or not record.matches_sender(sender):  # pragma: no cover
            continue
        if seen is not None:
            # Each connection and each WeakMethod resolution creates its own
            # bound method object, but bound methods compare equal when they
            # bind the same function to the same object.
            key = (
                receiver
                if inspect.ismethod(receiver) or inspect.isbuiltin(receiver)
                else id(receiver)
            )
            if key in seen:
                continue
            seen.add(key)
        result.append((receiver, record.accepted_args))
    return result
