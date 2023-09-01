import asyncio
import functools
import importlib
import inspect
import itertools
import logging
import os
import pkgutil
import re
import signal
import sys
from abc import ABC
from asyncio import AbstractEventLoop
from enum import Enum
from os import PathLike
from types import ModuleType
from typing import Dict, Union, List, Optional, Tuple, Any

import discord
from discord import ApplicationContext, ApplicationCommandError, User, Member, Embed, Color, option, Thread
from discord.abc import GuildChannel, PrivateChannel
from discord.ext import commands, tasks

from accounting_bot import utils, exceptions
from accounting_bot.config import Config
from accounting_bot.discordLogger import PycordHandler
from accounting_bot.exceptions import PluginLoadException, PluginNotFoundException, PluginDependencyException, \
    InputException
from accounting_bot.localization import LocalizationHandler
from accounting_bot.utils import State, log_error, send_exception, owner_only

logger = logging.getLogger("bot.main")

SILENT_EXCEPTIONS = [
    commands.CommandOnCooldown, InputException, discord.CheckFailure, commands.CheckFailure
]
LOUD_EXCEPTIONS = [
    exceptions.UnhandledCheckException
]
base_config = {
    "plugins": (list, []),
    "error_log_channel": (int, None),
    "admins": (list, []),
    "test_server": (int, -1),
    "main_server": (int, -1)
}


# noinspection PyUnusedLocal
def _handle_asyncio_exception(error_loop: AbstractEventLoop, context: dict[str, Any]):
    logger.error("Unhandled exception in event_loop: %s", context["message"])
    if "exception" in context:
        utils.log_error(logger, error=context["exception"], location="event_loop")


@functools.total_ordering
class PluginState(Enum):
    MISSING_DEPENDENCIES = -1
    CRASHED = 0
    UNLOADED = 1
    LOADED = 2
    ENABLED = 3

    def __repr__(self) -> str:
        return f"PluginStatus({self.name})"

    def __eq__(self, other):
        if not isinstance(other, PluginState):
            return False
        return other.value == self.value

    def __gt__(self, other):
        return self.value > other.value


