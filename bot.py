from datetime import datetime
import json
import os
import sys
import time
from os.path import exists
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

from classes import AccountingView, get_embeds

log_filename = "logs/" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
print("Logging outputs goes to: " + log_filename)
if not os.path.exists("logs/"):
    os.mkdir("logs")
logging.basicConfig(filename=log_filename, filemode="a",
                    format="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s",
                    level=logging.INFO)
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
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
bot = commands.Bot(command_prefix="§", intents=intents)


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
    activity = discord.Game(name="Eve Echoes", type=3)
    await bot.change_presence(status=discord.Status.online, activity=activity)
    logging.info("Setup complete.")


@bot.event
async def on_message(message):
    await bot.process_commands(message)


@bot.command()
async def setup(ctx):
    global MENU_MESSAGE, MENU_CHANNEL, GUILD
    if ctx.guild is None:
        await ctx.send("Can only be executed inside a guild")
    elif ctx.guild.id == GUILD or ctx.author.id == 485518598517948416:
        if ctx.author.guild_permissions.administrator or ctx.author.id == 485518598517948416:
            logging.warning("Setup")
            view = AccountingView(ctx=ctx, bot=bot, accounting_log=ACCOUNTING_LOG)
            msg = await ctx.send(view=view, embeds=get_embeds())
            MENU_MESSAGE = msg.id
            MENU_CHANNEL = ctx.channel.id
            GUILD = ctx.guild.id
            save_config()
            await ctx.send("Saved config")
        else:
            await ctx.send("Missing permissions")
    else:
        await ctx.send("Wrong server")

# noinspection SpellCheckingInspection
@bot.command()
async def setlogchannel(ctx):
    global ACCOUNTING_LOG
    if ctx.guild is None:
        ctx.send("Only available inside a guild")
    elif ctx.guild.id == GUILD:
        if ctx.author.id == 485518598517948416 or ctx.author.guild_permissions.administrator:
            ACCOUNTING_LOG = ctx.channel.id
            save_config()
            await ctx.send("Log channel set to this channel (" + str(ACCOUNTING_LOG) + ")")
        else:
            await ctx.send("Missing permissions")
    else:
        await ctx.send("Can only used inside the defined discord server")


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
    global outfile
    config["server"] = GUILD
    config["logChannel"] = ACCOUNTING_LOG
    config["menuMessage"] = MENU_MESSAGE
    config["menuChannel"] = MENU_CHANNEL
    with open("config.json", "w") as outfile:
        json.dump(config, outfile, indent=4)
        logging.warning("Config saved!")


bot.run(TOKEN)
