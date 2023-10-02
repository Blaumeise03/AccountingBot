# PluginConfig
# Name: ProjectPlugin
# Author: Blaumeise03
# Depends-On: [accounting_bot.ext.members, accounting_bot.ext.sheet.sheet_main]
# Localization: project_lang.xml
# End
import asyncio
import functools
import hashlib
import logging
import time
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Tuple, Callable

import discord
import pytz
from discord import ApplicationContext, InputTextStyle, Interaction, Option, option, Embed, Colour
from discord.ext import commands
from discord.ext.commands import cooldown
from discord.ui import InputText
from gspread import Cell

from accounting_bot import utils
from accounting_bot.exceptions import GoogleSheetException, BotOfflineException
from accounting_bot.ext.members import MembersPlugin, member_only
from accounting_bot.ext.sheet.projects import project_utils, _project_tools
from accounting_bot.ext.sheet import sheet_main
from accounting_bot.ext.sheet.projects.project_utils import format_list, Project
from accounting_bot.ext.sheet.sheet_main import SheetPlugin
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.universe.data_utils import Item
from accounting_bot.utils import string_to_file, list_to_string, AutoDisableView, ErrorHandledModal, admin_only, \
    online_only

logger = logging.getLogger("ext.project")
# logger.setLevel(logging.DEBUG)
CONFIG_TREE = {
    "sheet_overview_name": (str, "Ressourcenbedarf Projekte"),
    "sheet_overflow_name": (str, "Projektüberlauf"),
    "overview_area": (str, "A:B"),
    "overview_item_index": (int, 0),
    "overview_quantity_index": (int, 1)
}


class ProjectPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.projects_lock = asyncio.Lock()
        self.wk_project_names = []  # type: [str]
        self.all_projects = []  # type: [Project]
        self.sheet = None  # type: SheetPlugin | None
        self.member_p = None  # type: MembersPlugin | None
        self.config = self.bot.create_sub_config("sheet.projects")
        self.config.load_tree(CONFIG_TREE)
        self.project_resources = []  # type: List[str]
        self.contract_cache = {}  # type: Dict[str, Dict[str, str]]

    async def find_projects(self):
        return await _project_tools.find_projects(self)

    async def load_projects(self):
        return await _project_tools.load_projects(self)

    async def insert_investments(self, player: str, investments: Dict[str, List[int]]):
        return await _project_tools.insert_investments(self, player, investments)

    async def split_overflow(self, project_resources: List[str], log=None):
        return await _project_tools.split_overflow(self, project_resources, log)

    async def apply_overflow_split(self,
                                   investments: Dict[str, Dict[str, List[int]]],
                                   changes: List[Tuple[Cell, int]]):
        return await _project_tools.apply_overflow_split(self, investments, changes)

    async def load_pending_resources(self):
        return await _project_tools.load_pending_resources(await self.sheet.get_sheet(), self.config)

    def on_load(self):
        self.sheet = self.bot.get_plugin("SheetMain")
        self.member_p = self.bot.get_plugin("MembersPlugin")
        self.register_cog(ProjectCommands(self))
        self.project_resources = self.bot.config["sheet.project_resources"]

    async def on_enable(self):
        await self.find_projects()
        await self.load_projects()

    async def get_status(self, short=False) -> Dict[str, str]:
        result = {
            "Projects": str(len(self.all_projects))
        }
        return result


def hash_contract(items: List[Item]) -> str:
    items_sorted = sorted(items, key=lambda i: i.name)
    string = "".join(f"{i.name}:{i.amount}" for i in items_sorted)
    return hashlib.sha1(string.encode(encoding="utf-8")).hexdigest()


async def _check_permissions(plugin, interaction):
    if not (
            plugin.bot.is_admin(interaction.user)
    ):
        await interaction.response.send_message("Missing permissions", ephemeral=True)
        return
    if not plugin.bot.is_online():
        raise BotOfflineException()


