import io
import logging
import re
from enum import Enum
from typing import Dict, List

import discord
import pytz
from discord import ApplicationContext, InputTextStyle, Interaction, Option
from discord.ext import commands
from discord.ext.commands import Cooldown
from discord.ui import Modal, InputText, View

from accounting_bot import sheet, utils
from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.utils import string_to_file, list_to_string, send_exception, log_error

logger = logging.getLogger("bot.projects")
logger.setLevel(logging.DEBUG)
ADMINS = []
OWNER = -1


class ProjectCommands(commands.Cog):
    def __int__(self, bot, admins, owner):
        global ADMINS, OWNER
        self.bot = bot
        self.admins = admins
        ADMINS = admins
        self.owner = owner
        OWNER = owner

    @commands.slash_command(
        name="loadprojects",
        description="Loads and list all projects"
    )
    @commands.cooldown(1, 5, commands.BucketType.default)
    async def load_projects(self, ctx: ApplicationContext, silent: Option(bool, "Execute command silently", required=False, default=True)):
        if not (ctx.author.guild_permissions.administrator or ctx.author.id in ADMINS or ctx.author.id == OWNER):
            await ctx.respond("Missing permissions", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True)
        log = await sheet.load_projects()
        res = "Projectlist version: " + sheet.lastChanges.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M") + "\n"
        for p in sheet.allProjects:
            res += p.to_string() + "\n\n"

        await ctx.respond("Projektliste:", files=[
            string_to_file(list_to_string(log), "log.txt"),
            string_to_file(res, "project_list.txt")], ephemeral=silent)

    @commands.slash_command(
        name="listprojects",
        description="Lists all projects"
    )
    async def list_projects(self, ctx: ApplicationContext, silent: Option(bool, "Execute command silently", required=False, default=True)):
        res = "Projectlist version: " + sheet.lastChanges.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M") + "\n"
        async with sheet.projects_lock:
            for p in sheet.allProjects:
                res += p.to_string() + "\n\n"
        await ctx.respond("Projektliste:", file=string_to_file(res, "project_list.txt"), ephemeral=silent)

    @commands.slash_command(
        name="insertinvestment",
        description="Saves an investment into the sheet"
    )
    @commands.cooldown(1, 5, commands.BucketType.default)
    async def insert_investments(self, ctx: ApplicationContext):
        if ctx.author.guild_permissions.administrator or ctx.author.id in ADMINS or ctx.author.id == OWNER:
            await ctx.response.send_modal(ListModal())
        else:
            await ctx.response.send_message("Fehlende Berechtigungen!", ephemeral=True)


class ConfirmView(View):
    def __init__(self, investments: Dict[str, List[int]], player: str, log=None):
        super().__init__()
        if log is None:
            log = []
        self.log = log
        self.investments = investments
        self.player = player

    @discord.ui.button(label="Eintragen", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction: Interaction):
        if not (interaction.user.guild_permissions.administrator or interaction.user.id in ADMINS or interaction.user.id == OWNER):
            await interaction.response.send_message("Missing permissions", ephemeral=True)
            return
        await interaction.response.send_message("Bitte warten...", ephemeral=True)

        await interaction.message.edit(view=None)
        self.log.append("Inserting into sheet...")
        try:
            logger.info("Inserting investments for player %s: %s", self.player, self.investments)
            log = await sheet.insert_investments(self.player, self.investments)
            success = True
            self.log += log
        except GoogleSheetException as e:
            self.log += e.log
            self.log.append("Fatal error while processing list!")
            logger.error("Error while processing investments", exc_info=e)
            for s in self.log:
                logger.error("[Log] %s", s)
            success = False
        await interaction.followup.send(
            ("An **ERROR** occurred during execution of the command" if not success else "Investition wurde eingetragen!"),
            file=string_to_file(list_to_string(self.log), "log.txt"),
            ephemeral=False)

    async def on_error(self, error: Exception, item, interaction):
        logger.error(f"Error in ConfirmView: {error}", error)
        await send_exception(error, interaction)


