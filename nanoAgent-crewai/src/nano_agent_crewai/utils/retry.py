from __future__ import annotations

import time
from functools import wraps
from typing import Callable, ParamSpec, TypeVar


P = ParamSpec("P")
R = TypeVar("R")


def retry(
    attempts: int = 3,
    min_wait_seconds: float = 1.0,
    max_wait_seconds: float = 4.0,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """轻量重试装饰器：按指数退避重试，超过次数后抛出最后一次异常。"""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            last_exc: Exception | None = None
            for i in range(attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if i == attempts - 1:
                        break
                    wait = min(max_wait_seconds, min_wait_seconds * (2**i))
                    time.sleep(wait)
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("retry wrapper reached unexpected state")

        return wrapper

    return decorator
