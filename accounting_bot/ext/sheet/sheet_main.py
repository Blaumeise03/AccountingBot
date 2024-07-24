# PluginConfig
# Name: SheetMain
# Author: Blaumeise03
# Depends-On: [accounting_bot.ext.members]
# End
import datetime
import enum
import functools
import json
import logging
from enum import Enum
from os.path import exists
from typing import Dict, List, Union, Iterable, Optional

import gspread
import gspread_asyncio
from discord import ApplicationContext, option
from discord.ext import commands
from google.oauth2.service_account import Credentials
from gspread.utils import ValueRenderOption, ValueInputOption

from accounting_bot import utils
from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.ext.members import Player, MembersPlugin
from accounting_bot.ext.sheet import sheet_utils
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import admin_only, owner_only
from accounting_bot.utils.ui import AwaitConfirmView

logger = logging.getLogger("ext.sheet")
logger.setLevel(logging.DEBUG)

# Google Sheets API settings
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
SHEET_LOG_NAME = "Accounting Log"
SHEET_MARKET_NAME = "Ressourcenbedarf Projekte"

USER_OVERWRITES_FILE = "user_overwrites.json"

lastChanges = datetime.datetime(1970, 1, 1)

MEMBERS_WALLET_INDEX = 2  # The column index of the balance
MEMBERS_INVESTMENTS_INDEX = 3  # The column index of the investments
MEMBERS_AREA_LITE = "A4:D"  # The reduced area of the member list
MARKET_PRICE_INDEXES = [6, 7, 9]  # The columns containing market prices
MARKET_ITEM_INDEX = 0  # The column containing the item names
MARKET_AREA = "A:J"  # The total area

CONFIG_TREE = {
    "sheet_id": (str, "N/A"),
    "project_resources": (list, [],),
    "log_level": (str, "INFO")
}

CONFIG_TREE_MEMBERS = {
    "sheet_name": (str, "N/A"),
    "path_discord_ids": (str, "discord_ids.json"),
    "members_area": (str, "A4:P"),
    "members_active_index": (int, 10),
    "members_name_index": (int, 0),
    "members_rank_index": (int, 8),
    "members_note_index": (int, 15),
    "members_note_alt_prefix": (str, "Twink von "),
    "members_rank_abstract": (str, "Abstract User")
}


class UserRole(enum.StrEnum):
    writer = "write"
    reader = "reader"
    owner = "owner"
    commenter = "commenter"
    organizer = "organizer"
    fileOrganizer = "fileOrganizer"


class UserType(enum.StrEnum):
    user = "user"
    group = "group"
    domain = "domain"
    anyone = "anyone"


class SheetPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.name_overwrites = {}
        self.config = self.bot.create_sub_config("sheet")
        self.config.load_tree(CONFIG_TREE)
        self.member_config = self.config.create_sub_config("members")
        self.member_config.load_tree(CONFIG_TREE_MEMBERS)
        self.sheet_id = None
        self.sheet_name = None
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

    def on_load(self):
        self.register_cog(SheetCog(self))
        if exists(USER_OVERWRITES_FILE):
            with open(USER_OVERWRITES_FILE) as json_file:
                self.name_overwrites = json.load(json_file)
            logger.info("User overwrite config loaded")
        else:
            config = {}
            with open(USER_OVERWRITES_FILE) as outfile:
                json.dump(config, outfile, indent=4)
                logger.warning("User overwrite config not found, created new one")
        logger.setLevel(self.config["log_level"])
        self.sheet_id = self.config["sheet_id"]
        if self.sheet_id == "N/A":
            self.sheet_id = None
        members_plugin = self.bot.get_plugin("MembersPlugin")  # type: MembersPlugin
        (
            members_plugin
            .set_data_source()
            .map_data(functools.partial(load_usernames, plugin=self))
            .map_data(load_user_overwrites)
            .map_data(functools.partial(load_discord_ids, path=self.member_config["path_discord_ids"]))
        )
        (
            members_plugin
            .set_save_data_chain()
            .map_data(functools.partial(save_discord_ids, path=self.member_config["path_discord_ids"]))
        )

    async def on_enable(self):
        return await super().on_enable()

    async def get_sheet(self) -> gspread_asyncio.AsyncioGspreadSpreadsheet:
        agc = await self.agcm.authorize()
        sheet = await agc.open_by_key(self.sheet_id)
        if self.sheet_name is None:
            self.sheet_name = sheet.title
        return sheet

    async def load_permissions(self) -> List[Dict[str, Union[str, UserType, UserRole]]]:
        agc = await self.agcm.authorize()
        perms = await agc.list_permissions(self.sheet_id)
        users = []
        for perm in perms:
            if perm["kind"] != "drive#permission":
                continue
            users.append({
                "type": UserType[perm["type"]],
                "email": perm["emailAddress"],
                "role": UserRole[perm["role"]]
            })
        return users

    def check_name_overwrites(self, name: str) -> str:
        """
        Replaces a username with its defined overwrite (or returns the name itself if none is defined).

        :param name: The name to replace.
        :return: The defined overwrite or name.
        """
        overwrite = self.name_overwrites.get(name, None)
        if overwrite is not None:
            name = overwrite
        return name

    async def get_market_data(self):
        logger.info("Loading market data")
        agc = await self.agcm.authorize()
        sheet = await agc.open_by_key(self.sheet_id)
        wk_market = await sheet.worksheet(SHEET_MARKET_NAME)
        data = await wk_market.get_values(MARKET_AREA, value_render_option=ValueRenderOption.unformatted)
        prices = {}
        row_i = -1
        price_names = {}
        for row in data:
            row_i += 1
            if row_i == 0:
                for col in MARKET_PRICE_INDEXES:
                    if len(row) < col:
                        raise GoogleSheetException(f"Header row of {SHEET_MARKET_NAME} is to small")
                    price_names[row[col]] = col
                continue
            item = row[MARKET_ITEM_INDEX]
            item_prices = {}
            prices[item] = item_prices
            for p_name, col in price_names.items():
                if len(row) < col:
                    continue
                value = row[col]
                if type(value) == int or type(value) == float:
                    item_prices[p_name] = value
                elif value != "":
                    logger.warning("Market price '%s':%s for item '%s' in sheet '%s' is not a number: '%s'",
                                   p_name, col, item, SHEET_MARKET_NAME, value)
        logger.info("Market data loaded")
        return prices


def get_creds() -> Credentials:
    creds = Credentials.from_service_account_file("credentials.json")
    scoped = creds.with_scopes(SCOPES)
    return scoped


