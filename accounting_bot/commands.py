import datetime
import functools
import io
import logging
import math
from typing import TYPE_CHECKING, Optional, Callable, List, Union

import discord
from discord import User, ApplicationContext, AutocompleteContext, option, Role, SlashCommand, \
    SlashCommandGroup, DMChannel, MessageCommand, ContextMenuCommand, UserCommand, ChannelType
from discord.ext import commands
from discord.ext.commands import Context, Command
from discord.ui import InputText

from accounting_bot import accounting, sheet, utils
from accounting_bot.accounting import AccountingView
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import InputException
from accounting_bot.localisation import t_
from accounting_bot.universe import data_utils
from accounting_bot.universe.pi_planer import PiPlanningSession, PiPlanningView
from accounting_bot.utils import State, get_cmd_name, ErrorHandledModal, help_infos, help_info, admin_only, \
    main_guild_only, user_only, online_only, CmdAnnotation, owner_only, guild_only

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.commands")


def main_char_autocomplete(self: AutocompleteContext):
    return filter(lambda n: self.value is None or n.startswith(self.value.strip()), utils.main_chars)


STATE = None  # type: BotState | None
BOUNTY_ADMINS = []


def setup(state: "BotState"):
    global STATE, BOUNTY_ADMINS
    STATE = state
    BOUNTY_ADMINS = STATE.config["killmail_parser.admins"]


def get_cmd_help(cmd: Union[Callable, Command], opt: str = None, long=False, fallback=None):
    callback = cmd
    cmd_name = None
    if isinstance(cmd, (Command, SlashCommand, ContextMenuCommand)):
        cmd_name = get_cmd_name(cmd).replace(" ", "_")
        callback = cmd.callback
    elif cmd not in help_infos:
        return fallback

    if callback in help_infos:
        cmd_name = help_infos[callback]

    result = None
    extra = "_long" if long else ""
    if opt is None:
        result = t_(f"help_{cmd_name}{extra}", raise_not_found=False)
        if result is None:
            result = t_(f"help_{cmd_name}", raise_not_found=False)
    if result is None and opt is not None:
        result = t_(f"help_{cmd_name}_{opt}{extra}", raise_not_found=False)
        if result is None:
            result = t_(f"help_{cmd_name}_{opt}", raise_not_found=False)
        if result is None:
            result = t_(f"opt_{opt}{extra}", raise_not_found=False)
            if result is None:
                result = t_(f"opt_{opt}", raise_not_found=False)
    if result is None:
        return fallback
    return result