def button_admin_check(func: Callable):
    @functools.wraps(func)
    async def _wrapper(self, button, interaction: Interaction):
        await _check_permissions(self.plugin, interaction)
        return await func(self, button, interaction)

    return _wrapper


def modal_admin_check(func: Callable):
    @functools.wraps(func)
    async def _wrapper(self, interaction: Interaction):
        await _check_permissions(self.plugin, interaction)
        return await func(self, interaction)

    return _wrapper


class ProjectCommands(commands.Cog):
    def __init__(self, plugin: ProjectPlugin):
        self.plugin = plugin

    @commands.slash_command(name="loadprojects", description="Loads and list all projects")
    @cooldown(1, 5, commands.BucketType.default)
    @admin_only()
    @online_only()
    async def load_projects(self, ctx: discord.commands.context.ApplicationContext,
                            silent: Option(bool, "Execute command silently", required=False, default=True)):
        await ctx.response.defer(ephemeral=True)
        log = await self.plugin.load_projects()
        res = "Projectlist version: " + sheet_main.lastChanges.astimezone(pytz.timezone("Europe/Berlin")).strftime(
            "%d.%m.%Y %H:%M") + "\n"
        for p in self.plugin.all_projects:
            res += p.to_string() + "\n\n"

        await ctx.respond("Projektliste:", files=[
            string_to_file(list_to_string(log), "log.txt"),
            string_to_file(res, "project_list.txt")], ephemeral=silent)

    @commands.slash_command(name="listprojects", description="Lists all projects")
    @member_only()
    async def list_projects(self, ctx: ApplicationContext,
                            silent: Option(bool, "Execute command silently", required=False, default=True)):
        res = "Projectlist version: N/A\n"
        async with self.plugin.projects_lock:
            for p in self.plugin.all_projects:
                res += p.to_string() + "\n\n"
        await ctx.respond("Projektliste:", file=string_to_file(res, "project_list.txt"), ephemeral=silent)

    @commands.slash_command(name="insertinvestment", description="Saves an investment into the sheet")
    @option("skip_loading", description="Skip the reloading of the projects", required=False, default=False)
    @option("priority_project", required=False, default="",
            description="Prioritize this project, or multiple separated by a semicolon (;)")
    @commands.cooldown(1, 5, commands.BucketType.default)
    @admin_only()
    async def insert_investments(self,
                                 ctx: ApplicationContext,
                                 skip_loading: bool,
                                 priority_project: str
                                 ):
        if ";" in priority_project:
            priority_projects = priority_project.split(";")
        else:
            priority_projects = [priority_project]
        await ctx.response.send_modal(ListModal(self.plugin, skip_loading, priority_projects))

    @commands.slash_command(name="splitoverflow", description="Splits the overflow onto the projects")
    @commands.cooldown(1, 5, commands.BucketType.default)
    @admin_only()
    async def split_overflow(self, ctx: ApplicationContext):
        await ctx.defer()
        await self.plugin.load_projects()
        log = []
        investments, changes = await self.plugin.split_overflow(self.plugin.project_resources, log)
        msg_list = []
        for player, invests in investments.items():
            for proj, invest in invests.items():
                for index, amount in enumerate(invest):
                    if amount == 0:
                        continue
                    item = self.plugin.project_resources[index]
                    msg_list.append(f"{player}: {amount} {item} -> {proj}")
        await ctx.followup.send(f"Überlauf berechnet, soll {len(changes)} Änderung durchgeführt werden?",
                                file=string_to_file(list_to_string(msg_list), "split.txt"),
                                view=ConfirmOverflowView(self.plugin, investments, changes, log), ephemeral=False)


