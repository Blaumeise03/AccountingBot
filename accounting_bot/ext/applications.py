# PluginConfig
# Name: ApplicationsPlugin
# Author: Blaumeise03
# End
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Literal

import discord
import pytz
from discord import SlashCommandGroup, ApplicationContext, User, Embed, Color, option, ButtonStyle, InputTextStyle, \
    Message, PartialEmoji, Interaction
from discord.ext import commands, tasks
from discord.ui import Button, InputText

from accounting_bot import utils
from accounting_bot.config import Config
from accounting_bot.exceptions import ConfigException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import admin_only, online_only, AutoDisableView, ErrorHandledModal

logger = logging.getLogger("ext.apl")

CONFIG_TREE = {
    "questions": (list, []),
    "enabled": (bool, False),
    "resultChannel": (int, -1),
    "thumbnail_url": (str, ""),
    "ticket_command": (str, ""),
    "complete_message": (str, "Bewerbung abgeschickt."),
    "views": (list, [])
}


class ApplicationPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.config = Config()
        self.config.load_tree(CONFIG_TREE)
        self.config_path = "resources/application_config.json"
        self.active_sessions = []  # type: List[ApplicationSession]
        self.questions = []  # type: List[Question]
        self.thumbnail_url = None
        self.views = []  # type: List[AutoDisableView]

    def on_load(self):
        logger.info("Loading config")
        self.config.load_config(self.config_path)
        self.config.save_config(self.config_path)
        if self.config["enabled"]:
            self.questions = Question.load_from_array(self.config["questions"])
            self.thumbnail_url = self.config["thumbnail_url"]
            if len(self.thumbnail_url) == 0:
                self.thumbnail_url = None
            logger.info("Loaded %s questions", len(self.questions))
            logger.info("Adding ApplicationCommands")
            self.register_cog(ApplicationCommands(self))
        else:
            logger.info("ApplicationCommands are not enabled")
        msg_ids = []
        to_delete = []
        for view in self.config["views"]:
            if view["message"] in msg_ids:
                logger.warning("Duplicated views, deleting it. View %s at message %s in channel %s",
                               view["type"], view["message"], view["channel"])
                to_delete.append(view)
                continue
            msg_ids.append(view["message"])
        for v in to_delete:
            self.config["views"].remove(v)

    async def on_enable(self):
        self.apl_loop.start()
        _views = self.config["views"]  # type: List[Dict[str, Any]]
        logger.info("Setting up %s views", len(_views))
        coros = []
        to_delete = []
        for raw_view in _views:
            try:
                channel = await self.bot.get_or_fetch_channel(raw_view["channel"])
                message = await channel.fetch_message(raw_view["message"])
            except discord.HTTPException as e:
                logger.error("Failed to set up view in channel %s for message %s",
                             raw_view["channel"], raw_view["message"])
                utils.log_error(logger=self.logger, error=e, minimal=True)
                to_delete.append(raw_view)
                continue
            view_type = raw_view["type"]
            if view_type == "TICKET":
                view = TicketView(self)
            elif view_type == "APPLY":
                view = ApplyView(self)
            else:
                logger.error("Unknown view type %s for msg %s in channel %s",
                             view_type, raw_view["message"], raw_view["channel"])
                continue
            self.views.append(view)

            async def _edit_msg(msg: Message, _view: AutoDisableView):
                logger.info("Refreshing view for msg %s", msg.id)
                msg = await msg.edit(view=_view)
                if _view.message is None:
                    _view.message = msg
            coros.append(_edit_msg(message, view))
        for v in to_delete:
            _views.remove(v)
        self.config.save_config(self.config_path)
        await asyncio.gather(*coros)

    async def on_disable(self):
        self.apl_loop.cancel()
        coros = []
        logger.info("Removing %s views", len(self.views))
        for view in self.views:
            if view.message is not None:
                coros.append(view.message.edit(view=None))
        await asyncio.gather(*coros)

    @tasks.loop(minutes=5)
    async def apl_loop(self):
        delete_sessions = []
        try:
            c_time = datetime.now()
            max_time = timedelta(minutes=15)
            for session in self.active_sessions:
                if session.completed:
                    delete_sessions.append(session)
                    continue
                if (c_time - session.last_action) > max_time:
                    logger.info("Application session for %s:%s timed out on question %s",
                                session.user.name, session.user.id, len(session.questions_asked))
                    delete_sessions.append(session)
                    await asyncio.gather(
                        session.message.edit(view=None),
                        session.user.send(
                            "Es tut mir leid, auf Grund von Inaktivität wurde die Bewerbung abgebrochen, dein"
                            " aktueller Fortschritt wurde bereits übermittelt. Bitte starte die Bewerbung neu"
                            ", du kannst die bereits beantworteten Fragen überspringen (gib einfach einen "
                            "Punkt \".\" im Eingabefeld ein)."),
                        session.update_admin_msg(time_outed=True)
                    )
        except Exception as e:
            utils.log_error(logger, e, location="apl_loop")
        for session in delete_sessions:
            if session in self.active_sessions:
                self.active_sessions.remove(session)

    @apl_loop.error
    async def update_message_error(self, error):
        logger.error("Error in applications loop")
        utils.log_error(logger, error, location="applications_loop")


