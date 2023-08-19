# PluginConfig
# Name: Test Plugin
# Author: Blaumeise03
# Depends-On: []
# Localization: plugin_test_lang.xml
# End
import logging

from discord import ApplicationContext, ApplicationCommandError, option
from discord.ext import commands

from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
logger = logging.getLogger("test.plugin_test")


class MyPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)

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
    async def test(self, ctx: ApplicationContext):
        raise Exception("Errror")

    @commands.slash_command(name="test2")
    @option("echo", description="The text to repeat", required=False, default=None)
    async def test2(self, ctx: ApplicationContext, echo: str):
        await ctx.respond("Echo: " + echo, ephemeral=True)

    async def cog_command_error(self, ctx: ApplicationContext, error: ApplicationCommandError):
        logger.info("Command error in test")
        await ctx.respond("Error", ephemeral=True)