# noinspection PyUnusedLocal
class ConfirmView(AutoDisableView):
    def __init__(self, plugin: ProjectPlugin, investments: Dict[str, List[int]], player: str, log=None, split=None):
        super().__init__()
        if log is None:
            log = []
        self.log = log
        self.investments = investments
        self.player = player
        self.split = split
        self.plugin = plugin

    @discord.ui.button(label="Eintragen", style=discord.ButtonStyle.green)
    @button_admin_check
    async def btn_confirm_callback(self, button, interaction: Interaction):
        await interaction.response.defer(ephemeral=False, invisible=False)
        await interaction.message.edit(view=None)
        self.log.append("Inserting into sheet...")
        try:
            logger.info("Inserting investments for player %s: %s", self.player, self.investments)
            log, results = await self.plugin.insert_investments(self.player, self.investments)
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
        base_message = ("An **ERROR** occurred during execution of the command" if not success else
                        "Investition wurde eingetragen!")

        msg_files.append(utils.string_to_file(msg_list, "split.txt"))
        view = InformPlayerView(self.plugin, self.player, self.split, results, base_message)
        await view.load_user()
        view.message = await interaction.followup.send(
            base_message,
            files=msg_files,
            ephemeral=False, view=view)
        # To prevent pinging the user, the ping will be edited into the message instead
        await view.update_message()


class ConfirmOverflowView(AutoDisableView):
    def __init__(self, plugin: ProjectPlugin, investments: Dict[str, Dict[str, List[int]]], changes: [(Cell, int)],
                 log=None):
        super().__init__()
        if log is None:
            log = []
        self.log = log
        self.investments = investments
        self.changes = changes
        self.plugin = plugin

    @discord.ui.button(label="Eintragen", style=discord.ButtonStyle.green)
    @button_admin_check
    async def btn_confirm_callback(self, button, interaction: Interaction):
        await interaction.response.defer(invisible=False)
        await interaction.message.edit(view=None)
        log = await self.plugin.apply_overflow_split(self.investments, self.changes)
        await interaction.followup.send("Überlauf wurde auf die Projekte verteilt!",
                                        file=string_to_file(list_to_string(log)))


# noinspection PyUnusedLocal
class InformPlayerView(AutoDisableView):
    def __init__(self, plugin: ProjectPlugin, user: str, split: {str: [(str, int)]}, results: {str, bool},
                 base_message):
        super().__init__()
        self.base_message = base_message
        self.results = results
        self.split = split
        self.plugin = plugin
        self.user = user
        self.discord_id, _, _ = self.plugin.member_p.get_discord_id(user)

    async def load_user(self):
        if self.discord_id is None:
            main_char, _, _ = self.plugin.member_p.find_main_name(name=self.user)
            if main_char is None:
                main_char = self.user
            discord_id = self.plugin.member_p.get_discord_id(main_char, only_id=True)
            if discord_id is not None:
                self.discord_id = discord_id

    async def update_message(self):
        await self.message.edit(self.base_message + f"\n\nSoll der Nutzer <@{self.discord_id}> benachrichtigt werden?")

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    @button_admin_check
    async def btn_send_callback(self, button, interaction: Interaction):
        if self.discord_id is None:
            await interaction.response.send_message("Kein Nutzer gefunden...", ephemeral=True)
            return
        user = await self.plugin.bot.get_or_fetch_user(self.discord_id)
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
        # utils.save_discord_id(self.user, self.discord_id)
        await interaction.message.edit(view=None)

    @discord.ui.button(label="Ändern", style=discord.ButtonStyle.blurple)
    @button_admin_check
    async def btn_change_callback(self, button, interaction: Interaction):
        await interaction.response.send_modal(InformPlayerView.DiscordUserModal(self))

    class DiscordUserModal(ErrorHandledModal):
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
            matched_name, _, _ = self.view.plugin.member_p.find_main_name(name)

            if matched_name is not None:
                matched_name = self.view.plugin.sheet.check_name_overwrites(matched_name)
                # utils.save_discord_id(matched_name, int(discord_id))
                await interaction.response.send_message(
                    f"Spieler {matched_name} wurde zur ID {discord_id} eingespeichert!\n",
                    ephemeral=True)
                self.view.discord_id = discord_id
                await self.view.update_message()
            else:
                await interaction.response.send_message(f"Fehler, Spieler {name} nicht gefunden!", ephemeral=True)