class SheetCog(commands.Cog):
    def __init__(self, plugin: SheetPlugin):
        self.plugin = plugin

    @commands.slash_command(name="export_players", description="Saves the discord ids into a google sheet")
    @option(name="sheet", type=str, description="The target sheet name")
    @option(name="index_name", type=str, description="The column for char names (0-indexed or letter)")
    @option(name="index_main", type=str, description="The column for main char names (0-indexed or letter)")
    @option(name="index_id", type=str, description="The column for discord ids (0-indexed or letter)")
    @admin_only()
    async def cmd_export_player(self,
                                ctx: ApplicationContext,
                                sheet: str,
                                index_name: str,
                                index_main: str,
                                index_id: str):
        def _get_index(string: str) -> int:
            if string.strip().isnumeric():
                return int(string.strip())
            else:
                return gspread.utils.a1_to_rowcol(string.strip() + "1")[1] - 1

        index_id = _get_index(index_id)
        index_main = _get_index(index_main)
        index_name = _get_index(index_name)
        confirm = await (AwaitConfirmView(defer_response=False)
                         .send_view(ctx.response,
                                    message=f"Please confirm the operation:\nSheet: `{sheet}`\nIndizes (0-based):\n"
                                            f"Char Name: `{index_name}`\nMain Name: `{index_main}`\n"
                                            f"Discord ID Index: `{index_id}`\n\nDo you want to update the data?"))
        if not confirm.confirmed:
            if confirm.interaction:
                await confirm.interaction.response.send_message("Aborted", ephemeral=True)
            return
        members_plugin = self.plugin.bot.get_plugin("MembersPlugin")  # type: MembersPlugin
        logger.info("User %s:%s has started the player export into sheet %s (c %s, m %s, i %s)",
                    ctx.user.name, ctx.user.id, sheet, index_name, index_main, index_id)
        await confirm.interaction.response.defer(ephemeral=True)
        await save_players_to_sheet(
            players=members_plugin.players,
            sheet=await self.plugin.get_sheet(),
            wk_name=sheet, wk_i_id=index_id, wk_i_main=index_main, wk_i_char=index_name)
        await confirm.interaction.followup.send(f"Exported players to sheet {sheet}", ephemeral=True)

    @commands.slash_command(name="sheet_perms", description="Command to handle sheet permissions")
    @owner_only()
    async def cmd_get_perms(self, ctx: ApplicationContext):
        perms = await self.plugin.load_permissions()
        msg = ""
        for perm in sorted(perms, key=lambda p: p["email"]):
            msg += perm["role"] + ", " + perm["email"] + "\n"
        if len(msg) < 2000:
            await ctx.respond(msg, ephemeral=True)
        else:
            await ctx.respond("Alle Nutzer des Sheets:",
                              file=utils.string_to_file(msg, "permissions.csv"))


async def load_usernames(players: Dict[str, Player], plugin: SheetPlugin) -> Dict[str, Player]:
    logger.info("Loading usernames from sheet")
    sheet = await plugin.get_sheet()
    config = plugin.member_config
    # Load usernames
    wk_accounting = await sheet.worksheet(config["sheet_name"])
    user_raw = await wk_accounting.get_values(config["members_area"],
                                              value_render_option=ValueRenderOption.unformatted)
    inactive_players = []
    i_member_active = config["members_active_index"]
    i_member_name = config["members_name_index"]
    i_member_rank = config["members_rank_index"]
    i_member_note = config["members_note_index"]
    member_note_alt_prefix = config["members_note_alt_prefix"]
    abstract_rank = config["members_rank_abstract"]
    alt_chars = {}  # type: Dict[str, str]
    abstract_users = set()

    for row in user_raw:
        user = None
        # Check if main account
        if len(row) > i_member_active and row[i_member_active]:
            user = Player(name=row[i_member_name].strip())
            players[user.name] = user
        # Check if in the corp (and therefore has a rank)
        if len(row) > i_member_rank and len(row[i_member_rank].strip()) > 0:
            if user:
                user.rank = row[i_member_rank].strip()
            else:
                inactive_players.append(row[i_member_name].strip())
            if user and user.rank == abstract_rank:
                abstract_users.add(user)
                user.is_abstract = True
                continue
            # Check if it is an alt of a main account
            if len(row) > i_member_note and not row[i_member_active]:
                note = row[i_member_note]  # type: str
                if note.startswith(member_note_alt_prefix):
                    note = note.replace(member_note_alt_prefix, "").strip()
                    alt_chars[row[i_member_name].strip()] = note
    for alt, main in alt_chars.items():
        if main not in players:
            continue
        player = players[main]
        player.alts.append(alt)
        players[alt] = player
        if main in inactive_players and alt not in inactive_players:
            inactive_players.remove(main)

    logger.info("Loaded %s chars", len(players))
    players = dict(filter(lambda t: t[0] not in inactive_players, players.items()))
    logger.info("Found %s active chars", len(players))
    return players


