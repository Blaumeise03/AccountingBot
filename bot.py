import re
import traceback
from datetime import datetime
import logging
import json
import os
import sys
import time
from os.path import exists

import discord
import mariadb
import pytz as pytz
from discord import Option, ActivityType
from discord.ext import commands, tasks
from dotenv import load_dotenv

import classes
import sheet
from classes import AccountingView, get_embeds, InduRoleMenu
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
intents.message_content = True
intents.reactions = True
bot = commands.Bot(command_prefix="§", intents=intents, debug_guilds=[582649395149799491, 758444788449148938, GUILD])

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
    if MENU_CHANNEL == -1:
        return
    if LOG_CHANNEL != -1:
        log_channel = await bot.fetch_channel(LOG_CHANNEL)
        discord_handler.set_channel(log_channel)
    channel = await bot.fetch_channel(MENU_CHANNEL)
    accounting_log = await bot.fetch_channel(ACCOUNTING_LOG)
    msg = await channel.fetch_message(MENU_MESSAGE)
    ctx = await bot.get_context(message=msg)
    await msg.edit(view=AccountingView(ctx=ctx),
                   embeds=get_embeds(), content="")
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.online, activity=activity)
    logging.info("Setting up unverified accounting log entries")
    unverified = connector.get_unverified()
    logging.info(f"Found {len(unverified)} unverified message(s)")
    for m in unverified:
        try:
            msg = await accounting_log.fetch_message(m)
        except discord.errors.NotFound as ignored:
            msg = None
        if msg is not None:
            if msg.content.startswith("Verifiziert von"):
                logging.warning(f"Transaction already verified bot not inside database: {msg.id}: {msg.content}")
                connector.set_verification(m, 1)
            else:
                v = False
                user = None
                for r in msg.reactions:
                    emoji = r.emoji
                    name = ""
                    if isinstance(emoji, str):
                        name = emoji
                    else:
                        name = emoji.name
                    if name == "✅":
                        users = await r.users().flatten()
                        for u in users:
                            if u.id in ADMINS:
                                v = True
                                user = u.id
                                break
                        break
                if v:
                    try:
                        await save_embeds(msg, user)
                    except mariadb.Error as e:
                        pass
                    await msg.edit(view=None)
                else:
                    await msg.edit(view=classes.TransactionView(bot))
        else:
            connector.delete(m)
    logging.info("Setup complete.")


@bot.event
async def on_message(message):
    await bot.process_commands(message)


async def save_embeds(msg, user_id):
    if len(msg.embeds) > 0:
        embed = msg.embeds[0]
        t = embed.timestamp
        amount = -1
        purpose = ""
        reference = ""
        u_from = ""
        u_to = ""
        t_type = -1
        if embed.title.casefold() == "Transfer".casefold():
            t_type = 0
        elif embed.title.casefold() == "Einzahlen".casefold():
            t_type = 1
        elif embed.title.casefold() == "Auszahlen".casefold():
            t_type = 2
        for field in embed.fields:
            name = field.name.casefold()
            if name == "Menge:".casefold():
                amount = int(re.sub(r"[,a-zA-Z]", "", field.value))
            elif name == "Verwendungszweck:".casefold():
                purpose = field.value.strip()
            elif name == "Referenz:".casefold():
                reference = field.value.strip()
            elif name == "Von:".casefold():
                u_from = field.value.strip()
            elif name == "Zu:".casefold():
                u_to = field.value.strip()
            elif name == "Konto:".casefold():
                if t_type == 1:
                    u_to = field.value.strip()
                elif t_type == 2:
                    u_from = field.value.strip()
        if amount == -1 or (not u_from and not u_to) or t_type == -1 or not purpose:
            logging.error(
                f"Invalid embed in message {msg.id}! Can't parse transaction data: timestamp: {t} amount: {amount} purpose: {purpose} from: {u_from} to: {u_to} type: {t_type}")
        else:
            time_formatted = t.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
            sheet.add_transaction(time_formatted, u_from, u_to,
                                  amount, purpose, reference)
            user = await bot.get_or_fetch_user(user_id)
            logging.info(f"Verified transaction {msg.id} ({time_formatted}. Verified by {user.name} ({user.id}).")
            connector.set_verification(msg.id, verified=1)
            await msg.edit(content=f"Verifiziert von {user.name}", view=None)
    else:
        pass


