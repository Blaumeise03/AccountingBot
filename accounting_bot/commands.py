import asyncio
import datetime
import enum
import functools
import logging
import re
import time
from typing import TYPE_CHECKING, Optional, Callable, List, Union

import discord
from discord import User, ApplicationContext, AutocompleteContext, option, Role, SlashCommand, \
    MessageCommand, ContextMenuCommand, UserCommand, Embed, Message, Color, Interaction
from discord.ext import commands
from discord.ext.commands import Command
from discord.ui import InputText

from accounting_bot import sheet, utils
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import InputException, SingletonException
from accounting_bot.localisation import t_
from accounting_bot.universe import data_utils
from accounting_bot.utils import State, get_cmd_name, ErrorHandledModal, help_infos, help_info, admin_only, \
    user_only, online_only, CmdAnnotation, owner_only, guild_only, AutoDisableView, ConfirmView

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.commands")


def main_char_autocomplete(self: AutocompleteContext):
    return filter(lambda n: self.value is None or n.startswith(self.value.strip()), utils.main_chars)


STATE = None  # type: BotState | None
BOUNTY_ADMINS = []
FRPs_CHANNEL = None  # type: int | None
FRPs_MSG_ID = None  # type: int | None
FRPs_MSG = None  # type: Message | None
FRP_ROLE_PING = None  # type: int | None


def setup(state: "BotState"):
    global STATE, BOUNTY_ADMINS, FRPs_CHANNEL, FRPs_MSG_ID, FRP_ROLE_PING
    STATE = state
    BOUNTY_ADMINS = STATE.config["killmail_parser.admins"]
    FRPs_CHANNEL = STATE.config["frpMenuChannel"]
    FRP_ROLE_PING = STATE.config["frpRolePing"]
    if FRPs_CHANNEL == -1:
        FRPs_CHANNEL = None
    if FRPs_CHANNEL is not None:
        FRPs_MSG_ID = STATE.config["frpMenuMessage"]
        if FRPs_MSG_ID == -1 or FRPs_MSG_ID is None:
            FRPs_MSG_ID = None
            FRPs_CHANNEL = None
    if FRP_ROLE_PING == -1:
        FRP_ROLE_PING = None
    FRPsState()


def save_config():
    STATE.config["frpMenuChannel"] = -1 if FRPs_CHANNEL is None else FRPs_CHANNEL
    STATE.config["frpMenuMessage"] = -1 if FRPs_MSG_ID is None else FRPs_MSG_ID
    STATE.config.save_config()


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
                    cmd_desc += f"\n*{t_('parameter')}*:\n"
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
    @option(name="embed", description="The name of the embed to send", type=str, required=True)
    @option(name="msg", description="Edit this message ID instead of posting a new message", default=None)
    @admin_only()
    async def embed(self, ctx: ApplicationContext, embed: str, msg: str = None):
        if embed not in STATE.bot.embeds:
            await ctx.respond(f"Embed `{embed}` not found!", ephemeral=True)
            return
        if msg is None:
            logger.info("Sending embed %s to channel %s:%s", embed, ctx.channel.name, ctx.channel.id)
            await ctx.send(embed=STATE.bot.embeds[embed])
            await ctx.respond("Neues Menü gesendet.", ephemeral=True)
            return
        try:
            msg = await ctx.channel.fetch_message(int(msg))
        except discord.NotFound:
            await ctx.respond("Message not found in this channel", ephemeral=True)
            return
        logger.info("Updating embed %s on message %s in channel %s:%s", embed, msg, ctx.channel.name,
                    ctx.channel.id)
        await msg.edit(embeds=STATE.bot.embeds[embed])
        await ctx.respond("Embed updated", ephemeral=True)

    @commands.slash_command(name="frp_menu", description="Creates a FRP ping menu")
    @admin_only()
    @guild_only()
    async def cmd_frp_ping(self, ctx: ApplicationContext):
        global FRPs_MSG_ID, FRPs_CHANNEL
        await ctx.response.defer(ephemeral=True, invisible=False)
        logger.info("Sending FRPs menu")
        view = FRPsView()
        m = await ctx.send(view=view)
        if view.message is None:
            view.message = m
        await view.refresh_msg()
        FRPs_MSG_ID = m.id
        FRPs_CHANNEL = m.channel.id
        save_config()
        await ctx.followup.send("Neues Menü gesendet.")

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
    @option("user", description="The user to look up", required=False, default=None)
    @option("silent", description="Default true, execute the command silently", required=False, default=True)
    async def cmd_show_bounties(self, ctx: ApplicationContext, user: User = None, silent: bool = True):
        if user is None:
            user = ctx.user
        player = utils.get_main_account(discord_id=user.id)[0]
        if player is None:
            await ctx.response.send_message("Kein Spieler zu diesem Discord Account gefunden", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=silent, invisible=False)
        start, end = utils.get_month_edges(datetime.datetime.now())
        res = await data_utils.get_bounties_by_player(start, end, player)
        for b in res:
            if b["type"] == "T":
                factor = sheet.BOUNTY_TACKLE
            elif b["region"] in sheet.BOUNTY_HOME_REGIONS:
                factor = sheet.BOUNTY_HOME
            else:
                factor = sheet.BOUNTY_NORMAL
            b["value"] = factor * min(b["value"], sheet.BOUNTY_MAX)
            if b["ship"] is None:
                b["ship"] = "N/A"
        msg = f"Bounties aus diesem Monat für `{player}`\n```"
        b_sum = functools.reduce(lambda x, y: x + y, map(lambda b: b["value"], res))
        i = 0
        for b in res:
            msg += f"\n{b['type']} {b['kill_id']:<7} {b['ship']:<12.12} {b['value']:11,.0f} ISK"
            if len(msg) > 1400:
                msg += f"\ntruncated {len(res) - i - 1} more killmails"
                break
            i += 1
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





