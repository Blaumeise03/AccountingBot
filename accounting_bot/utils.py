import asyncio
import datetime
import difflib
import functools
import io
import json
import logging
import math
import re
import traceback
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from os.path import exists
from typing import Union, Tuple, Optional, TYPE_CHECKING, Type, List, Callable, TypeVar, Dict

import cv2
import discord
from discord import Interaction, ApplicationContext, InteractionResponded, ActivityType, Member
from discord.ext import commands
from discord.ext.commands import Bot, Context, Command, CheckFailure
from discord.ui import View, Modal, Item
from numpy import ndarray

from accounting_bot import exceptions
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import LoggedException, NoPermissionsException, BotOfflineException, ConfigException

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.utils")
CONFIG = None  # type: Config | None
BOT = None  # type: Bot | None
STATE = None  # type: BotState | None

discord_users = {}  # type: {str: int}
ingame_twinks = {}
ingame_chars = []
main_chars = []
resource_order = []  # type: List[str]

if exists("discord_ids.json"):
    with open("discord_ids.json") as json_file:
        discord_users = json.load(json_file)

executor = ThreadPoolExecutor(max_workers=5)
loop = asyncio.get_event_loop()
_T = TypeVar("_T")

help_infos = {}  # type: Dict[Callable, str]
BOUNTY_ADMINS = []


def setup(state: "BotState"):
    global STATE, CONFIG, BOT, resource_order, BOUNTY_ADMINS
    STATE = state
    CONFIG = STATE.config
    BOT = STATE.bot
    resource_order = state.config["project_resources"]
    BOUNTY_ADMINS = state.config["killmail_parser.admins"]


def wrap_async(func: Callable[..., _T]):
    @functools.wraps(func)
    async def run(*args, **kwargs) -> _T:
        return await loop.run_in_executor(executor, functools.partial(func, *args, **kwargs))
    return run


def parse_number(string: str) -> (int, str):
    """
    Converts a string into an integer. It ignores all letters, spaces and commas. A dot will be interpreted as a
    decimal seperator. Everything after the first dot will be discarded.

    :param string: the string to convert
    :return: the number or None if it had an invalid format
    """
    warnings = ""
    dots = string.count(".")
    comma = string.count(",")
    if dots > 1 >= comma:
        string = string.replace(",", ";")
        string = string.replace(".", ",")
        string = string.replace(";", ".")
        warnings += "Warnung: Es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach " \
                    "dem Format \"1.000.000,00 ISK\" geparsed. " \
                    "Bitte zur Vermeidung von Fehlern das Englische Zahlenformat verwenden!\n"
    elif ("," in string) or ("." in string):
        warnings += "Hinweis: Es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach " \
                    "dem Format \"1,000,000.00 ISK\" geparsed.\n"

    if bool(re.match(r"[0-9]+(,[0-9]+)*(\.[0-9]+)?[a-zA-Z]*", string)):
        number = re.sub(r"[,a-zA-Z ]", "", string).split(".", 1)[0]
        return int(number), warnings
    else:
        return None, ""


def get_cmd_name(cmd: Union[Command, discord.ApplicationCommand, None]) -> Optional[str]:
    if cmd is None:
        return None
    return f"{cmd.full_parent_name} {cmd.name}".strip()


def get_error_location(ctx: Union[ApplicationContext, Interaction, Context]):
    if isinstance(ctx, Interaction):
        location = "interaction in channel {} in guild {}, user {}:{}" \
            .format(ctx.channel_id, ctx.guild_id, ctx.user.name, ctx.user.id)
    elif isinstance(ctx, Context):
        location = "prefixed command \"{}\", user {}:{} in channel {}, guild {}, " \
            .format(get_cmd_name(ctx.command),
                    ctx.channel.id,
                    ctx.guild.id if ctx.guild is not None else "N/A",
                    ctx.author.name,
                    ctx.author.id)
    elif isinstance(ctx, ApplicationContext):
        location = "command \"{}\" in channel {} in guild {}, user {}:{}" \
            .format(get_cmd_name(ctx.command),
                    ctx.channel.id,
                    ctx.guild.id if ctx.guild is not None else "N/A",
                    ctx.user.id,
                    ctx.user.name)
    else:
        raise TypeError(f"Expected Interaction or ApplicationContext, got {type(ctx)}")
    return location


def rchop(s: str, suffix: str):
    if suffix and s.endswith(suffix):
        return s[:-len(suffix)]
    return s


def get_cause_chain(error: Exception, sep="\n"):
    error_chain = ""
    err = error
    while err is not None:
        if err is err.__cause__:
            err = err.__cause__
            continue
        error_chain += err.__class__.__name__ + (f": {str(err)}" if len(str(err)) > 0 else "") + sep
        if err.__cause__ is not None:
            error_chain += "caused by "
        err = err.__cause__
    return rchop(error_chain, sep)


