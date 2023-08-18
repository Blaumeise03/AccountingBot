import asyncio
import logging
import os
import sys
from asyncio import AbstractEventLoop
from datetime import datetime
from typing import Any

import discord
from discord.ext import commands
from discord.ext.commands import CommandOnCooldown, CheckFailure
from dotenv import load_dotenv

from accounting_bot import utils
from accounting_bot.exceptions import InputException
from accounting_bot.main_bot import AccountingBot

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
# Discord channel log handler ToDo: Add Discord log handler
# discord_handler = PycordHandler(level=logging.WARNING)
# discord_handler.setFormatter(formatter)
# logger.addHandler(discord_handler)
# Root logger
logger.setLevel(logging.INFO)
# interaction_logger = logging.getLogger("bot.access") ToDo: Add interaction logger


loop = asyncio.get_event_loop()

# loading env
logging.info("Loading .env for discord token and config path")
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CNFG_PATH = os.getenv("BOT_CONFIG", "config.json")

# Setting up discord intents
intents = discord.Intents.default()
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.message_content = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.reactions = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.members = True

bot = AccountingBot(intents=intents, help_command=None)
bot.load_config(CNFG_PATH)
bot.config.save_config(CNFG_PATH)
bot.load_plugins()


def handle_asyncio_exception(error_loop: AbstractEventLoop, context: dict[str, Any]):
    logger.error("Unhandled exception in event_loop: %s", context["message"])
    if "exception" in context:
        utils.log_error(logger, error=context["exception"], location="event_loop")


loop.set_exception_handler(handle_asyncio_exception)

loop.run_until_complete(bot.start(TOKEN))
