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

from accounting_bot import utils
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import InputException, SingletonException
from accounting_bot.localization import t_
from accounting_bot.universe import data_utils
from accounting_bot.utils import State, get_cmd_name, ErrorHandledModal, help_infos, help_info, admin_only, \
    CmdAnnotation, owner_only, guild_only, AutoDisableView, ConfirmView

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
