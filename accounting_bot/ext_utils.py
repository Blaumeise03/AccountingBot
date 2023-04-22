import asyncio
import functools
import logging
from typing import Dict, List, Callable

from accounting_bot import utils

event_callbacks = {}  # type: Dict[str, List[Coroutine]]
logger = logging.getLogger("bot.ext.events")


def event(coro: Callable):
    if not asyncio.iscoroutinefunction(coro):
        raise TypeError("event registered must be a coroutine function")
    name = coro.__name__
    if name in event_callbacks:
        event_callbacks[name].append(coro)
    else:
        event_callbacks[name] = [coro]


def event_handler(coro: Callable):
    @functools.wraps(coro)
    async def wrapper(*args, **kwargs):
        await coro(*args, **kwargs)
        name = coro.__name__
        if name in event_callbacks:
            for ev in event_callbacks[name]:
                try:
                    await ev(*args, **kwargs)
                except Exception as e:
                    logger.error("An error occurred during execution of event %s", name)
                    utils.log_error(logger, e, "ext_events")
    return wrapper
