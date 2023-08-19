import functools
import importlib
import inspect
import logging
import pkgutil
import re
import sys
from abc import ABC
from enum import Enum
from types import ModuleType
from typing import Dict, Union, List, Optional

import discord
from discord import ApplicationContext, ApplicationCommandError
from discord.ext import commands

from accounting_bot import utils
from accounting_bot.config import Config
from accounting_bot.exceptions import PluginLoadException, PluginNotFoundException, PluginDependencyException, \
    InputException
from accounting_bot.utils import State, log_error, send_exception

logger = logging.getLogger("bot.main")

SILENT_EXCEPTIONS = [
    commands.CommandOnCooldown, InputException, commands.NoPrivateMessage, commands.NotOwner, commands.PrivateMessageOnly,
    commands.CheckFailure
]


# noinspection PyMethodMayBeStatic
class AccountingBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = State.offline
        self.embeds = {}
        self.plugins = []  # type: List[PluginWrapper]
        self.config = Config()
        self.config.load_tree(base_config)

    def is_online(self):
        return self.state.value >= State.online.value

    def load_config(self, path: str):
        self.config.load_config(path)

    def load_plugins(self):
        plugins = []
        for plugin in self.config["plugins"]:
            try:
                plugins.append(PluginWrapper.from_config(plugin))
            except PluginLoadException as e:
                logger.error("Error while preparing plugin %s", plugin)
                utils.log_error(logger, e, location="plugin_loader")
        try:
            plugins = find_plugin_order(plugins)
        except PluginDependencyException as e:
            logger.error("Failed to resolve plugin load order, no plugin was loaded")
            utils.log_error(logger, e, location="plugin_loader")
            return
        for plugin in plugins:
            try:
                plugin.load_plugin(self)
                self.plugins.append(plugin)
            except PluginLoadException as e:
                logger.error("Error while loading plugin %s:%s", plugin.module_name, plugin.name)
                utils.log_error(logger, e)

    async def enable_plugins(self):
        for plugin in self.plugins:
            try:
                await plugin.enable_plugin()
            except PluginLoadException as e:
                logger.error("Error while enabling plugin %s:%s", plugin.module_name, plugin.name)
                utils.log_error(logger, e)

    async def shutdown(self):
        for plugin in reversed(self.plugins):
            if plugin.status == PluginStatus.ENABLED:
                try:
                    await plugin.disable_plugin()
                except PluginLoadException as e:
                    logger.error("Error while disabling plugin %s:%s", plugin.module_name, plugin.name)
                    utils.log_error(logger, e)

        for plugin in reversed(self.plugins):
            if plugin.status == PluginStatus.LOADED:
                try:
                    plugin.unload_plugin()
                except PluginLoadException as e:
                    logger.error("Error while unloading plugin %s:%s", plugin.module_name, plugin.name)
                    utils.log_error(logger, e)

    async def on_error(self, event_name, *args, **kwargs):
        info = sys.exc_info()
        if info and len(info) > 2 and info[0] == discord.errors.NotFound:
            logging.warning("discord.errors.NotFound Error in %s: %s", event_name, str(info[1]))
            return
        if info and len(info) > 2:
            utils.log_error(logger, info[1], location="bot.on_error")
        else:
            logging.exception("An unknown error occurred: %s", event_name)

    def get_plugin_by_cog(self, cog: Optional[commands.Cog]):
        if cog is None:
            return None
        for wrapper in self.plugins:
            if cog in wrapper.plugin.cogs:
                return wrapper
        return None

    async def on_application_command_error(self, ctx: ApplicationContext, err: ApplicationCommandError):
        """
        Exception handler for slash commands.

        :param ctx:     Context
        :param err:   the error that occurred
        """
        silent = False
        plugin = self.get_plugin_by_cog(ctx.cog)
        for cls in SILENT_EXCEPTIONS:
            if isinstance(err, cls):
                silent = True
                break
        location = None
        if plugin is not None:
            location = "plugin " + plugin.module_name
        log_error(plugin.plugin.logger if plugin else logger, err, location=location, ctx=ctx, minimal=silent)
        await send_exception(err, ctx)

    async def on_command_error(self, ctx: commands.Context, err: commands.CommandError):
        silent = False
        for cls in SILENT_EXCEPTIONS:
            if isinstance(err, cls):
                silent = True
                break
        log_error(logging.getLogger(), err, minimal=silent)
        await send_exception(err, ctx)

    async def on_ready(self):
        logger.info("Bot has logged in")
        await self.enable_plugins()


