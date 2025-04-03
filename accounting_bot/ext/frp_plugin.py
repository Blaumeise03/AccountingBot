# PluginConfig
# Name: FrpPlugin
# Author: Blaumeise03
# End
import asyncio
import enum
import functools
import logging
import re
import time
from datetime import datetime, timedelta
from typing import List, Dict

import discord
from discord import Message, User, Embed, Color, Interaction, ApplicationContext, RawReactionActionEvent
from discord.ext import commands, tasks
from discord.ui import InputText

from accounting_bot import utils
from accounting_bot.exceptions import InputException, ConfigException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import admin_only, guild_only, AutoDisableView, ErrorHandledModal
from accounting_bot.utils.ui import ConfirmView

logger = logging.getLogger("ext.frp")
CONFIG_TREE = {
    "channel_ids": (list, []),
    "msg_ids": (list, []),
    "ping_role": (int, None)
}


class FrpPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.config = bot.create_sub_config("frp_plugin")
        self.config.load_tree(CONFIG_TREE)
        self.frp_messages = {}  # type: Dict[int, int]
        self.frp_states = []  # type: List[FRPsState]

    def on_load(self):
        self.register_cog(FrpCommands(self))
        if len(self.config["channel_ids"]) != len(self.config["msg_ids"]):
            raise ConfigException("channel_ids and msg_ids have a different length")
        for i, c in enumerate(self.config["channel_ids"]):
            if type(c) != int:
                raise ConfigException("channel_ids must all be integers")
            if type(self.config["msg_ids"][i]) != int:
                raise ConfigException("msg_ids must all be integers")
            self.frp_messages[self.config["msg_ids"][i]] = c
        logger.info("Loaded %s frp messages from config", len(self.frp_messages))

    async def on_enable(self):
        to_delete = []
        for msg_id, chan_id in self.frp_messages.items():
            channel = await self.bot.get_or_fetch_channel(chan_id)
            if channel is None:
                logger.info("Channel %s for message %s not found, deleting it", chan_id, msg_id)
                to_delete.append((msg_id, chan_id))
                continue
            p_msg = channel.get_partial_message(msg_id)
            frp_state = FRPsState(self)
            view = FRPsView(frp_state)
            try:
                msg = await p_msg.edit(view=view)
                view.real_message_handle = msg
            except discord.NotFound:
                logger.info("Message %s not found in channel %s, deleting it", msg_id, chan_id)
                to_delete.append((msg_id, chan_id))
                continue
            except discord.Forbidden as e:
                logger.error("Message %s in channel %s can't be edited: %s", msg_id, chan_id, e)
                continue
            self.frp_states.append(frp_state)
        funcs = []
        for frp_state in self.frp_states:
            funcs.append(frp_state.view.refresh_msg())
        logger.info("Refreshing %s frp messages", len(funcs))
        await asyncio.gather(*funcs)

    async def on_disable(self):
        for state in self.frp_states:
            await state.inform_users(msg="Der Bot wurde gestoppt, es wird keine Erinnerungen mehr geben")
        self.frp_states.clear()

    def on_unload(self):
        self.frp_messages.clear()

    async def update_messages(self):
        funcs = []
        for frp in self.frp_states:
            funcs.append(frp.tick())
        await asyncio.gather(*funcs)