def get_user_error_msg(error: Exception):
    error_chain = get_cause_chain(error)
    if isinstance(error, LoggedException):
        return f"An unexpected error occurred: \n```\n{error_chain}\n```\n" \
               f"For more details, take a look at the attached log."
    else:
        return f"An unexpected error occurred: \n```\n{error_chain}\n```"


# noinspection PyShadowingNames
def log_error(logger: logging.Logger,
              error: Exception,
              location: Optional[Union[str, Type]] = None,
              ctx: Union[ApplicationContext, Interaction, Context] = None,
              minimal: bool = False):
    location = location if type(location) == str else f"class {location.__name__}" if location else None
    if error and error.__class__ == discord.errors.NotFound:
        logger.warning("discord.errors.NotFound Error at %s: %s", location, str(error))
        return

    full_error = traceback.format_exception(type(error), error, error.__traceback__)

    if error and error.__class__ == exceptions.BotOfflineException:
        if len(full_error) > 2:
            full_error = [full_error[0], full_error[-2], full_error[-1]]

    if ctx is not None:
        if location is not None:
            err_msg = "An error occurred at {} caused by {}".format(location, get_error_location(ctx))
        else:
            err_msg = "An error occurred, caused by {}".format(get_error_location(ctx))
    else:
        if location is not None:
            err_msg = "An error occurred at {}".format(location)
        else:
            err_msg = "An error occurred"

    if minimal:
        logger.info(err_msg + ":")
        logger.info("Ignored error: %s", get_cause_chain(error, ", "))
        return

    logger.error(err_msg)
    regexp = re.compile(r" *File .*[/\\]site-packages[/\\]((discord)|(sqlalchemy)).*")
    skipped = 0
    for line in full_error:
        if regexp.search(line):
            skipped += 1
            continue
        for line2 in line.split("\n"):
            if len(line2.strip()) > 0:
                logger.exception(line2, exc_info=False)
    logger.warning("Skipped %s traceback frames", skipped)


async def send_exception(error: Exception, ctx: Union[ApplicationContext, Context, Interaction]):
    location = get_error_location(ctx)
    if isinstance(error, discord.NotFound):
        logger.info("Ignoring NotFound error caused by %s", location)
        return

    if isinstance(ctx, Context):
        try:
            await ctx.author.send(f"An unexpected error occurred: {error.__class__.__name__}\n{str(error)}")
        except discord.Forbidden:
            pass
        return
    err_msg = get_user_error_msg(error)
    try:
        try:
            # Defer interaction to ensure we can use a followup
            await ctx.response.defer(ephemeral=True, invisible=False)
        except InteractionResponded:
            pass
        if isinstance(error, LoggedException):
            # Append additional log
            await ctx.followup.send(err_msg, file=string_to_file(error.get_log()), ephemeral=True)
        else:
            await ctx.followup.send(err_msg, ephemeral=True)
    except discord.NotFound:
        try:
            await ctx.user.send(err_msg)
        except discord.Forbidden:
            pass
        logger.warning("Can't send error message for \"%s\", caused by %s: NotFound",
                       error.__class__.__name__,
                       location)


def image_to_file(img: ndarray, encoding: str, filename: str):
    if img is None:
        return None
    is_success, im_buf_arr = cv2.imencode(encoding, img)
    if is_success:
        img_byte = io.BytesIO(im_buf_arr)
        file = discord.File(img_byte, filename)
        return file
    return None


def string_to_file(text: str, filename="message.txt"):
    data = io.BytesIO(text.encode())
    data.seek(0)
    return discord.File(fp=data, filename=filename)


def list_to_string(line: List[str]):
    res = ""
    for s in line:
        res += s + "\n"
    return res


def str_to_list(text: str, sep=";") -> List[str]:
    if text is None:
        text_list = []
    else:
        text_list = text.split(sep)
        text_list = [r.strip() for r in text_list]
        text_list = list(filter(len, text_list))
    return text_list


def get_main_account(name: str = None, discord_id: int = None) -> Tuple[Union[str, None], Union[str, None], bool]:
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


def help_info(value: str = ""):
    def _callback(func: Callable):
        help_infos[func] = value
        return func
    return _callback


def admin_only(admin_type="global") -> Callable[[_T], _T]:
    async def predicate(ctx: ApplicationContext) -> bool:
        is_admin = ctx.user.id in STATE.admins or ctx.user.id == STATE.owner
        if admin_type == "bounty" and not is_admin:
            is_admin = ctx.user.id in BOUNTY_ADMINS
        if not is_admin:
            raise CheckFailure("Can't execute command") \
                from NoPermissionsException("Only an administrators may execute this command")
        return True
    return commands.check(predicate)