class FRPsState(object):
    defaultState = None  # type: FRPsState | None

    @functools.total_ordering
    class State(enum.Enum):
        idle = 0
        pinged = 1
        active = 2
        completed = 3

        def __eq__(self, o: object) -> bool:
            if not isinstance(o, FRPsState.State):
                return NotImplemented
            return o.value == self.value

        def __ge__(self, o: object) -> bool:
            if not isinstance(o, FRPsState.State):
                return NotImplemented
            return self.value >= o.value

        def get_str(self):
            match self:
                case FRPsState.State.idle:
                    return "Nicht aktiv"
                case FRPsState.State.pinged:
                    return "FRPs gepingt"
                case FRPsState.State.active:
                    return "FRPs aktiv"
                case FRPsState.State.completed:
                    return "FRPs beendet"
            return None

    def __init__(self) -> None:
        if FRPsState.defaultState is not None:
            raise SingletonException("Only one instance of FRPsState is allowed")
        self.state = FRPsState.State.idle
        self.user = None  # type: int | None
        self.user_name = None  # type: str | None
        self.time = None  # type: datetime.datetime | None
        self.info = None  # type: str | None
        self.view = None  # type: FRPsView | None
        self.ping = None  # type: Message | None
        self.next_reminder = None  # type: datetime.datetime | None
        self.reminder_list = []  # type: List[User]
        FRPsState.defaultState = self

    def reset(self) -> None:
        self.state = FRPsState.State.idle
        self.user = None
        self.user_name = None
        self.time = None
        self.info = None
        self.next_reminder = None
        self.reminder_list.clear()

    async def tick(self):
        current_t = datetime.datetime.now()
        if self.time is not None and self.state == FRPsState.State.pinged:
            if self.time < current_t:
                self.state = FRPsState.State.active
                logger.info("FRPs automatically activated, user %s:%s, info %s", self.user_name, self.user, self.info)
                await self.view.refresh_msg()
        if self.state > FRPsState.State.pinged:
            if self.next_reminder is None or self.next_reminder < current_t:
                self.next_reminder = current_t + datetime.timedelta(minutes=20)
                await self.send_reminders()

    async def send_reminders(self):
        await self.inform_users(
            "Erinnerung: Deaktiviere die Jammer Tower sobald diese nicht mehr benötigt werden.\n"
            "Diese Erinnerung wird alle 20min wiederholt. Sobald die FRPs beendet sind und die "
            "Jammer wieder aktiv sind, klicke erst auf \"FRPs beendet\" und dann \"Jammer aktiv\" um "
            "die Erinnerung zu deaktivieren."
        )

    async def inform_users(self, msg: str):
        routines = []
        if self.user is not None:
            user = STATE.bot.get_user(self.user)
            if user is None:
                user = await STATE.bot.fetch_user(self.user)
            routines.append(user.send(msg))
        for user in self.reminder_list:
            routines.append(user.send(msg))
        await asyncio.gather(*routines)


