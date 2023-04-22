import json
import logging
from typing import TYPE_CHECKING

from discord import Embed

if TYPE_CHECKING:
    from bot import BotState, AccountingBot


logger = logging.getLogger("bot.embeds")


def setup(bot: "AccountingBot"):
    with open("resources/embeds.json", "r", encoding="utf8") as embed_file:
        embeds = json.load(embed_file)
        logger.info("Loading %s embeds", len(embeds))
        for key, value in embeds.items():
            if type(value) == str:
                bot.embeds[key] = value
            else:
                bot.embeds[key] = Embed.from_dict(value)
        logger.info("Embeds loaded")


def teardown(bot: "AccountingBot"):
    bot.embeds.clear()
