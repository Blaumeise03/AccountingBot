import logging
import re
from enum import Enum
from typing import Dict, List

import discord
import pytz
from discord import ApplicationContext, InputTextStyle, Interaction, Option, option
from discord.ext import commands
from discord.ui import Modal, InputText

from accounting_bot import sheet, utils, project_utils
from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.project_utils import format_list
from accounting_bot.utils import string_to_file, list_to_string, send_exception, log_error, AutoDisableView

logger = logging.getLogger("bot.projects")
logger.setLevel(logging.DEBUG)
ADMINS = []
OWNER = -1
GUILD = -1
USER_ROLE = -1
BOT = None  # type: commands.Bot | None


class ProjectCommands(commands.Cog):
    def __init__(self, bot, admins, owner, guild, user_role):
        global ADMINS, BOT, OWNER, GUILD, USER_ROLE
        self.bot = bot
        BOT = bot
        self.admins = admins
        ADMINS = admins
        self.owner = owner
        OWNER = owner
        GUILD = guild
        USER_ROLE = user_role

    @commands.slash_command(
        name="loadprojects",
        description="Loads and list all projects"
    )
    @commands.cooldown(1, 5, commands.BucketType.default)
    async def load_projects(self, ctx: ApplicationContext,
                            silent: Option(bool, "Execute command silently", required=False, default=True)):
        if not (ctx.author.guild_permissions.administrator or ctx.author.id in ADMINS or ctx.author.id == OWNER):
            await ctx.respond("Missing permissions", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True)
        log = await sheet.load_projects()
        res = "Projectlist version: " + sheet.lastChanges.astimezone(pytz.timezone("Europe/Berlin")).strftime(
            "%d.%m.%Y %H:%M") + "\n"
        for p in sheet.allProjects:
            res += p.to_string() + "\n\n"

        await ctx.respond("Projektliste:", files=[
            string_to_file(list_to_string(log), "log.txt"),
            string_to_file(res, "project_list.txt")], ephemeral=silent)

    @commands.slash_command(
        name="listprojects",
        description="Lists all projects"
    )
    async def list_projects(self, ctx: ApplicationContext,
                            silent: Option(bool, "Execute command silently", required=False, default=True)):
        res = "Projectlist version: " + sheet.lastChanges.astimezone(pytz.timezone("Europe/Berlin")).strftime(
            "%d.%m.%Y %H:%M") + "\n"
        async with sheet.projects_lock:
            for p in sheet.allProjects:
                res += p.to_string() + "\n\n"
        await ctx.respond("Projektliste:", file=string_to_file(res, "project_list.txt"), ephemeral=silent)

    @commands.slash_command(
        name="insertinvestment",
        description="Saves an investment into the sheet"
    )
    @option(
        "skip_loading",
        description="Skip the reloading of the projects",
        required=False,
        default=False
    )
    @option(
        "priority_project",
        description="Prioritize this project, or multiple separated by a semicolon (;)",
        required=False,
        default=""
    )
    @commands.cooldown(1, 5, commands.BucketType.default)
    async def insert_investments(self,
                                 ctx: ApplicationContext,
                                 skip_loading: bool,
                                 priority_project: str
                                 ):
        if ";" in priority_project:
            priority_projects = priority_project.split(";")
        else:
            priority_projects = [priority_project]
        if ctx.author.guild_permissions.administrator or ctx.author.id in ADMINS or ctx.author.id == OWNER:
            await ctx.response.send_modal(ListModal(skip_loading, priority_projects))
        else:
            await ctx.response.send_message("Fehlende Berechtigungen!", ephemeral=True)