class HelpCommand(commands.Cog):
    def __init__(self, state: 'BotState'):
        self.state = state

    def commands_autocomplete(self, ctx: AutocompleteContext):
        cmds = []
        for name, cog in self.state.bot.cogs.items():
            cmds.append(name)
            for cmd in cog.walk_commands():
                cmds.append(f"{get_cmd_name(cmd)}")
        for cmd in self.state.bot.commands:
            cmds.append(f"{get_cmd_name(cmd)}".strip())
        return filter(lambda n: ctx.value is None or n.casefold().startswith(ctx.value.casefold().strip()), cmds)

    @staticmethod
    def get_general_embed(bot: commands.Bot):
        emb = discord.Embed(title=t_("help"), color=discord.Color.red(),
                            description=t_("emb_help_general_desc"))
        for name, cog in bot.cogs.items():  # type: str, commands.Cog
            cmd_desc = ""
            for cmd in cog.walk_commands():
                desc = get_cmd_help(cmd, fallback=cmd.description)
                cmd_desc += f"`{get_cmd_name(cmd)}`: {desc}\n"
            emb.add_field(name=name, value=cmd_desc, inline=False)
        cmd_desc = ""
        for cmd in bot.walk_commands():
            if not cmd.cog_name and not cmd.hidden:
                desc = get_cmd_help(cmd, fallback=cmd.description)
                cmd_desc += f"{get_cmd_name(cmd)} - {desc}\n"
        if cmd_desc:
            emb.add_field(name=t_("other_cmds"), value=cmd_desc)
        return emb

    @staticmethod
    def get_cog_embed(cog: commands.Cog):
        emb = discord.Embed(title=t_("help_about").format(cog.__cog_name__), color=discord.Color.red(),
                            description=t_("emb_help_cog_desc"))
        for cmd in cog.walk_commands():
            cmd_name = get_cmd_name(cmd)
            cmd_desc = get_cmd_help(cmd, fallback=cmd.description)
            cmd_details = CmdAnnotation.get_cmd_details(cmd.callback)
            extra = ""
            if isinstance(cmd, ContextMenuCommand):
                extra = t_("ctx_command") + ". "
            if cmd_details is not None:
                cmd_desc = f"*{cmd_details}*\n{extra}{cmd_desc}\n"
            if isinstance(cmd, SlashCommand):
                if len(cmd.options) > 0:
                    cmd_desc += f" *{t_('parameter')}*:\n"
                for opt in cmd.options:
                    # noinspection PyUnresolvedReferences
                    cmd_desc += f"`{'[' if opt.required else '<'}{opt.name}: {opt.input_type.name}" \
                                f"{']' if opt.required else '>'}`: " \
                                f"{get_cmd_help(cmd, opt.name, fallback=opt.description)}\n"
            emb.add_field(name=f"**{cmd_name}**", value=cmd_desc, inline=False)
        return emb

    @staticmethod
    def get_command_embed(command: commands.Command):
        description = get_cmd_help(command, long=True, fallback=command.description)
        if description is None or len(description) == 0:
            description = t_("no_desc_available")
        cmd_details = CmdAnnotation.get_cmd_details(command.callback)
        if cmd_details is not None:
            description = f"*{t_('restrictions')}*: *{cmd_details}*\n{description}"
        emb = discord.Embed(title=t_("help_about").format(get_cmd_name(command)), color=discord.Color.red(),
                            description=description)
        if isinstance(command, MessageCommand):
            description += "\n" + t_("ctx_command_info").format(t_("message"))
        elif isinstance(command, UserCommand):
            description += "\n" + t_("ctx_command_info").format(t_("user"))
        if isinstance(command, SlashCommand):
            if len(command.options) > 0:
                description += f"\n\n**{t_('parameter')}**:"
            for opt in command.options:
                # noinspection PyUnresolvedReferences
                emb.add_field(name=opt.name,
                              value=f"({t_('optional') if not opt.required else t_('required')}):"
                                    f" `{opt.input_type.name}`\n"
                                    f"Default: `{str(opt.default)}`\n"
                                    f"{get_cmd_help(command, opt.name, fallback=opt.description)}",
                              inline=False)
        emb.description = description
        return emb

    @staticmethod
    def get_help_embed(bot: commands.Bot, selection: Optional[str] = None):
        if selection is None:
            return HelpCommand.get_general_embed(bot)
        selection = selection.strip()
        if selection in bot.cogs:
            cog = bot.cogs[selection]
            return HelpCommand.get_cog_embed(cog)
        command = None
        for cmd in bot.walk_commands():
            if f"{get_cmd_name(cmd)}".casefold() == selection.casefold():
                command = cmd
                break
        if command is None:
            for cog in bot.cogs.values():
                for cmd in cog.walk_commands():
                    if f"{get_cmd_name(cmd)}".casefold() == selection.casefold():
                        command = cmd
                        break
                if command is not None:
                    break
        if command is not None:
            return HelpCommand.get_command_embed(command)
        return discord.Embed(title=t_("help"), color=discord.Color.red(),
                             description=t_("cmd_not_found").format(selection=selection))

    @commands.slash_command(name="help", description="Help-Command")
    @option(name="selection", description="The command/module to get help about", type=str, required=False,
            autocomplete=commands_autocomplete)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True,
            autocomplete=commands_autocomplete)
    @option(name="edit_msg", description="Edit this message and update the embed", type=str, required=False,
            default=None)
    async def cmd_help(self, ctx: ApplicationContext,
                       selection: str, silent: bool, edit_msg: str):
        emb = HelpCommand.get_help_embed(self.state.bot, selection)
        if edit_msg is not None:
            try:
                edit_msg = int(edit_msg.strip())
            except ValueError as e:
                await ctx.response.send_message(f"Message id `'{edit_msg}'` is not a number:\n"
                                                f"{str(e)}.", ephemeral=silent)
                return
            await ctx.response.defer(ephemeral=silent)
            try:
                msg = await ctx.channel.fetch_message(edit_msg)
                await msg.edit(embed=emb)
                await ctx.followup.send("Message edited", ephemeral=silent)
                return
            except discord.NotFound:
                await ctx.followup.send("Message not found in current channel", ephemeral=silent)
                return
        await ctx.response.send_message(embed=emb, ephemeral=silent)


