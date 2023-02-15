import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import Option, User, ApplicationContext, AutocompleteContext, option, Role
from discord.ext import commands

from accounting_bot import accounting, sheet, utils
from accounting_bot.accounting import AccountingView
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.utils import State

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.commands")


def main_char_autocomplete(self: AutocompleteContext):
    return filter(lambda n: self.value is None or n.startswith(self.value.strip()), utils.main_chars)


class BaseCommands(commands.Cog):
    def __init__(self, config: Config, connector: DatabaseConnector, state: 'BotState'):
        self.config = config
        self.guild = config["server"]
        self.admins = config["admins"]
        self.owner = config["owner"]
        self.connector = connector
        self.state = state

    def has_permissions(self, ctx: ApplicationContext):
        return (ctx.guild and self.guild == ctx.guild.id and ctx.author.guild_permissions.administrator) \
            or ctx.author.id in self.admins or ctx.author.id == self.owner

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
    async def get_balance(self, ctx: ApplicationContext,
                          force: Option(bool, "Force sheet reload", required=False, default=False),
                          user: Option(User, "The user to look up", required=False, default=None)):
        await ctx.defer(ephemeral=True)
        await sheet.load_wallets(force)
        if not user:
            user_id = ctx.author.id
        else:
            user_id = user.id

        name, _, _ = utils.get_main_account(discord_id=user_id)
        if name is None:
            await ctx.followup.send("This discord account is not connected to any ingame account!", ephemeral=True)
            return
        name = sheet.check_name_overwrites(name)
        balance = await sheet.get_balance(name)
        investments = await sheet.get_investments(name, default=0)
        if balance is None:
            await ctx.followup.send("Konto nicht gefunden!", ephemeral=True)
            return
        await ctx.followup.send("Der Kontostand von {} beträgt `{:,} ISK`.\nDie Projekteinlagen betragen `{:,} ISK`"
                                .format(name, balance, investments), ephemeral=True)

    @commands.slash_command(
        name="registeruser",
        description="Registers a user to a discord ID"
    )
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The user to register", required=True)
    async def register_user(self, ctx: ApplicationContext, ingame_name: str, user: User):
        if not self.has_permissions(ctx):
            await ctx.respond("You don't have the permission to use this command.", ephemeral=True)
            return
        if user is None:
            await ctx.respond("Either a user is required.", ephemeral=True)
            return
        user_id = user.id
        if ingame_name is None or ingame_name == "":
            await ctx.respond("Ingame name is required!", ephemeral=True)
            return
        matched_name, _, _ = utils.get_main_account(ingame_name)

        if matched_name is not None:
            old_id = utils.get_discord_id(matched_name)
            utils.save_discord_id(matched_name, int(user_id))
            logger.info("(%s) Saved discord id %s to player %s, old id %s", ctx.user.id, user_id, matched_name, old_id)
            await ctx.response.send_message(
                f"Spieler `{matched_name}` wurde zur ID `{user_id}` (<@{user_id}>) eingespeichert!\n" +
                ("" if not old_id else f"Die alte ID war `{old_id}` (<@{old_id}>)."),
                ephemeral=True)
        else:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(
        name="listunregusers",
        description="Lists all unregistered users of the discord"
    )
    @option("role", description="The role to check", required=True)
    async def find_unregistered_users(self, ctx: ApplicationContext, role: Role):
        if not self.has_permissions(ctx):
            await ctx.respond("You don't have the permission to use this command.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        users = await ctx.guild \
                         .fetch_members() \
                         .filter(lambda m: m.get_role(role.id) is not None) \
                         .map(lambda m: (m.nick if m.nick is not None else m.name, m)) \
                         .flatten()
        unreg_users = []
        old_users = []
        for name, user in users:  # type: str, discord.Member
            if user.id not in utils.discord_users.values():
                unreg_users.append(user)
            elif utils.get_main_account(discord_id=user.id)[0] not in utils.main_chars:
                old_users.append((utils.get_main_account(discord_id=user.id)[0], user))

        msg = f"Found {len(unreg_users)} unregistered users that have the specified role.\n"
        for user in unreg_users:
            msg += f"<@{user.id}> ({user.name})\n"
            if len(msg) > 1900:
                msg += "**Truncated**\n"
                break
        if len(old_users) > 0:
            msg += f"Found {len(old_users)} users that have no active (main) character inside the corp.\n"
            for name, user in old_users:
                msg += f"<@{user.id}> ({user.name}): Ingame: {name}\n"
                if len(msg) > 1900:
                    msg += "**Truncated**\n"
                    break
        await ctx.followup.send(msg, ephemeral=True)

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
    async def stop(self, ctx: ApplicationContext):
        if ctx.user.id == self.owner:
            logger.critical("Shutdown Command received, shutting down bot in 10 seconds")
            await ctx.respond("Bot wird in 10 Sekunden gestoppt...")
            self.state.state = State.terminated
            await utils.terminate_bot(connector=self.connector)
        else:
            await ctx.respond("Fehler! Berechtigungen fehlen.", ephemeral=True)
