"""
Unintelligent message bus.

The bus delivers messages on topics. It never looks into a message, holds no
business state and makes no routing decisions: senders choose the topic. The
in-memory implementation can reorder and duplicate deliveries, which models
at-least-once delivery (A1) and is used for fault injection in the tests.
"""

import random
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from .messages import Message

Handler = Callable[[Message], None]


class Bus:
    def subscribe(self, topic: str, handler: Handler) -> None:
        raise NotImplementedError

    def publish(self, topic: str, message: Message) -> None:
        raise NotImplementedError


class NonTermination(Exception):
    pass


class InMemoryBus(Bus):
    """
    Deliveries are queued per subscriber. By default they are delivered in FIFO
    order; with `shuffle` a seeded random pending delivery is chosen at each
    step, and with `duplicates` up to that many deliveries are delivered twice.
    """

    def __init__(self, seed: Optional[int] = None, shuffle: bool = False,
                 duplicates: int = 0, duplicate_probability: float = 0.3):
        self.rng = random.Random(seed)
        self.shuffle = shuffle
        self.duplicates = duplicates
        self.duplicate_probability = duplicate_probability
        self.subscribers: Dict[str, List[Handler]] = defaultdict(list)
        self.pending: List[Tuple[Handler, str, str]] = []
        self.log: List[Tuple[str, Message]] = []      # every published message, in publish order
        self.delivered = 0
        self.clock = 0

    def subscribe(self, topic: str, handler: Handler) -> None:
        self.subscribers[topic].append(handler)

    def publish(self, topic: str, message: Message) -> None:
        self.clock += 1
        message.time = self.clock
        text = message.to_json()
        self.log.append((topic, Message.from_json(text)))
        for handler in self.subscribers.get(topic, []):
            self.pending.append((handler, topic, text))

    @property
    def idle(self) -> bool:
        return not self.pending

    def step(self) -> bool:
        if not self.pending:
            return False
        index = self.rng.randrange(len(self.pending)) if self.shuffle else 0
        handler, topic, text = self.pending.pop(index)
        if self.duplicates > 0 and self.rng.random() < self.duplicate_probability:
            self.duplicates -= 1
            self.pending.append((handler, topic, text))
        self.delivered += 1
        handler(Message.from_json(text))
        return True

    def run(self, max_steps: int = 100_000) -> int:
        """Deliver until no message is in transit; returns the number of deliveries."""
        steps = 0
        while self.step():
            steps += 1
            if steps > max_steps:
                raise NonTermination(f"more than {max_steps} deliveries")
        return steps
