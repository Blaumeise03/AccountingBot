# PluginConfig
# Name: TestPlugin
# Author: Blaumeise03
# Depends-On: []
# Load-After: []
# Localization: plugin_test_lang.xml
# End
import logging

from discord import ApplicationContext, ApplicationCommandError, option, TextChannel
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

    @commands.slash_command(name="error_test")
    async def error_test(self, ctx: ApplicationContext):
        # This error will get handled both by the bot's main error handling and by the error handling
        # inside this cog (cog_command_error).
        raise Exception("Errror")

    @commands.slash_command(name="echo")
    @option("echo", description="The text to repeat", required=False, default=None)
    async def test2(self, ctx: ApplicationContext, echo: str):
        await ctx.respond("Echo: " + echo, ephemeral=True)

    @commands.slash_command(name="channels")
    @option("channel_name", description="The target channel", required=False, default=None)
    async def channels(self, ctx: ApplicationContext, channel_name: str):
        await ctx.response.defer(ephemeral=True)
        if channel_name is None:
            msg = ""
            for c in ctx.guild.channels:
                msg += f"{c.name}\n"
        else:
            channel = None  # type: TextChannel | None
            for c in ctx.guild.channels:
                if c.name == channel_name or str(c.id) == channel_name:
                    channel = c
                    break
            if channel is None:
                await ctx.followup.send(f"Unknown channel `{channel_name}`")
                return
            msg = ""
            for t, p in channel.overwrites.items():
                msg += f"\n{t.name}: READ: {p.read_messages}"
        await ctx.followup.send(msg)

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
            ModalForm(title="Test", send_response="Abgeschickt")
            .add_field(label="A")
            .add_field(label="B")
            .open_form(ctx.response)
        )
        await ctx.followup.send(str(res), ephemeral=True)
