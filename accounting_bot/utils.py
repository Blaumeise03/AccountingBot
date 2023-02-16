import asyncio
import datetime
import difflib
import io
import json
import logging
import traceback
from abc import ABC, abstractmethod
from enum import Enum
from os.path import exists
from typing import Union, Tuple, Optional, TYPE_CHECKING

import discord
from discord import Interaction, ApplicationContext, InteractionResponded, ActivityType
from discord.ext.commands import Bot
from discord.ui import View, Modal, Item

from accounting_bot import exceptions
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import LoggedException

if TYPE_CHECKING:
    from bot import BotState
    from accounting_bot.accounting import Transaction

logger = logging.getLogger("bot.utils")
CONFIG = None  # type: Config | None
BOT = None  # type: Bot | None
STATE = None  # type: BotState | None


def set_config(config: Config, bot):
    global CONFIG, BOT
    CONFIG = config
    BOT = bot


discord_users = {}  # type: {str: int}
ingame_twinks = {}
ingame_chars = []
main_chars = []

if exists("discord_ids.json"):
    with open("discord_ids.json") as json_file:
        discord_users = json.load(json_file)


# noinspection PyShadowingNames
def log_error(logger: logging.Logger, error: Exception, in_class=None,
              ctx: Union[ApplicationContext, Interaction] = None):
    class_name = in_class if type(in_class) == str else in_class.__name__ if in_class else "N/A"
    if error and error.__class__ == discord.errors.NotFound:
        logger.warning("discord.errors.NotFound Error in %s: %s", class_name, str(error))
        return

    full_error = traceback.format_exception(type(error), error, error.__traceback__)

    if error and error.__class__ == exceptions.BotOfflineException:
        if len(full_error) > 2:
            full_error = [full_error[0], full_error[-2], full_error[-1]]

    if isinstance(ctx, ApplicationContext):
        if ctx.guild is not None:
            # Error occurred inside a server
            err_msg = "An error occurred in class {} in guild {} in channel {}, sent by {}:{} during execution of command \"{}\"" \
                .format(class_name, ctx.guild.id, ctx.channel_id, ctx.user.id, ctx.user.name,
                        ctx.command.name)
        else:
            # Error occurred inside a direct message
            err_msg = "An error occurred in class {} outside of a guild in channel {}, sent by {}:{} during execution of command \"%s\"" \
                .format(class_name, ctx.channel_id, ctx.user.id, ctx.user.name,
                        ctx.command.name)
    elif isinstance(ctx, Interaction):
        err_msg = "An error occurred in class {} during interaction in guild {} in channel {}, user %s: {}" \
            .format(class_name, ctx.guild_id, ctx.channel_id, ctx.user.id, ctx.user.name)
    else:
        err_msg = "An error occurred in class {}".format(class_name)

    logger.error(err_msg)
    for line in full_error:
        for line2 in line.split("\n"):
            if len(line2.strip()) > 0:
                logger.exception(line2, exc_info=False)


async def send_exception(error: Exception, ctx: Union[ApplicationContext, Interaction]):
    if isinstance(ctx, Interaction):
        location = "interaction in channel {} in guild {}, user {}" \
            .format(ctx.channel_id, ctx.guild_id, ctx.user.id)
    else:
        location = "command in channel {} in guild {}, user {}:{}" \
            .format(ctx.channel_id, ctx.guild_id, ctx.user.id, ctx.user.name)
    if isinstance(error, discord.NotFound):
        logger.info("Ignoring NotFound error caused by %s", location)
        return
    if not isinstance(ctx, Interaction) and not isinstance(ctx, ApplicationContext):
        raise TypeError(f"Expected Interaction or ApplicationContext, got {type(ctx)}")
    try:
        try:
            # Defer interaction to ensure we can use a followup
            await ctx.response.defer(ephemeral=True)
        except InteractionResponded:
            pass
        if isinstance(error, LoggedException):
            # Append additional log
            await ctx.followup.send(f"Error: {str(error)}.\nFor more details, take a look at the log below.",
                                    file=string_to_file(error.get_log()), ephemeral=True)
        else:
            await ctx.followup.send(f"An unexpected error occurred: \n{error.__class__.__name__}\n{str(error)}",
                                    ephemeral=True)
    except discord.NotFound:
        try:
            await ctx.user.send(f"An unexpected error occurred: \n{error.__class__.__name__}\n{str(error)}")
        except discord.Forbidden:
            pass
        logger.warning("Can't send error message for \"%s\", caused by %s: NotFound", error.__class__.__name__,
                       location)


def string_to_file(text: str, filename="message.txt"):
    data = io.BytesIO(text.encode())
    data.seek(0)
    return discord.File(fp=data, filename=filename)


def list_to_string(line: [str]):
    res = ""
    for s in line:
        res += s + "\n"
    return res


