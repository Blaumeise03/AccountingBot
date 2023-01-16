import asyncio
import logging

from discord import Option
from discord.ext import commands

from accounting_bot import accounting, sheet, utils
from accounting_bot.accounting import AccountingView
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector

logger = logging.getLogger("bot.commands")


class BaseCommands(commands.Cog):
    def __init__(self, config: Config, connector: DatabaseConnector):
        self.config = config
        self.guild = config["server"]
        self.admins = config["admins"]
        self.owner = config["owner"]
        self.connector = connector

    @commands.slash_command(description="Creates the main menu for the bot and sets all required settings.")
    async def setup(self, ctx):
        logging.info("Setup command called by user " + str(ctx.author.id))
        if ctx.guild is None:
            await ctx.respond("Can only be executed inside a guild")
            return
        if ctx.guild.id != self.config["server"] and ctx.author.id != self.config["owner"]:
            await ctx.respond("Wrong server", ephemeral=True)
            return

        if ctx.author.guild_permissions.administrator or \
                ctx.author.id in self.config["admins"] or \
                ctx.author.id == self.config["owner"]:
            # Running setup
            logger.info("User verified for setup-command, starting setup...")
            view = AccountingView()
            msg = await ctx.send(view=view, embeds=accounting.get_menu_embeds())
            logger.info("Send menu message with id " + str(msg.id))
            self.config["menuMessage"] = msg.id
            self.config["menuChannel"] = ctx.channel.id
            self.config["server"] = ctx.guild.id
            self.config.save_config()
            logger.info("Setup completed.")
            await ctx.respond("Saved config", ephemeral=True)
        else:
            logger.info(f"User {ctx.author.id} is missing permissions to run the setup command")
            await ctx.respond("Missing permissions", ephemeral=True)

    @commands.slash_command(
        name="setlogchannel",
        description="Sets the current channel as the accounting log channel.")
    async def set_log_channel(self, ctx):
        logger.info("SetLogChannel command received.")
        if ctx.guild is None:
            logger.info("Command was send via DM!")
            await ctx.respond("Only available inside a guild")
            return
        if ctx.guild.id != self.config["server"]:
            logger.info("Wrong server!")
            await ctx.respond("Can only used inside the defined discord server", ephemeral=True)
            return

        if ctx.author.id == self.config["owner"] or ctx.author.guild_permissions.administrator:
            logger.info("User Verified. Setting up channel...")
            self.config["logChannel"] = ctx.channel.id
            self.config.save_config()
            logger.info("Channel changed!")
            await ctx.respond("Log channel set to this channel (" + str(self.config["logChannel"]) + ")")
        else:
            logger.info(f"User {ctx.author.id} is missing permissions to run the setlogchannel command")
            await ctx.respond("Missing permissions", ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(description="Creates a new shortcut menu containing all buttons.")
    async def createshortcut(self, ctx):
        if ctx.guild is None:
            await ctx.respond("Can only be executed inside a guild")
            return
        if ctx.guild.id != self.guild and ctx.author.id != self.owner:
            logging.info("Wrong server!")
            await ctx.respond("Wrong server", ephemeral=True)
            return

        if ctx.author.guild_permissions.administrator or ctx.author.id in self.admins or ctx.author.id == self.owner:
            view = AccountingView()
            msg = await ctx.send(view=view, embed=accounting.EMBED_MENU_SHORTCUT)
            self.connector.add_shortcut(msg.id, ctx.channel.id)
            await ctx.respond("Shortcut menu posted", ephemeral=True)
        else:
            logging.info(f"User {ctx.author.id} is missing permissions to run the createshortcut command")
            await ctx.respond("Missing permissions", ephemeral=True)

    @commands.slash_command(
        name="balance",
        description="Get your current accounting balance."
    )
    async def get_balance(self, ctx, force: Option(bool, "Force sheet reload", required=False, default=False)):
        await sheet.load_wallets(force)
        user_id = ctx.author.id
        name = utils.get_main_account(discord_id=user_id)
        if name is None:
            await ctx.respond("This discord account is not connected to any ingame account!", ephemeral=True)
            return
        name = sheet.check_name_overwrites(name)
        balance = sheet.get_balance(name)
        if balance is None:
            await ctx.respond("Wallet not found!", ephemeral=True)
            return
        balance = "{:,} ISK".format(balance)
        await ctx.respond(f"Dein Kontostand beträgt : `{balance}`", ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(description="Posts a menu with all available manufacturing roles.")
    async def indumenu(self, ctx, msg: Option(str, "Message ID", required=False, default=None)):
        if msg is None:
            logger.info("Sending role menu...")
            await ctx.send(embeds=[accounting.EMBED_INDU_MENU])
            await ctx.respond("Neues Menü gesendet.", ephemeral=True)
        else:
            logger.info("Updating role menu " + str(msg))
            msg = await ctx.channel.fetch_message(int(msg))
            await msg.edit(embeds=[accounting.EMBED_INDU_MENU])
            await ctx.respond("Menü geupdated.", ephemeral=True)

    @commands.slash_command(description="Shuts down the discord bot, if set up properly, it will restart.")
    async def stop(self, ctx):
        if ctx.author.id == self.owner:
            logger.critical("Shutdown Command received, shutting down bot in 10 seconds")
            await ctx.respond("Bot wird in 10 Sekunden gestoppt...")
            self.connector.con.close()
            await asyncio.sleep(10)
            exit(0)
        else:
            await ctx.respond("Fehler! Berechtigungen fehlen.", ephemeral=True)
