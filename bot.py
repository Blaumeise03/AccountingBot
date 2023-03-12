import asyncio
import logging
import os
import signal
import sys
from asyncio import AbstractEventLoop
from datetime import datetime
from threading import Thread
from typing import Any

import discord
import mariadb
import pytesseract.pytesseract
from discord import ActivityType, Message, DMChannel, ApplicationContext
from discord.ext import commands, tasks
from discord.ext.commands import CommandOnCooldown
from dotenv import load_dotenv

import accounting_bot.commands
from accounting_bot import accounting, sheet, projects, utils, corpmissionOCR, exceptions
from accounting_bot.accounting import AccountingView, get_menu_embeds
from accounting_bot.commands import BaseCommands, HelpCommand, UniverseCommands
from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.discordLogger import PycordHandler
from accounting_bot.exceptions import InputException
from accounting_bot.universe import data_utils, pi_planer
from accounting_bot.universe.universe_database import UniverseDatabase
from accounting_bot.utils import log_error, State, send_exception, get_cmd_name

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


class BotState:
    def __init__(self) -> None:
        self.state = State.online
        self.ocr = False
        self.bot = None  # type: commands.Bot | None

    def is_online(self):
        return self.state.value >= State.online.value


STATE = BotState()
STATE.state = State.preparing
exceptions.STATE = STATE
sheet.STATE = STATE
corpmissionOCR.STATE = STATE
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
logging.info("Config loaded")
ACCOUNTING_LOG = config["logChannel"]
KILLMAIL_CHANNEL = config["killmail_parser.channel"]
if KILLMAIL_CHANNEL == -1:
    KILLMAIL_CHANNEL = None
if KILLMAIL_CHANNEL is None:
    logger.warning("Killmail channel is not specified, automated killmail parsing will be disabled")
if config["killmail_parser.field_id"] == "":
    logger.warning("Killmail format is not specified, automated killmail parsing will be disabled")