base_config = {
    "plugins": (list, []),
    "owner": (int, -1),
    "error_log_channel": (int, -1),
    "admins": (list, []),
    "test_server": (int, -1),
    "main_server": (int, -1)
}


class BotPlugin(ABC):
    """
    Plugin/Bot lifecycle:
        1. Bot preparation (loading basic config).
        2. Loading all plugins.
        3. Starting up bot (login to discord).
        4. Enabling all plugins.
    Shutdown:
        5. Disabling all plugins.
        6. Unloading all plugins.
        7. Disconnecting bot from api
    While the bot is running, for reloading a plugin:
        1. Disabling plugin if enabled
        2. Unloading plugin if loaded
        3. Reloading python module
        4. Loading plugin
        5. Enabling plugin
    Loading and unloading must be a synchronous task, while enabling and disabling must be async
    """

    def __init__(self, bot: AccountingBot, wrapper: "PluginWrapper", p_logger: Optional[logging.Logger]) -> None:
        super().__init__()
        self.bot = bot
        self._wrapper = wrapper
        self.logger = p_logger or logging.getLogger(self._wrapper.module_name)
        self.cogs = []  # type: List[commands.Cog]

    def info(self, msg, *args):
        self.logger.info(msg, *args)

    def warning(self, msg, *args):
        self.logger.warning(msg, *args)

    def error(self, msg, *args, exc_info: Exception):
        self.logger.error(msg, *args)
        utils.log_error(self.logger, exc_info)

    def register_cog(self, cog: commands.Cog):
        self.cogs.append(cog)
        self.bot.add_cog(cog)
        logger.info("Registered cog %s for plugin %s", cog.__cog_name__, self._wrapper.module_name)

    def remove_cog(self, name: str):
        for cog in self.cogs:
            if cog.name == name:
                self.cogs.remove(cog)
                break
        self.bot.remove_cog(name)

    def on_load(self):
        # Gets called before the Bot starts
        pass

    async def on_enable(self):
        # Gets called after the Bot logged in
        pass

    async def on_disable(self):
        # Gets called before the Bot shuts down
        pass

    def on_unload(self):
        # Gets called before reloading the extension
        pass