class FRPsView(AutoDisableView):
    def __init__(self, *args, **kwargs):
        super().__init__(timeout=None)
        FRPsState.defaultState.view = self

    async def refresh_msg(self):
        embed = Embed(
            title="FRPs Pingen", color=Color(3066993),
            description="Nutze dieses Menü um FRPs zu pingen.")
        embed.add_field(
            name="Erklärung", inline=False,
            value="Wenn du FRPs pingen willst, drücke auf den entsprechenden Knopf und gebe die weiteren Infos ein. "
                  "Als Zeit gib bitte eine Uhrzeit (z.B. `20:00`) oder die Anzahl der Minuten (z.B. `15min`) ein.\n\n"
                  "Sobald die FRPs starten (der Start kann auch manuel durch den Knopf ohne Ping ausgelöst werden), "
                  "bekommst du alle 20min eine Erinnerung, die **Jammer wieder zu reaktivieren**.\n\n"
                  "Sobald die FRPs beendet sind und die Jammer wieder aktiviert sind, drücke die entsprechenden "
                  "Knöpfe.")
        embed.add_field(
            name="Weiteres", inline=False,
            value="⏰: Fügt dich zur Erinnerungsliste hinzu\n`Absagen`: Beendet die FRPs ohne den Ping zu löschen"
        )
        embed.add_field(
            name="Status", inline=False,
            value=FRPsState.defaultState.state.get_str()
        )
        state = FRPsState.defaultState.state
        if state > FRPsState.State.idle:
            t = int(time.mktime(FRPsState.defaultState.time.timetuple()))
            embed.add_field(
                name="Startzeit", inline=False,
                value=f"<t:{t}:R>\n<t:{t}:f>"
            )
            embed.add_field(
                name="Info", inline=False,
                value=f"{FRPsState.defaultState.info}\nGepingt von <@{FRPsState.defaultState.user}>"
            )
        for btn in self.children:
            if isinstance(btn, discord.ui.Button):
                match btn:
                    case self.btn_ping:
                        btn.disabled = state > FRPsState.State.idle
                    case self.btn_start:
                        btn.disabled = state == FRPsState.State.active
                    case self.btn_stop:
                        btn.disabled = state != FRPsState.State.active
                    case self.btn_jammer:
                        btn.disabled = state != FRPsState.State.completed
                    case self.btn_reminder:
                        btn.disabled = state < FRPsState.State.pinged
                    case self.btn_postpone:
                        btn.disabled = state < FRPsState.State.pinged
                    case _:
                        btn.disabled = False
        await self.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Ping", style=discord.ButtonStyle.green, row=0)
    async def btn_ping(self, button: discord.Button, ctx: Interaction):
        await ctx.response.send_modal(FRPsModal())

    @discord.ui.button(label="Starten", style=discord.ButtonStyle.green, row=0)
    async def btn_start(self, button: discord.Button, ctx: ApplicationContext):
        state = FRPsState.defaultState
        if state.state != FRPsState.State.active:
            state.state = FRPsState.State.active
        if state.time is None:
            state.time = datetime.datetime.now()
        if state.user is None:
            state.user = ctx.user.id
            state.user_name = ctx.user.name
        await ctx.response.defer(ephemeral=True, invisible=True)
        await state.view.refresh_msg()

    @discord.ui.button(emoji="⏰", style=discord.ButtonStyle.blurple, row=0)
    async def btn_reminder(self, button: discord.Button, ctx: ApplicationContext):
        state = FRPsState.defaultState
        if state.state < FRPsState.State.pinged:
            await ctx.response.send_message("Aktuell laufen keine FRPs, pinge oder starte zunächst die FRPs.",
                                            ephemeral=True)
            return
        if ctx.user.id == state.user or ctx.user in state.reminder_list:
            await ctx.response.send_message("Du erhältst bereits Erinnerungen",
                                            ephemeral=True)
            return
        state.reminder_list.append(ctx.user)
        await ctx.response.send_message("Du erhältst jetzt alle 20 Minuten Erinnerungen, sobald die FRPs starten.",
                                        ephemeral=True)

    @discord.ui.button(label="FRPs beendet", style=discord.ButtonStyle.red, row=1)
    async def btn_stop(self, button: discord.Button, ctx: ApplicationContext):
        state = FRPsState.defaultState
        if state.state != FRPsState.State.active:
            await ctx.response.send_message("FRPs sind nicht aktiv", ephemeral=True)
            return
        state.state = FRPsState.State.completed
        await ctx.response.send_message("FRPs als beendet markiert, sobald alle Jammer wieder aktiv sind, drücke den"
                                        "Knopf \"Jammer aktiv\" um die Erinnerungen zu deaktivieren.", ephemeral=True)
        await state.view.refresh_msg()
        if state.ping is not None:
            await state.ping.delete()
        state.ping = None

    @discord.ui.button(label="Jammer aktiv", style=discord.ButtonStyle.red, row=1)
    async def btn_jammer(self, button: discord.Button, ctx: ApplicationContext):
        state = FRPsState.defaultState
        if state.state != FRPsState.State.completed:
            await ctx.response.send_message("Die FRPs sind noch nicht beendet", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True, invisible=False)
        await state.inform_users("Die Erinnerungen wurden deaktiviert.")
        logger.info("Jammer reactivation confirmed by %s:%s", ctx.user.name, ctx.user.id)
        state.reset()
        await asyncio.gather(ctx.followup.send("Erinnerung deaktiviert"),
                             state.view.refresh_msg())

    @discord.ui.button(label="Absagen", style=discord.ButtonStyle.gray, row=0)
    async def btn_postpone(self, button: discord.Button, ctx: ApplicationContext):
        state = FRPsState.defaultState
        if state.state < FRPsState.State.pinged:
            await ctx.response.send_message("FRPs sind nicht gepingt.", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True, invisible=False)
        await state.inform_users("Die Erinnerungen wurden deaktiviert, da die FRPs abgesagt/verschoben wurden.")
        state.reset()
        await asyncio.gather(ctx.followup.send("FRPs verschoben"),
                             state.view.refresh_msg())