class ApplicationSession(object):
    def __init__(self, user: User, plugin: ApplicationPlugin):
        self.plugin = plugin
        self.user = user
        self.start_time = datetime.now()
        self.last_action = datetime.now()
        self.questions_asked = []  # type: List[Question]
        self.answers = []
        self.message = None  # type: Message | None
        self.admin_msg = None  # type: Message | None
        self.completed = False
        plugin.active_sessions.append(self)

    def next_question(self):
        if len(self.plugin.questions) == 0:
            raise ConfigException("No questions loaded")
        if len(self.questions_asked) == 0:
            self.questions_asked.append(self.plugin.questions[0])
            return True
        if len(self.questions_asked) < len(self.plugin.questions):
            self.questions_asked.append(self.plugin.questions[len(self.questions_asked)])
            return True
        return False

    async def start(self):
        view = QuestionView(self)
        self.next_question()
        self.message = await self.user.send(embed=self.build_user_embed(), view=view)
        await self.update_admin_msg()

    def build_user_embed(self):
        embed = Embed(
            title="Bewerbung",
            colour=Color.red(),
            description="Bitte beantworte die folgenden Fragen. Du hast für jede Frage maximal 10 Minuten Zeit, danach "
                        "musst Du die Bewerbung von vorne Anfangen.",
            timestamp=self.start_time
        )
        if self.plugin.thumbnail_url is not None:
            embed.set_thumbnail(url=self.plugin.thumbnail_url)
        i = None
        for i, q in enumerate(self.questions_asked):
            embed.add_field(name=f"Frage {i + 1}", value=q.content, inline=False)
        embed.add_field(name="Fortschritt", inline=False,
                        value=f"Du bist bei Frage {i + 1} von {len(self.plugin.questions)}. Bitte drücke den Knopf um die Aktuelle "
                              f"zu beantworten")
        return embed

    def build_result_embed(self, time_outed=False, reduced=False):
        created_time = self.user.created_at
        age = datetime.now(pytz.UTC) - created_time
        emb_desc = f"Nutzer-ID: `{self.user.id}`\n"
        if not reduced:
            emb_desc += f"Account Alter: `{age}`\n" \
                        f"Account Erstellt: <t:{int(time.mktime(created_time.timetuple()))}:f>\n"
        embed = Embed(
            title=f"Bewerbung von `{self.user.name}`",
            description=emb_desc,
            timestamp=self.start_time, color=Color.red()
        )
        embed.set_thumbnail(url=str(self.user.display_avatar.url))
        for i, q in enumerate(self.questions_asked):
            if i < len(self.answers):
                answer = self.answers[i]
            else:
                answer = "*Befragung abgebrochen*" if time_outed else "*Steht noch aus...*"
            embed.add_field(name=f"Frage {i + 1}: {q.content}", inline=False,
                            value=answer)
        if reduced:
            return embed
        if self.completed:
            embed.add_field(name="Status",
                            value=f"Befragung abgeschlossen.\nStartzeit "
                                  f"<t:{int(time.mktime(self.start_time.timetuple()))}:f>\nEndzeit "
                                  f"<t:{int(time.mktime(datetime.now().timetuple()))}:f>")
        else:
            embed.add_field(name="Status",
                            value=
                            ("*Befragung abgebrochen*" if time_outed else "Befragung läuft...") +
                            f"\nStartzeit war <t:{int(time.mktime(self.start_time.timetuple()))}:f>\nLetzte Antwort wurde "
                            f"<t:{int(time.mktime(self.last_action.timetuple()))}:R> abgegeben.")
        return embed

    async def update_admin_msg(self, time_outed=False):
        if self.admin_msg is None:
            channel = await self.plugin.bot.fetch_channel(self.plugin.config["resultChannel"])
            self.admin_msg = await channel.send(embed=self.build_result_embed(time_outed))
        else:
            await self.admin_msg.edit(embed=self.build_result_embed(time_outed))

    async def update_user_msg(self):
        await self.message.edit(embed=self.build_user_embed())

    async def open_ticket(self):
        await self.admin_msg.channel.send(self.plugin.config["ticket_command"].format_map(
            defaultdict(str, id=self.user.id, reason="Application")))