# noinspection PyMethodMayBeStatic
class AccountingBot(commands.Bot):
    def __init__(self, config_path: str, pycord_handler: Optional[PycordHandler] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = State.offline
        self.embeds = {}
        self.plugins = []  # type: List[PluginWrapper]
        self.config = Config()
        self.config.load_tree(base_config)
        self.localization = LocalizationHandler()
        self.config_path = config_path
        self.admins = []  # type: List[int]
        # It is correct, trust me ;-)
        # noinspection PyTypeChecker
        self.add_application_command(cmd_status)
        # noinspection PyTypeChecker
        self.add_application_command(cmd_stop)
        self.pycord_handler = pycord_handler
        self.log_loop.start()

        def _get_locale(ctx: commands.Context):
            if isinstance(ctx, ApplicationContext) and ctx.locale is not None:
                if ctx.locale.startswith("en-"):
                    return "en"
                return ctx.locale
            return "en"

        self.localization.init_bot(self, _get_locale)

    def is_admin(self, user: Union[int, User, Member]):
        if isinstance(user, (User, Member)):
            if user.id in self.admins:
                return True
            return self.owner_id is not None and user.id == self.owner_id
        if type(user) == int:
            if user in self.admins:
                return True
            return self.owner_id is not None and user == self.owner_id
        raise TypeError(f"Expected User or int, got {type(user)}")

    def is_online(self):
        return self.state.value >= State.online.value

    def load_config(self) -> None:
        self.config.load_config(self.config_path)

    def save_config(self) -> None:
        self.config.save_config(self.config_path)

    def create_sub_config(self, root_key: str) -> Config:
        return self.config.create_sub_config(root_key)

    def load_localization(self, path: Union[PathLike, str]) -> None:
        """
        Adds a localization file, already existing values will be replaced.

        :param path: The path to the file
        """
        self.localization.load_from_xml(path)

    def load_plugins(self):
        self.state = State.preparing
        self.load_config()
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
                self.load_config()
                plugin.load_plugin(self)
                self.plugins.append(plugin)
            except PluginLoadException as e:
                logger.error("Error while loading plugin %s:%s", plugin.module_name, plugin.name)
                utils.log_error(logger, e)
        self.save_config()

    async def fetch_owner(self):
        app = await self.application_info()  # type: ignore
        if app.team:
            self.owner_ids = {m.id for m in app.team.members}
        else:
            self.owner_id = app.owner.id

    async def enable_plugins(self):
        self.state = State.starting
        if self.owner_id is None:
            await self.fetch_owner()
        for plugin in self.plugins:
            if plugin.state == PluginState.ENABLED:
                continue
            if self.state == State.terminated:
                logger.warning("Bot is terminated, aborting loading of plugins")
                return
            try:
                await plugin.enable_plugin()
            except PluginLoadException as e:
                logger.error("Error while enabling plugin %s:%s", plugin.module_name, plugin.name)
                utils.log_error(logger, e)

    async def stop(self):
        self.state = State.terminated
        for plugin in reversed(self.plugins):
            if plugin.state == PluginState.ENABLED:
                try:
                    await plugin.disable_plugin()
                except PluginLoadException as e:
                    logger.error("Error while disabling plugin %s:%s", plugin.module_name, plugin.name)
                    utils.log_error(logger, e)
        await asyncio.sleep(5)
        await self.close()

    async def on_error(self, event_name, *args, **kwargs):
        info = sys.exc_info()
        if info and len(info) > 2 and info[0] == discord.errors.NotFound:
            logging.warning("discord.errors.NotFound Error in %s: %s", event_name, str(info[1]))
            return
        if info and len(info) > 2:
            utils.log_error(logger, info[1], location=event_name if event_name else "bot.on_error")
        else:
            logging.exception("An unknown error occurred: %s", event_name)

    def get_plugin_by_cog(self, cog: Optional[commands.Cog]):
        if cog is None:
            return None
        for wrapper in self.plugins:
            if cog in wrapper.plugin.cogs:
                return wrapper
        return None

    def get_plugin(self, name: str, require_state=PluginState.LOADED):
        for wrapper in self.plugins:
            if wrapper.name == name or wrapper.module_name == name:
                if wrapper.state < require_state:
                    raise PluginLoadException(
                        f"Plugin {wrapper.name} has invalid state, required was {require_state.name}, got {wrapper.state.name}")
                return wrapper.plugin
        raise PluginNotFoundException(f"Plugin {name} was not found")

    def get_plugins(self, require_state: Optional[PluginState] = None, exact=True) -> List["PluginWrapper"]:
        res = []
        for wrapper in self.plugins:
            if require_state is None or (wrapper.state == require_state) or not exact and (
                    wrapper.state > require_state):
                res.append(wrapper)
        return res

    def has_plugin(self, name: str, require_state=PluginState.LOADED):
        for wrapper in self.plugins:
            if wrapper.name == name or wrapper.module_name == name:
                return wrapper.state >= require_state
        return False

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
        for cls in LOUD_EXCEPTIONS:
            if isinstance(err, cls):
                silent = False
                break
        location = None
        if plugin is not None:
            location = "plugin " + plugin.name
        log_error(plugin.plugin.logger if plugin else logger, err, location=location, ctx=ctx, minimal=silent)
        await send_exception(err, ctx)

    async def on_command_error(self, ctx: commands.Context, err: commands.CommandError):
        silent = False
        for cls in SILENT_EXCEPTIONS:
            if isinstance(err, cls):
                silent = True
                break
        for cls in LOUD_EXCEPTIONS:
            if isinstance(err, cls):
                silent = False
                break
        log_error(logging.getLogger(), err, minimal=silent)
        await send_exception(err, ctx)

    @tasks.loop(seconds=30)
    async def log_loop(self):
        try:
            await self.pycord_handler.process_logs()
        except Exception as e:
            utils.log_error(logger, e, location="log_loop")

    async def on_ready(self):
        logger.info("Bot has logged in")
        error_log = self.config["error_log_channel"]
        if error_log is not None and error_log != -1:
            channel = await self.get_or_fetch_channel(error_log)
            if channel is None:
                logger.error("Error log channel with id %s was not found", error_log)
            self.pycord_handler.set_channel(channel)
            logger.warning("Pycord log handler set to channel %s:%s in guild %s:%s",
                           channel.name, channel.id, channel.guild.name, channel.guild.id)
        else:
            logger.info("No error log channel defined in config")
        await self.enable_plugins()
        self.state = State.online

    async def get_or_fetch_channel(self, channel_id: int) -> Union[GuildChannel, Thread, PrivateChannel, None]:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        return channel

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.loop.set_exception_handler(_handle_asyncio_exception)
        self.load_plugins()
        loop = self.loop

        try:
            loop.add_signal_handler(signal.SIGINT, loop.stop)
            loop.add_signal_handler(signal.SIGTERM, loop.stop)
        except RuntimeError:
            pass

        async def runner():
            try:
                await self.start(*args, **kwargs)
            finally:
                if not self.is_closed():
                    await self.stop()

        def stop_loop_on_completion(f):
            loop.stop()

        future = asyncio.ensure_future(runner(), loop=loop)
        future.add_done_callback(stop_loop_on_completion)
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            logger.info("Received signal to terminate bot and event loop")
        finally:
            future.remove_done_callback(stop_loop_on_completion)
            logger.info("Cleaning up tasks")
            # noinspection PyProtectedMember
            discord.client._cleanup_loop(loop)

        if not future.cancelled():
            try:
                return future.result()
            except KeyboardInterrupt:
                # I am unsure why this gets raised here but suppress it anyway
                return None

        logger.info("Unloading plugins")
        for plugin in reversed(self.plugins):
            if plugin.state == PluginState.LOADED:
                try:
                    plugin.unload_plugin()
                except PluginLoadException as e:
                    logger.error("Error while unloading plugin %s:%s", plugin.module_name, plugin.name)
                    utils.log_error(logger, e)
        logger.info("Clean shutdown completed")


@commands.slash_command(name="status")
@option(name="silent", description="Execute the command silently", type=bool, required=False, default=True)
async def cmd_status(ctx: ApplicationContext, silent: bool):
    await ctx.response.defer(ephemeral=silent)
    embed = await build_status_embed(ctx.bot)
    await ctx.followup.send(embed=embed)


@commands.slash_command(name="stop", description="Shuts down the discord bot, if set up properly, it will restart")
@owner_only()
async def cmd_stop(ctx: ApplicationContext):
    logger.critical("Shutdown Command received, shutting down bot in 10 seconds")
    await ctx.respond("Bot wird gestoppt...")
    # noinspection PyTypeChecker
    bot = ctx.bot  # type: AccountingBot
    await bot.stop()


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


class PluginWrapper(object):
    def __init__(self, name: str, module_name: str, author: str = None,
                 dep_names: Optional[List[str]] = None, opt_dep_names: Optional[List[str]] = None) -> None:
        super().__init__()
        self.author = author
        self.module_name = module_name
        self.name = name
        self.plugin = None  # type: BotPlugin | None
        self.state = PluginState.UNLOADED
        self.dep_names = [] if dep_names is None else dep_names
        self.opt_dep_names = [] if opt_dep_names is None else opt_dep_names
        self.dependencies = []  # type: List[PluginWrapper]
        self.optional_dependencies = []  # type: List[PluginWrapper]
        self.required_by = []
        self.module = None  # type: ModuleType | None
        self.localization_raw = None  # type: Union[PathLike, str, None]
        self.localization_path = None  # type: Union[PathLike, str, None]

    def __repr__(self):
        return f"PluginWrapper(module={self.module_name})"

    def get_dep_names(self, dependencies: str):
        self.dep_names = list(
            filter(lambda s: len(s) > 0, map(str.strip, dependencies.lstrip("[").rstrip("]").split(","))))

    def get_opt_dep_names(self, dependencies: str):
        self.opt_dep_names = list(
            filter(lambda s: len(s) > 0, map(str.strip, dependencies.lstrip("[").rstrip("]").split(","))))

    def find_dependencies(self, plugins: List["PluginWrapper"]) -> Tuple[List["PluginWrapper"], List["PluginWrapper"]]:
        """
        Finds all the required dependencies if possible.
        Raises a PluginDependencyException if there are missing dependencies.

        :param plugins: The list of available plugins
        :return: The list of required and optional dependencies
        """
        res = []
        opt_res = []
        found = []
        for p in plugins:
            if p.module_name in self.dep_names:
                res.append(p)
                found.append(p.module_name)
                if self not in p.required_by:
                    p.required_by.append(self)
            if p.module_name in self.opt_dep_names:
                opt_res.append(p)
        if len(found) == len(self.dep_names):
            return res, opt_res
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

        logger.debug("Loading plugin %s", self.name)
        if self.state == PluginState.MISSING_DEPENDENCIES:
            raise PluginLoadException(f"Can't load plugin {self.module_name}: Missing dependencies")
        for p in self.dependencies:
            if p.state < PluginState.LOADED:
                raise PluginLoadException(
                    f"Can't load plugin {self.module_name}: Requirement {p.module_name} is not loaded: {p.state}")
        if not reload:
            if self.state > PluginState.UNLOADED:
                raise PluginLoadException(
                    f"Can't load plugin {self.module_name}: Plugin is already loaded with status " + self.state.name)
            self.module = importlib.import_module(self.module_name)
        else:
            self.module = importlib.reload(self.module)
        if self.localization_raw is not None:
            self.localization_path = os.path.join(os.path.dirname(self.module.__file__),
                                                  self.localization_raw)
            if not os.path.exists(self.localization_path):
                logger.error("Localization file for plugin %s is missing: %s",
                             self.name, self.localization_path)
                self.localization_path = None
            else:
                logger.info("Loaded localization for plugin %s", self.name)
        if self.localization_path is not None:
            bot.load_localization(self.localization_path)
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
            self.state = PluginState.CRASHED
            raise PluginLoadException(f"Plugin {self.module_name} crashed during loading") from e
        logger.debug("Loaded plugin %s", self.name)
        self.state = PluginState.LOADED

    async def enable_plugin(self):
        if self.state == PluginState.ENABLED:
            raise PluginLoadException(f"Plugin {self.module_name} is already enabled")
        if self.state != PluginState.LOADED:
            raise PluginLoadException(f"Plugin {self.module_name} is not loaded")
        for p in self.dependencies:
            if p.state < PluginState.ENABLED:
                raise PluginLoadException(
                    f"Can't load plugin {self.module_name}: Requirement {p.module_name} is not enabled: {p.state}")
        logger.info("Enabling plugin %s", self.name)
        try:
            await self.plugin.on_enable()
        except Exception as e:
            self.state = PluginState.CRASHED
            raise PluginLoadException(f"Loading of plugin {self.module_name} failed") from e
        self.state = PluginState.ENABLED
        logger.info("Enabled plugin %s", self.name)

    async def disable_plugin(self):
        if self.state != PluginState.ENABLED:
            raise PluginLoadException(f"Disabling of plugin {self.module_name} is not possible, as it's not enabled")
        try:
            logger.info("Disabling plugin %s", self.name)
            for cog in self.plugin.cogs:
                self.plugin.bot.remove_cog(cog.__cog_name__)
            await self.plugin.on_disable()
        except Exception as e:
            self.state = PluginState.CRASHED
            raise PluginLoadException(f"Disabling of plugin {self.module_name} failed") from e
        self.state = PluginState.LOADED
        logger.info("Disabled plugin %s", self.name)

    def unload_plugin(self):
        if self.state != PluginState.LOADED:
            raise PluginLoadException(f"Unloading of plugin {self.module_name} is not possible, as it's not loaded")
        try:
            self.plugin.on_unload()
        except Exception as e:
            self.state = PluginState.CRASHED
            raise PluginLoadException(f"Unloading of plugin {self.module_name} failed") from e
        self.state = PluginState.UNLOADED

    async def reload_plugin(self, bot: AccountingBot, force=False):
        logger.info("Reloading plugin %s", self.module_name)
        if self.state == PluginState.ENABLED:
            try:
                await self.disable_plugin()
            except PluginLoadException as e:
                if not force:
                    raise PluginLoadException(f"Reloading of plugin {self.module_name} failed") from e
                else:
                    logger.warning("Plugin %s threw an error while disabling, ignoring it", self.module_name)
                    utils.log_error(logger, e, "reload_plugin", minimal=True)
        if self.state == PluginState.LOADED:
            try:
                self.unload_plugin()
            except PluginLoadException as e:
                if not force:
                    raise PluginLoadException(f"Reloading of plugin {self.module_name} failed") from e
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
            author=config.get("Author", None),
        )
        if "Depends-On" in config:
            plugin.get_dep_names(config["Depends-On"])
        if "Localization" in config:
            plugin.localization_raw = config["Localization"]
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
    with open(plugin_path, "r", encoding="utf-8") as file:
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
            plugin.dependencies, plugin.optional_dependencies = plugin.find_dependencies(plugins)
            for dep in plugin.dependencies:
                if dep.state == PluginState.MISSING_DEPENDENCIES:
                    plugin.state = PluginState.MISSING_DEPENDENCIES
        except PluginDependencyException as e:
            logger.error("Failed to resolve dependencies for plugin %s: %s", plugin, str(e))
            if plugin is not None:
                plugin.state = PluginState.MISSING_DEPENDENCIES
    root = []
    for plugin in plugins:
        if len(plugin.dependencies) == 0 == len(plugin.optional_dependencies):
            root.append(plugin)
    if len(root) == 0:
        raise PluginDependencyException("Failed to resolve dependency tree root: no root plugin found")
    order = []
    left = plugins.copy()

    def _dep_filter(p: PluginWrapper):
        for d in itertools.chain(p.dependencies, p.optional_dependencies):
            if d not in order:
                return False
        return True

    while len(left) > 0:
        n = next(filter(_dep_filter, left))
        left.remove(n)
        order.append(n)
    return order


