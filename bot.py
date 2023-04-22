import asyncio
import importlib
import logging
import os
import signal
import sys
from asyncio import AbstractEventLoop
from datetime import datetime
from typing import Any, List, Callable, Union

import discord
from discord import ActivityType, Message, DMChannel, ApplicationContext, Interaction, InteractionType, Reaction, \
    RawReactionActionEvent, Member, User
from discord.ext import commands, tasks
from discord.ext.commands import CommandOnCooldown, CheckFailure
from dotenv import load_dotenv

import accounting_bot.commands
from accounting_bot import sheet, utils, exceptions, ext_utils
from accounting_bot.commands import BaseCommands, HelpCommand, FRPsState
from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.discordLogger import PycordHandler
from accounting_bot.exceptions import InputException
from accounting_bot.localisation import LocalisationHandler
from accounting_bot.universe import data_utils, pi_planer
from accounting_bot.universe.universe_database import UniverseDatabase
from accounting_bot.utils import log_error, State, send_exception, get_cmd_name, ShutdownOrderType, shutdown_procedure

logger = logging.getLogger()
log_filename = "logs/" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
print("Logging outputs goes to: " + log_filename)
if not os.path.exists("logs/"):
    os.mkdir("logs")
formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s")

# File log handler
file_handler = logging.FileHandler(log_filename)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
# Console log handler
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.DEBUG)
console.setFormatter(formatter)
logger.addHandler(console)
# Discord channel log handler
discord_handler = PycordHandler(level=logging.WARNING)
discord_handler.setFormatter(formatter)
logger.addHandler(discord_handler)
# Root logger
logger.setLevel(logging.INFO)
SILENT_EXCEPTIONS = [
    CommandOnCooldown, InputException, commands.NoPrivateMessage, commands.NotOwner, commands.PrivateMessageOnly,
    CheckFailure
]
interaction_logger = logging.getLogger("bot.access")


class BotState:
    def __init__(self) -> None:
        self.state = State.online
        self.ocr = False
        self.bot = None  # type: AccountingBot | None
        self.config = None  # type: Config | None
        self.guild = -1
        self.owner = -1
        self.admins = []  # type: List[int]
        self.reloadFuncs = []  # type: List[Callable[[BotState], None]]
        self.user_role = None  # type: int | None
        self.db_connector = None  # type: DatabaseConnector | None
        self.extensions = {}

    def reload(self):
        self.guild = self.config["server"]
        self.owner = self.config["owner"]
        self.admins = self.config["admins"]
        self.user_role = self.config["user_role"]
        if self.user_role is None:
            logger.warning("User role is not defined, all users will be allowed to execute the commands")
        for func in self.reloadFuncs:
            func(self)

    def is_online(self):
        return self.state.value >= State.online.value


STATE = BotState()
STATE.state = State.preparing
exceptions.STATE = STATE
sheet.STATE = STATE
utils.STATE = STATE

loop = asyncio.get_event_loop()

# loading env
logging.info("Loading .env...")
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# loading json config
logging.info("Loading JSON Config...")
config_structure = {
    "prefix": (str, "§"),
    "server": (int, -1),
    "test_server": (int, -1),
    "extensions": (list, []),
    "user_role": (int, -1),
    "logChannel": (int, -1),
    "adminLogChannel": (int, -1),
    "menuMessage": (int, -1),
    "menuChannel": (int, -1),
    "owner": (int, -1),
    "logToChannel": (bool, False),
    "errorLogChannel": (int, -1),
    "admins": (list, []),
    "shipyard_admins": (list, []),
    "frpMenuChannel": (int, -1),
    "frpMenuMessage": (int, -1),
    "frpRolePing": (int, -1),
    "db": {
        "user": (str, "N/A"),
        "password": (str, "N/A"),
        "port": (int, -1),
        "host": (str, "N/A"),
        "name": (str, "N/A"),
        "universe_name": (str, "N/A")
    },
    "google_sheet": (str, "N/A"),
    "project_resources": (list, [],),
    "pytesseract_cmd_path": (str, "N/A"),
    "logger": {
        "sheet": (str, "INFO")
    },
    "killmail_parser": {
        "channel": (int, -1),
        "admins": (list, []),
        "home_regions": (list, []),
        "field_id": (str, ""),
        "regex_id": (str, ".*"),
        "field_final_blow": (str, ""),
        "regex_final_blow": (str, ".*"),
        "field_ship": (str, ""),
        "regex_ship": (str, ".*"),
        "field_kill_value": (str, ""),
        "regex_kill_value": (str, ".*"),
        "field_system": (str, ""),
        "regex_system": (str, ".*"),
    }
}
config = Config("config.json", ConfigTree(config_structure))
config.load_config()
config.save_config()
STATE.config = config
logging.info("Config loaded")
ACCOUNTING_LOG = config["logChannel"]
KILLMAIL_CHANNEL = config["killmail_parser.channel"]
if KILLMAIL_CHANNEL == -1:
    KILLMAIL_CHANNEL = None