def user_only() -> Callable[[_T], _T]:
    async def predicate(ctx: ApplicationContext) -> bool:
        is_admin = ctx.user.id in STATE.admins or ctx.user.id == STATE.owner
        is_user = False
        if isinstance(ctx.user, Member) and ctx.guild is not None and ctx.guild == STATE.guild:
            is_user = STATE.user_role is None or ctx.user.get_role(STATE.user_role) is not None
        else:
            guild = STATE.bot.get_guild(STATE.guild)
            if guild is None:
                guild = await STATE.bot.fetch_guild(STATE.guild)
            if guild is None:
                raise ConfigException(f"Guild with id {STATE.guild} not found")
            user = guild.get_member(ctx.user.id)
            if user is None:
                user = await guild.fetch_member(ctx.user.id)
            if user is not None and user.get_role(STATE.user_role) is not None:
                is_user = True
        if not is_admin and not is_user:
            raise CheckFailure("Can't execute command")\
                from NoPermissionsException("Only users with the member role may use this command")
        return True
    return commands.check(predicate)


def online_only() -> Callable[[_T], _T]:
    async def predicate(ctx: ApplicationContext) -> bool:
        if not STATE.is_online():
            raise CheckFailure("Can't execute command") \
                from BotOfflineException("Only an administrators may execute this command")
        return True
    return commands.check(predicate)


def main_guild_only() -> Callable[[_T], _T]:
    async def predicate(ctx: ApplicationContext) -> bool:
        if (ctx.guild is None or ctx.guild.id != STATE.guild) and ctx.user.id != STATE.owner:
            raise CheckFailure() from NoPermissionsException(
                "This command can only be executed when the bot is fully online")
        return True
    return commands.check(predicate)


class Item(object):
    def __init__(self, name: str, amount: Union[int, float]):
        self.name = name
        self.amount = amount

    @staticmethod
    def sort_list(items: List[Item], order: List[str]) -> None:
        for item in items:  # type: Item
            if item.name not in order:
                order.append(item.name)
        items.sort(key=lambda x: order.index(x.name) if x.name in order else math.inf)

    @staticmethod
    def parse_ingame_list(raw: str) -> List[Item]:
        items = []  # type: List[Item]
        for line in raw.split("\n"):
            if re.fullmatch("[a-zA-Z ]*", line):
                continue
            line = re.sub("\t", "    ", line.strip())  # Replace Tabs with spaces
            line = re.sub("^\\d+ *", "", line.strip())  # Delete first column (numeric Index)
            if len(re.findall("[0-9]+", line.strip())) > 1:
                line = re.sub(" *[0-9.]+$", "", line.strip())  # Delete last column (Valuation, decimal)
            item = re.sub(" +\\d+$", "", line)
            quantity = line.replace(item, "").strip()
            if len(quantity) == 0:
                continue
            item = item.strip()
            items.append(Item(item, int(quantity)))
        Item.sort_list(items, resource_order)
        return items

    @staticmethod
    def parse_list(raw: str, skip_negative=False) -> List[Item]:
        items = []  # type: List[Item]
        for line in raw.split("\n"):
            if re.fullmatch("[a-zA-Z ]*", line):
                continue
            line = re.sub("\t", "    ", line.strip())  # Replace Tabs with spaces
            line = re.sub("^\\d+ *", "", line.strip())  # Delete first column (numeric Index)
            item = re.sub(" +[0-9.]+$", "", line)
            quantity = line.replace(item, "").strip()
            if len(quantity) == 0:
                continue
            item = item.strip()
            quantity = float(quantity)
            if skip_negative and quantity < 0:
                continue
            items.append(Item(item, quantity))
        Item.sort_list(items, resource_order)
        return items

    @staticmethod
    def to_string(items):
        res = ""
        for item in items:
            res += f"{item.name}: {item.amount}\n"
        return res


class TransactionBase(ABC):
    @abstractmethod
    def has_permissions(self, user_name: int) -> bool:
        pass


# noinspection PyMethodMayBeStatic
class TransactionLike(TransactionBase, ABC):
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


class OCRBaseData:
    def __init__(self) -> None:
        super().__init__()
        self.bounding_box = None  # type: dict[str, int] | None
        self.img = None  # type: ndarray | None
        self.valid = False  # type: bool


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
            try:
                await self.message.edit(view=None)
            except discord.errors.HTTPException as e:
                logger.info("Can't edit view: %s", e)
                try:
                    c = await STATE.bot.fetch_channel(self.message.channel.id)
                    msg = await c.fetch_message(self.message.id)
                    await msg.edit(view=None)
                except discord.errors.HTTPException as e2:
                    logger.info("Can't fetch message of view to edit: %s", e2)
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
    BOT.remove_cog("BaseCommands")
    BOT.remove_cog("ProjectCommands")
    BOT.remove_cog("UniverseCommands")
    BOT.remove_cog("HelpCommand")
    logger.warning("Stopping data_utils executor")
    executor.shutdown(wait=True)
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