class ListModal(Modal):
    def __init__(self,
                 *args, **kwargs):
        super().__init__(title="Ingame List Parser", *args, **kwargs)
        self.add_item(InputText(label="Spielername", placeholder="Spielername", required=True))
        self.add_item(InputText(label="Ingame List", placeholder="Ingame liste hier einfügen",
                                required=True, style=InputTextStyle.long))

    async def callback(self, interaction: Interaction):
        if not (interaction.user.guild_permissions.administrator or interaction.user.id in ADMINS or interaction.user.id == OWNER):
            await interaction.response.send_message("Missing permissions", ephemeral=True)
            return
        logger.debug("Insert Investments command received...")
        await interaction.response.send_message("Bitte warten, dies kann einige Sekunden dauern.", ephemeral=True)
        log = []
        player, is_perfect = utils.parse_player(self.children[0].value, sheet.users)
        player = sheet.check_name_overwrites(player)
        if player is None:
            await interaction.followup.send(f"Fehler: Spieler \"{self.children[0].value}\" nicht gefunden!")
            return
        log.append("Parsing list...")
        items = Project.Item.parse_list(self.children[1].value)
        await interaction.followup.send("Eingabe verarbeitet, lade Projekte. Bitte warten, dies dauert nun einige Sekunden", ephemeral=True)
        log.append("Reloading projects...")
        await sheet.load_projects()
        log.append("Splitting contract...")
        async with sheet.projects_lock:
            logger.debug("Splitting contract for %s ", player)
            split = Project.split_contract(items, sheet.allProjects)
        log.append("Calculating investments...")
        investments = Project.calc_investments(split)
        if not is_perfect:
            await interaction.followup.send(
                f"Meintest du \"{player}\"? (Deine Eingabe war \"{self.children[0].value}\").\n"
                f"Eingelesene items: \n```\n" + Project.Item.to_string(items) + "\n```\n"
                "Willst du diese Liste als Investition eintragen?",
                view=ConfirmView(investments, player, log),
                ephemeral=False)
            return
        await interaction.followup.send(
            f"Willst du diese Liste als Investition für \"{player}\" eintragen?\n"
            f"Eingelesene items: \n```\n" + Project.Item.to_string(items) + "\n```",
            view=ConfirmView(investments, player, log),
            ephemeral=False)

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        log_error(logger, error)
        await send_exception(error, interaction)


class Project(object):
    def __init__(self, name: str):
        self.name = name
        self.exclude = Project.ExcludeSettings.none
        self.pendingResources = []  # type: [Project.Item]
        self.investments_range = None

    def get_pending_resource(self, resource: str) -> int:
        resource = resource.casefold()
        for item in self.pendingResources:
            if item.name.casefold() == resource:
                return item.amount
        return 0

    def to_string(self) -> str:
        exclude = ""
        if self.exclude == Project.ExcludeSettings.all:
            exclude = " (ausgeblendet)"
        elif self.exclude == Project.ExcludeSettings.investments:
            exclude = " (keine Investitionen)"
        res = f"{self.name}{exclude}\nRessource: ausstehende Menge"
        for r in self.pendingResources:  # type: Project.Item
            res += f"\n{r.name}: {r.amount}"
        return res

    @staticmethod
    def split_contract(items, project_list) -> {str: [(str, int)]}:
        split = {}                                  # type: {str: [(str, int)]}
        for item in items:                          # type: Project.Item
            left = item.amount
            split[item.name] = []
            for project in reversed(project_list):  # type: Project
                if project.exclude != Project.ExcludeSettings.none:
                    continue
                pending = project.get_pending_resource(item.name)
                amount = min(pending, left)
                if pending > 0 and amount > 0:
                    left -= amount
                    split[item.name].append((project.name, amount))
            if left > 0:
                split[item.name].append(("overflow", left))
        return split

    @staticmethod
    def calc_investments(split: {str: [(str, int)]}):
        log = []
        investments = {}  # type: (str, [int])
        item_names = sheet.PROJECT_RESOURCES
        for item_name in split:  # type: str
            if item_name in item_names:
                index = item_names.index(item_name)
            else:
                log.append(f"Error: {item_name} is not a project resource!")
                continue
            for (project, amount) in split[item_name]:  # type: str, int
                if project not in investments:
                    investments[project] = [0] * len(item_names)
                investments[project][index] += amount
        return investments

    class Item(object):
        def __init__(self, name: str, amount: int):
            self.name = name
            self.amount = amount

        @staticmethod
        def sort_list(items: [], order: [str]) -> None:
            for item in items:  # type: Project.Item
                if item.name not in order:
                    order.append(item.name)
            items.sort(key=lambda x: order.index(x.name))

        @staticmethod
        def parse_list(raw: str) -> []:
            items = []  # type: [Project.Item]
            for line in raw.split("\n"):
                if re.fullmatch("[a-zA-Z ]*", line):
                    continue
                line = re.sub("\t", "    ", line.strip())      # Replace Tabs with spaces
                line = re.sub("^\\d+ *", "", line.strip())     # Delete first column (numeric Index)
                if len(re.findall("[0-9]+", line.strip())) > 1:
                    line = re.sub(" *[0-9.]+$", "", line.strip())  # Delete last column (Valuation, decimal)
                item = re.sub(" +\\d+$", "", line)
                quantity = line.replace(item, "").strip()
                item = item.strip()
                items.append(Project.Item(item, int(quantity)))
            Project.Item.sort_list(items, sheet.PROJECT_RESOURCES.copy())
            return items

        @staticmethod
        def to_string(items):
            res = ""
            for item in items:
                res += f"{item.name}: {item.amount}\n"
            return res

    class ExcludeSettings(Enum):
        none = 0  # Don't exclude the projects
        investments = 1  # Exclude the project from investments
        all = 2  # Completely hides the project
