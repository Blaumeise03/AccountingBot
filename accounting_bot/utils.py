import asyncio
import calendar
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
from typing import Union, Tuple, Optional, Type, List, Callable, TypeVar, Dict, Coroutine, TYPE_CHECKING

import cv2
import discord
from discord import Interaction, ApplicationContext, InteractionResponded, ActivityType, Member, DMChannel, \
    ApplicationCommand
from discord.ext import commands
from discord.ext.commands import Bot, Context, Command, CheckFailure, NotOwner
from discord.ui import View, Modal, Item, Button
from numpy import ndarray

from accounting_bot import exceptions
from accounting_bot.config import Config
from accounting_bot.exceptions import LoggedException, NoPermissionException, BotOfflineException, ConfigException, \
    UnhandledCheckException

if TYPE_CHECKING:
    from accounting_bot.main_bot import AccountingBot

logger = logging.getLogger("bot.utils")
CONFIG = None  # type: Config | None
BOT = None  # type: Bot | None

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

cmd_annotations = {}  # type: Dict[Callable, List[CmdAnnotation]]
BOUNTY_ADMINS = []

terminate_funcs = []  # type: List[ShutdownProcedure]


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
        try:
            return await loop.run_in_executor(executor, functools.partial(func, *args, **kwargs))
        except RuntimeError as e:
            if "cannot schedule new futures after shutdown" in str(e):
                raise BotOfflineException(f"Can't start new executor task '{func.__name__}'") from e
            else:
                raise RuntimeError(f"Starting executor task '{func.__name__}' failed") from e
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


def get_month_edges(time: datetime.datetime):
    _, e = calendar.monthrange(time.year, time.month)
    start = datetime.datetime(time.year, time.month, 1)
    end = datetime.datetime(time.year, time.month, e, 23, 59, 59, 999)
    return start, end


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


def is_caused_by(error: Exception, class_or_tuple):
    while error is not None:
        if isinstance(error, class_or_tuple):
            return True
        error = error.__cause__
    return False


def get_user_error_msg(error: Exception):
    error_chain = get_cause_chain(error)
    if isinstance(error, LoggedException):
        return f"An error occurred: \n```\n{error_chain}\n```\n" \
               f"For more details, take a look at the attached log."
    else:
        return f"An error occurred: \n```\n{error_chain}\n```"


def get_minimal_traceback(trace: List[str]):
    last_line = None
    last_line_error = None
    regexp = re.compile(r" *File .*[/\\]site-packages[/\\][.\n]*")
    regexp_file = re.compile(r" *File [.\n]*")
    for line in trace:
        if regexp_file.match(line):
            if regexp.match(line):
                continue
            last_line = line
            last_line_error = None
        elif last_line is not None and last_line_error is None:
            last_line_error = line
    if last_line is None:
        return []
    if last_line_error is None:
        return [last_line]
    return [last_line, last_line_error]


# noinspection PyShadowingNames
def log_error(logger: logging.Logger,
              error: Exception,
              location: Optional[Union[str, Type]] = None,
              ctx: Union[ApplicationContext, Interaction, Context] = None,
              minimal: bool = False):
    location = location if type(location) == str else f"class {location.__name__}" if location else None
    full_error = traceback.format_exception(type(error), error, error.__traceback__)
    silent = False
    if error and is_caused_by(error, discord.errors.NotFound) and ("Unknown interaction" in str(error)):
        logger.warning("discord.errors.NotFound Error at %s: %s", location, str(error))
        full_error = get_minimal_traceback(full_error)
        silent = True

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

    if not silent:
        logger.error(err_msg)
    regexp = re.compile(r" *File .*[/\\]site-packages[/\\]((discord)|(sqlalchemy)).*")
    skipped = 0
    for line in full_error:
        if regexp.search(line):
            skipped += 1
            continue
        for line2 in line.split("\n"):
            if len(line2.strip()) > 0:
                if not silent:
                    logger.exception(line2, exc_info=False)
                else:
                    logger.warning(line2)
    if skipped > 0:
        logger.warning("Skipped %s traceback frames", skipped)


async def send_exception(error: Exception, ctx: Union[ApplicationContext, Context, Interaction]):
    location = get_error_location(ctx)
    ignore = False
    if is_caused_by(error, discord.errors.NotFound) and ("Unknown interaction" in str(error)):
        ignore = True

    err_msg = get_user_error_msg(error)
    if isinstance(ctx, Context):
        try:
            await ctx.author.send(err_msg)
        except discord.Forbidden:
            pass
        return

    try:
        try:
            if isinstance(error, LoggedException):
                # Append additional log
                await ctx.response.send_message(err_msg, file=string_to_file(error.get_log()), ephemeral=True)
            else:
                await ctx.response.send_message(err_msg, ephemeral=True)
        except InteractionResponded:
            if ignore:
                logger.info("Ignoring NotFound error caused by %s", location)
                return
            if isinstance(error, LoggedException):
                # Append additional log
                await ctx.followup.send(err_msg, file=string_to_file(error.get_log()), ephemeral=True)
            else:
                await ctx.followup.send(err_msg, ephemeral=True)
    except discord.errors.NotFound:
        if ignore:
            logger.info("Ignoring NotFound error caused by %s", location)
            return
        try:
            await ctx.user.send(err_msg)
            return
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
        return discord.File(img_byte, filename)
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


async def get_or_find_discord_id(bot: Bot = None, guild: int = None, user_role: int = None, player_name="") \
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