async def build_status_embed(bot: AccountingBot) -> Embed:
    owner = None
    if bot.owner_id is not None:
        o = bot.get_user(bot.owner_id)
        if o is None:
            try:
                o = await bot.fetch_user(bot.owner_id)
            except discord.NotFound:
                pass
        if o is not None:
            owner = o.name
    desc = f"Status: `{bot.state.name}`\nShard-ID: `{bot.shard_id}`\nShards: `{bot.shard_count}`\nPing: `{bot.latency:.3f} sec`\n" \
           f"Owner: `{owner}`"
    embed = Embed(title="Bot Status", colour=Color.gold(), description=desc)
    embed.add_field(
        name="Plugins", inline=False,
        value=f"```\n"
              f"All: {len(bot.get_plugins())}\n"
              f"Missing Dep: {len(bot.get_plugins(PluginState.MISSING_DEPENDENCIES))}\n"
              f"Crashed: {len(bot.get_plugins(PluginState.CRASHED))}\n"
              f"Unloaded: {len(bot.get_plugins(PluginState.UNLOADED))}\n"
              f"Loaded: {len(bot.get_plugins(PluginState.LOADED))}\n"
              f"Enabled: {len(bot.get_plugins(PluginState.ENABLED))}\n```"
    )
    desc = ""
    for plugin in sorted(map(lambda w: w.name, bot.get_plugins(PluginState.ENABLED))):
        desc += plugin + ", "
    desc = desc.rstrip(", ")
    if len(desc) == 0:
        desc = "N/A"
    embed.add_field(
        name="Enabled Plugins", inline=False,
        value=f"```\n{desc}\n```"
    )
    embed.set_thumbnail(url=str(bot.user.display_avatar.url))
    return embed
