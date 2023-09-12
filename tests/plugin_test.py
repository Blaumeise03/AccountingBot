# PluginConfig
# Name: TestPlugin
# Author: Blaumeise03
# Depends-On: []
# Load-After: []
# Localization: plugin_test_lang.xml
# End
import logging

from discord import ApplicationContext, ApplicationCommandError, option
from discord.ext import commands

from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils.ui import ModalForm

logger = logging.getLogger("test.plugin_test")


class MyPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)

    def on_load(self):
        self.warning("MyPlugin loading")
        self.register_cog(TestCommands(self.bot))

    async def on_enable(self):
        self.warning("MyPlugin enabling")

    async def on_disable(self):
        self.warning("MyPlugin disabling")

    def on_unload(self):
        self.warning("MyPlugin unloading")


class TestCommands(commands.Cog):
    def __init__(self, bot: AccountingBot):
        self.bot = bot

    @commands.slash_command(name="test")
    async def test(self, ctx: ApplicationContext):
        # This error will get handled both by the bot's main error handling and by the error handling
        # inside this cog (cog_command_error).
        raise Exception("Errror")

    @commands.slash_command(name="echo")
    @option("echo", description="The text to repeat", required=False, default=None)
    async def test2(self, ctx: ApplicationContext, echo: str):
        await ctx.respond("Echo: " + echo, ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("Bot has logged in, bot is in %s guilds", len(self.bot.guilds))

    async def cog_command_error(self, ctx: ApplicationContext, error: ApplicationCommandError):
        # Handles errors caused by this cog, but the errors will still be handled by the bot itself.
        # There is no need to log the stack traces here.
        logger.info("Command error in test")
        await ctx.respond("Error", ephemeral=True)

    @commands.slash_command(name="modal")
    async def test_modal_form(self, ctx: ApplicationContext):
        res = await (
            ModalForm(title="Test", submit_message="Abgeschickt")
            .add_field(label="A")
            .add_field(label="B")
            .open_form(ctx.response)
        )
        await ctx.followup.send(str(res), ephemeral=True)