class QuestionView(AutoDisableView):
    def __init__(self, session: ApplicationSession):
        self.session = session
        super().__init__(timeout=None)

    @discord.ui.button(label="Beantworten...", style=ButtonStyle.green)
    async def btn_answer(self, button: Button, ctx: ApplicationContext):
        modal = QuestionModal(self.session)
        await ctx.response.send_modal(modal)


class QuestionModal(ErrorHandledModal):
    def __init__(self, session: ApplicationSession):
        self.session = session
        super().__init__(title=f"Frage {len(self.session.questions_asked)} beantworten")
        self.add_item(InputText(style=InputTextStyle.multiline,
                                label="Antwort",
                                placeholder=session.questions_asked[-1].content,
                                required=not session.questions_asked[-1].optional,
                                max_length=session.questions_asked[-1].max_length))

    async def callback(self, ctx: ApplicationContext):
        await ctx.response.defer(ephemeral=True)
        answer = self.children[0].value
        self.session.answers.append(answer)
        self.session.last_action = datetime.now()
        if self.session.next_question():
            await self.session.update_user_msg()
            # noinspection PyArgumentList
            await ctx.followup.send(content="Bitte beantworte die nächte Frage (siehe oben)",
                                    delete_after=10)
            await self.session.update_admin_msg()
        else:
            self.session.completed = True
            await self.session.update_admin_msg()
            await ctx.followup.send(content=self.session.plugin.config["complete_message"],
                                    embed=self.session.build_result_embed(reduced=True))
            await self.session.open_ticket()
            await self.session.message.edit(view=None)


class Question(object):
    def __init__(self):
        self.content = None  # type: str | None
        self.optional = False
        self.max_length = 250

    def to_dict(self):
        return {
            "content": self.content,
            "optional": self.optional,
            "max_length": self.max_length
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]):
        question = Question()
        if "content" in raw:
            question.content = raw["content"]
        else:
            raise ConfigException(f"Can't load question from dict {raw}")
        if "optional" in raw:
            question.optional = bool(raw["optional"])
        if "max_length" in raw:
            question.max_length = int(raw["max_length"])
        return question

    @staticmethod
    def load_from_array(raw: List[Dict[str, Any]]):
        result = []
        for r in raw:
            result.append(Question.from_dict(r))
        return result


