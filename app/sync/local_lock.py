"""Reference-counted in-process lock entries shared by sync ownership paths."""

import asyncio


class LocalLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0  # coroutines holding or waiting for the lock
