import asyncio
import logging
import os
import random
import shutil
import string
import sys
from datetime import datetime
from threading import Thread

import discord
import mariadb
import pytesseract.pytesseract
import pytz as pytz
import requests
from discord import ActivityType, Message, DMChannel
from discord.ext import commands, tasks
from discord.ext.commands import CommandOnCooldown
from dotenv import load_dotenv

from accounting_bot import classes, sheet, projects, utils, corpmissionOCR
from accounting_bot.classes import AccountingView, Transaction, get_menu_embeds
from accounting_bot.commands import BaseCommands
from accounting_bot.database import DatabaseConnector
from accounting_bot.discordLogger import PycordHandler
from accounting_bot.exceptions import LoggedException
from accounting_bot.utils import log_error, string_to_file
from accounting_bot.config import Config, ConfigTree
from accounting_bot.corpmissionOCR import CorporationMission

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
    "pytesseract_cmd_path": (str, "N/A")
}
config = Config("config.json", ConfigTree(config_structure))
config.load_config()
logging.info("Config loaded")

GUILD = config["server"]
USER_ROLE = config["user_role"]
ACCOUNTING_LOG = config["logChannel"]
MENU_MESSAGE = config["menuMessage"]
MENU_CHANNEL = config["menuChannel"]
ADMINS = config["admins"]
OWNER = config["owner"]
LOG_CHANNEL = config["errorLogChannel"]
PREFIX = config["prefix"]
PROJECT_RESOURCES = config["project_resources"]

CONNECTOR = DatabaseConnector(
    username=config["db.user"],
    password=config["db.password"],
    port=config["db.port"],
    host=config["db.host"],
    database=config["db.name"]
)

try:
    if config["pytesseract_cmd_path"] != "N/A":
        pytesseract.pytesseract.tesseract_cmd = config["pytesseract_cmd_path"]
    else:
        config.save_config()
    tesseract_version = pytesseract.pytesseract.get_tesseract_version()
    logging.info("Tesseract version " + str(tesseract_version) + " installed!")
    corpmissionOCR.OCR_ENABLED = True
except pytesseract.TesseractNotFoundError as error:
    logging.warning("Tesseract is not installed, OCR will be disabled. Please add tesseract to the PATH or to the "
                    "config.")


logging.info("Starting up bot...")
intents = discord.Intents.default()
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.message_content = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.reactions = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.members = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, debug_guilds=[582649395149799491, GUILD])

classes.set_up(CONNECTOR, ADMINS, bot, ACCOUNTING_LOG, GUILD)

bot.add_cog(BaseCommands(GUILD, ADMINS, OWNER, CONNECTOR))
bot.add_cog(projects.ProjectCommands(bot, ADMINS, OWNER, GUILD, USER_ROLE))


@bot.event
async def on_error(event_name, *args, **kwargs):
    logging.exception("An Error occurred: %s", event_name)
    pass


@tasks.loop(seconds=10.0)
async def log_loop():
    """
    Logging loop. Will send every 10 seconds all logs into a specified discord channel.
    See :class:`accounting_bot.discordLogger.PycordHandler` for more details.
    """
    await discord_handler.process_logs()

log_loop.start()


@tasks.loop(seconds=3.0)
async def ocr_result_loop():
    mission_list = corpmissionOCR.return_missions
    with mission_list.lock:
        for i in range(len(mission_list.list)):  # type int, CorporationMission
            if mission_list.list[i] is None:
                continue
            author, mission = mission_list.list[i]
            mission_list.list[i] = None
            user = await bot.get_or_fetch_user(author)
            if not user:
                logging.error("User " + author + " from OCR result list not found!")
                continue
            if isinstance(mission, Exception):
                await user.send("An error occurred: " + str(mission))
                continue
            msg = f"Gültig: {str(mission.valid)}\nTitel: {mission.title}\nNutzername: {mission.username}\n" \
                  f"Menge: {str(mission.amount)}\nErhalte ISK: {str(mission.pay_isk)}" \
                  f"\nLimitiert: {str(mission.has_limit)}"
            await user.send("Bild wurde verarbeitet: \n" + msg)
        while None in mission_list.list:
            mission_list.list.remove(None)


ocr_result_loop.start()


