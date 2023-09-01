# PluginConfig
# Name: EmbedPlugin
# Author: Blaumeise03
# Depends-On: []
# End
import json
import logging
import os
from typing import Dict, Union

from discord import Embed, Color

from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper

logger = logging.getLogger("ext.embeds")


class EmbedPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.embeds = {}  # type: Dict[str, Embed]

    def on_load(self):
        directory = os.fsencode("resources/embeds")

        for file in os.listdir(directory):
            filename = os.fsdecode(file)
            if not filename.endswith(".json"):
                continue
            with open(f"resources/embeds/{filename}", "r", encoding="utf8") as embed_file:
                embeds = json.load(embed_file)
                logger.info("Loading %s embeds from %s", filename, len(embeds))
                for key, value in embeds.items():
                    if type(value) == str:
                        self.embeds[key] = value
                    else:
                        self.embeds[key] = Embed.from_dict(value)
            logger.info("%s embeds loaded", len(self.embeds))

    def on_unload(self):
        self.embeds.clear()

    def get_embed(self, name: str) -> Union[Embed, str]:
        if name in self.embeds:
            return self.embeds[name]
        logger.error("Embed with name %s not found", name)
        return Embed(title="Embed not found", description=f"Embed with name `{name}` not found", colour=Color.red())
