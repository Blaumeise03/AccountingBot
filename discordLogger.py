from logging import Handler, LogRecord
from typing import Union

from discord import Thread
from discord.abc import GuildChannel, PrivateChannel


class PycordHandler(Handler):
    def __init__(self, channel: Union[GuildChannel, PrivateChannel, Thread] = None, level: Union[int, str] = "FATAL") -> None:
        super().__init__(level)
        self.channel = channel
        self.cache = []

    def emit(self, record: LogRecord) -> None:
        self.cache.append(record)

    def set_channel(self, channel: Union[GuildChannel, PrivateChannel, Thread]):
        self.channel = channel

    async def process_logs(self):
        if self.channel is not None:
            msg = "```"
            while len(self.cache) > 0:
                record = self.cache.pop(0)
                text = self.format(record)
                if len(msg) + len(text) < 1980:
                    msg += "\n" + text
                else:
                    msg += "\n```"
                    await self.channel.send(content=msg)
                    if len(text) > 1980:
                        text = text[:1980] + " **(Truncated)**"
                    msg = "```\n" + text
            if len(msg) > 3:
                msg += "\n```"
                await self.channel.send(content=msg)
        if len(self.cache) > 50:
            self.cache.clear()
