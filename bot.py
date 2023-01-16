import asyncio
import logging
import os
import sys
from datetime import datetime
from enum import Enum
from threading import Thread

import discord
import mariadb
import pytesseract.pytesseract
from discord import ActivityType, Message, DMChannel
from discord.ext import commands, tasks
from discord.ext.commands import CommandOnCooldown
from dotenv import load_dotenv

from accounting_bot import accounting, sheet, projects, utils, corpmissionOCR
from accounting_bot.accounting import AccountingView, get_menu_embeds
from accounting_bot.commands import BaseCommands
from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.discordLogger import PycordHandler
from accounting_bot.exceptions import LoggedException
from accounting_bot.utils import log_error, string_to_file

log_filename = "logs/" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
print("Logging outputs goes to: " + log_filename)
if not os.path.exists("logs/"):
    os.mkdir("logs")
formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s")

# File log handler
file_handler = logging.FileHandler(log_filename)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)
# Console log handler
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.DEBUG)
console.setFormatter(formatter)
logging.getLogger().addHandler(console)
# Discord channel log handler
discord_handler = PycordHandler(level=logging.WARNING)
discord_handler.setFormatter(formatter)
logging.getLogger().addHandler(discord_handler)
# Root logger
logging.getLogger().setLevel(logging.INFO)


class State(Enum):
    offline = 0
    preparing = 1
    starting = 2
    online = 3


class BotState:
    def __init__(self) -> None:
        self.state = State.online
        self.ocr = False
        self.bot = None  # type: commands.Bot | None


STATE = BotState()
STATE.state = State.preparing

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
    "menuMessage": (int, -1),
    "menuChannel": (int, -1),
    "owner": (int, -1),
    "errorLogChannel": (int, -1),
    "admins": (list, []),
    "db": {
        "user": (str, "N/A"),
        "password": (str, "N/A"),
        "port": (int, -1),
        "host": (str, "N/A"),
        "name": (str, "N/A")
    },
    "google_sheet": (str, "N/A"),
    "project_resources": (list, [],),
    "pytesseract_cmd_path": (str, "N/A"),
    "logger": {
        "sheet": (str, "INFO")
    }
}
config = Config("config.json", ConfigTree(config_structure))
config.load_config()
config.save_config()
logging.info("Config loaded")
ACCOUNTING_LOG = config["logChannel"]

CONNECTOR = DatabaseConnector(
    username=config["db.user"],
    password=config["db.password"],
    port=config["db.port"],
    host=config["db.host"],
    database=config["db.name"]
)

corpmissionOCR.STATE = STATE
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
bot = commands.Bot(command_prefix=config["prefix"], intents=intents, debug_guilds=[config["test_server"], config["server"]])
STATE.bot = bot

accounting.set_up(config, CONNECTOR, bot, STATE)
utils.set_config(config, bot)

bot.add_cog(BaseCommands(config, CONNECTOR))
bot.add_cog(projects.ProjectCommands(bot, config["admins"], config["owner"], config["server"], config["user_role"]))


# noinspection PyUnusedLocal
@bot.event
async def on_error(event_name, *args, **kwargs):
    info = sys.exc_info()
    if info and len(info) > 2 and info[0] == discord.errors.NotFound:
        logging.warning("discord.errors.NotFound Error in %s: %s", event_name, str(info[1]))
        return
    logging.exception("An Error occurred: %s", event_name)
    pass


@tasks.loop(seconds=10.0)
async def log_loop():
    """
    Logging loop. Will send every 10 seconds all logs into a specified discord channel.
    See :class:`accounting_bot.discordLogger.PycordHandler` for more details.
    """
    await discord_handler.process_logs()


@bot.event
async def on_application_command_error(ctx, err):
    """
    Exception handler for slash commands.

    :param ctx:     Context
    :param err:   the error that occurred
    """
    silent = False
    # Don't log command rate limit errors, but send a response to the interaction
    if isinstance(err, CommandOnCooldown):
        silent = True
    if not silent:
        if ctx.guild is not None:
            # Error occurred inside a server
            logging.error(
                "Error in guild " + str(ctx.guild.id) + " in channel " + str(ctx.channel.id) +
                ", sent by " + str(
                    ctx.author.id) + ": " + ctx.author.name + " while executing command " + ctx.command.name)
        else:
            # Error occurred inside a direct message
            logging.error(
                "Error outside of guild in channel " + str(ctx.channel.id) +
                ", sent by " + str(ctx.author.id) + ": " + ctx.author.name)
        log_error(logging.getLogger(), err)
    if isinstance(err, LoggedException):
        # Append additional log
        await ctx.respond(f"Error: {str(err)}.\nFor more details, take a look at the log below.",
                          file=string_to_file(err.get_log()), ephemeral=True)
    else:
        # Normal error
        await ctx.respond(f"Error: {str(err)}", ephemeral=True)


@bot.event
async def on_ready():
    logging.info("Logged in!")

    logging.info("Setting up channels...")
    # Basic setup
    if config["menuChannel"] == -1:
        return
    if config["errorLogChannel"] != -1:
        log_channel = await bot.fetch_channel(config["errorLogChannel"])
        discord_handler.set_channel(log_channel)
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

    # Basic setup completed
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.idle, activity=activity)

    logging.info("Starting Google sheets API...")
    await sheet.setup_sheet(config["google_sheet"], config["project_resources"], config["logger.sheet"])
    logging.info("Google sheets API loaded.")

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
            logging.warning(f"Transaction already verified but not inside database: {msg.id}: {msg.content}")
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

    # Reload projects
    await sheet.find_projects()
    # Setup completed
    logging.info("Setup complete.")
    await bot.change_presence(status=discord.Status.online, activity=activity)


@bot.event
async def on_message(message: Message):
    await bot.process_commands(message)
    if isinstance(message.channel, DMChannel) or message.channel.id == ACCOUNTING_LOG:
        for att in message.attachments:
            if not att.content_type.startswith("image"):
                continue
            url = att.url
            if not "://cdn.discordapp.com".casefold() in url.casefold():
                return
            if not isinstance(message.channel, DMChannel):
                await message.author.send("Du hast ein Bild im Accountinglog gepostet. Wenn es sich um eine "
                                          "Corporationsmission handelt, musst Du sie mir hier per Direktnachricht "
                                          "schicken, um sie per Texterkennung automatisch verarbeiten zu lassen.")
                return
            channel = message.author.id
            thread = Thread(
                target=corpmissionOCR.handle_image,
                args=(url, att.content_type, message, channel, message.author.id))
            thread.start()
            await message.reply("Verarbeite Bild, bitte warten. Dies dauert einige Sekunden.")



@bot.event
async def on_raw_reaction_add(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == config["logChannel"]:
        # Message is not verified
        channel = bot.get_channel(config["logChannel"])
        msg = await channel.fetch_message(reaction.message_id)
        await accounting.verify_transaction(reaction.user_id, msg)


@bot.event
async def on_raw_reaction_remove(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == config["logChannel"] and reaction.user_id in config["admins"]:
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
        await bot.close()


async def main():
    await asyncio.gather(run_bot())

log_loop.start()
corpmissionOCR.ocr_result_loop.start()
loop.run_until_complete(main())