class CmdAnnotation(Enum):
    admin = "Admin only"
    owner = "Owner only"
    main_guild = "Main Server only"
    user = "Members only"
    guild = "Guild only"

    @staticmethod
    def annotate_cmd(func: Callable, annotation: "CmdAnnotation"):
        if func in cmd_annotations:
            cmd_annotations[func].append(annotation)
        else:
            cmd_annotations[func] = [annotation]

    @staticmethod
    def get_cmd_details(func: Callable):
        if func not in cmd_annotations or len(cmd_annotations[func]) == 0:
            return None
        msg = ""
        for a in cmd_annotations[func]:
            msg += a.value + ", "
        return rchop(msg, ", ")


def cmd_check(coro: Callable) -> Callable:
    """
    Command predicates should be annotated with this, all errors inside the predicate will get handled automatically.

    :param coro:
    :return:
    """
    async def _error_handled(*args, **kwargs):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            if isinstance(e, CheckFailure):
                raise e
            raise UnhandledCheckException("Unhandled error during command check") from e
    return _error_handled


def admin_only(admin_type="global") -> Callable[[_T], _T]:
    def decorator(func):
        @cmd_check
        async def predicate(ctx: ApplicationContext) -> bool:
            # noinspection PyTypeChecker
            bot = ctx.bot  # type: AccountingBot
            is_admin = bot.is_admin(ctx.user)
            if admin_type == "bounty" and not is_admin:
                is_admin = ctx.user.id in BOUNTY_ADMINS
            if not is_admin:
                raise CheckFailure("Can't execute command") \
                    from NoPermissionException("Only an administrators may execute this command")
            return True
        CmdAnnotation.annotate_cmd(func, CmdAnnotation.admin)
        return commands.check(predicate)(func)
    return decorator


def online_only() -> Callable[[_T], _T]:
    @cmd_check
    async def predicate(ctx: ApplicationContext) -> bool:
        # noinspection PyTypeChecker
        bot = ctx.bot  # type: AccountingBot
        if not bot.is_online():
            raise CheckFailure() from BotOfflineException("Can't execute the command while the bot is offline")
        return True
    return commands.check(predicate)


def owner_only() -> Callable[[_T], _T]:
    def decorator(func):
        @cmd_check
        async def predicate(ctx: Context) -> bool:
            if not await ctx.bot.is_owner(ctx.author):
                raise NotOwner("Command may only be used by the owner")
            return True

        CmdAnnotation.annotate_cmd(func, CmdAnnotation.owner)
        return commands.check(predicate)(func)
    return decorator


def guild_only() -> Callable:
    def inner(command: Callable):
        if isinstance(command, ApplicationCommand):
            command.guild_only = True
            CmdAnnotation.annotate_cmd(command.callback, CmdAnnotation.guild)
        else:
            command.__guild_only__ = True
            CmdAnnotation.annotate_cmd(command, CmdAnnotation.guild)
        return command
    return inner


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
            found = False
            for i in items:
                if i.name == item:
                    i.amount += int(quantity)
                    found = True
                    break
            if not found:
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


# noinspection PyUnusedLocal
class ConfirmView(AutoDisableView):
    def __init__(self, callback: Callable[[ApplicationContext], Coroutine], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.function = callback

    @discord.ui.button(label="Bestätigen", style=discord.ButtonStyle.green)
    async def btn_confirm(self, button: Button, ctx: ApplicationContext):
        await self.function(ctx)
        await self.message.delete()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.grey)
    async def btn_abort(self, button: Button, ctx: ApplicationContext):
        await ctx.response.defer(invisible=True)
        await self.message.delete()


class State(Enum):
    terminated = 0
    offline = 1
    preparing = 2
    starting = 3
    online = 4


class ShutdownOrderType(Enum):
    user_input = 0
    database = 1
    final = 2

    def __eq__(self, o: object) -> bool:
        if not isinstance(o, ShutdownOrderType):
            return False
        return o.value == self.value


def shutdown_procedure(order: ShutdownOrderType):
    def decorator(func: Callable):
        ShutdownProcedure(order, func)
    return decorator


class ShutdownProcedure(object):
    def __init__(self, order: ShutdownOrderType, callback: Callable):
        self.order = order
        self.callable = callback
        terminate_funcs.append(self)

    @staticmethod
    async def execute_phase(phase: ShutdownOrderType):
        for procedure in filter(lambda s: s.order == phase, terminate_funcs):
            try:
                if asyncio.iscoroutinefunction(procedure.callable):
                    await procedure.callable()
                else:
                    procedure.callable()
            except Exception as error:
                log_error(logger, error, location="shutdown")


@shutdown_procedure(order=ShutdownOrderType.user_input)
def shutdown_executor():
    logger.warning("Stopping data_utils executor")
    executor.shutdown(wait=True)


async def terminate_bot():
    logger.critical("Terminating bot")
    STATE.state = State.terminated
    activity = discord.Activity(name="Shutting down...", type=ActivityType.custom)
    if BOT is not None:
        await BOT.change_presence(status=discord.Status.idle, activity=activity)
    await ShutdownProcedure.execute_phase(ShutdownOrderType.user_input)
    # Wait for all pending interactions to complete
    logger.warning("Waiting for interactions to complete")
    await asyncio.sleep(15)
    await ShutdownProcedure.execute_phase(ShutdownOrderType.database)
    await ShutdownProcedure.execute_phase(ShutdownOrderType.final)
    await asyncio.sleep(10)
    # Closing the connection should end the event loop directly, causing the program to exit
    # Should this not happen within 10 seconds, the program will be terminated anyways
    logger.error("Force closing process")
    exit(1)
