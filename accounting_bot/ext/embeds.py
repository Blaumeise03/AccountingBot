# PluginConfig
# Name: EmbedPlugin
# Author: Blaumeise03
# Depends-On: []
# End
import json
import logging
from typing import Dict

from discord import Embed

from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper

logger = logging.getLogger("bot.embeds")


class EmbedPlugin(BotPlugin):

    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.embeds = {}  # type: Dict[str, Embed]

    def on_load(self):
        with open("resources/embeds.json", "r", encoding="utf8") as embed_file:
            embeds = json.load(embed_file)
            logger.info("Loading %s embeds", len(embeds))
            for key, value in embeds.items():
                if type(value) == str:
                    self.embeds[key] = value
                else:
                    self.embeds[key] = Embed.from_dict(value)
            logger.info("Embeds loaded")

    def on_unload(self):
        self.embeds.clear()
