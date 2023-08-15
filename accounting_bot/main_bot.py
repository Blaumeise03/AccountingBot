import functools
from abc import ABC
from enum import Enum
from typing import Dict, Any, Union

from discord.ext import commands

from accounting_bot.utils import State


class AccountingBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = State.offline
        self.embeds = {}
        self.plugins = {}

    def is_online(self):
        return self.state.value >= State.online.value


class BotPlugin(ABC):
    def __init__(self, bot: AccountingBot) -> None:
        super().__init__()

    async def on_load(self):
        # Gets called before the Bot starts
        pass

    async def on_enable(self):
        # Gets called after the Bot logged in
        pass

    async def on_disable(self):
        # Gets called before the Bot shuts down
        pass

    async def on_reload(self):
        # Gets called to reload the extension
        pass

    def get_config(self):
        # Should return a config
        pass


@functools.total_ordering
class PluginStatus(Enum):
    CRASHED = 0
    UNLOADED = 1
    LOADED = 2
    ENABLED = 3

    def __repr__(self) -> str:
        return f"PluginStatus({self.name})"

    def __eq__(self, other):
        if not isinstance(other, PluginStatus):
            return False
        return other.value == self.value

    def __gt__(self, other):
        return self.value > other.value


class PluginWrapper(object):
    def __init__(self, name: str, module_name: str, author: str = None) -> None:
        super().__init__()
        self.author = author
        self.module_name = module_name
        self.name = name
        self.plugin = None  # type: BotPlugin | None
        self.status = PluginStatus.UNLOADED
        self.dependencies = []

    def load_dependencies(self, dependencies: str):
        self.dependencies = list(map(str.strip, dependencies.lstrip("[").rstrip("]").split(",")))

    @classmethod
    def from_config(cls, module_name: str, config: Union[Dict[str, str], None] = None) -> "PluginWrapper":
        if config is None:
            config = {}
        plugin = PluginWrapper(
            name=config.get("Name", module_name),
            module_name=module_name,
            author=config.get("Author", None)
        )
        if "Depends-On" in config:
            plugin.load_dependencies(config["Depends-On"])
        return plugin
