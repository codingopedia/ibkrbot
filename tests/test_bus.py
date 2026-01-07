from __future__ import annotations

from dataclasses import dataclass

from trader.bus import EventBus


@dataclass(frozen=True)
class E:
    x: int


def test_bus_publish_subscribe() -> None:
    bus = EventBus()
    seen: list[int] = []

    def h(e: E) -> None:
        seen.append(e.x)

    bus.subscribe(E, h)
    bus.publish(E(1))
    bus.publish(E(2))

    assert seen == [1, 2]