@functools.total_ordering
class PluginStatus(Enum):
    MISSING_DEPENDENCIES = -1
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
    def __init__(self, name: str, module_name: str, author: str = None,
                 dep_names: Union[List[str], None] = None) -> None:
        super().__init__()
        self.author = author
        self.module_name = module_name
        self.name = name
        self.plugin = None  # type: BotPlugin | None
        self.status = PluginStatus.UNLOADED
        self.dep_names = [] if dep_names is None else dep_names
        self.dependencies = []  # type: List[PluginWrapper]
        self.required_by = []
        self.module = None  # type: ModuleType | None

    def __repr__(self):
        return f"PluginWrapper(module={self.module_name})"

    def get_dep_names(self, dependencies: str):
        self.dep_names = list(filter(lambda s: len(s) > 0, map(str.strip, dependencies.lstrip("[").rstrip("]").split(","))))

    def find_dependencies(self, plugins: List["PluginWrapper"]) -> List["PluginWrapper"]:
        """
        Finds all the required dependencies if possible.
        Raises a PluginDependencyException if there are missing dependencies.

        :param plugins: The list of available plugins
        :return: The list of required dependencies
        """
        res = []
        found = []
        for p in plugins:
            if p.module_name in self.dep_names:
                res.append(p)
                found.append(p.module_name)
                if self not in p.required_by:
                    p.required_by.append(self)
        if len(found) == len(self.dep_names):
            return res
        if len(found) > len(self.dep_names):
            raise PluginDependencyException(
                f"Found more dependencies than required, found {found}, required: {self.dep_names}")
        diff = [d for d in self.dep_names if d not in found]
        raise PluginDependencyException(f"Missing dependencies: {diff}")

    def load_plugin(self, bot: AccountingBot, reload=False):
        def _filter(o):
            if not inspect.isclass(o):
                return False
            return inspect.getmodule(o).__name__ == self.module_name and issubclass(o, BotPlugin)

        logger.debug("Loading %s:%s", self.module_name, self.name)
        if self.status == PluginStatus.MISSING_DEPENDENCIES:
            raise PluginLoadException(f"Can't load plugin {self.module_name}: Missing dependencies")
        for p in self.dependencies:
            if p.status < PluginStatus.LOADED:
                raise PluginLoadException(f"Can't load plugin {self.module_name}: Requirement {p.module_name} is not loaded: {p.status}")
        if not reload:
            if self.status > PluginStatus.UNLOADED:
                raise PluginLoadException(f"Can't load plugin {self.module_name}: Plugin is already loaded with status " + self.status.name)
            self.module = importlib.import_module(self.module_name)
        else:
            self.module = importlib.reload(self.module)
        classes = inspect.getmembers(self.module, _filter)
        if len(classes) == 0:
            raise PluginNotFoundException("Can't find plugin class in module " + self.module_name)
        if len(classes) > 1:
            raise PluginLoadException(
                f"Can't load plugin {self.module_name}: The module contains multiple plugin classes")
        plugin_cls = classes[0][1]
        try:
            self.plugin = plugin_cls(bot, self)  # type: BotPlugin
            self.plugin.on_load()
        except Exception as e:
            self.status = PluginStatus.CRASHED
            raise PluginLoadException(f"Plugin {self.module_name} crashed during loading", e)
        logger.debug("Loaded %s:%s", self.module_name, self.name)
        self.status = PluginStatus.LOADED

    async def enable_plugin(self):
        if self.status == PluginStatus.ENABLED:
            raise PluginLoadException(f"Plugin {self.module_name} is already enabled")
        if self.status != PluginStatus.LOADED:
            raise PluginLoadException(f"Plugin {self.module_name} is not loaded")
        for p in self.dependencies:
            if p.status < PluginStatus.ENABLED:
                raise PluginLoadException(
                    f"Can't load plugin {self.module_name}: Requirement {p.module_name} is not enabled: {p.status}")
        logger.info("Enabling plugin %s:%s", self.module_name, self.name)
        try:
            await self.plugin.on_enable()
        except Exception as e:
            self.status = PluginStatus.CRASHED
            raise PluginLoadException(f"Loading of plugin {self.module_name} failed", e)
        self.status = PluginStatus.ENABLED
        logger.info("Enabled plugin %s:%s", self.module_name, self.name)

    async def disable_plugin(self):
        if self.status != PluginStatus.ENABLED:
            raise PluginLoadException(f"Disabling of plugin {self.module_name} is not possible, as it's not enabled")
        try:
            for cog in self.plugin.cogs:
                self.plugin.bot.remove_cog(cog.__cog_name__)
            await self.plugin.on_disable()
        except Exception as e:
            self.status = PluginStatus.CRASHED
            raise PluginLoadException(f"Disabling of plugin {self.module_name} failed", e)
        self.status = PluginStatus.LOADED

    def unload_plugin(self):
        if self.status != PluginStatus.LOADED:
            raise PluginLoadException(f"Unloading of plugin {self.module_name} is not possible, as it's not loaded")
        try:
            self.plugin.on_unload()
        except Exception as e:
            self.status = PluginStatus.CRASHED
            raise PluginLoadException(f"Unloading of plugin {self.module_name} failed", e)
        self.status = PluginStatus.UNLOADED

    async def reload_plugin(self, bot: AccountingBot, force=False):
        logger.info("Reloading plugin %s", self.module_name)
        if self.status == PluginStatus.ENABLED:
            try:
                await self.disable_plugin()
            except PluginLoadException as e:
                if not force:
                    raise PluginLoadException(f"Reloading of plugin {self.module_name} failed", e)
                else:
                    logger.warning("Plugin %s threw an error while disabling, ignoring it", self.module_name)
                    utils.log_error(logger, e, "reload_plugin", minimal=True)
        if self.status == PluginStatus.LOADED:
            try:
                self.unload_plugin()
            except PluginLoadException as e:
                if not force:
                    raise PluginLoadException(f"Reloading of plugin {self.module_name} failed", e)
                else:
                    logger.warning("Plugin %s threw an error while unloading, ignoring it", self.module_name)
                    utils.log_error(logger, e, "reload_plugin", minimal=True)
        self.load_plugin(bot, reload=True)
        await self.enable_plugin()
        logger.info("Reloaded plugin %s", self.module_name)

    @classmethod
    def from_config(cls, module_name: str, config: Union[Dict[str, str], None] = None) -> "PluginWrapper":
        if config is None:
            config = get_raw_plugin_config(module_name)
        plugin = PluginWrapper(
            name=config.get("Name", module_name),
            module_name=module_name,
            author=config.get("Author", None)
        )
        if "Depends-On" in config:
            plugin.get_dep_names(config["Depends-On"])
        return plugin


