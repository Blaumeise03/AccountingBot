import json
import logging
import os
import sys
import time
from datetime import datetime
from os.path import exists

import discord
import mariadb
import pytz as pytz
from discord import Option, ActivityType
from discord.ext import commands, tasks
from dotenv import load_dotenv

import classes
import sheet
from classes import AccountingView, Transaction, get_menu_embeds
from database import DatabaseConnector
from discordLogger import PycordHandler

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
console.setLevel(logging.INFO)
console.setFormatter(formatter)
logging.getLogger().addHandler(console)
# Discord channel log handler
discord_handler = PycordHandler(level=logging.WARNING)
discord_handler.setFormatter(formatter)
logging.getLogger().addHandler(discord_handler)
# Root logger
logging.getLogger().setLevel(logging.INFO)

# loading env
logging.info("Loading .env...")
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = -1
ACCOUNTING_LOG = -1
MENU_MESSAGE = -1
MENU_CHANNEL = -1
LOG_CHANNEL = -1
OWNER = -1
ADMINS = []
PREFIX = "§"

# loading json config
logging.info("Loading JSON Config...")
config = None
if exists("config.json"):
    with open("config.json") as json_file:
        config = json.load(json_file)
    if config["server"] == -1:
        logging.error("ERROR: Config is empty, please change the settings and restart!")
    else:
        GUILD = config["server"]
        ACCOUNTING_LOG = config["logChannel"]
        MENU_MESSAGE = config["menuMessage"]
        MENU_CHANNEL = config["menuChannel"]
        ADMINS = config["admins"]
        OWNER = config["owner"]
        LOG_CHANNEL = config["errorLogChannel"]
        PREFIX = config["prefix"]
else:
    config = {
        "server": -1,
        "logChannel": -1,
        "menuMessage": -1,
        "menuChannel": -1,
        "errorLogChannel": -1,
        "admins": [
        ],
        "db_user": "Username",
        "db_password": "Password",
        "db_port": 3306,
        "db_host": "localhost",
        "db_name": "accountingBot",
        "google_sheet": "SHEET_ID",
        "prefix": "§"
    }
    with open("config.json", "w") as outfile:
        json.dump(config, outfile, indent=4)
        logging.error("ERROR: Config not found, created new one. Please change the settings and restart!")

connector = DatabaseConnector(
    username=config["db_user"],
    password=config["db_password"],
    port=config["db_port"],
    host=config["db_host"],
    database=config["db_name"]
)

logging.info("Starting Google sheets API...")
sheet.setup_sheet(config["google_sheet"])

logging.info("Starting up bot...")
intents = discord.Intents.default()
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.message_content = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.reactions = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents, debug_guilds=[582649395149799491, GUILD])

classes.set_up(connector, ADMINS, bot, ACCOUNTING_LOG, GUILD)


@bot.event
async def on_error(event_name, *args, **kwargs):
    logging.exception("Error:")


@tasks.loop(seconds=10.0)
async def log_loop():
    await discord_handler.process_logs()

log_loop.start()


@bot.event
async def on_command_error(ctx, error):
    if ctx.guild is not None:
        logging.error(
            "Error in guild " + str(ctx.guild.id) + " in channel " + str(ctx.channel.id) +
            ", sent by " + str(ctx.author.id) + ": " + ctx.author.name)
    else:
        logging.error(
            "Error outside of guild in channel " + str(ctx.channel.id) +
            ", sent by " + str(ctx.author.id) + ": " + ctx.author.name)
    logging.exception(error, exc_info=False)


@bot.event
async def on_ready():
    logging.info("Logged in!")

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
    shortcuts = connector.get_shortcuts()
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
            connector.delete_shortcut(m)

    # Basic setup completed
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.online, activity=activity)

    # Updating unverified accountinglog entries
    logging.info("Setting up unverified accounting log entries")
    unverified = connector.get_unverified()
    logging.info(f"Found {len(unverified)} unverified message(s)")
    for m in unverified:
        try:
            msg = await accounting_log.fetch_message(m)
        except discord.errors.NotFound:
            connector.delete(m)
            continue
        if msg.content.startswith("Verifiziert von"):
            logging.warning(f"Transaction already verified but not inside database: {msg.id}: {msg.content}")
            connector.set_verification(m, 1)
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
    # Setup completed
    logging.info("Setup complete.")


@bot.event
async def on_message(message):
    await bot.process_commands(message)