CONNECTOR = DatabaseConnector(
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
utils.resource_order = config["project_resources"]

try:
    if config["pytesseract_cmd_path"] != "N/A":
        pytesseract.pytesseract.tesseract_cmd = config["pytesseract_cmd_path"]
    else:
        config.save_config()
    tesseract_version = pytesseract.pytesseract.get_tesseract_version()
    logging.info("Tesseract version " + str(tesseract_version) + " installed!")
    STATE.ocr = True
except pytesseract.TesseractNotFoundError as error:
    logging.warning("Tesseract is not installed, OCR will be disabled. Please add tesseract to the PATH or to the "
                    "config.")

logging.info("Starting up bot...")
STATE.state = State.starting
intents = discord.Intents.default()
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.message_content = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.reactions = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.members = True
bot = commands.Bot(
    command_prefix=config["prefix"],
    intents=intents,
    debug_guilds=[config["test_server"], config["server"]],
    help_command=None
)
STATE.bot = bot

accounting.set_up(config, CONNECTOR, bot, STATE)
utils.set_config(config, bot)
projects.STATE = STATE

bot.add_cog(HelpCommand(STATE))
bot.add_cog(BaseCommands(config, CONNECTOR, STATE))
bot.add_cog(projects.ProjectCommands(bot, config["admins"], config["owner"], config["server"], config["user_role"]))
bot.add_cog(UniverseCommands(STATE))


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


@bot.event
async def on_application_command_error(ctx: ApplicationContext, err):
    """
    Exception handler for slash commands.

    :param ctx:     Context
    :param err:   the error that occurred
    """
    silent = False
    # Don't log command rate limit errors, but send a response to the interaction
    if isinstance(err, CommandOnCooldown) or isinstance(err, InputException):
        silent = True
    location = accounting_bot.commands.get_cmd_name(ctx.command)
    if location is not None:
        location = "command " + location
    if not silent:
        log_error(logging.getLogger(), err, location=location, ctx=ctx)
    await send_exception(err, ctx)


@bot.event
async def on_command_error(ctx: commands.Context, err: commands.CommandError):
    silent = False
    # Don't log command rate limit errors, but send a response to the interaction
    if isinstance(err, CommandOnCooldown) or isinstance(err, InputException):
        silent = True
    location = get_cmd_name(ctx.command)
    if location is not None:
        location = "prefixed command " + location
    if not silent:
        log_error(logging.getLogger(), err, location=location)
    await send_exception(err, ctx)


@bot.event
async def on_ready():
    logging.info("Logged in!")

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
    channel = await bot.fetch_channel(config["menuChannel"])
    accounting_log = await bot.fetch_channel(config["logChannel"])
    msg = await channel.fetch_message(config["menuMessage"])
    # ctx = await bot.get_context(message=msg)

    # Updating View on the menu message
    await msg.edit(view=AccountingView(),
                   embeds=get_menu_embeds(), content="")

    # Updating shortcut menus
    shortcuts = CONNECTOR.get_shortcuts()
    logging.info(f"Found {len(shortcuts)} shortcut menus")
    for (m, c) in shortcuts:
        chan = bot.get_channel(c)
        if chan is None:
            chan = bot.fetch_channel(c)
        try:
            msg = await chan.fetch_message(m)
            await msg.edit(view=AccountingView(),
                           embed=accounting.EMBED_MENU_SHORTCUT, content="")
        except discord.errors.NotFound:
            logging.warning(f"Message {m} in channel {c} not found, deleting it from DB")
            CONNECTOR.delete_shortcut(m)

    # Basic setup completed, bot is operational
    STATE.state = State.online
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.idle, activity=activity)

    logging.info("Starting Google sheets API...")
    await sheet.setup_sheet(config["google_sheet"], config["project_resources"], config["logger.sheet"])
    await sheet.load_wallets(force=True, validate=True)
    logging.info("Google sheets API loaded.")
    if not market_loop.is_running():
        market_loop.start()
    # Updating unverified accountinglog entries
    logging.info("Setting up unverified accounting log entries")
    unverified = CONNECTOR.get_unverified()
    logging.info(f"Found {len(unverified)} unverified message(s)")
    for m in unverified:
        try:
            msg = await accounting_log.fetch_message(m)
        except discord.errors.NotFound:
            CONNECTOR.delete(m)
            continue
        if msg.content.startswith("Verifiziert von"):
            logging.warning(f"Transaction already verified but not inside database: %s: %s",
                            msg.id, msg.content)
            CONNECTOR.set_verification(m, 1)
            continue
        v = False  # Was transaction verified while the bot was offline?
        user = None  # User ID who verified the message
        # Checking all the reactions below the message
        for r in msg.reactions:
            emoji = r.emoji
            if isinstance(emoji, str):
                name = emoji
            else:
                name = emoji.name
            if name != "✅":
                continue
            users = await r.users().flatten()
            for u in users:
                if u.id in config["admins"]:
                    # User is admin, the transaction is therefore verified
                    v = True
                    user = u.id
                    break
            break
        if v:
            # Message was verified
            try:
                # Saving transaction to google sheet
                await accounting.save_embeds(msg, user)
            except mariadb.Error:
                pass
            # Removing the View
            await msg.edit(view=None)
        else:
            # Updating the message View, so it can be used by the users
            await msg.edit(view=accounting.TransactionView())
            if len(msg.embeds) > 0:
                transaction = accounting.transaction_from_embed(msg.embeds[0])
                state = await transaction.get_state()
                if state == 2:
                    await msg.add_reaction("⚠️")
                elif state == 3:
                    await msg.add_reaction("❌")
            else:
                logging.warning("Message %s is listed as transaction but does not have an embed", msg.id)

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
        for att in message.attachments:
            if not att.content_type.startswith("image"):
                continue
            url = att.url
            if not "://cdn.discordapp.com".casefold() in url.casefold():
                return
            if not isinstance(message.channel, DMChannel):
                # await message.author.send("Du hast ein Bild im Accountinglog gepostet. Wenn es sich um eine "
                #                           "Corporationsmission oder eine Spende handelt, kannst Du sie mir hier per "
                #                           "Direktnachricht schicken, um sie per Texterkennung automatisch verarbeiten "
                #                           "zu lassen.\nSpenden kannst Du dann auch selbst verifizieren.")
                return
            channel = message.author.id
            thread = Thread(
                target=corpmissionOCR.handle_image,
                args=(url, att.content_type, message, channel, message.author.id))
            thread.start()
            await message.reply("Verarbeite Bild, bitte warten. Dies dauert einige Sekunden.")
    if message.channel.id == KILLMAIL_CHANNEL:
        if len(message.embeds) > 0:
            logger.info("Received message %s with embed, parsing killmail", message.id)
            await data_utils.save_killmail(message.embeds[0])


@bot.event
async def on_application_command(ctx: ApplicationContext):
    cmd_name = accounting_bot.commands.get_cmd_name(ctx.command)
    logger.info("Command '%s' called by %s:%s in channel %s",
                cmd_name,
                ctx.user.name,
                ctx.user.id,
                ctx.channel.id)


@bot.event
async def on_raw_reaction_add(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == config["logChannel"]:
        # Message is not verified
        channel = bot.get_channel(config["logChannel"])
        msg = await channel.fetch_message(reaction.message_id)
        await accounting.verify_transaction(reaction.user_id, msg)


@bot.event
async def on_raw_reaction_remove(reaction):
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


async def run_bot():
    try:
        await bot.start(token=TOKEN)
    except Exception as e:
        logging.critical("Bot crashed", e)
        STATE.state = State.offline
        await utils.terminate_bot(CONNECTOR)


async def main():
    await asyncio.gather(run_bot())


async def kill_bot(signum):
    """
    Shuts down the bot
    """
    STATE.state = State.terminated
    logging.critical("Received signal %s, stopping bot", signal.Signals(signum).name)
    await utils.terminate_bot(CONNECTOR)


@tasks.loop(seconds=1.0)
async def kill_loop():
    """
    In case loop.add_signal_handler does not work, this loop will be started by :func:`kill_bot_sync` as the bot has to
    be stopped asynchronously.
    """
    kill_loop.stop()
    logging.critical("Stopping bot")
    await utils.terminate_bot(CONNECTOR)


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
corpmissionOCR.ocr_result_loop.start()
loop.set_exception_handler(handle_asyncio_exception)
loop.run_until_complete(main())
logger.info("Bot stopped")
sys.exit(0)