if KILLMAIL_CHANNEL is None:
    logger.warning("Killmail channel is not specified, automated killmail parsing will be disabled")
if config["killmail_parser.field_id"] == "":
    logger.warning("Killmail format is not specified, automated killmail parsing will be disabled")

STATE.db_connector = DatabaseConnector(
    username=config["db.user"],
    password=config["db.password"],
    port=config["db.port"],
    host=config["db.host"],
    database=config["db.name"]
)
data_utils.db = UniverseDatabase(
    username=config["db.user"],
    password=config["db.password"],
    port=config["db.port"],
    host=config["db.host"],
    database=config["db.universe_name"]
)
data_utils.killmail_config = config["killmail_parser"]
data_utils.killmail_admins = config["killmail_parser.admins"]

logging.info("Starting up bot...")
STATE.state = State.starting
intents = discord.Intents.default()
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.message_content = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.reactions = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.members = True


class AccountingBot(commands.Bot):
    def __init__(self, state=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = state or BotState()
        self.embeds = {}


bot = AccountingBot(
    state=STATE,
    command_prefix=config["prefix"],
    intents=intents,
    # debug_guilds=[config["test_server"], config["server"]],
    help_command=None,
    owner_id=config["owner"]
)
STATE.bot = bot

# STATE.reloadFuncs.append(accounting.setup)
STATE.reloadFuncs.append(utils.setup)
# STATE.reloadFuncs.append(projects.setup)
STATE.reloadFuncs.append(accounting_bot.commands.setup)

bot.add_cog(HelpCommand(STATE))
bot.add_cog(BaseCommands(STATE))

STATE.reload()


# noinspection PyUnusedLocal
def get_locale(ctx: commands.Context):
    return "de"


localisation = LocalisationHandler()
localisation.load_from_xml("resources/translations.xml")
localisation.init_bot(bot, get_locale)


@shutdown_procedure(order=ShutdownOrderType.user_input)
def shutdown_commands():
    logger.warning("Disabling discord commands")
    bot.remove_cog("BaseCommands")
    bot.remove_cog("ProjectCommands")
    bot.remove_cog("UniverseCommands")
    bot.remove_cog("HelpCommand")


@shutdown_procedure(order=ShutdownOrderType.final)
async def shutdown_bot():
    logger.warning("Closing bot")
    await bot.close()


# noinspection PyUnusedLocal
@bot.event
async def on_error(event_name, *args, **kwargs):
    info = sys.exc_info()
    if info and len(info) > 2 and info[0] == discord.errors.NotFound:
        logging.warning("discord.errors.NotFound Error in %s: %s", event_name, str(info[1]))
        return
    if info and len(info) > 2:
        utils.log_error(logger, info[1], location="bot.event.on_error")
    else:
        logging.exception("An unhandled error occurred: %s", event_name)
    pass


# noinspection PyUnusedLocal
def handle_asyncio_exception(error_loop: AbstractEventLoop, context: dict[str, Any]):
    logger.error("Unhandled exception in event_loop: %s", context["message"])
    if "exception" in context:
        utils.log_error(logger, error=context["exception"], location="event_loop")


@tasks.loop(seconds=10.0)
async def log_loop():
    """
    Logging loop. Will send every 10 seconds all logs into a specified discord channel.
    See :class:`accounting_bot.discordLogger.PycordHandler` for more details.
    """
    try:
        await discord_handler.process_logs()
        if log_loop.minutes != 0 and log_loop.seconds != 10:
            # If the log_loop intervall is not set to the default value (e.g. because of an exception, see below),
            # the interval is reset to default if no error occurred this time.
            log_loop.change_interval(minutes=0, seconds=10)
    except Exception as e:
        utils.log_error(logger, e, location="log_loop")
        # Pausing log_loop to avoid the exception getting spammed
        logger.warning("Pausing log_loop for 10 Minutes")
        log_loop.change_interval(minutes=10, seconds=0)


@tasks.loop(hours=12)
async def market_loop():
    try:
        logger.info("Reloading market data")
        await data_utils.init_market_data()
        pi_planer.item_prices = await data_utils.get_market_data(item_type="pi")
        pi_planer.available_prices = await data_utils.get_available_market_data("pi")
        logger.info("Market data reload completed")
        await pi_planer.reload_pending_resources()
    except Exception as e:
        utils.log_error(logger, e, location="market_loop")


@tasks.loop(seconds=20)
async def frp_reminder_loop():
    try:
        await FRPsState.defaultState.tick()
    except Exception as e:
        utils.log_error(logger, e, location="frp_reminder_loop")


@bot.event
async def on_application_command_error(ctx: ApplicationContext, err):
    """
    Exception handler for slash commands.

    :param ctx:     Context
    :param err:   the error that occurred
    """
    silent = False
    for cls in SILENT_EXCEPTIONS:
        if isinstance(err, cls):
            silent = True
            break
    location = accounting_bot.commands.get_cmd_name(ctx.command)
    if location is not None:
        location = "command " + location
    log_error(logger, err, location=location, ctx=ctx, minimal=silent)
    await send_exception(err, ctx)


@bot.event
async def on_command_error(ctx: commands.Context, err: commands.CommandError):
    silent = False
    for cls in SILENT_EXCEPTIONS:
        if isinstance(err, cls):
            silent = True
            break
    location = get_cmd_name(ctx.command)
    if location is not None:
        location = "prefixed command " + location
    log_error(logging.getLogger(), err, location=location, minimal=silent)
    await send_exception(err, ctx)


@bot.event
@ext_utils.event_handler
async def on_ready():
    logging.info("Logged in!")
    guilds = await bot.fetch_guilds().flatten()
    guild_msg = ""
    for guild in guilds:
        if guild.id != config["server"] and guild.id != config["test_server"]:
            logging.warning("Bot is in unknown guild: %s:%s, owner %s:%s",
                            guild.id, guild.name, guild.owner.name, guild.owner.id)
        guild_msg += f"'{guild.name}', "
    logger.info("Bot is in %s guilds: %s", len(guilds), utils.rchop(guild_msg, ", "))
    logging.info("Setting up channels...")
    # Basic setup
    if config["menuChannel"] == -1:
        return
    if config["logToChannel"] and config["errorLogChannel"] != -1:
        logging.info("Discord logchannel: %s", config["errorLogChannel"])
        log_channel = await bot.fetch_channel(config["errorLogChannel"])
        discord_handler.set_channel(log_channel)
    else:
        logging.info("Discord logchannel is deactivated")

    # Updating frp menu
    try:
        if config["frpMenuChannel"] != -1 and config["frpMenuMessage"] != -1:
            channel = await bot.fetch_channel(config["frpMenuChannel"])
            msg = await channel.fetch_message(config["frpMenuMessage"])
            view = accounting_bot.commands.FRPsView()
            view.message = msg
            await view.refresh_msg()
            logger.info("FRP menu updated")
    except discord.NotFound:
        logger.warning("FRP menu message %s in channel %s not found",
                       config["frpMenuMessage"], config["frpMenuChannel"])

    # Basic setup completed, bot is operational
    STATE.state = State.online
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.idle, activity=activity)

    logger.info("Starting Google sheets API...")
    await sheet.setup_sheet(config, config["google_sheet"], config["project_resources"], config["logger.sheet"])
    await sheet.load_wallets(force=True, validate=True)
    logger.info("Google sheets API loaded.")
    if not market_loop.is_running():
        market_loop.start()

    # Reload projects
    await sheet.find_projects()
    # Setup completed
    logging.info("Setup complete.")
    await bot.change_presence(status=discord.Status.online, activity=activity)