async def save_embeds(msg, user_id):
    """
    Saves a transaction to the sheet

    :param msg:
    :param user_id:
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
    sheet.add_transaction(transaction=transaction)
    user = await bot.get_or_fetch_user(user_id)
    logging.info(f"Verified transaction {msg.id} ({time_formatted}. Verified by {user.name} ({user.id}).")
    # Set message as verified
    connector.set_verification(msg.id, verified=1)
    await msg.edit(content=f"Verifiziert von {user.name}", view=None)


@bot.event
async def on_raw_reaction_add(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == ACCOUNTING_LOG and reaction.user_id in ADMINS:
        is_verified = connector.is_unverified_transaction(message=reaction.message_id)
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
            connector.set_verification(msg.id, True)
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


# noinspection SpellCheckingInspection
@bot.slash_command(description="Creates a new shortcut menu containing all buttons.")
async def createshortcut(ctx):
    global MENU_MESSAGE, MENU_CHANNEL, GUILD
    if ctx.guild is None:
        await ctx.respond("Can only be executed inside a guild")
        return
    if ctx.guild.id != GUILD and ctx.author.id != OWNER:
        logging.info("Wrong server!")
        await ctx.respond("Wrong server", ephemeral=True)
        return

    if ctx.author.guild_permissions.administrator or ctx.author.id in ADMINS or ctx.author.id == OWNER:
        view = AccountingView()
        msg = await ctx.send(view=view, embed=classes.EMBED_MENU_SHORTCUT)
        connector.add_shortcut(msg.id, ctx.channel.id)
        await ctx.respond("Shortcut menu posted", ephemeral=True)
    else:
        logging.info(f"User {ctx.author.id} is missing permissions to run the createshortcut command")
        await ctx.respond("Missing permissions", ephemeral=True)


# noinspection SpellCheckingInspection
@bot.slash_command(description="Sets the current channel as the accounting log channel.")
async def setlogchannel(ctx):
    global ACCOUNTING_LOG
    logging.info("SetLogChannel command received.")
    if ctx.guild is None:
        logging.info("Command was send via DM!")
        await ctx.respond("Only available inside a guild")
        return
    if ctx.guild.id != GUILD:
        logging.info("Wrong server!")
        await ctx.respond("Can only used inside the defined discord server", ephemeral=True)
        return

    if ctx.author.id == OWNER or ctx.author.guild_permissions.administrator:
        logging.info("User Verified. Setting up channel...")
        ACCOUNTING_LOG = ctx.channel.id
        save_config()
        logging.info("Channel changed!")
        await ctx.respond("Log channel set to this channel (" + str(ACCOUNTING_LOG) + ")")
    else:
        logging.info(f"User {ctx.author.id} is missing permissions to run the setlogchannel command")
        await ctx.respond("Missing permissions", ephemeral=True)


# noinspection SpellCheckingInspection
@bot.slash_command(description="Posts a menu with all available manufacturing roles.")
async def indumenu(ctx, msg: Option(str, "Message ID", required=False, default=None)):
    if msg is None:
        logging.info("Sending role menu...")
        await ctx.send(embeds=[classes.EMBED_INDU_MENU])
        await ctx.respond("Neues Menü gesendet.", ephemeral=True)
    else:
        logging.info("Updating role menu " + str(msg))
        msg = await ctx.channel.fetch_message(int(msg))
        await msg.edit(embeds=[classes.EMBED_INDU_MENU])
        await ctx.respond("Menü geupdated.", ephemeral=True)


@bot.slash_command(description="Shuts down the discord bot, if set up properly, it will restart.")
async def stop(ctx):
    if ctx.author.id == OWNER:
        logging.critical("Shutdown Command received, shutting down bot in 10 seconds")
        await ctx.respond("Bot wird in 10 Sekunden gestoppt...")
        connector.con.close()
        time.sleep(10)
        exit(0)
    else:
        await ctx.respond("Fehler! Berechtigungen fehlen.", ephemeral=True)


def save_config():
    """
    Saves the config
    """
    logging.warning("Saving config...")
    global outfile
    config["server"] = GUILD
    config["logChannel"] = ACCOUNTING_LOG
    config["menuMessage"] = MENU_MESSAGE
    config["menuChannel"] = MENU_CHANNEL
    logging.info("Config dict updated. Writing to file...")
    with open("config.json", "w") as outfile:
        json.dump(config, outfile, indent=4)
        logging.warning("Config saved!")
    logging.info("Save completed.")


bot.run(TOKEN)