class BaseCommands(commands.Cog):
    def __init__(self, state: "BotState"):
        state.reloadFuncs.append(self.set_settings)
        self.config = None  # type: Config | None
        self.guild = None  # type: int | None
        self.admins = []  # type: List[int]
        self.owner = None  # type: int | None
        self.connector = None  # type: DatabaseConnector | None
        self.state = None  # type: BotState | None

    def set_settings(self, state: "BotState"):
        self.config = state.config
        self.guild = self.config["server"]
        self.admins = self.config["admins"]
        self.owner = self.config["owner"]
        self.connector = state.db_connector
        self.state = state

    @commands.slash_command(description="Creates the main menu for the bot and sets all required settings")
    @admin_only()
    @main_guild_only()
    @guild_only()
    async def setup(self, ctx: ApplicationContext):
        logger.info("User verified for setup-command, starting setup...")
        view = AccountingView()
        msg = await ctx.send(view=view, embeds=accounting.get_menu_embeds())
        logger.info("Send menu message with id " + str(msg.id))
        STATE.config["menuMessage"] = msg.id
        STATE.config["menuChannel"] = ctx.channel.id
        STATE.config["server"] = ctx.guild.id
        STATE.config.save_config()
        logger.info("Setup completed.")
        await ctx.response.send_message("Saved config", ephemeral=True)

    @commands.slash_command(
        name="setlogchannel",
        description="Sets the current channel as the accounting log channel")
    @admin_only()
    @main_guild_only()
    @guild_only()
    async def set_log_channel(self, ctx):
        logger.info("User Verified. Setting up channel...")
        STATE.config["logChannel"] = ctx.channel.id
        STATE.config.save_config()
        logger.info("Channel changed!")
        await ctx.respond("Log channel set to this channel (`" + str(STATE.config["logChannel"]) + "`)")

    # noinspection SpellCheckingInspection
    @commands.slash_command(description="Creates a new shortcut menu containing all buttons")
    @main_guild_only()
    @guild_only()
    async def createshortcut(self, ctx):
        if ctx.author.guild_permissions.administrator or ctx.author.id in STATE.admins or ctx.author.id == self.owner:
            view = AccountingView()
            msg = await ctx.send(view=view, embed=accounting.EMBED_MENU_SHORTCUT)
            self.connector.add_shortcut(msg.id, ctx.channel.id)
            await ctx.respond("Shortcut menu posted", ephemeral=True)
        else:
            logging.info(f"User {ctx.author.id} is missing permissions to run the createshortcut command")
            await ctx.respond("Missing permissions", ephemeral=True)

    @commands.slash_command(name="balance", description="Get the balance of a user")
    @option("force", description="Enforce data reload from sheet", required=False, default=False)
    @option("user", description="The user to lookup", required=False, default=None)
    @user_only()
    @online_only()
    async def get_balance(self, ctx: ApplicationContext, force: bool = False, user: User = None):
        await ctx.defer(ephemeral=True)
        await sheet.load_wallets(force)
        if not user:
            user_id = ctx.user.id
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

    @commands.slash_command(name="registeruser", description="Registers a user to a discord ID")
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The discord user to register", required=True)
    @admin_only()
    async def register_user(self, ctx: ApplicationContext, ingame_name: str, user: User):
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
    @commands.slash_command(name="listunregusers", description="Lists all unregistered users of the discord")
    @option("role", description="The role to check", required=True)
    @help_info("Listet alle unregistrierten Nutzer mit einer ausgewählten Rolle auf.")
    @admin_only()
    @guild_only()
    async def find_unregistered_users(self, ctx: ApplicationContext, role: Role):
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
    @commands.slash_command(description="Posts a menu with all available manufacturing roles")
    @option(name="msg", description="Edit this message ID instead of posting a new message", default=None)
    async def indumenu(self, ctx, msg: str = None):
        if msg is None:
            logger.info("Sending role menu")
            await ctx.send(embeds=[accounting.EMBED_INDU_MENU])
            await ctx.respond("Neues Menü gesendet.", ephemeral=True)
        else:
            logger.info("Updating role menu " + str(msg))
            msg = await ctx.channel.fetch_message(int(msg))
            await msg.edit(embeds=[accounting.EMBED_INDU_MENU])
            await ctx.respond("Menü geupdated.", ephemeral=True)

    @commands.slash_command(name="stop", description="Shuts down the discord bot, if set up properly, it will restart")
    @owner_only()
    async def stop(self, ctx: ApplicationContext):
        logger.critical("Shutdown Command received, shutting down bot in 10 seconds")
        await ctx.respond("Bot wird in 10 Sekunden gestoppt...")
        STATE.state = State.terminated
        await utils.terminate_bot()

    @commands.slash_command(name="parse_killmails", description="Loads all killmails of the channel into the database")
    @option(name="after", description="ID of message to start the search (exclusive)", required=True)
    @admin_only("bounty")
    @online_only()
    async def cmd_parse_killmails(self, ctx: ApplicationContext, after: str):
        if not after.isnumeric():
            raise InputException("Message ID has to be a number")
        await ctx.response.defer(ephemeral=True, invisible=False)
        message = await ctx.channel.fetch_message(int(after))
        # noinspection PyTypeChecker
        messages = await ctx.channel.history(after=message, oldest_first=True).flatten()
        num = 0
        for message in messages:
            if len(message.embeds) > 0:
                state = await data_utils.save_killmail(message.embeds[0])
                if state > 0:
                    num += 1
                if state == 1:
                    await message.add_reaction("⚠️")
                elif state == 2:
                    await message.add_reaction("✅")
        await ctx.followup.send(f"Loaded {num} killmails into the database")

    @commands.slash_command(name="save_killmails",
                            description="Saves the killmails between the ids into the google sheet")
    @option(name="first", description="ID of first killmail", required=True)
    @option(name="last", description="ID of last killmail", required=True)
    @option(name="month", description="Number of month (1=January...)", min=1, max=12, required=False, default=None)
    @option(name="autofix", description="Automatically fixes old sheet data", required=False, default=False)
    @admin_only("bounty")
    @online_only()
    async def cmd_save_killmails(
            self,
            ctx: ApplicationContext,
            first: int,
            last: int,
            month: int = None,
            autofix: bool = False):
        await ctx.response.defer(ephemeral=True, invisible=False)
        time = datetime.datetime.now()
        if month is not None:
            time = datetime.datetime(time.year, month, 1)
        warnings = await data_utils.verify_bounties(first, last, time)
        bounties = await data_utils.get_all_bounties(first, last)
        num_updated, num_new = await sheet.update_killmails(bounties, warnings, autofix)
        length = sum(map(len, warnings))
        msg = f"Bounty Sheet aktualisiert, es wurden {num_updated} Einträge aktualisiert und {num_new} neue Bounties " \
              f"eingetragen. Es gab {len(warnings)} Warnungen."
        if length > 900:
            file = utils.string_to_file(utils.list_to_string(warnings), "warnings.txt")
            await ctx.followup.send(f"{msg} Siehe Anhang.", file=file)
            return
        if length == 0:
            await ctx.followup.send(msg)
            return
        await ctx.followup.send(f"{msg}. Warnungen:\n```\n{utils.list_to_string(warnings)}\n```")

    @commands.slash_command(name="showbounties", description="Shows the bounty stats of a player")
    @option("user", desciption="The user to look up", required=False, default=None)
    async def cmd_show_bounties(self, ctx: ApplicationContext, user: User = None):
        if user is None:
            user = ctx.user
        player = utils.get_main_account(discord_id=user.id)[0]
        if player is None:
            await ctx.response.send_message("Kein Spieler zu diesem Discord Account gefunden", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True, invisible=False)
        start, end = utils.get_month_edges(datetime.datetime.now())
        res = await data_utils.get_bounties_by_player(start, end, player)
        for b in res:
            if b["type"] == "T":
                factor = sheet.BOUNTY_TACKLE
            elif b["region"] in sheet.BOUNTY_HOME_REGIONS:
                factor = 1
            else:
                factor = sheet.BOUNTY_NORMAL
            b["value"] *= factor
        msg = f"Bounties aus diesem Monat für `{player}`\n```"
        b_sum = functools.reduce(lambda x, y: x+y, map(lambda b: b["value"], res))
        for b in res:
            msg += f"\n{b['kill_id']:9} {b['value']:14,.0f} ISK"
            if len(msg) > 1400:
                msg += "\ntruncated..."
                break
        msg += f"\n```\nSumme: {b_sum:14,.0f} ISK\n*Hinweis: Dies ist nur eine ungefähre Vorschau*"
        await ctx.followup.send(msg)

    @commands.message_command(name="Add Tackle")
    @admin_only("bounty")
    @online_only()
    async def ctx_cmd_add_tackle(self, ctx: ApplicationContext, message: discord.Message):
        if len(message.embeds) == 0:
            await ctx.response.send_message(f"Nachricht enthält kein Embed:\n{message.jump_url}", ephemeral=True)
            return
        await ctx.response.send_modal(AddBountyModal(message))

    @commands.message_command(name="Show Bounties")
    @user_only()
    async def ctx_cmd_show_bounties(self, ctx: ApplicationContext, message: discord.Message):
        if len(message.embeds) == 0:
            await ctx.response.send_message(f"Nachricht enthält kein Embed:\n{message.jump_url}", ephemeral=True)
            return
        kill_id = data_utils.get_kill_id(message.embeds[0])
        await ctx.response.defer(ephemeral=True, invisible=False)
        bounties = await data_utils.get_bounties(kill_id)
        killmail = await data_utils.get_killmail(kill_id)
        msg = (f"{message.jump_url}\nKillmail `{kill_id}`:\n```\n"
               f"Spieler: {killmail.final_blow}\n" +
               (f"Schiff: {killmail.ship.name}\n" if killmail.ship is not None else "Schiff: N/A\n") +
               (f"System: {killmail.system.name}\n" if killmail.system is not None else "System: N/A\n") +
               f"Wert: {killmail.kill_value:,} ISK\nBounties:")
        for bounty in bounties:
            msg += f"\n{bounty['type']:1} {bounty['player']:10}"
        msg += "\n```"
        await ctx.followup.send(msg)


