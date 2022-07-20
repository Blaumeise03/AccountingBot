import traceback
from datetime import datetime
import logging
import json
import os
import sys
import time
from os.path import exists

import discord
from discord import Option, ActivityType
from discord.ext import commands
from dotenv import load_dotenv

from classes import AccountingView, get_embeds, InduRoleMenu

log_filename = "logs/" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
print("Logging outputs goes to: " + log_filename)
if not os.path.exists("logs/"):
    os.mkdir("logs")
formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s")
# logging.basicConfig(filename=log_filename, filemode="a",
#                    format="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s",
#                    level=logging.INFO)
file_handler = logging.FileHandler(log_filename)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.INFO)
console.setFormatter(formatter)
logging.getLogger().addHandler(console)
logging.getLogger().setLevel(logging.INFO)

# loading env
logging.info("Loading .env...")
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = -1
ACCOUNTING_LOG = -1
MENU_MESSAGE = -1
MENU_CHANNEL = -1
ACCOUNTING_LOG = -1

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
else:
    config = {
        "server": -1,
        "logChannel": -1,
        "menuMessage": -1,
        "menuChannel": -1
    }
    with open("config.json", "w") as outfile:
        json.dump(config, outfile, indent=4)
        logging.error("ERROR: Config not found, created new one. Please change the settings and restart!")

logging.info("Starting up bot...")
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="§", intents=intents, debug_guilds=[582649395149799491, 758444788449148938])


@bot.event
async def on_error(event_name, *args, **kwargs):
    logging.exception("Error:")


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
    channel = await bot.fetch_channel(MENU_CHANNEL)
    await bot.fetch_channel(ACCOUNTING_LOG)
    msg = await channel.fetch_message(MENU_MESSAGE)
    ctx = await bot.get_context(message=msg)
    await msg.edit(view=AccountingView(ctx=ctx, bot=bot, accounting_log=ACCOUNTING_LOG),
                   embeds=get_embeds(), content="")
    activity = discord.Activity(name="IAK-JW", type=ActivityType.competing)
    await bot.change_presence(status=discord.Status.online, activity=activity)
    logging.info("Setup complete.")


@bot.event
async def on_message(message):
    await bot.process_commands(message)


@bot.command()
async def setup(ctx):
    global MENU_MESSAGE, MENU_CHANNEL, GUILD
    logging.info("Setup command called by user " + str(ctx.author.id))
    if ctx.guild is None:
        logging.info("Command was send via DM!")
        await ctx.send("Can only be executed inside a guild")
    elif ctx.guild.id == GUILD or ctx.author.id == 485518598517948416:
        if ctx.author.guild_permissions.administrator or ctx.author.id == 485518598517948416:
            logging.info("User verified, starting setup...")
            view = AccountingView(ctx=ctx, bot=bot, accounting_log=ACCOUNTING_LOG)
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
        if ctx.author.id == 485518598517948416 or ctx.author.guild_permissions.administrator:
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

"""
@bot.slash_command()
async def test(ctx, msg_id: Option(str, "Enter your friend's name", required=False, default=None)):
    if msg_id is None:
        pass
    else:
        msg = await ctx.channel.fetch_message(int(msg_id))
        print(msg.content)
        print(f'Clean_Content: {msg.clean_content}')
        print(f'System_Content: {msg.system_content}')
        await ctx.respond("Test", ephemeral=True)
"""


@bot.command()
async def stop(ctx):
    if ctx.author.id == 485518598517948416:
        logging.critical("Shutdown Command received, shutting down bot in 10 seconds")
        await ctx.send("Bot wird in 10 Sekunden gestoppt...")
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
