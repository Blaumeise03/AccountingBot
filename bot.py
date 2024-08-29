import asyncio
import logging
import os
import sys
from datetime import datetime

import discord
from dotenv import load_dotenv

from accounting_bot.discordLogger import PycordHandler
from accounting_bot.main_bot import AccountingBot
from accounting_bot.universe import pi_planer

logger = logging.getLogger()
log_filename = "logs/" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".log"
print("Logging outputs goes to: " + log_filename)
if not os.path.exists("logs/"):
    os.mkdir("logs")
formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s")

# File log handler
file_handler = logging.FileHandler(log_filename, encoding="utf-8")
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
# interaction_logger = logging.getLogger("bot.access") ToDo: Add interaction logger


loop = asyncio.get_event_loop()

# loading env
logging.info("Loading .env for discord token and config path")
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CNFG_PATH = os.getenv("BOT_CONFIG", "config.json")
pi_planer.average_prices_url = os.getenv("AVERAGE_PRICES_URL", None)

# Setting up discord intents
intents = discord.Intents.default()
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.message_content = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.reactions = True
# noinspection PyUnresolvedReferences,PyDunderSlots
intents.members = True

bot = AccountingBot(
    intents=intents,
    help_command=None,
    config_path=CNFG_PATH,
    pycord_handler=discord_handler,
    # debug_guilds=[582649395149799491, 758444788449148938]
)

bot.run(TOKEN)