class AddBountyModal(ErrorHandledModal):
    def __init__(self, msg: discord.Message, *args, **kwargs):
        super().__init__(title="Bounty hinzufügen", *args, **kwargs)
        self.msg = msg
        self.add_item(InputText(label="Tackler/Logi", placeholder="Oder \"clear\"", required=True))

    async def callback(self, ctx: ApplicationContext):
        kill_id = data_utils.get_kill_id(self.msg.embeds[0])
        if self.children[0].value.strip().casefold() == "clear".casefold():
            await ctx.response.defer(ephemeral=True, invisible=False)
            await data_utils.clear_bounties(kill_id)
            await ctx.followup.send(f"Bounties gelöscht\n{self.msg.jump_url}")
            return
        player = utils.get_main_account(self.children[0].value.strip())[0]
        if player is None:
            await ctx.response.send_message(
                f"Spieler `{self.children[0].value.strip()}` nicht gefunden!\n{self.msg.jump_url}", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True, invisible=False)
        await data_utils.add_bounty(kill_id, player, "T")
        bounties = await data_utils.get_bounties(kill_id)
        msg = f"Spieler `{player}` wurde als Tackle/Logi für Kill `{kill_id}` eingetragen:\n```"
        for bounty in bounties:
            msg += f"\n{bounty['type']:1} {bounty['player']:10}"
        msg += f"\n```\n{self.msg.jump_url}"
        await ctx.followup.send(msg)


