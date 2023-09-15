# PluginConfig
# Name: CheckListPlugin
# Author: Blaumeise03
# End
import asyncio
import functools
import json
import logging
import os
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any, Union, Literal

import discord
from dateutil import parser
from dateutil.relativedelta import relativedelta
from discord import SlashCommandGroup, ApplicationContext, Embed, Colour, Message, Interaction
from discord.ext import commands

from accounting_bot.exceptions import UnexpectedStateException, InputException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import AutoDisableView
from accounting_bot.utils.ui import ModalForm, NumPadView

logger = logging.getLogger("ext.checklist")
CONFIG_PATH = "config/checklists.json"
hour_pattern = re.compile(r"[01]?\d:\d\d")


class CheckListPlugin(BotPlugin):

    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.checklists = []  # type: List[CheckList]

    def load_checklists(self):
        if not os.path.exists("config"):
            os.mkdir("config")
        self.checklists.clear()
        if not os.path.exists(CONFIG_PATH):
            return
        with open(CONFIG_PATH, mode="r", encoding="utf-8") as file:
            raw = json.load(file)
            for raw_list in raw["checklists"]:
                checklist = CheckList.from_dict(raw_list, self)
                self.checklists.append(checklist)

    def save_checklists(self):
        if not os.path.exists("config"):
            os.mkdir("config")
        raw = {"checklists": []}
        for checklist in self.checklists:
            raw["checklists"].append(checklist.to_dict())
        with open(CONFIG_PATH, mode="w", encoding="utf-8") as file:
            json.dump(raw, file, ensure_ascii=False, indent=2)

    async def update_messages(self):
        # ToDo: Optimize this to reduce api requests
        async_tasks = []
        to_delete = []
        for checklist in self.checklists:
            if checklist.message_id is None:
                to_delete.append(checklist)
                continue
            async_tasks.append(checklist.update_message(ignore_error=True))
        for d in to_delete:
            self.checklists.remove(d)
        self.save_checklists()
        await asyncio.gather(*async_tasks)

    def on_load(self):
        self.register_cog(CheckListCommands(self))
        self.load_checklists()

    async def on_enable(self):
        await self.update_messages()

    async def on_disable(self):
        for checklist in self.checklists:
            checklist.message = None
            if checklist.view is not None:
                checklist.view.disable_all_items()
            checklist.view = None

    def on_unload(self):
        self.save_checklists()


@functools.total_ordering
class RepeatDelay(Enum):
    never = 0
    daily = 1
    weekly = 2
    monthly = 3

    def get_next_time(self, start_time: datetime):
        today = datetime.now()
        if today < start_time:
            return start_time
        if self is RepeatDelay.never:
            return start_time
        if self is RepeatDelay.daily:
            return start_time + timedelta(days=1)
        if self is RepeatDelay.weekly:
            return start_time + timedelta(weeks=1)
        if self is RepeatDelay.monthly:
            return start_time + relativedelta(months=1)

    def __eq__(self, other):
        if not isinstance(other, RepeatDelay):
            return False
        return other.value == self.value

    def __ge__(self, other):
        if not isinstance(other, RepeatDelay):
            raise TypeError(f"Type {type(other)} is not comparable to RepeatDelay")
        return self.value >= other.value

    @staticmethod
    def from_string(string: str):
        if string.startswith("n"):
            return RepeatDelay.never
        if string.startswith("d"):
            return RepeatDelay.daily
        if string.startswith("w"):
            return RepeatDelay.weekly
        if string.startswith("m"):
            return RepeatDelay.monthly
        raise InputException(f"Unknown repeat delay '{string}'")


class Task:
    def __init__(self,
                 name: Optional[str] = None,
                 time: Optional[datetime] = None,
                 repeat: RepeatDelay = RepeatDelay.never):
        self.name = name  # type: str | None
        self.time = time  # type: datetime | None
        self.repeat = repeat  # type: RepeatDelay
        self.finished = False

    def update_time(self):
        if self.repeat == RepeatDelay.never:
            return
        now = datetime.now()
        while self.time < now:
            self.time = self.repeat.get_next_time(self.time)
            self.finished = False


def _task_filter(tasks: List[Task], time_range: Union[RepeatDelay, Literal['expired']]):
    now = datetime.now()
    if type(time_range) == str:
        max_age = now
        min_age = None
    elif isinstance(time_range, RepeatDelay):
        max_age = time_range.get_next_time(now)
        min_age = now
        if time_range > RepeatDelay.daily:
            min_age = RepeatDelay(time_range.value - 1).get_next_time(now)
    else:
        raise TypeError(f"Unsupported type {type(time_range)} for time_range")
    return filter(
        lambda task: task.time is not None and (min_age is None or min_age <= task.time) and task.time <= max_age,
        tasks
    )