@bot.event
async def on_message(message: Message):
    await bot.process_commands(message)
    if isinstance(message.channel, DMChannel):
        if message.author.id == bot.user.id:
            return
        has_img = False
        for att in message.attachments:
            if not att.content_type.startswith("image"):
                continue
            url = att.url
            if not "://cdn.discordapp.com".casefold() in url.casefold():
                return
            if not isinstance(message.channel, DMChannel):
                return
            has_img = True
            break
        if has_img:
            await message.reply("Image recognition is disabled in the current build")
    if message.channel.id == KILLMAIL_CHANNEL:
        if len(message.embeds) > 0:
            logger.info("Received message %s with embed, parsing killmail", message.id)
            state = await data_utils.save_killmail(message.embeds[0])
            if state == 1:
                await message.add_reaction("⚠️")
            elif state == 2:
                await message.add_reaction("✅")


@bot.event
async def on_application_command(ctx: ApplicationContext):
    cmd_name = accounting_bot.commands.get_cmd_name(ctx.command)
    interaction_logger.info(
        "Command '%s' called by %s:%s in channel %s",
        cmd_name,
        ctx.user.name,
        ctx.user.id,
        ctx.channel.id if not isinstance(ctx.channel, DMChannel) else "DM")


@bot.event
async def on_interaction(interaction: Interaction):
    if interaction.type == InteractionType.component or interaction.type == InteractionType.modal_submit:
        # noinspection PyUnresolvedReferences
        interaction_logger.info(
            "Interaction type %s called by %s:%s in channel %s message %s",
            interaction.type.name,
            interaction.user.name, interaction.user.id,
            interaction.channel_id if not isinstance(interaction.channel, DMChannel) else "DM",
            interaction.message.id if interaction.message is not None else "N/A")
    await bot.process_application_commands(interaction)


