# PluginConfig
# Name: SheetMain
# Author: Blaumeise03
# End
import datetime
import json
import logging
from os.path import exists

import gspread_asyncio
from discord.ext import commands
from google.oauth2.service_account import Credentials
from gspread.utils import ValueRenderOption

from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper

logger = logging.getLogger("bot.sheet")
logger.setLevel(logging.DEBUG)

# Google Sheets API settings
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
SHEET_LOG_NAME = "Accounting Log"
SHEET_MARKET_NAME = "Ressourcenbedarf Projekte"

lastChanges = datetime.datetime(1970, 1, 1)

MEMBERS_WALLET_INDEX = 2  # The column index of the balance
MEMBERS_INVESTMENTS_INDEX = 3  # The column index of the investments
MEMBERS_AREA_LITE = "A4:D"  # The reduced area of the member list
MEMBERS_AREA = "A4:O"  # The area of the member list
MEMBERS_NAME_INDEX = 0  # The column index of the name
MEMBERS_ACTIVE_INDEX = 10  # The column index of the "active" column
MEMBERS_RANK_INDEX = 8  # The column index of the "rank" column
MEMBERS_NOTE_INDEX = 14  # The column containing notes for users

MARKET_PRICE_INDEXES = [6, 7, 9]  # The columns containing market prices
MARKET_ITEM_INDEX = 0  # The column containing the item names
MARKET_AREA = "A:J"  # The total area

CONFIG_TREE = {
    "sheet_id": (str, "N/A"),
    "project_resources": (list, [],),
    "log_level": (str, "INFO")
}


class SheetPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.name_overwrites = {}
        self.config = self.bot.create_sub_config("sheet")
        self.config.load_tree(CONFIG_TREE)
        self.sheet_id = None
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)

    def on_load(self):
        if exists("user_overwrites.json"):
            with open("user_overwrites.json") as json_file:
                self.name_overwrites = json.load(json_file)
            logger.info("User overwrite config loaded")
        else:
            config = {}
            with open("user_overwrites.json", "w") as outfile:
                json.dump(config, outfile, indent=4)
                logger.warning("User overwrite config not found, created new one")
        logger.setLevel(self.config["log_level"])
        self.sheet_id = self.config["sheet_id"]
        if self.sheet_id == "N/A":
            self.sheet_id = None

    async def on_enable(self):
        return await super().on_enable()

    async def get_sheet(self):
        agc = await self.agcm.authorize()
        return await agc.open_by_key(self.sheet_id)

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