# noinspection PyUnusedLocal
class ConfirmView(AutoDisableView):
    def __init__(self, investments: Dict[str, List[int]], player: str, log=None, split=None):
        super().__init__()
        if log is None:
            log = []
        self.log = log
        self.investments = investments
        self.player = player
        self.split = split

    @discord.ui.button(label="Eintragen", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction: Interaction):
        if not (
                interaction.user.guild_permissions.administrator or interaction.user.id in ADMINS or interaction.user.id == OWNER):
            await interaction.response.send_message("Missing permissions", ephemeral=True)
            return
        await interaction.response.send_message("Bitte warten...", ephemeral=True)

        await interaction.message.edit(view=None)
        self.log.append("Inserting into sheet...")
        try:
            logger.info("Inserting investments for player %s: %s", self.player, self.investments)
            log, results = await sheet.insert_investments(self.player, self.investments)
            success = True
            self.log += log
        except GoogleSheetException as e:
            self.log += e.log
            self.log.append("Fatal error while processing list!")
            results = e.progress
            # logger.error("Error while processing investments", exc_info=e)
            for s in self.log:
                logger.error("[Log] %s", s)
            success = False
        msg_list = format_list(self.split, results)
        msg_files = [string_to_file(list_to_string(self.log), "log.txt")]
        base_message = (("An **ERROR** occurred during execution of the command" if not success else
                        "Investition wurde eingetragen!"))

        msg_files.append(utils.string_to_file(msg_list, "split.txt"))
        view = InformPlayerView(BOT, self.player, self.split, results, base_message)
        await view.load_user()
        view.message = await interaction.followup.send(
            base_message,
            files=msg_files,
            ephemeral=False, view=view)
        # To prevent pinging the user, the ping will be edited into the message instead
        await view.update_message()

    async def on_error(self, error: Exception, item, interaction):
        logger.error("Error in ConfirmView: %s", error, exc_info=error)
        await send_exception(error, interaction)


# noinspection PyUnusedLocal
class InformPlayerView(AutoDisableView):
    def __init__(self, bot: commands.Bot, user: str, split: {str: [(str, int)]}, results: {str, bool}, base_message):
        super().__init__()
        self.base_message = base_message
        self.results = results
        self.split = split
        self.bot = bot
        self.user = user
        self.discord_id = utils.get_discord_id(user)

    async def load_user(self):
        if self.discord_id is None:
            main_char = utils.get_main_account(name=self.user)
            if main_char is None:
                main_char = self.user
            name, perfect, nicknames = await utils.find_discord_id(self.bot, GUILD, USER_ROLE, main_char)
            if name is not None:
                self.discord_id = nicknames[name]

    async def update_message(self):
        await self.message.edit(self.base_message + f"\n\nSoll der Nutzer <@{self.discord_id}> benachrichtigt werden?")

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    async def btn_send_callback(self, button, interaction: Interaction):
        if not (
                interaction.user.guild_permissions.administrator or interaction.user.id in ADMINS or interaction.user.id == OWNER):
            await interaction.response.send_message("Missing permissions", ephemeral=True)
            return
        if self.discord_id is None:
            await interaction.response.send_message("Kein Nutzer gefunden...", ephemeral=True)
            return
        user = await BOT.get_or_fetch_user(self.discord_id)
        admin_name = interaction.user.nick if interaction.user.nick is not None else interaction.user.name
        message = f"Dein Investitionsvertrag wurde von {admin_name} angenommen und für {self.user} eingetragen:\n"
        msg_list = f"```\n{format_list(self.split, self.results)}\n```"
        msg_files = []
        if len(message) + len(msg_list) < 1500:
            message += msg_list
        else:
            msg_files = [utils.string_to_file(msg_list, "split.txt")]
        await user.send(message, files=msg_files)
        await interaction.response.send_message(
            f"Nutzer {self.user}: {self.discord_id} wurde informiert.", ephemeral=True)
        utils.save_discord_id(self.user, self.discord_id)
        await interaction.message.edit(view=None)

    @discord.ui.button(label="Ändern", style=discord.ButtonStyle.blurple)
    async def btn_change_callback(self, button, interaction: Interaction):
        if not (
                interaction.user.guild_permissions.administrator or interaction.user.id in ADMINS or interaction.user.id == OWNER):
            await interaction.response.send_message("Missing permissions", ephemeral=True)
            return
        await interaction.response.send_modal(InformPlayerView.DiscordUserModal(self))

    async def on_error(self, error: Exception, item, interaction):
        logger.error("Error in InformPlayerView: %s", error, exc_info=error)
        await send_exception(error, interaction)

    class DiscordUserModal(Modal):
        def __init__(self,
                     view,  # type: InformPlayerView
                     *args, **kwargs):
            super().__init__(title="Discord Account hinzufügen/ändern...", *args, **kwargs)
            self.view = view
            self.add_item(InputText(label="Spielername", placeholder="Spielername", required=True))
            self.add_item(InputText(label="Discord ID", placeholder="Discord User ID",
                                    required=True, style=InputTextStyle.singleline))

        async def callback(self, interaction: Interaction):
            name = self.children[0].value
            discord_id = self.children[1].value
            matched_name, _, perfect = utils.get_main_account(name)

            if matched_name is not None:
                matched_name = sheet.check_name_overwrites(matched_name)
                utils.save_discord_id(matched_name, int(discord_id))
                await interaction.response.send_message(f"Spieler {matched_name} wurde zur ID {discord_id} eingespeichert!\n",
                                                        ephemeral=True)
                self.view.discord_id = utils.get_discord_id(matched_name)
                await self.view.update_message()
            else:
                await interaction.response.send_message(f"Fehler, Spieler {name} nicht gefunden!", ephemeral=True)

        async def on_error(self, error: Exception, interaction: Interaction) -> None:
            log_error(logger, error)
            await send_exception(error, interaction)