class CheckList:
    def __init__(self, plugin: CheckListPlugin):
        self.plugin = plugin
        self.id = None  # type: int | None
        self.channel_id = None  # type: int | None
        self.message_id = None  # type: int | None
        self.message = None  # type: Message | None
        self.view = None  # type: CheckListView | None
        self.tasks = []  # type: List[Task]

    async def update_message(self, ignore_error=False):
        if self.message is None:
            try:
                channel = await self.plugin.bot.get_or_fetch_channel(self.channel_id)
                self.message = await channel.fetch_message(self.message_id)
            except discord.NotFound as e:
                if not ignore_error:
                    raise e
                else:
                    self.message_id = None
        if self.view is None:
            self.view = CheckListView(self)
        await self.message.edit(embed=self.build_embed(), view=self.view)

    def cleanup_tasks(self):
        now = datetime.now()
        min_time = now - timedelta(days=2)
        to_delete = []
        for task in self.tasks:
            if task.time < min_time and task.repeat == RepeatDelay.never:
                to_delete.append(task)
                continue
            if task.time > now:
                continue
            if task.finished or task.time < min_time:
                task.update_time()
        for d in to_delete:
            self.tasks.remove(d)
        self.tasks.sort(key=lambda t: t.time)

    def build_embed(self) -> Embed:
        self.cleanup_tasks()
        embed = Embed(title="Checklist", timestamp=datetime.now(), colour=Colour.red())

        def _add_field(title: str, tasks: List[Task], time_format="F"):
            msg = ""
            for task in tasks:
                if task.finished:
                    msg += "✅"
                else:
                    msg += "❌"
                msg += f"<t:{int(task.time.timestamp())}:{time_format}> {task.name}\n"
            if len(msg) > 0:
                embed.add_field(name=title, inline=False, value=msg)

        expired = sorted(_task_filter(self.tasks, "expired"), key=lambda task: task.time)
        today = sorted(_task_filter(self.tasks, RepeatDelay.daily), key=lambda task: task.time)
        week = sorted(_task_filter(self.tasks, RepeatDelay.weekly), key=lambda task: task.time)
        month = sorted(_task_filter(self.tasks, RepeatDelay.monthly), key=lambda task: task.time)

        _add_field(title="Expired", tasks=list(expired), time_format="R")
        _add_field(title="Today", tasks=list(today), time_format="t")
        _add_field(title="This Week", tasks=list(week), time_format="F")
        _add_field(title="This Month", tasks=list(month), time_format="f")

        return embed

    def build_list(self) -> str:
        msg = "```"
        for i, task in enumerate(self.tasks):
            time_str = task.time.strftime("%Y-%m-%d, %H:%M")
            msg += f"\n{i + 1:2} {'✓' if task.finished else ' '} {time_str} {task.repeat.name:7}: {task.name}  "
        msg += "\n```"
        return msg

    def to_dict(self):
        result = {
            "id": self.id,
            "channel": self.channel_id,
            "message": self.message_id,
            "tasks": []
        }
        for task in self.tasks:
            result["tasks"].append({
                "name": task.name,
                "repeat": task.repeat.value,
                "time": task.time.timestamp(),
                "finished": task.finished
            })
        return result

    @staticmethod
    def from_dict(data: Dict[str, Any], plugin: CheckListPlugin) -> "CheckList":
        checklist = CheckList(plugin)
        checklist.id = data["id"]
        checklist.channel_id = data["channel"]
        checklist.message_id = data["message"]
        for raw_task in data["tasks"]:
            task = Task(name=raw_task["name"])
            task.repeat = RepeatDelay(raw_task["repeat"])
            task.time = datetime.fromtimestamp((raw_task["time"]))
            if "finished" in raw_task:
                task.finished = raw_task["finished"]
            checklist.tasks.append(task)
        return checklist


class CheckListView(AutoDisableView):
    def __init__(self, checklist: CheckList):
        super().__init__(timeout=None)
        self.checklist = checklist

    @discord.ui.button(emoji="➕", style=discord.ButtonStyle.green, row=0)
    async def btn_add_task(self, button: discord.Button, ctx: Interaction):
        modal = (
            await
            ModalForm(title="New Task", ignore_timeout=True)
            .add_field(label="Task Name", placeholder="Enter the name of the task here")
            .add_field(label="Time", value=datetime.now().isoformat(sep=" ", timespec="minutes"))
            .open_form(ctx.response)
        )
        if modal.is_timeout():
            return
        res = modal.retrieve_results()
        task_name = res["Task Name"]
        raw_time = res["Time"]
        task_time = parser.parse(raw_time, parserinfo=parser.parserinfo(dayfirst=True))
        if not re.search(hour_pattern, raw_time):
            task_time = task_time.replace(hour=20, minute=0)
        task = Task(name=task_name, time=task_time)
        self.checklist.tasks.append(task)
        await asyncio.gather(
            modal.get_response().defer(invisible=True),
            self.checklist.update_message()
        )

    @discord.ui.button(label="✏", style=discord.ButtonStyle.blurple, row=0)
    async def btn_edit(self, button: discord.Button, ctx: Interaction):
        await ctx.response.send_message(content=self.checklist.build_list(),
                                        ephemeral=True, view=EditTaskView(self.checklist))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.gray, row=0)
    async def btn_refresh(self, button: discord.Button, ctx: Interaction):
        await ctx.response.defer(invisible=True)
        await self.checklist.update_message()

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.green, row=0)
    async def btn_save(self, button: discord.Button, ctx: Interaction):
        self.checklist.plugin.save_checklists()
        await ctx.response.defer(invisible=True)