class UniverseCommands(commands.Cog):
    def __init__(self, state: "BotState"):
        self.state = state

    cmd_pi = SlashCommandGroup(name="pi", description="Access planetary production data.")

    @cmd_pi.command(name="stats", description="View statistical data for pi in a selected constellation")
    @option(name="const", description="Target Constellation", type=str, required=True)
    @option(name="resources", description="List of pi, seperated by ';'", type=str, required=False)
    @option(name="compare_regions",
            description="List of regions, seperated by ';' to compare the selected constellation with",
            type=str, required=False)
    @option(name="vertical", description="Create a vertical boxplot (default false)",
            default=False, required=False)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_const_stats(self, ctx: ApplicationContext, const: str, resources: str, compare_regions: str,
                              vertical: bool, silent: bool):
        await ctx.response.defer(ephemeral=silent)
        resource_names = utils.str_to_list(resources, ";")
        region_names = utils.str_to_list(compare_regions, ";")

        figure, n = await data_utils.create_pi_boxplot_async(const, resource_names, region_names, vertical)
        img_binary = await data_utils.create_image(figure,
                                                   height=max(n * 45, 500) + 80 if vertical else 500,
                                                   width=700 if vertical else max(n * 45, 500))
        arr = io.BytesIO(img_binary)
        arr.seek(0)
        file = discord.File(arr, "image.jpeg")
        await ctx.followup.send(f"PI Analyse für {const} abgeschlossen:", file=file, ephemeral=silent)

    @cmd_pi.command(name="find", description="Returns a list with the best planets for selected pi")
    @option(name="const_sys", description="Target Constellation or origin system", type=str, required=True)
    @option(name="resource", description="Name of pi to search", type=str, required=True)
    @option(name="distance", description="Distance from origin system to look up",
            type=int, min_value=0, max_value=30, required=False, default=0)
    @option(name="amount", description="Number of planets to return", type=int, required=False, default=None)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_find_pi(self, ctx: ApplicationContext, const_sys: str, resource: str, distance: int, amount: int,
                          silent: bool):
        await ctx.response.defer(ephemeral=True)
        resource = resource.strip()
        const = await data_utils.get_constellation(const_sys)
        has_sys = False
        if const is not None:
            result = await data_utils.get_best_pi_planets(const.name, resource, amount)
            title = f"{resource} in {const_sys}"
        else:
            sys = await data_utils.get_system(const_sys)
            if sys is None:
                await ctx.followup.send(f"\"{const_sys}\" is not a system/constellation.", ephemeral=silent)
                return
            if distance is None:
                await ctx.followup.send(f"An distance of jumps from the selected system is required.", ephemeral=silent)
                return
            result = await data_utils.get_best_pi_by_planet(sys.name, distance, resource, amount)
            title = f"{resource} near {const_sys}"
            has_sys = True
        result = sorted(result, key=lambda r: r["out"], reverse=True)
        msg = "Output in units per factory per hour\n```"
        msg += f"{'Planet':<12}: {'Output':<6}" + (f"  Jumps\n" if has_sys else "\n")
        for res in result:
            msg += f"\n{res['p_name']:<12}: {res['out']:6.2f}" + (f"  {res['distance']}j" if has_sys else "")
            if len(msg) > 3900:
                msg += "\n**(Truncated)**"
                break
        msg += "\n```"
        emb = discord.Embed(title=title, color=discord.Color.green(),
                            description="Kein Planet gefunden/ungültige Eingabe" if len(result) == 0 else msg)
        await ctx.followup.send(embed=emb, ephemeral=silent)

    @cmd_pi.command(name="planer", description="Opens the pi planer to manage your planets")
    async def cmd_pi_plan(self, ctx: ApplicationContext):
        plan = PiPlanningSession(ctx.user)
        await plan.load_plans()
        if ctx.channel.type == ChannelType.private:
            msg = await ctx.response.send_message(
                f"Du hast aktuell {len(plan.plans)} aktive Pi Pläne:",
                embeds=plan.get_embeds(),
                view=PiPlanningView(plan))
            plan.message = msg
            return
        msg = await ctx.user.send(
            f"Du hast aktuell {len(plan.plans)} aktive Pi Pläne:",
            embeds=plan.get_embeds(),
            view=PiPlanningView(plan))
        plan.message = msg
        await ctx.response.send_message("Überprüfe deine Direktnachrichten", ephemeral=True)