@bot.event
async def on_reaction_add(reaction: Reaction, user: Union[Member, User]):
    if (
            reaction.message.channel.id == accounting_bot.commands.FRPs_CHANNEL and
            reaction.message.author == bot.user and
            FRPsState.defaultState.view is not None and
            reaction.message != FRPsState.defaultState.view.message and
            reaction.emoji == "🗑️" and user != bot.user
    ):
        await reaction.message.delete(reason=f"Deleted by {user.id}:{user.name}")
        if FRPsState.defaultState.ping == reaction.message:
            FRPsState.defaultState.ping = None
            await FRPsState.defaultState.inform_users("Der Ping wurde gelöscht")


@bot.event
@ext_utils.event_handler
async def on_raw_reaction_add(reaction: RawReactionActionEvent):
    pass


@bot.event
async def on_raw_reaction_remove(reaction: RawReactionActionEvent):
    if (
            reaction.emoji.name == "✅" and
            reaction.channel_id == config["logChannel"] and
            reaction.user_id in config["admins"]
    ):
        logging.info(f"{reaction.user_id} removed checkmark from {reaction.message_id}!")


def save_config():
    """
    Saves the config
    """
    config.save_config()


def load_extensions():
    loaded = []
    failed = []
    for name in config["extensions"]:
        try:
            logger.info("Loading extension %s", name)
            mod = importlib.import_module(name)
            mod.setup(bot)
            loaded.append(name)
            STATE.extensions[name] = mod
        except ImportError:
            logger.error("Failed to load extension %s: ImportError", name)
            failed.append(name)
        except AttributeError as e:
            logger.error("Failed to load extension %s: AttributeError", name)
            utils.log_error(logger, e, "extension_loader")
            failed.append(name)
        except Exception as e:
            logger.error("Failed to load extension %s", name)
            utils.log_error(logger, e, "extension_loader")
            failed.append(name)
    if len(failed) == 0:
        logger.info("Loaded %s extensions: %s", len(loaded), loaded)
    else:
        logger.warning("Loaded %s of %s extensions: %s", len(loaded), len(loaded) + len(failed), loaded)
        logger.warning("Couldn't load these extensions: %s", failed)


async def run_bot():
    try:
        load_extensions()
        logger.warning("Logging in bot")
        await bot.start(token=TOKEN)
    except Exception as e:
        logging.critical("Bot crashed", e)
        STATE.state = State.offline
        await utils.terminate_bot()


async def main():
    await asyncio.gather(run_bot())


async def kill_bot(signum):
    """
    Shuts down the bot
    """
    STATE.state = State.terminated
    logging.critical("Received signal %s, stopping bot", signal.Signals(signum).name)
    await utils.terminate_bot()


@tasks.loop(seconds=1.0)
async def kill_loop():
    """
    In case loop.add_signal_handler does not work, this loop will be started by :func:`kill_bot_sync` as the bot has to
    be stopped asynchronously.
    """
    kill_loop.stop()
    logging.critical("Stopping bot")
    await utils.terminate_bot()


# noinspection PyUnusedLocal
def kill_bot_sync(signum, frame):
    """
    Alternate kill function in case `loop.add_signal_handler` does not work.
    """
    logging.critical("Received signal %s, starting kill-loop", signal.Signals(signum).name)
    kill_loop.start()


try:
    # Try to add signal handlers to the event loop, this may not work on all operating systems
    loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(kill_bot(signal.SIGTERM)))
    loop.add_signal_handler(signal.SIGINT, lambda: asyncio.ensure_future(kill_bot(signal.SIGINT)))
except NotImplementedError:
    # If the event loop does not support signal handlers, they will be handled directly
    signal.signal(signal.SIGTERM, kill_bot_sync)
    signal.signal(signal.SIGINT, kill_bot_sync)

log_loop.start()
frp_reminder_loop.start()
loop.set_exception_handler(handle_asyncio_exception)
loop.run_until_complete(main())
logger.info("Bot stopped")
sys.exit(0)