@bot.event
async def on_raw_reaction_add(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == ACCOUNTING_LOG and reaction.user_id in ADMINS:
        res = connector.is_unverified_transaction(message=reaction.message_id)
        if res is None:
            return
        if res:
            channel = bot.get_channel(ACCOUNTING_LOG)
            msg = await channel.fetch_message(reaction.message_id)
            if msg.content.startswith("Verifiziert von"):
                author = await bot.get_or_fetch_user(reaction.user_id)
                await author.send(content="Hinweis: Diese Transaktion wurde bereits verifiziert, sie wurde nicht "
                                          "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls "
                                          "dies nötig ist.")
                return
            else:
                await save_embeds(msg, reaction.user_id)
        else:
            author = await bot.get_or_fetch_user(reaction.user_id)
            await author.send(content="Hinweis: Diese Transaktion wurde bereits verifiziert, sie wurde nicht "
                                      "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls "
                                      "dies nötig ist.")


@bot.event
async def on_raw_reaction_remove(reaction):
    if reaction.emoji.name == "✅" and reaction.channel_id == ACCOUNTING_LOG and reaction.user_id in ADMINS:
        logging.info(f"{reaction.user_id} removed checkmark from {reaction.message_id}!")


@bot.command()
async def setup(ctx):
    global MENU_MESSAGE, MENU_CHANNEL, GUILD
    logging.info("Setup command called by user " + str(ctx.author.id))
    if ctx.guild is None:
        logging.info("Command was send via DM!")
        await ctx.send("Can only be executed inside a guild")
    elif ctx.guild.id == GUILD or ctx.author.id == OWNER:
        if ctx.author.guild_permissions.administrator or ctx.author.id == OWNER:
            logging.info("User verified, starting setup...")
            view = AccountingView(ctx=ctx)
            msg = await ctx.send(view=view, embeds=get_embeds())
            logging.info("Send menu message with id " + str(msg.id))
            MENU_MESSAGE = msg.id
            MENU_CHANNEL = ctx.channel.id
            GUILD = ctx.guild.id
            save_config()
            logging.info("Setup completed.")
            await ctx.send("Saved config")
        else:
            logging.info("Missing perms!")
            await ctx.send("Missing permissions")
    else:
        logging.info("Wrong server!")
        await ctx.send("Wrong server")


# noinspection SpellCheckingInspection
@bot.command()
async def setlogchannel(ctx):
    global ACCOUNTING_LOG
    logging.info("SetLogChannel command received.")
    if ctx.guild is None:
        logging.info("Command was send via DM!")
        await ctx.send("Only available inside a guild")
    elif ctx.guild.id == GUILD:
        if ctx.author.id == OWNER or ctx.author.guild_permissions.administrator:
            logging.info("User Verified. Setting up channel...")
            ACCOUNTING_LOG = ctx.channel.id
            save_config()
            logging.info("Channel changed!")
            await ctx.send("Log channel set to this channel (" + str(ACCOUNTING_LOG) + ")")
        else:
            logging.info("Missing perms!")
            await ctx.send("Missing permissions")
    else:
        logging.info("Wrong server!")
        await ctx.send("Can only used inside the defined discord server")


@bot.slash_command()
async def indumenu(ctx, msg: Option(str, "Enter your friend's name", required=False, default=None)):
    if msg is None:
        logging.info("Sending role menu...")
        await ctx.send(embeds=[InduRoleMenu()])
        await ctx.respond("Neues Menü gesendet.", ephemeral=True)
    else:
        logging.info("Updating role menu " + str(msg))
        msg = await ctx.channel.fetch_message(int(msg))
        await msg.edit(embeds=[InduRoleMenu()])
        await ctx.respond("Menü geupdated.", ephemeral=True)


@bot.command()
async def stop(ctx):
    if ctx.author.id == OWNER:
        logging.critical("Shutdown Command received, shutting down bot in 10 seconds")
        await ctx.send("Bot wird in 10 Sekunden gestoppt...")
        connector.con.close()
        time.sleep(10)
        exit(0)
    else:
        await ctx.send("Fehler! Berechtigungen fehlen...")


def save_config():
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