class FrpCommands(commands.Cog):
    def __init__(self, plugin: FrpPlugin):
        super().__init__()
        self.plugin = plugin
        self.update_messages.start()

    def cog_unload(self) -> None:
        self.update_messages.cancel()

    @commands.slash_command(name="frp_menu", description="Creates a FRP ping menu")
    @admin_only()
    @guild_only()
    async def cmd_frp_menu(self, ctx: ApplicationContext):
        await ctx.response.defer(ephemeral=True, invisible=False)
        logger.info("Sending FRPs menu")
        state = FRPsState(self.plugin)
        view = FRPsView(state)
        m = await ctx.send(view=view)
        view.real_message_handle = m
        await view.refresh_msg()
        self.plugin.frp_states.append(state)
        self.plugin.config["channel_ids"].append(m.channel.id)
        self.plugin.config["msg_ids"].append(m.id)
        self.plugin.bot.save_config()
        await ctx.followup.send("Neues Menü gesendet.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, event: RawReactionActionEvent):
        if event.emoji.name == "🗑️":
            channel = await self.plugin.bot.get_or_fetch_channel(event.channel_id)
            msg = await channel.fetch_message(event.message_id)
            if msg.author != self.plugin.bot.user:
                return
            if event.user_id == self.plugin.bot.user.id:
                return
            if msg.reference is None:
                return
            is_ping = False
            state = None
            for state in self.plugin.frp_states:
                if state.view.message.id == msg.reference.message_id:
                    is_ping = True
                    break
            if not is_ping:
                return
            await msg.delete(reason=f"Deleted by {event.user_id}")
            if state.state > FRPsState.State.idle:
                await state.inform_users(f"Ping wurde von {event.user_id} gelöscht")
            logger.warning("Ping in channel %s for menu %s was deleted by %s",
                           event.channel_id, msg.reference.message_id, event.user_id)

    @tasks.loop(minutes=1)
    async def update_messages(self):
        await self.plugin.update_messages()

    @update_messages.error
    async def update_message_error(self, error):
        logger.error("Error in frp loop")
        utils.log_error(logger, error, location="frp_loop")


class FRPsState(object):
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

    def __init__(self, plugin: FrpPlugin) -> None:
        self.plugin = plugin
        self.state = FRPsState.State.idle
        self.user = None  # type: int | None
        self.user_name = None  # type: str | None
        self.time = None  # type: datetime | None
        self.info = None  # type: str | None
        self.view = None  # type: FRPsView | None
        self.ping = None  # type: Message | None
        self.next_reminder = None  # type: datetime | None
        self.reminder_list = []  # type: List[User]
        # FRPsState.default_state = self

    def reset(self) -> None:
        self.state = FRPsState.State.idle
        self.user = None
        self.user_name = None
        self.time = None
        self.info = None
        self.next_reminder = None
        self.reminder_list.clear()

    async def tick(self):
        current_t = datetime.now()
        if self.time is not None and self.state == FRPsState.State.pinged:
            if self.time < current_t:
                self.state = FRPsState.State.active
                logger.info("FRPs automatically activated, user %s:%s, info %s", self.user_name, self.user, self.info)
                await self.view.refresh_msg()
        if self.state > FRPsState.State.pinged:
            if self.next_reminder is None or self.next_reminder < current_t:
                self.next_reminder = current_t + timedelta(minutes=20)
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
            user = await self.plugin.bot.get_or_fetch_user(self.user)
            routines.append(user.send(msg))
        for user in self.reminder_list:
            routines.append(user.send(msg))
        await asyncio.gather(*routines)


# noinspection PyUnusedLocal
class FRPsView(AutoDisableView):
    def __init__(self, frp_state: FRPsState, *args, **kwargs):
        super().__init__(timeout=None)
        self.frp_state = frp_state
        frp_state.view = self

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
            value=self.frp_state.state.get_str()
        )
        state = self.frp_state.state
        if state > FRPsState.State.idle:
            t = int(time.mktime(self.frp_state.time.timetuple()))
            embed.add_field(
                name="Startzeit", inline=False,
                value=f"<t:{t}:R>\n<t:{t}:f>"
            )
            embed.add_field(
                name="Info", inline=False,
                value=f"{self.frp_state.info}\nGepingt von <@{self.frp_state.user}>"
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
        await ctx.response.send_modal(FRPsModal(self.frp_state))

    @discord.ui.button(label="Starten", style=discord.ButtonStyle.green, row=0)
    async def btn_start(self, button: discord.Button, ctx: ApplicationContext):
        state = self.frp_state
        if state.state != FRPsState.State.active:
            state.state = FRPsState.State.active
        if state.time is None:
            state.time = datetime.now()
        if state.user is None:
            state.user = ctx.user.id
            state.user_name = ctx.user.name
        await ctx.response.defer(ephemeral=True, invisible=True)
        await state.view.refresh_msg()

    @discord.ui.button(emoji="⏰", style=discord.ButtonStyle.blurple, row=0)
    async def btn_reminder(self, button: discord.Button, ctx: ApplicationContext):
        state = self.frp_state
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
        state = self.frp_state
        if state.state != FRPsState.State.active:
            await ctx.response.send_message("FRPs sind nicht aktiv", ephemeral=True)
            return
        state.state = FRPsState.State.completed
        await ctx.response.send_message("FRPs als beendet markiert, sobald alle Jammer wieder aktiv sind, drücke den"
                                        "Knopf \"Jammer aktiv\" um die Erinnerungen zu deaktivieren.", ephemeral=True)
        await state.view.refresh_msg()
        if state.ping is not None:
            try:
                await state.ping.delete()
            except discord.NotFound:
                pass
        state.ping = None

    @discord.ui.button(label="Jammer aktiv", style=discord.ButtonStyle.red, row=1)
    async def btn_jammer(self, button: discord.Button, ctx: ApplicationContext):
        state = self.frp_state
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
        state = self.frp_state
        if state.state < FRPsState.State.pinged:
            await ctx.response.send_message("FRPs sind nicht gepingt.", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True, invisible=False)
        await state.inform_users("Die Erinnerungen wurden deaktiviert, da die FRPs abgesagt/verschoben wurden.")
        state.reset()
        await asyncio.gather(ctx.followup.send("FRPs verschoben"),
                             state.view.refresh_msg())


class FRPsModal(ErrorHandledModal):
    def __init__(self, frp_state: FRPsState, *args, **kwargs):
        super().__init__(title="FRPs Pingen", *args, **kwargs)
        self.frp_state = frp_state
        self.add_item(InputText(label="Anzahl", placeholder="z.B. \"3 FRPs\" oder \"3-5 FRPs\"", required=True))
        self.add_item(
            InputText(label="Zeit", placeholder="z.B. \"20:00\" oder \"15min\"", value="15min", required=True))

    async def callback(self, ctx: ApplicationContext):
        amount = self.children[0].value
        time_raw = self.children[1].value.lower()
        start_time = datetime.now()
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
            time_raw = re.sub(r"\D", "", time_raw)
            if len(time_raw) == 0:
                raise InputException(f"Time {time_raw} is invalid! Use format \"HH:MM\" or \"xy min\"")
            minutes = int(time_raw)
            start_time = start_time + timedelta(minutes=minutes)
        t = int(time.mktime(start_time.timetuple()))

        async def _confirm_ping(_ctx: ApplicationContext):
            await _ctx.response.defer(ephemeral=True, invisible=True)
            state = self.frp_state
            state.user = ctx.user.id
            state.user_name = ctx.user.name
            state.state = FRPsState.State.pinged
            state.time = start_time
            state.info = amount
            logger.info("FRP pinged by %s:%s, time: %s, info: %s",
                        ctx.user.name, ctx.user.id, state.time, state.info)
            role_id = self.frp_state.plugin.config["ping_role"]
            if role_id is not None and role_id != -1:
                msg = await self.frp_state.view.message.reply(f"<@&{role_id}> {state.info} <t:{t}:R>\n<t:{t}:f>")
            else:
                msg = await self.frp_state.view.message.reply(f"@here {state.info} <t:{t}:R>\n<t:{t}:f>")
            state.ping = msg
            await state.view.refresh_msg()
            await msg.add_reaction("🗑️")

        await ctx.response.send_message(
            f"Willst du diesen Ping senden?\n\n{amount} <t:{t}:R>\n<t:{t}:f>",
            view=ConfirmView(_confirm_ping), ephemeral=True)
