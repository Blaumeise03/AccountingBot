# PluginConfig
# Name: Test Plugin
# Author: Blaumeise03
# Depends-On: []
#
# End
from discord import ApplicationContext
from discord.ext import commands

from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper


class MyPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper)

    def on_load(self):
        self.warning("MyPlugin loading")
        self.register_cog(TestCommands())

    async def on_enable(self):
        self.warning("MyPlugin enabling")

    async def on_disable(self):
        self.warning("MyPlugin disabling")

    def on_unload(self):
        self.warning("MyPlugin unloading")


class TestCommands(commands.Cog):
    @commands.slash_command(name="test")
    async def test(self, ctx):
        raise Exception("Errror")