class ListModal(Modal):
    def __init__(self,
                 skip_loading: bool,
                 priority_projects: [str],
                 *args, **kwargs):
        super().__init__(title="Ingame List Parser", *args, **kwargs)
        self.skip_loading = skip_loading
        self.priority_projects = priority_projects
        self.add_item(InputText(label="Spielername", placeholder="Spielername", required=True))
        self.add_item(InputText(label="Ingame List", placeholder="Ingame liste hier einfügen",
                                required=True, style=InputTextStyle.long))

    async def callback(self, interaction: Interaction):
        if not (
                interaction.user.guild_permissions.administrator or interaction.user.id in ADMINS or interaction.user.id == OWNER):
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
        if not self.skip_loading:
            await interaction.followup.send(
                "Eingabe verarbeitet, lade Projekte. Bitte warten, dies dauert nun einige Sekunden", ephemeral=True)
            log.append("Reloading projects...")
            await sheet.load_projects()
        log.append("Splitting contract...")
        async with sheet.projects_lock:
            logger.debug("Splitting contract for %s ", player)
            split = Project.split_contract(items, sheet.allProjects, self.priority_projects)
        log.append("Calculating investments...")
        investments = Project.calc_investments(split)
        message = ""
        if not is_perfect:
            message = f"Meintest du \"{player}\"? (Deine Eingabe war \"{self.children[0].value}\").\n"
        msg_list = project_utils.format_list(split, [])
        message += f"Eingelesene items: \n```\n{Project.Item.to_string(items)}\n```\n" \
                   f"Willst du diese Liste als Investition für {player} eintragen?\n" \
                   f"Sheet: `{sheet.sheet_name}`"
        msg_file = [utils.string_to_file(msg_list, "split.txt")]

        await interaction.followup.send(
            message,
            view=ConfirmView(investments, player, log, split),
            files=msg_file,
            ephemeral=False)
        return

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
    def split_contract(items, project_list: ['Project'], priority_projects: [str] = None) -> {str: [(str, int)]}:
        projects_ordered = project_list[::-1]  # Reverse the list
        if priority_projects is None:
            priority_projects = []
        else:
            for p_name in reversed(priority_projects):
                for p in projects_ordered:  # type: Project
                    if p.name == p_name:
                        projects_ordered.remove(p)
                        projects_ordered.insert(0, p)
        split = {}  # type: {str: [(str, int)]}
        for item in items:  # type: Project.Item
            left = item.amount
            split[item.name] = []
            for project in projects_ordered:  # type: Project
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
                line = re.sub("\t", "    ", line.strip())  # Replace Tabs with spaces
                line = re.sub("^\\d+ *", "", line.strip())  # Delete first column (numeric Index)
                if len(re.findall("[0-9]+", line.strip())) > 1:
                    line = re.sub(" *[0-9.]+$", "", line.strip())  # Delete last column (Valuation, decimal)
                item = re.sub(" +\\d+$", "", line)
                quantity = line.replace(item, "").strip()
                if len(quantity) == 0:
                    continue
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
        none = 0  # Don't exclude the project
        investments = 1  # Exclude the project from investments
        all = 2  # Completely hides the project
