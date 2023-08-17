# PluginConfig
# Name: Test Plugin
# Author: Blaumeise03
# Depends-On: []
#
# End
from accounting_bot.main_bot import BotPlugin, AccountingBot


class MyPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, module_name: str) -> None:
        super().__init__(bot, module_name)

    def on_load(self):
        self.warning("MyPlugin loading")

    async def on_enable(self):
        self.warning("MyPlugin enabling")

    async def on_disable(self):
        self.warning("MyPlugin disabling")

    def on_unload(self):
        self.warning("MyPlugin unloading")