@bot.event
async def on_application_command_error(ctx, error):
    """
    Exception handler for slash commands.

    :param ctx:     Context
    :param error:   the error that occurred
    """
    silent = False
    # Don't log command rate limit errors, but send a response to the interaction
    if isinstance(error, CommandOnCooldown):
        silent = True
    if not silent:
        if ctx.guild is not None:
            # Error occurred inside a server
            logging.error(
                "Error in guild " + str(ctx.guild.id) + " in channel " + str(ctx.channel.id) +
                ", sent by " + str(ctx.author.id) + ": " + ctx.author.name + " while executing command " + ctx.command.name)
        else:
            # Error occurred inside a direct message
            logging.error(
                "Error outside of guild in channel " + str(ctx.channel.id) +
                ", sent by " + str(ctx.author.id) + ": " + ctx.author.name)
        log_error(logging.getLogger(), error)
    if isinstance(error, LoggedException):
        # Append additional log
        await ctx.respond(f"Error: {str(error)}. \nFor more details, take a look at the log below.",
                          file=string_to_file(error.get_log()), ephemeral=True)
    else:
        # Normal error
        await ctx.respond(f"Error: {str(error)}", ephemeral=True)


@bot.event
async def on_ready():
    logging.info("Logged in!")

    logging.info("Setting up channels...")
    # Basic setup
    if MENU_CHANNEL == -1:
        return
    if LOG_CHANNEL != -1:
        log_channel = await bot.fetch_channel(LOG_CHANNEL)
        discord_handler.set_channel(log_channel)
    channel = await bot.fetch_channel(MENU_CHANNEL)
    accounting_log = await bot.fetch_channel(ACCOUNTING_LOG)
    msg = await channel.fetch_message(MENU_MESSAGE)
    ctx = await bot.get_context(message=msg)

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
                           embed=classes.EMBED_MENU_SHORTCUT, content="")
        except discord.errors.NotFound as ignored:
            logging.warning(f"Message {m} in channel {c} not found, deleting it from DB")
            CONNECTOR.delete_shortcut(m)

    # Basic setup completed
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.idle, activity=activity)

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
                if u.id in ADMINS:
                    # User is admin, the transaction is therefore verified
                    v = True
                    user = u.id
                    break
            break
        if v:
            # Message was verified
            try:
                # Saving transaction to google sheet
                await save_embeds(msg, user)
            except mariadb.Error as e:
                pass
            # Removing the View
            await msg.edit(view=None)
        else:
            # Updating the message View, so it can be used by the users
            await msg.edit(view=classes.TransactionView())

    logging.info("Starting Google sheets API...")
    await sheet.setup_sheet(config["google_sheet"], PROJECT_RESOURCES)
    logging.info("Google sheets API loaded.")
    # Setup completed
    logging.info("Setup complete.")
    await bot.change_presence(status=discord.Status.online, activity=activity)


@bot.event
async def on_message(message: Message):
    await bot.process_commands(message)
    if isinstance(message.channel, DMChannel):
        for att in message.attachments:
            if not att.content_type.startswith("image"):
                continue
            url = att.url
            if not "://cdn.discordapp.com".casefold() in url.casefold():
                return
            thread = Thread(target=corpmissionOCR.handle_image, args=(url, att.content_type, message))
            thread.start()
            await message.reply("Processing image, please wait...")


async def inform_player(transaction, discord_id):
    user = bot.fetch_user(discord_id)
    if user is not None:
        user.send()


async def save_embeds(msg, user_id):
    """
    Saves the transaction of a message into the sheet

    :param msg:     The message with the transaction embed
    :param user_id: The user ID that verified the transaction
    """
    if len(msg.embeds) == 0:
        return
    elif len(msg.embeds) > 1:
        logging.warning(f"Message {msg.id} has more than one embed ({msg.embeds})!")
    # Getting embed of the message, should contain only one
    embed = msg.embeds[0]
    # Convert embed to Transaction
    transaction = Transaction.from_embed(embed)
    # Check if transaction is valid
    if transaction.amount is None or (not transaction.name_from and not transaction.name_to) or not transaction.purpose:
        logging.error(f"Invalid embed in message {msg.id}! Can't parse transaction data: {transaction}")
        return
    time_formatted = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    # Save transaction to sheet
    await sheet.add_transaction(transaction=transaction)
    user = await bot.get_or_fetch_user(user_id)
    logging.info(f"Verified transaction {msg.id} ({time_formatted}. Verified by {user.name} ({user.id}).")
    # Set message as verified
    CONNECTOR.set_verification(msg.id, verified=1)
    await msg.edit(content=f"Verifiziert von {user.name}", view=None)
    """
    # Find discord account
    id_from = utils.get_discord_id(transaction.name_from)
    if id_from is None:
        name, perfect, nicknames = await utils.find_discord_id(bot, GUILD, USER_ROLE, transaction.name_from)
        if perfect:
            id_from = nicknames[name]
    id_to = utils.get_discord_id(transaction.name_to)
    if id_to is None:
        name, perfect, nicknames = await utils.find_discord_id(bot, GUILD, USER_ROLE, transaction.name_to)
        if perfect:
            id_to = nicknames[name]
    """


