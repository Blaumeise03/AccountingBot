import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, List, Dict, Any

import discord
import pytz
from discord import SlashCommandGroup, ApplicationContext, User, Embed, Color, option, ButtonStyle, InputTextStyle, \
    Message
from discord.ext import commands
from discord.ui import Button, InputText

from accounting_bot.config import Config, ConfigTree
from accounting_bot.exceptions import ConfigException
from accounting_bot.utils import admin_only, online_only, user_only, AutoDisableView, ErrorHandledModal

if TYPE_CHECKING:
    from bot import BotState, AccountingBot

logger = logging.getLogger("bot.ext.apl")

config_structure = {
    "questions": (list, []),
    "enabled": (bool, False),
    "resultChannel": (int, -1),
    "thumbnail_url": (str, ""),
    "ticket_command": (str, ""),
    "complete_message": (str, "Bewerbung abgeschickt.")
}
config = None  # type: Config | None

questions = []  # type: List[Question]
thumbnail_url = None

active_sessions = []  # type: List[ApplicationSession]


def setup(bot: "AccountingBot"):
    global config, questions, thumbnail_url
    logger.info("Loading config")
    config = Config("resources/application_config.json", ConfigTree(config_structure))
    config.load_config()
    config.save_config()
    if config["enabled"]:
        questions = Question.load_from_array(config["questions"])
        thumbnail_url = config["thumbnail_url"]
        if len(thumbnail_url) == 0:
            thumbnail_url = None
        logger.info("Loaded %s questions", len(questions))
        logger.info("Adding ApplicationCommands")
        bot.add_cog(ApplicationCommands(bot.state))
    else:
        logger.info("ApplicationCommands are not enabled")


def teardown(bot: "AccountingBot"):
    bot.remove_cog("ApplicationCommands")


class ApplicationSession(object):
    def __init__(self, user: User, state: "BotState"):
        self.bot_state = state
        self.user = user
        self.start_time = datetime.now()
        self.last_action = datetime.now()
        self.questions = []  # type: List[Question]
        self.answers = []
        self.message = None  # type: Message | None
        self.admin_msg = None  # type: Message | None
        self.completed = False
        active_sessions.append(self)

    def next_question(self):
        if len(questions) == 0:
            raise ConfigException("No questions loaded")
        if len(self.questions) == 0:
            self.questions.append(questions[0])
            return True
        if len(self.questions) < len(questions):
            self.questions.append(questions[len(self.questions)])
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
        if thumbnail_url is not None:
            embed.set_thumbnail(url=thumbnail_url)
        i = None
        for i, q in enumerate(self.questions):
            embed.add_field(name=f"Frage {i + 1}", value=q.content, inline=False)
        embed.add_field(name="Fortschritt", inline=False,
                        value=f"Du bist bei Frage {i + 1} von {len(questions)}. Bitte drücke den Knopf um die Aktuelle "
                              f"zu beantworten")
        return embed

    def build_result_embed(self):
        created_time = self.user.created_at
        age = datetime.now(pytz.UTC) - created_time
        embed = Embed(
            title=f"Bewerbung von `{self.user.name}`",
            description=f"Nutzer-ID: `{self.user.id}`\n"
                        f"Account Alter: `{age}`\n"
                        f"Account Erstellt: <t:{int(time.mktime(created_time.timetuple()))}:f>\n",
            timestamp=self.start_time, color=Color.red()
        )
        embed.set_thumbnail(url=str(self.user.display_avatar.url))
        for i, q in enumerate(self.questions):
            if i < len(self.answers):
                answer = self.answers[i]
            else:
                answer = "*Steht noch aus...*"
            embed.add_field(name=f"Frage {i + 1}: {q.content}", inline=False,
                            value=answer)
        if self.completed:
            embed.add_field(name="Status",
                            value=f"Befragung abgeschlossen. Startzeit war "
                                  f"<t:{int(time.mktime(self.start_time.timetuple()))}:f>.")
        else:
            embed.add_field(name="Status",
                            value=f"Befragung läuft...\nStartzeit war "
                                  f"<t:{int(time.mktime(self.start_time.timetuple()))}:f>\nLetzte Antwort wurde "
                                  f"<t:{int(time.mktime(self.last_action.timetuple()))}:R> abgegeben.")
        return embed

    async def update_admin_msg(self):
        if self.admin_msg is None:
            channel = await self.bot_state.bot.fetch_channel(config["resultChannel"])
            self.admin_msg = await channel.send(embed=self.build_result_embed())
        else:
            await self.admin_msg.edit(embed=self.build_result_embed())

    async def update_user_msg(self):
        await self.message.edit(embed=self.build_user_embed())

    async def open_ticket(self):
        await self.admin_msg.channel.send(config["ticket_command"].format_map(
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
        super().__init__(title=f"Frage {len(self.session.questions)} beantworten")
        self.add_item(InputText(style=InputTextStyle.multiline,
                                label="Antwort",
                                placeholder=session.questions[-1].content,
                                required=not session.questions[-1].optional,
                                max_length=session.questions[-1].max_length))

    async def callback(self, ctx: ApplicationContext):
        await ctx.response.defer(ephemeral=True)
        answer = self.children[0].value
        self.session.answers.append(answer)
        self.session.last_action = datetime.now()
        if self.session.next_question():
            await self.session.update_user_msg()
            await ctx.followup.send(content="Bitte beantworte die nächte Frage (siehe oben)", delete_after=10)
            await self.session.update_admin_msg()
        else:
            self.session.completed = True
            await self.session.update_admin_msg()
            await ctx.followup.send(content=config["complete_message"])
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
    def __init__(self, state: "BotState"):
        self.state = state

    cmd_o7 = SlashCommandGroup(name="o7", description="Application management")

    @cmd_o7.command(name="apply", description="Apply to the corp")
    async def cmd_o7_apply(self, ctx: ApplicationContext):
        session = ApplicationSession(ctx.user, self.state)
        await asyncio.gather(
            ctx.respond("Bitte überprüfe deine Direktnachrichten.", ephemeral=True),
            session.start()
        )

    @cmd_o7.command(name="reload", description="Reloads the config")
    @admin_only()
    @online_only()
    async def cmd_o7_reload(self, ctx: ApplicationContext):
        await ctx.defer(ephemeral=True)
        config.load_config()
        await ctx.response.send_message("Config neu geladen")

    @cmd_o7.command(name="show_questions", description="Shows a preview of all questions")
    @option(name="silent", description="Default true, if set to false, the command will be executed publicly",
            default=True, required=False)
    @admin_only()
    @online_only()
    async def cmd_o7_show(self, ctx: ApplicationContext, silent: bool):
        embed = Embed(title="Vetting Questions", color=Color.red())
        for question in questions:
            embed.add_field(name=question.content, inline=False,
                            value=f"Optional: {question.optional}\nMax Length: {question.max_length}")
        await ctx.respond(embed=embed, ephemeral=silent)

    @cmd_o7.command(name="ticket", description="Opens a ticket (for diplo)")
    @online_only()
    async def cmd_o7_ticket(self, ctx: ApplicationContext):
        await ctx.respond("Opening ticket...", ephemeral=True)
        channel = await self.state.bot.fetch_channel(config["resultChannel"])
        await channel.send(config["ticket_command"].format_map(defaultdict(str, id=ctx.user.id, reason="Diplomatic Request")))
