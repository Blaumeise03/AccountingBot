# PluginConfig
# Name: SheetMain
# Author: Blaumeise03
# Depends-On: [accounting_bot.ext.members]
# End
import datetime
import functools
import json
import logging
from os.path import exists
from typing import Dict, List

import gspread_asyncio
from discord.ext import commands
from google.oauth2.service_account import Credentials
from gspread.utils import ValueRenderOption

from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.ext.members import Player, MembersPlugin
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper

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
}


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

    async def get_sheet(self):
        agc = await self.agcm.authorize()
        sheet = await agc.open_by_key(self.sheet_id)
        if self.sheet_name is None:
            self.sheet_name = sheet.title
        return sheet

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

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Connect to API
        logger.info("Loading google sheet")
        agc = await self.agcm.authorize()
        if self.plugin.sheet_id is None:
            logger.warning("Sheet id not specified")
            return
        sheet = await agc.open_by_key(self.plugin.sheet_id)
        self.plugin.sheet_name = sheet.title
        logger.info("Sheet connected")


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
    alt_chars = {}  # type: Dict[str, str]

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
            players[k] = pseudo_user
            count += 1
    logger.info("Loaded %s pseudo-users from overwrites config", count)
    return players