def get_main_account(name: str = None, discord_id: int = None) -> (Union[str, None], Union[str, None], bool):
    """
    Finds the closest playername match for a given string. And returns the main account of this player, together with
    the parsed input name and the information, whether it was a perfect match.
    Alternatively searches for the character name belonging to the discord account.

    :param name: the string which should be looked up or
    :param discord_id: the id to search for
    :return:    Main Char: str or None,
                Char name: str or None,
                Perfect match: bool
    """
    if name is None and discord_id is None:
        return None, None, False
    if discord_id is not None:
        for main_char, d_id in discord_users.items():
            if d_id == discord_id:
                return main_char, main_char, True
        return None, None, False
    names = difflib.get_close_matches(name, ingame_chars, 1)
    if len(names) > 0:
        n = str(names[0])
        main_char = n
        if main_char in ingame_twinks:
            main_char = ingame_twinks[main_char]
        if name.casefold() == n.casefold():
            return main_char, n, True
        return main_char, n, False
    return None, None, False


def parse_player(string: str, users: [str]) -> (Union[str, None], bool):
    """
    Finds the closest playername match for a given string. It returns the name or None if not found, as well as a
    boolean indicating whether it was a perfect match.

    :param string: the string which should be looked up
    :param users: the available usernames
    :return: (Playername: str or None, Perfect match: bool)
    """
    names = difflib.get_close_matches(string, users, 1)
    if len(names) > 0:
        name = str(names[0])
        if name.casefold() == string.casefold():
            return str(names[0]), True
        return str(names[0]), False
    return None, False


async def get_or_find_discord_id(bot=None, guild=None, user_role=None, player_name="") \
        -> Tuple[Optional[int], Optional[str], Optional[bool]]:
    if bot is None:
        bot = BOT
    if guild is None:
        guild = CONFIG["server"]
    if user_role is None:
        user_role = CONFIG["user_role"]
    player_name = get_main_account(name=player_name)[0]

    discord_id = get_discord_id(player_name)
    if discord_id:
        return discord_id, player_name, True
    name, perfect, nicknames = await find_discord_id(bot, guild, user_role, player_name)
    if perfect:
        return nicknames[name], name, True
    return None, name, False


def get_discord_id(name: str):
    if name in discord_users:
        return discord_users[name]
    else:
        return None


async def find_discord_id(bot, guild, user_role, player_name):
    nicknames = dict(await bot.get_guild(guild)
                     .fetch_members()
                     .filter(lambda m: m.get_role(user_role) is not None)
                     .map(lambda m: (m.nick if m.nick is not None else m.name, m.id))
                     .flatten())
    name, perfect = parse_player(player_name, nicknames)
    return name, perfect, nicknames


def save_discord_id(name: str, discord_id: int):
    if name in discord_users and discord_users[name] == discord_id:
        return
    while discord_id in discord_users.values():
        for k, v in list(discord_users.items()):
            if v == discord_id:
                logger.warning("Deleted discord id %s (user: %s)", v, k)
                del discord_users[k]
    discord_users[name] = discord_id
    save_discord_config()


def save_discord_config():
    with open("discord_ids.json", "w") as outfile:
        json.dump(discord_users, outfile, indent=4)


# noinspection PyMethodMayBeStatic
class TransactionLike(ABC):
    def get_from(self) -> Optional[str]:
        return None

    def get_to(self) -> Optional[str]:
        return None

    @abstractmethod
    def get_amount(self) -> int:
        pass

    def get_time(self) -> Optional[datetime.datetime]:
        return None

    @abstractmethod
    def get_purpose(self) -> str:
        pass

    def get_reference(self) -> Optional[str]:
        return None


class ErrorHandledModal(Modal):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        log_error(logger, error, self.__class__, ctx=interaction)
        await send_exception(error, interaction)


class ErrorHandledView(View):
    def __init__(self, *items: Item,
                 timeout: Optional[float] = 300.0):
        super().__init__(*items, timeout=timeout)

    async def on_error(self, error: Exception, item, interaction):
        log_error(logger, error, self.__class__, ctx=interaction)
        await send_exception(error, interaction)


class AutoDisableView(ErrorHandledView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def on_timeout(self) -> None:
        logger.info("View %s timed out (%s) in channel %s.",
                    self.id,
                    self.message.id if self.message is not None else "None",
                    self.message.channel.id if self.message is not None else "None")
        if self.message is not None:
            await self.message.edit(view=None)
        self.clear_items()
        self.disable_all_items()


class State(Enum):
    terminated = 0
    offline = 1
    preparing = 2
    starting = 3
    online = 4


async def terminate_bot(connector: DatabaseConnector):
    logger.critical("Terminating bot")
    STATE.state = State.terminated
    activity = discord.Activity(name="Shutting down...", type=ActivityType.custom)
    await BOT.change_presence(status=discord.Status.idle, activity=activity)
    logger.warning("Disabling discord commands")
    BOT.remove_cog('BaseCommands')
    BOT.remove_cog('ProjectCommands')
    # Wait for all pending interactions to complete
    logger.warning("Waiting for interactions to complete")
    await asyncio.sleep(15)
    logger.warning("Closing SQL connection")
    connector.con.close()
    logger.warning("Closing bot")
    await BOT.close()
    await asyncio.sleep(10)
    # Closing the connection should end the event loop directly, causing the program to exit
    # Should this not happen within 10 seconds, the program will be terminated anyways
    logger.error("Force closing process")
    exit(1)
