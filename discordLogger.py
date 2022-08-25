from logging import Handler, LogRecord
from typing import Union

from discord import Thread
from discord.abc import GuildChannel, PrivateChannel


class PycordHandler(Handler):
    """
    Logging handler to send the logs into a discord channel. The logs are cached by the handler, by calling process_logs
    the cache will be sent into the channel.

    """
    def __init__(self,
                 channel: Union[GuildChannel, PrivateChannel, Thread] = None,
                 level: Union[int, str] = "FATAL"
                 ) -> None:
        super().__init__(level)
        self.channel = channel
        self.cache = []

    def emit(self, record: LogRecord) -> None:
        self.cache.append(record)

    def set_channel(self, channel: Union[GuildChannel, PrivateChannel, Thread]):
        self.channel = channel

    async def process_logs(self):
        """
        Processes the log cache and send out all cached logs into the channel.

        The cached log messages will be combined into one message to reduce the API traffic (to prevent rate limits).
        Should the log messages exceed the message limit, they will be split onto multiple messages. Should a single log
        entry exceed the limit, it will be truncated.

        **Warning:** Should the cache exceed 100, it will be cleared completely before sending the messages (to clear up
        space in case the messages did not get send because of an error)!
        """
        if self.channel is None:
            return
        if len(self.cache) > 100:
            self.cache.clear()
            return
        msg = "```"
        while len(self.cache) > 0:
            record = self.cache.pop(0)
            text = self.format(record)
            if len(msg) + len(text) < 1980:
                # Message length is fine
                msg += "\n" + text
            else:
                # Message would become to long
                msg += "\n```"
                await self.channel.send(content=msg)
                if len(text) > 1980:
                    # Truncating text
                    text = text[:1980] + " **(Truncated)**"
                msg = "```\n" + text
        if len(msg) > 3:
            msg += "\n```"
            await self.channel.send(content=msg)