class EditTaskView(NumPadView):
    def __init__(self, checklist: CheckList):
        super().__init__(start_row=1)
        self.checklist = checklist

    @discord.ui.select(
        placeholder="Select edit mode",
        min_values=1,
        max_values=1,
        row=0,
        options=[
            discord.SelectOption(
                label="Toggle completed",
                description="Check or uncheck pending tasks"
            ),
            discord.SelectOption(
                label="Edit time",
                description="Change the time of a task"
            ),
            discord.SelectOption(
                label="Edit repeat delay",
                description="Select how often the task should repeat"
            ),
            discord.SelectOption(
                label="Delete task",
                description="Remove a task from the checklist"
            )
        ]
    )
    async def select_callback(self, select, interaction):
        await interaction.response.defer(invisible=True)

    async def callback(self, number: int, ctx: Interaction):
        dropdown = None  # type: discord.ui.Select | None
        for child in self.children:
            if child.type == discord.ComponentType.select:
                # noinspection PyTypeChecker
                dropdown = child
        if dropdown is None:
            raise TypeError("Did not found select menu in view")
        if len(dropdown.values) == 0:
            await ctx.response.send_message(
                "No edit option selected. Please select what you want to edit from the  dropdown", ephemeral=True)
            return
        if number < 1 or number > len(self.checklist.tasks):
            await ctx.response.send_message(
                f"Selected number `{number}` is invalid, it must be between `1` and `{len(self.checklist.tasks)}`!",
                ephemeral=True)
            return
        task = self.checklist.tasks[number - 1]
        match dropdown.values[0]:
            case "Edit time":
                raw_time = (
                    await
                    ModalForm(title="Change time", submit_message=True, ignore_timeout=True)
                    .add_field(label="Time", value=datetime.now().isoformat(sep=" ", timespec="minutes"))
                    .open_form(ctx.response)
                ).retrieve_result()
                task_time = parser.parse(raw_time, parserinfo=parser.parserinfo(dayfirst=True))
                if not re.search(hour_pattern, raw_time):
                    task_time = task_time.replace(hour=20, minute=0)
                if task_time is None:
                    return
                task.time = task_time
            case "Toggle completed":
                task.finished = not task.finished
                await ctx.response.defer(ephemeral=True, invisible=True)
            case "Edit repeat delay":
                task_delay = (
                    await
                    ModalForm(title="Change delay", submit_message=True, ignore_timeout=True)
                    .add_field(label="Delay", placeholder="[N]ever, [D]aily, [W]eekly, [M]onthly")
                    .open_form(ctx.response)
                ).retrieve_result()
                if task_delay is None:
                    return
                task.repeat = RepeatDelay.from_string(task_delay.lower())
            case "Delete task":
                self.checklist.tasks.remove(task)
                await ctx.response.defer(ephemeral=True, invisible=True)
            case _:
                raise UnexpectedStateException("Unknown selection " + dropdown.values[0])
        for opt in dropdown.options:
            opt.default = False
            if opt.value == dropdown.values[0]:
                opt.default = True
        await asyncio.gather(
            self.checklist.update_message(),
            self.message.edit(content=self.checklist.build_list(), view=self)
        )


class CheckListCommands(commands.Cog):
    group = SlashCommandGroup(name="checklist", description="Tools for creating and managing tasks")

    def __init__(self, plugin: CheckListPlugin):
        self.plugin = plugin

    @discord.ext.tasks.loop(hours=4)
    async def update_messages(self):
        await self.plugin.update_messages()

    @group.command(name="new", description="Create a new checklist")
    async def cmd_status(self, ctx: ApplicationContext):
        checklist = CheckList(self.plugin)
        checklist.channel_id = ctx.channel_id
        checklist.view = CheckListView(checklist)
        msg = await ctx.channel.send(embed=checklist.build_embed(), view=checklist.view)
        checklist.message_id = msg.id
        checklist.message = msg
        self.plugin.checklists.append(checklist)
        await ctx.response.defer(ephemeral=True, invisible=True)

    @group.command(name="save", description="Saves all checklists")
    async def cmd_save(self, ctx: ApplicationContext):
        self.plugin.save_checklists()
        await ctx.response.send_message("Saved all checklists!", ephemeral=True)