@bot.event
async def on_raw_reaction_add(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == ACCOUNTING_LOG and reaction.user_id in ADMINS:
        is_verified = CONNECTOR.is_unverified_transaction(message=reaction.message_id)
        if is_verified is None:
            return
        if not is_verified:
            # Message is already verified
            author = await bot.get_or_fetch_user(reaction.user_id)
            await author.send(content="Hinweis: Diese Transaktion wurde bereits verifiziert, sie wurde nicht "
                                      "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls "
                                      "dies nötig ist.")
            return
        # Message is not verified
        channel = bot.get_channel(ACCOUNTING_LOG)
        msg = await channel.fetch_message(reaction.message_id)
        if msg.content.startswith("Verifiziert von"):
            # Message was already verified, but due to an Error it got not updated in the SQL DB
            author = await bot.get_or_fetch_user(reaction.user_id)
            await author.send(content="Hinweis: Diese Transaktion wurde bereits verifiziert, sie wurde nicht "
                                      "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls "
                                      "dies nötig ist.")
            CONNECTOR.set_verification(msg.id, True)
            return
        else:
            # Save transaction
            await save_embeds(msg, reaction.user_id)


@bot.event
async def on_raw_reaction_remove(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == ACCOUNTING_LOG and reaction.user_id in ADMINS:
        logging.info(f"{reaction.user_id} removed checkmark from {reaction.message_id}!")


@bot.slash_command(description="Creates the main menu for the bot and sets all required settings.")
async def setup(ctx):
    global MENU_MESSAGE, MENU_CHANNEL, GUILD
    logging.info("Setup command called by user " + str(ctx.author.id))
    if ctx.guild is None:
        logging.info("Command was send via DM!")
        await ctx.respond("Can only be executed inside a guild")
        return
    if ctx.guild.id != GUILD and ctx.author.id != OWNER:
        logging.info("Wrong server!")
        await ctx.respond("Wrong server", ephemeral=True)
        return

    if ctx.author.guild_permissions.administrator or ctx.author.id in ADMINS or ctx.author.id == OWNER:
        # Running setup
        logging.info("User verified, starting setup...")
        view = AccountingView()
        msg = await ctx.send(view=view, embeds=get_menu_embeds())
        logging.info("Send menu message with id " + str(msg.id))
        MENU_MESSAGE = msg.id
        MENU_CHANNEL = ctx.channel.id
        GUILD = ctx.guild.id
        save_config()
        logging.info("Setup completed.")
        await ctx.respond("Saved config", ephemeral=True)
    else:
        logging.info(f"User {ctx.author.id} is missing permissions to run the setup command")
        await ctx.respond("Missing permissions", ephemeral=True)


@commands.slash_command(
    name="setlogchannel",
    description="Sets the current channel as the accounting log channel.")
async def set_log_channel(self, ctx):
    global ACCOUNTING_LOG
    logging.info("SetLogChannel command received.")
    if ctx.guild is None:
        logging.info("Command was send via DM!")
        await ctx.respond("Only available inside a guild")
        return
    if ctx.guild.id != self.guild:
        logging.info("Wrong server!")
        await ctx.respond("Can only used inside the defined discord server", ephemeral=True)
        return

    if ctx.author.id == self.owner or ctx.author.guild_permissions.administrator:
        logging.info("User Verified. Setting up channel...")
        ACCOUNTING_LOG = ctx.channel.id
        save_config()
        logging.info("Channel changed!")
        await ctx.respond("Log channel set to this channel (" + str(ACCOUNTING_LOG) + ")")
    else:
        logging.info(f"User {ctx.author.id} is missing permissions to run the setlogchannel command")
        await ctx.respond("Missing permissions", ephemeral=True)


def save_config():
    """
    Saves the global variable into the config
    """
    config["server"] = GUILD
    config["logChannel"] = ACCOUNTING_LOG
    config["menuMessage"] = MENU_MESSAGE
    config["menuChannel"] = MENU_CHANNEL
    config.save_config()


async def run_bot():
    try:
        await bot.start(token=TOKEN)
    except Exception:
        await bot.close()


async def main():
    await asyncio.gather(#asyncio.to_thread(corpmissionOCR.ocr_worker()),
                         run_bot())

loop.run_until_complete(main())
