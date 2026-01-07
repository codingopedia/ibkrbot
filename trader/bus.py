from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, DefaultDict, List, Type, TypeVar

T = TypeVar("T")


class EventBus:
    """Tiny in-process pub/sub bus.

    Keep it simple for MVP. If we later need async or multiprocessing,
    we can replace this with an internal queue + workers.
    """

    def __init__(self) -> None:
        self._handlers: DefaultDict[Type[Any], List[Callable[[Any], None]]] = defaultdict(list)
        self._log = logging.getLogger("bus")

    def subscribe(self, event_type: Type[T], handler: Callable[[T], None]) -> None:
        self._handlers[event_type].append(handler)  # type: ignore[arg-type]

    def publish(self, event: Any) -> None:
        et = type(event)
        for h in self._handlers.get(et, []):
            try:
                h(event)
            except Exception:
                self._log.exception("handler_failed", extra={"event_type": et.__name__})