class FRPsModal(ErrorHandledModal):
    def __init__(self, *args, **kwargs):
        super().__init__(title="FRPs Pingen", *args, **kwargs)
        self.add_item(InputText(label="Anzahl", placeholder="z.B. \"3 FRPs\" oder \"3-5 FRPs\"", required=True))
        self.add_item(
            InputText(label="Zeit", placeholder="z.B. \"20:00\" oder \"15min\"", value="15min", required=True))

    async def callback(self, ctx: ApplicationContext):
        amount = self.children[0].value
        time_raw = self.children[1].value.lower()
        start_time = datetime.datetime.now()
        if re.fullmatch(r"[0-9- ]+", amount):
            amount = f"{amount} FRPs"
        if ":" in time_raw or "uhr" in time_raw:
            time_raw = re.sub(r"[^0-9:]", "", time_raw)
            time_raw = time_raw.split(":")
            if len(time_raw[0]) == 0:
                raise InputException(f"Time {time_raw} is invalid! Use format \"HH:MM\"")
            hours = int(time_raw[0])
            if len(time_raw) > 1:
                mins = int(time_raw[1])
            else:
                mins = 0
            start_time = start_time.replace(hour=hours, minute=mins)
        else:
            time_raw = re.sub(r"[^0-9]", "", time_raw)
            if len(time_raw) == 0:
                raise InputException(f"Time {time_raw} is invalid! Use format \"HH:MM\" or \"xy min\"")
            minutes = int(time_raw)
            start_time = start_time + datetime.timedelta(minutes=minutes)
        t = int(time.mktime(start_time.timetuple()))

        async def _confirm_ping(_ctx: ApplicationContext):
            global FRPs_MSG
            await _ctx.response.defer(ephemeral=True, invisible=True)
            if FRPs_MSG is None:
                FRPs_MSG = await ctx.channel.fetch_message(FRPs_MSG_ID)
            state = FRPsState.defaultState
            state.user = ctx.user.id
            state.user_name = ctx.user.name
            state.state = FRPsState.State.pinged
            state.time = start_time
            state.info = amount
            logger.info("FRP pinged by %s:%s, time: %s, info: %s",
                        ctx.user.name, ctx.user.id, state.time, state.info)

            msg = await FRPs_MSG.reply(f"<@&{FRP_ROLE_PING}> {state.info} <t:{t}:R>\n<t:{t}:f>")
            state.ping = msg
            await state.view.refresh_msg()
            await msg.add_reaction("🗑️")

        await ctx.response.send_message(
            f"Willst du diesen Ping senden?\n\n{amount} <t:{t}:R>\n<t:{t}:f>",
            view=ConfirmView(_confirm_ping), ephemeral=True)