def get_raw_plugin_config(plugin_name: str) -> Dict[str, str]:
    """
    Loads the plugin config from a python module. The config has to be at the beginning of the file. All lines have to
    start with '#' or have to be empty. The config part has to start with "PluginConfig" and end with "End". An example
    config looks like this::
        # PluginConfig
        # Name: Name of the plugin
        # Author: Name of the author
        # Depends-On: a, list, of.plugins, that.are, required.for, this.plugin
        # End

    :param plugin_name: The module name of the plugin
    :return: A dictionary with the raw config options
    """
    module = pkgutil.get_loader(plugin_name)
    if module is None:
        raise PluginNotFoundException("Plugin \"" + plugin_name + "\" not found")
    # noinspection PyUnresolvedReferences
    plugin_path = module.get_filename()
    raw_settings = {}
    with open(plugin_path, "r") as file:
        is_config = False
        for line in file:
            if not is_config and not (len(line.lstrip()) == 0 or line.lstrip().startswith("#")):
                break
            if not is_config and "PluginConfig".casefold() in line.casefold():
                is_config = True
            if not is_config:
                continue
            trimmed = re.sub(r"^ *# *", "", line).rstrip("\n")
            if trimmed.casefold().startswith("End".casefold()):
                is_config = False
                break
            if trimmed.startswith("-"):
                continue
            if ":" not in trimmed:
                continue
            split = trimmed.split(":", 1)
            raw_settings[split[0].strip()] = split[1].strip()
    if is_config:
        logger.warning("Module %s has malformed config: Missing End-Tag", plugin_name)
    if len(raw_settings) == 0:
        logger.warning("Module %s has no config", plugin_name)
    return raw_settings


def prepare_plugin(plugin_name: str) -> PluginWrapper:
    """
    Loads the config for a given plugin and returns a PluginWrapper object.

    :param plugin_name: The module name of the plugin
    :return: The Wrapper object
    """
    cnfg = get_raw_plugin_config(plugin_name)
    plugin = PluginWrapper.from_config(plugin_name, cnfg)
    return plugin


def find_plugin_order(plugins: List[PluginWrapper]):
    if len(plugins) == 0:
        return []
    for plugin in plugins:
        try:
            plugin.dependencies = plugin.find_dependencies(plugins)
            for dep in plugin.dependencies:
                if dep.status == PluginStatus.MISSING_DEPENDENCIES:
                    plugin.status = PluginStatus.MISSING_DEPENDENCIES
        except PluginDependencyException as e:
            logger.error("Failed to resolve dependencies for plugin %s: %s", plugin, str(e))
            if plugin is not None:
                plugin.status = PluginStatus.MISSING_DEPENDENCIES
    root = []
    for plugin in plugins:
        if len(plugin.dependencies) == 0:
            root.append(plugin)
    if len(root) == 0:
        raise PluginDependencyException("Failed to resolve dependency tree root: no root plugin found")
    order = []
    left = plugins.copy()

    def _dep_filter(p: PluginWrapper):
        for d in p.dependencies:
            if d not in order:
                return False
        return True

    while len(left) > 0:
        n = next(filter(_dep_filter, left))
        left.remove(n)
        order.append(n)
    return order