class ApplicationCommands(commands.Cog):
    def __init__(self, plugin: ApplicationPlugin):
        self.plugin = plugin

    cmd_o7 = SlashCommandGroup(name="o7", description="Application management")

    @cmd_o7.command(name="apply", description="Apply to the corp")
    async def cmd_o7_apply(self, ctx: ApplicationContext):
        session = ApplicationSession(ctx.user, self.plugin)
        await asyncio.gather(
            ctx.respond("Bitte überprüfe deine Direktnachrichten.", ephemeral=True),
            session.start()
        )

    @cmd_o7.command(name="reload", description="Reloads the config")
    @admin_only()
    @online_only()
    async def cmd_o7_reload(self, ctx: ApplicationContext):
        await ctx.defer(ephemeral=True)
        self.plugin.config.load_config(self.config_path)
        await ctx.response.send_message("Config neu geladen")

    @cmd_o7.command(name="show_questions", description="Shows a preview of all questions")
    @option(name="silent", description="Default true, if set to false, the command will be executed publicly",
            default=True, required=False)
    @admin_only()
    @online_only()
    async def cmd_o7_show(self, ctx: ApplicationContext, silent: bool):
        embed = Embed(title="Vetting Questions", color=Color.red())
        for question in self.plugin.questions:
            embed.add_field(name=question.content, inline=False,
                            value=f"Optional: {question.optional}\nMax Length: {question.max_length}")
        await ctx.respond(embed=embed, ephemeral=silent)

    @cmd_o7.command(name="add_view", description="Appends a view to a message in the current channel")
    @option(name="msg_id", description="The discord id of the message", type=str)
    @option(name="view_type", description="'TICKET' or 'APPLY'", type=str, choices=["TICKET", "APPLY"])
    @admin_only()
    async def cmd_o7_add_view(self, ctx: ApplicationContext, msg_id: str, view_type: Literal["TICKET", "APPLY"]):
        await ctx.defer(ephemeral=True)
        try:
            msg = await ctx.channel.fetch_message(int(msg_id))
        except discord.NotFound:
            await ctx.followup.send(f"Message with id `{msg_id}` not found in current channel", ephemeral=True)
            return
        if view_type == "TICKET":
            view = TicketView(self.plugin)
        elif view_type == "APPLY":
            view = ApplyView(self.plugin)
        else:
            await ctx.followup.send(f"Unknown view type `{view_type}`.", ephemeral=True)
            return
        await msg.edit(view=view)
        if view.message is None:
            view.message = msg
        await ctx.followup.send(f"Attached view `{view.__class__.__name__}` to message `{msg.id}`.",
                                ephemeral=True)
        self.plugin.views.append(view)
        self.plugin.config["views"].append({
            "channel": msg.channel.id,
            "message": msg.id,
            "type": view_type
        })
        self.plugin.config.save_config(self.plugin.config_path)
        logger.info("User %s:%s added view %s to message %s in %s",
                    ctx.user.name, ctx.user.id, view_type, msg.id, msg.channel.id)

    @cmd_o7.command(name="ticket", description="Opens a ticket (for diplo)")
    @online_only()
    async def cmd_o7_ticket(self, ctx: ApplicationContext):
        await ctx.respond("Opening ticket...", ephemeral=True)
        channel = await self.plugin.bot.fetch_channel(self.plugin.config["resultChannel"])
        await channel.send(
            self.plugin.config["ticket_command"].format_map(
                defaultdict(str, id=ctx.user.id, reason="Diplomatic Request")))


class ApplyView(AutoDisableView):
    def __init__(self, plugin: ApplicationPlugin, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)
        self.plugin = plugin

    @discord.ui.button(emoji="📨",
                       label="Bewerben",
                       style=discord.ButtonStyle.green, row=0)
    async def btn_apply(self, button: discord.Button, ctx: Interaction):
        session = ApplicationSession(ctx.user, self.plugin)
        await asyncio.gather(
            ctx.response.send_message("Bitte überprüfe deine Direktnachrichten.", ephemeral=True),
            session.start()
        )


class TicketView(AutoDisableView):
    def __init__(self, plugin: ApplicationPlugin, *args, **kwargs):
        super().__init__(timeout=None, *args, **kwargs)
        self.plugin = plugin

    @discord.ui.button(emoji="🏳️",
                       label="Open ticket",
                       style=discord.ButtonStyle.green, row=0)
    async def btn_open_ticket(self, button: discord.Button, ctx: Interaction):
        await ctx.response.send_message("Opening ticket...", ephemeral=True)
        channel = await self.plugin.bot.fetch_channel(self.plugin.config["resultChannel"])
        await channel.send(
            self.plugin.config["ticket_command"].format_map(
                defaultdict(str, id=ctx.user.id, reason="Diplomatic Request")))