class ListModal(ErrorHandledModal):
    def __init__(self,
                 plugin: ProjectPlugin,
                 skip_loading: bool,
                 priority_projects: [str],
                 *args, **kwargs):
        super().__init__(title="Ingame List Parser", *args, **kwargs)
        self.skip_loading = skip_loading
        self.plugin = plugin
        self.priority_projects = []
        for p_name in priority_projects:
            found = False
            for project in self.plugin.all_projects:  # type: Project
                if project.name.casefold() == p_name.casefold():
                    self.priority_projects.append(project.name)
                    found = True
                    break
            if found:
                continue
            best_ratio = 0.75
            best_project = None
            for project in self.plugin.all_projects:  # type: Project
                ratio = SequenceMatcher(None, project.name, p_name).ratio()
                if ratio > best_ratio:
                    if project.name in self.priority_projects:
                        continue
                    best_project = project
                    best_ratio = ratio
            if best_project is None:
                continue
            self.priority_projects.append(best_project.name)
        self.add_item(InputText(label="Spielername", placeholder="Spielername", required=True))
        self.add_item(InputText(label="Ingame List", placeholder="Ingame liste hier einfügen",
                                required=True, style=InputTextStyle.long))

    @modal_admin_check
    async def callback(self, interaction: Interaction):
        logger.debug("Insert Investments command received")
        await interaction.response.send_message("Bitte warten, dies kann einige Sekunden dauern.", ephemeral=True)
        log = []
        player, _, is_perfect = self.plugin.member_p.find_main_name(self.children[0].value)
        player = self.plugin.sheet.check_name_overwrites(player)
        if player is None:
            await interaction.followup.send(f"Fehler: Spieler \"{self.children[0].value}\" nicht gefunden!")
            return
        log.append("Parsing list...")
        items = Item.parse_ingame_list(self.children[1].value)
        if not self.skip_loading:
            await interaction.followup.send(
                "Eingabe verarbeitet, lade Projekte. Bitte warten, dies dauert nun einige Sekunden", ephemeral=True)
            log.append("Reloading projects...")
            await self.plugin.load_projects()
        log.append("Splitting contract...")
        async with self.plugin.projects_lock:
            logger.debug("Splitting contract for %s ", player)
            split = Project.split_contract(items,
                                           project_list=self.plugin.all_projects,
                                           project_resources=self.plugin.project_resources,
                                           priority_projects=self.priority_projects)
        log.append("Calculating investments...")
        investments = Project.calc_investments(split, self.plugin.project_resources)
        message = ""
        if not is_perfect:
            message = f"Meintest du \"{player}\"? (Deine Eingabe war \"{self.children[0].value}\").\n"
        msg_list = project_utils.format_list(split, [])
        if len(self.priority_projects) == 0:
            p_priority = None
        else:
            p_priority = " > ".join(self.priority_projects)
        message += f"Eingelesene items: \n```\n{Item.to_string(items)}\n```\n" \
                   f"Projektpriorität: `{p_priority}`\n" \
                   f"Willst du diese Liste als Investition für `{player}` eintragen?\n" \
                   f"Sheet: `{self.plugin.sheet.sheet_name}`"
        msg_file = [utils.string_to_file(msg_list, "split.txt")]
        items_hash = hash_contract(items)
        embed = None
        if items_hash in self.plugin.contract_cache:
            old_time = self.plugin.contract_cache[items_hash]["time"]
            old_player = self.plugin.contract_cache[items_hash]["player"]
            embed = Embed(colour=Colour.red(), title="Doppelter Vertrag:exclamation:",
                          description="Dieser Vertrag wurde bereits verarbeitet (was aber nicht heißt dass dieser auch "
                                      "eingetragen wurde).\n"
                                      f"Zeit: <t:{old_time}:f> <t:{old_time}:R>\nSpieler: `{old_player}`")
        self.plugin.contract_cache[items_hash] = {
            "player": player,
            "time": int(time.time())
        }
        await interaction.followup.send(
            message,
            view=ConfirmView(self.plugin, investments, player, log, split),
            files=msg_file,
            embed=embed,
            ephemeral=False)