def load_discord_ids(players: Dict[str, Player], path: str):
    logger.info("Loading discord ids")
    if exists(path):
        with open(path, "r", encoding="utf-8") as file:
            raw = json.load(file)
    else:
        raw = {
            "owners": {},
            "granted_permissions": {}
        }
        with open(path) as outfile:
            json.dump(raw, outfile, indent=4)
            logger.warning("Discord id list not found, created new one")
    if "owners" in raw:
        owner_ids = raw["owners"]
    else:
        owner_ids = {}
    if "granted_permissions" in raw:
        perms_ids = raw["granted_permissions"]
    else:
        perms_ids = {}
    for player in players.values():
        if player.name in owner_ids:
            player.discord_id = owner_ids[player.name]
        if player.name in perms_ids:
            player.authorized_discord_ids.extend(perms_ids[player.name])
    logger.info("Loaded discord ids")
    return players


def save_discord_ids(players: List[Player], path: str):
    raw = {
        "owners": {}, "granted_permissions": {}
    }
    for player in players:
        if player.discord_id is not None:
            raw["owners"][player.name] = player.discord_id
        if len(player.authorized_discord_ids) > 0:
            raw["granted_permissions"][player.name] = player.authorized_discord_ids
    with open(path, "w", encoding="utf-8") as file:
        json.dump(raw, file, ensure_ascii=False, indent=4)


async def save_players_to_sheet(
        players: Iterable[Player],
        sheet: gspread_asyncio.AsyncioGspreadSpreadsheet,
        wk_name: str, wk_i_char: int, wk_i_main: int, wk_i_id: int):
    """
    All indizes have to be 0-indexed
    :param players:
    :param sheet:
    :param wk_name:
    :param wk_i_char:
    :param wk_i_main:
    :param wk_i_id:
    :return:
    """
    logger.info("Preparing update of player sheet %s for %s players", wk_name, len(players))
    wk = await sheet.worksheet(wk_name)
    data = await wk.get_values(value_render_option=ValueRenderOption.unformatted)

    def _find_player_row(_name: str):
        for i, row in enumerate(data):
            if len(row) < wk_i_char:
                continue
            if row[wk_i_char] == _name:
                return i, row
        return None, None

    batch_change = []
    new_data = []

    def _insert_update_char(_name: str, _main: str, _id: Optional[int] = None):
        if _id is None:
            return
        r, d = _find_player_row(_name)
        if r is None:
            new = [None] * (max(wk_i_char, wk_i_main, wk_i_id) + 1)  # type: List[Union[None, int, str]]
            new[wk_i_id] = str(_id)
            new[wk_i_main] = _main
            new[wk_i_char] = _name
            new_data.append(new)
        else:
            if str(d[wk_i_main]) != str(_main):
                batch_change.append({
                    "range": gspread.utils.rowcol_to_a1(r + 1, wk_i_main + 1),
                    "values": [[_main]]
                })
            if str(d[wk_i_id]) != str(_id):
                batch_change.append({
                    "range": gspread.utils.rowcol_to_a1(r + 1, wk_i_id + 1),
                    "values": [[str(_id)]]
                })

    for player in players:
        _insert_update_char(player.name, player.name, player.discord_id)
        for char in player.alts:
            _insert_update_char(char, player.name, player.discord_id)
    if len(new_data) != 0:
        logger.info("Inserting %s new rows into worksheet", len(new_data))
        await wk.append_rows(new_data, value_input_option=ValueInputOption.user_entered)
    else:
        logger.info("No new data")
    if len(batch_change) != 0:
        logger.info("Updating %s existing cells", len(batch_change))
        await wk.batch_update(batch_change, value_input_option=ValueInputOption.user_entered)
    else:
        logger.info("No updates required")
    logger.info("Updated user data in sheet %s", wk_name)


def load_user_overwrites(players: Dict[str, Player]):
    logger.info("Loading user overwrites")
    count = 0
    if exists(USER_OVERWRITES_FILE):
        with open(USER_OVERWRITES_FILE, "r") as file:
            raw = json.load(file)
        for k, v in raw.items():
            if k in players or v is not None:
                continue
            pseudo_user = Player(name=k)
            pseudo_user.is_abstract = True
            players[k] = pseudo_user
            count += 1
    logger.info("Loaded %s pseudo-users from overwrites config", count)
    return players
