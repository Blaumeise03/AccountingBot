import asyncio
import datetime
import json
import logging
import re
import time
from operator import add
from os.path import exists
from typing import Union, Tuple, Dict, List, Optional, TYPE_CHECKING

import gspread_asyncio
import pytz
from google.oauth2.service_account import Credentials
from gspread import GSpreadException, Cell
from gspread.utils import ValueRenderOption, ValueInputOption

from accounting_bot import projects, utils
from accounting_bot.exceptions import GoogleSheetException, BotOfflineException
from accounting_bot.project_utils import find_player_row, calculate_changes, verify_batch_data, process_first_column
from accounting_bot.projects import Project
from accounting_bot.utils import State, Item

if TYPE_CHECKING:
    from bot import BotState
    from accounting_bot.accounting import Transaction

logger = logging.getLogger("bot.sheet")
logger.setLevel(logging.DEBUG)

STATE = None  # type: BotState | None

# Google Sheets API settings
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
SPREADSHEET_ID = ""
SHEET_LOG_NAME = "Accounting Log"
SHEET_ACCOUNTING_NAME = "Accounting"
SHEET_OVERFLOW_NAME = "Projektüberlauf"
SHEET_MARKET_NAME = "Ressourcenbedarf Projekte"
sheet_name = "N/A"

# Projekt worksheet names
wkProjectNames = []  # type: [str]
users = []
overwrites = {}
allProjects = []  # type: [Project]
lastChanges = datetime.datetime(1970, 1, 1)

wallets = {}  # type: {str: int}
investments = {}  # type: {str: int}
wallets_last_reload = 0
MEMBERS_WALLET_INDEX = 2  # The column index of the balance
MEMBERS_INVESTMENTS_INDEX = 3  # The column index of the investments
MEMBERS_AREA_LITE = "A4:D"  # The reduced area of the member list
MEMBERS_AREA = "A4:O"  # The area of the member list
MEMBERS_NAME_INDEX = 0  # The column index of the name
MEMBERS_ACTIVE_INDEX = 10  # The column index of the "active" column
MEMBERS_RANK_INDEX = 8  # The column index of the "rank" column
MEMBERS_NOTE_INDEX = 14  # The column containing notes for users

MARKET_PRICE_INDEXES = [6, 7, 9]
MARKET_ITEM_INDEX = 0
MARKET_AREA = "A:J"

# All resources that exist, will be used to verify the integrity of the received data
PROJECT_RESOURCES = []

projects_lock = asyncio.Lock()
wallet_lock = asyncio.Lock()


def load_config() -> None:
    """
    Loads the user overwrites config if it exits, or it creates one.
    """
    global overwrites
    if exists("user_overwrites.json"):
        with open("user_overwrites.json") as json_file:
            overwrites = json.load(json_file)
        logger.info("User overwrite config loaded.")
    else:
        config = {}
        with open("user_overwrites.json", "w") as outfile:
            json.dump(config, outfile, indent=4)
            logger.warning("User overwrite config not found, created new one.")


def get_creds() -> Credentials:
    creds = Credentials.from_service_account_file("credentials.json")
    scoped = creds.with_scopes(SCOPES)
    return scoped


agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)


async def setup_sheet(sheet_id: str, project_resources: [str], log_level) -> None:
    """
    Set-ups the Google Sheet API.

    :param sheet_id: the id of the Google Sheet
    :param project_resources: the array of the names of the available project resources
    :param log_level: the loglevel for the logger
    """
    global SPREADSHEET_ID, PROJECT_RESOURCES, users, sheet_name
    logger.setLevel(log_level)
    SPREADSHEET_ID = sheet_id
    PROJECT_RESOURCES = project_resources
    # Connect to API
    logger.info("Loading google sheet...")
    agc = await agcm.authorize()
    load_config()
    sheet = await agc.open_by_key(sheet_id)
    sheet_name = sheet.title
    logger.info("Loading usernames from sheet")
    # Load usernames
    wk_accounting = await sheet.worksheet("Accounting")
    user_raw = await wk_accounting.get_values(MEMBERS_AREA, value_render_option=ValueRenderOption.unformatted)
    users.clear()
    utils.ingame_twinks.clear()
    utils.ingame_chars.clear()
    utils.main_chars.clear()

    for u in user_raw:
        # Check if main account
        if len(u) > MEMBERS_ACTIVE_INDEX and u[MEMBERS_ACTIVE_INDEX]:
            users.append(u[MEMBERS_NAME_INDEX])
            utils.main_chars.append(u[MEMBERS_NAME_INDEX])

        # Check if in the corp (and therefore has a rank)
        if len(u) > MEMBERS_RANK_INDEX and len(u[MEMBERS_RANK_INDEX].strip()) > 0:
            utils.ingame_chars.append(u[MEMBERS_NAME_INDEX])

            # Check if twink of a main account
            if len(u) > MEMBERS_NOTE_INDEX and not u[MEMBERS_ACTIVE_INDEX]:
                note = u[MEMBERS_NOTE_INDEX]  # type: str
                if note.startswith("Twink von "):
                    note = note.replace("Twink von ", "").strip()
                    utils.ingame_twinks[u[MEMBERS_NAME_INDEX]] = note

    for u in overwrites.keys():
        u_2 = overwrites.get(u)
        if u_2 is None:
            users.append(u)
        else:
            users.append(u_2)
    logger.info("Loaded %s main chars, %s active chars and %s twinks.",
                len(utils.main_chars),
                len(utils.ingame_chars),
                len(utils.ingame_twinks))


def check_name_overwrites(name: str) -> str:
    """
    Replaces a username with its defined overwrite (or returns the name itself if none is defined).

    :param name: the name to replace.
    :return: the defined overwrite or name.
    """
    overwrite = overwrites.get(name, None)
    if overwrite is not None:
        name = overwrite
    return name


async def add_transaction(transaction: 'Transaction') -> None:
    """
    Saves a transaction into the Accounting sheet. The usernames will be replaced with their defined overwrite (if any),
    see `sheet.check_name_overwrites`.

    :param transaction: the transaction to save
    """
    if transaction is None:
        return
    # Get data from transaction
    user_f = transaction.name_from if transaction.name_from is not None else ""
    user_t = transaction.name_to if transaction.name_to is not None else ""
    transaction_time = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    amount = transaction.amount
    purpose = transaction.purpose if transaction.purpose is not None else ""
    reference = transaction.reference if transaction.reference is not None else ""

    # Applying custom username overwrites
    user_f = check_name_overwrites(user_f)
    user_t = check_name_overwrites(user_t)

    # Saving the data
    logger.info(f"Saving row [{transaction_time}; {user_f}; {user_t}; {amount}; {purpose}; {reference}]")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    wk_log = await sheet.worksheet("Accounting Log")
    await wk_log.append_row([transaction_time, user_f, user_t, amount, purpose, reference],
                            value_input_option=ValueInputOption.user_entered)
    logger.debug("Saved row")


async def load_wallets(force=False, validate=False):
    global wallets, investments, wallets_last_reload
    t = time.time()
    if (t - wallets_last_reload) < 60 * 60 * 5 and not force:
        return
    async with wallet_lock:
        wallets_last_reload = t
        wallets.clear()
        agc = await agcm.authorize()
        sheet = await agc.open_by_key(SPREADSHEET_ID)
        wk_accounting = await sheet.worksheet("Accounting")
        user_raw = await wk_accounting.get_values(MEMBERS_AREA_LITE, value_render_option=ValueRenderOption.unformatted)
        for u in user_raw:
            if len(u) >= 3:
                bal = u[MEMBERS_WALLET_INDEX]
                if type(bal) == int or type(bal) == float:
                    if validate and type(bal) == float:
                        logger.warning("Balance for %s is a float: %s", u[MEMBERS_NAME_INDEX], bal)
                    wallets[u[MEMBERS_NAME_INDEX]] = int(bal)
            if len(u) >= 4:
                inv = u[MEMBERS_INVESTMENTS_INDEX]
                if type(inv) == int or type(inv) == float:
                    if validate and type(inv) == float:
                        # logger.warning("Investment sum for %s is a float: %s", u[MEMBERS_NAME_INDEX], inv)
                        pass
                    investments[u[MEMBERS_NAME_INDEX]] = int(inv)


async def get_balance(name: str, default: Optional[int] = None) -> int:
    async with wallet_lock:
        if name in wallets:
            return wallets[name]
        return default


async def get_investments(name: str, default: Optional[int] = None) -> int:
    async with wallet_lock:
        if name in investments:
            return investments[name]
        return default


async def get_market_data():
    logger.info("Loading market data")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
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
            elif not value == "":
                logger.warning("Market price '%s':%s for item '%s' in sheet '%s' is not a number: '%s'",
                               p_name, col, item, SHEET_MARKET_NAME, value)
    return prices


async def find_projects():
    """
    Reloads the list of available projects and loads them into the cache.
    """
    logger.debug("Reloading projects...")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    wk_projects = []
    wkProjectNames.clear()
    for s in await sheet.worksheets():
        if s.title.startswith("Project"):
            wk_projects.append(s)
            wkProjectNames.append(s.title)
    names = ""
    for n in wkProjectNames:
        names += n
        if wkProjectNames.index(n) < len(wkProjectNames) - 1:
            names += ", "
    logger.info(f"Found {len(wkProjectNames)} project sheets: " + names)
    await load_projects()


async def load_projects() -> [str]:
    """
    Reloads the projects, parallel execution of the function is prevented, instead all other calls will be delayed until
    the first call is finished.

    :return: the reloading log.
    """
    global lastChanges
    # Prevent parallel execution of function, instead all other calls will be delayed until the first call is finished
    # In that case, the function won't reload the projects, as they were just reloaded recently
    if projects_lock.locked():
        logger.debug("load_projects is locked, waiting for it to complete...")
        while projects_lock.locked():  # and (time - loadProject_time).total_seconds() / 60.0 < 2:
            await asyncio.sleep(5)
        return [
            "Parallel command call discovered! Method evaluation was canceled. The received data may be deprecated."]

    async with projects_lock:
        log = []
        agc = await agcm.authorize()
        sheet = await agc.open_by_key(SPREADSHEET_ID)
        lastChanges = datetime.datetime.strptime(sheet.ss.lastUpdateTime, "%Y-%m-%dT%H:%M:%S.%fZ")
        # Clear all current project
        allProjects.clear()

        for project_name in wkProjectNames:
            # Load projects
            await load_project(project_name, log, sheet)

    logger.debug("Projects loaded")
    return log


async def load_project(project_name: str, log: [str], sheet: gspread_asyncio.AsyncioGspreadSpreadsheet) -> None:
    """
    Loads a specific project from the Google sheet. It will load all pending resources and save them into the cache. It
    will take a given log and extend it.

    :param project_name: the name of the project sheet
    :param log: the current reloading log
    :param sheet: the current AsyncioGspreadSpreadsheet instance
    """
    if not STATE.is_online():
        raise BotOfflineException()
    logger.info(f"Loading project {project_name}")
    log.append(f"Starting processing of project sheet \"{project_name}\"")
    s = await sheet.worksheet(project_name)

    # Scanning project sheet to find the locations of the required entries
    batch_cells = await s.findall(re.compile(r"(ausstehende Ressourcenkosten)|(Investitionen)|(Auszahlung)"))

    pending_cell, investments_cell, payout_cell = process_first_column(batch_cells, log)

    if pending_cell is None:
        log.append(f"  ERROR: Project sheet {project_name} is malformed")
        logger.warning(f"Project sheet {project_name} is malformed")
        return

    project = projects.Project(project_name)
    allProjects.append(project)

    log.append("  Performing batch request...")
    logger.debug("Batch requesting data for project \"%s\" (res.: %s, invest.: %s, payout: %s)",
                 project_name,
                 pending_cell.address,
                 investments_cell.address if investments_cell is not None else "N/A",
                 payout_cell.address if payout_cell is not None else "N/A")
    invest_cell_row = investments_cell.row if investments_cell is not None else 1
    payout_cell_row = payout_cell.row if payout_cell is not None else 2

    # Batch requesting all data
    batch_data = await s.batch_get(
        ["H2:2",  # Item names row
         f"H{pending_cell.row}:{pending_cell.row}",  # Item quantities row (pending resources)
         "A1",  # Project settings (exclude or not)
         f"A{invest_cell_row}:A{payout_cell_row - 1}"  # Investment area
         ],
        value_render_option=ValueRenderOption.unformatted)
    log.append(f"  Received {len(batch_data)} results!")

    # Verify result length
    try:
        verify_batch_data(batch_data, log)
    except GoogleSheetException as e:
        raise GoogleSheetException(log, f"Could not verify batch data for {project_name}!") from e

    items_names = batch_data[0][0]  # type: [str]
    item_quantities = batch_data[1][0]  # type: [str]

    if len(batch_data[2]) > 0 and len(batch_data[2][0]) > 0:
        exclude = batch_data[2][0][0].casefold()
        if exclude == "ExcludeAll".casefold():
            project.exclude = Project.ExcludeSettings.all
        elif exclude == "ExcludeInvestments".casefold():
            project.exclude = Project.ExcludeSettings.investments

    # Verifying resource names (top row of the sheet)
    i = 0
    for name in items_names:  # type: str
        if i >= len(PROJECT_RESOURCES):
            log.append(
                f"  Error: More Project resources ({len(items_names)}) found than expected ({len(PROJECT_RESOURCES)})")
            raise GoogleSheetException(log, f"More Project resources ({len(items_names)}) found than expected "
                                            f"({len(PROJECT_RESOURCES)})")
        if name.casefold() != PROJECT_RESOURCES[i].casefold():
            log.append(
                f"  Error: Unexpected item at position {i}: Found \"{name}\", expected {PROJECT_RESOURCES[i]}")
            raise GoogleSheetException(log, f"Unexpected item at position {i} in {project_name}: Found \"{name}\", "
                                            f"expected \"{PROJECT_RESOURCES[i]}\"")
        i += 1

    # Processing investments area
    investments_raw = batch_data[3]
    i = -1
    investments_range = None
    if project.exclude == Project.ExcludeSettings.none:
        for row in investments_raw:
            i += 1
            if len(row) > 0 and row[0].casefold() == "Gesamtanteile".casefold():
                log.append(f"  Found investment rows: {investments_cell.row + i + 1} until {payout_cell.row - 1}")
                investments_range = (investments_cell.row + i + 1, payout_cell.row - 1)
                project.investments_range = investments_range
                break
        if investments_range is None or investments_range[0] is None:
            log.append("  ERROR: Investments area malformed, cell \"Gesamtanteile\" is missing")
            raise GoogleSheetException(log, f"Investments area for {project_name} is malformed, cell "
                                            f"\"Gesamtanteile\" is missing")
    log.append(f"  Data integrity of \"{project_name}\" verified!")

    for name in items_names:
        quantity = int(item_quantities[items_names.index(name)])
        if quantity > 0:
            project.pendingResources.append(Item(name, quantity))
    log.append(f"\"{project_name}\" processed!")


async def insert_investments(player: str, investments: {str: [int]}) -> ([str], {str: bool}):
    """
    Inserts investments for a player into the Google Sheet.

    :param player: the player name.
    :param investments: the investments as a dictionary, with the keys being the project names and the values being an
    array of ints that represent the quantities (while the indices for the resources is defined by
    sheet.PROJECT_RESOURCES).

    :return: the log and a dictionary containing which projects succeeded and which failed (no entry = not attempted)

    :raises exception.GoogleSheetException: if any exception occurred during the insertion process.
    """
    log = []
    success = {}
    if not STATE.is_online():
        raise BotOfflineException()
    for project in investments:
        log.append(f"Processing investment into {project} for player {player}")
        try:
            success[project] = await insert_investment(player, project, investments[project], log)
        except Exception as e:
            log.append("Error: " + str(e))
            success[project] = False
            logger.exception("Error while inserting investments for player %s", player, exc_info=e)
            raise GoogleSheetException(log,
                                       f"Error while trying to insert investments for {player}",
                                       progress=success) from e
    return log, success


async def insert_investment(player: str, project_name: str, quantities: [int], log=None) -> bool:
    """
    Inserts an investment (for a specific project) into according Worksheet.

    :param player:          the player name.
    :param project_name:    the project sheet name.
    :param quantities:      the array of quantities which should be inserted (the indices are defined by
                            sheet.PROJECT_RESOURCES).
    :param log:             the log that will be filled out during the process.
    :return:                True if the insertion was successful.
    """
    if log is None:
        log = []
    if project_name.casefold() == "overflow".casefold():
        return await insert_overflow(player, quantities, log)
    async with projects_lock:
        logger.debug("Inserting investment for %s into %s", player, project_name)
        agc = await agcm.authorize()
        sheet = await agc.open_by_key(SPREADSHEET_ID)
        log.append(f"  Quantities: {quantities}")
        log.append(f"  Loading project sheet...")
        worksheet = await sheet.worksheet(project_name)
        project = None  # type: Project | None
        for project in allProjects:
            if project.name == project_name:
                break
        if project is None:
            log.append(f"  Error, project sheet {project_name} not found!")
            raise GoogleSheetException(
                log,
                f"Error while inserting investments for {player}, project sheet {project} not found!")
        if project.investments_range is None:
            log.append(f"Error, project sheet {project_name} has no investment range!")
            raise GoogleSheetException(
                log,
                f"Error while inserting investments for {player} in project sheet {project}:"
                " Investment range not found!"
            )

        # Loading all investment cells
        cells = await worksheet.range(f"{project.investments_range[0]}:{project.investments_range[1]}")
        log.append("  Loaded investment range")

        # Find or create investment row for player, throws error
        player_row = await find_player_row(cells, player, project, worksheet, log)

        # Loading raw formulas to change them, as worksheet.range doesn't return the raw formulas
        log.append(f"  Loading raw formulas")
        # worksheet.get throws a TypeError for unknown reasons, that's why worksheet.batch_get is used
        player_row_formulas = await worksheet.batch_get([f"{player_row}:{player_row}"],
                                                        value_render_option=ValueRenderOption.formula)
        if len(player_row_formulas) == 0 or len(player_row_formulas[0]) == 0:
            log.append("  Error while loading raw formulas: Not found")
            raise GSpreadException(
                log,
                f"Error while fetching investment row for player {player} in {project} (row {player_row})"
            )
        # Extracting row from returned array
        player_row_formulas = player_row_formulas[0][0]

        log.append("  Calculating changes...")
        changes = calculate_changes(
            PROJECT_RESOURCES, quantities,
            player_row, player_row_formulas,
            project_name, player,
            cells, log)

        log.append(f"  Applying {len(changes)} changes to {project_name}:")
        for change in changes:
            log.append(f"    {change['range']}: '{change['values'][0][0]}'")
        await worksheet.batch_update(changes, value_input_option=ValueInputOption.user_entered)
    logger.debug("Inserted investment for %s into %s!", player, project_name)
    log.append(f"Project {project_name} processed!")
    return True


async def insert_overflow(player: str, quantities: [int], log=None) -> bool:
    """
    Inserts the overflow for a player into the Overflow sheet.

    :param player:      the player name.
    :param quantities:  the quantities.
    :param log:         the log that will be extended.
    :return:            True if the insertion was successful.
    """
    if log is None:
        log = []
    logger.debug("Inserting overflow for %s", player)
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    s = await sheet.worksheet(SHEET_OVERFLOW_NAME)
    log.append(f"Generating overflow for {player}: {quantities}")
    request = []
    for item, quantity in zip(PROJECT_RESOURCES, quantities):
        if quantity > 0:
            log.append(f"  Item \"{item}\": {quantity}")
            request.append([item, quantity, player])
    log.append(f"Overflow table generated:")
    for r in request:
        log.append(f"  {r}")
    log.append("  Inserting into sheet...")
    await s.append_rows(request, value_input_option=ValueInputOption.user_entered)
    log.append("Overflow inserted!")
    logger.debug("Inserted overflow for %s!", player)
    return True


def find_cell(cells: [Cell], row: int, col: int, start=0) -> Union[Cell, None]:
    lower_i = start - 1
    upper_i = start
    while lower_i > 0 or upper_i < len(cells):
        if lower_i > 0 and cells[lower_i].row == row and cells[lower_i].col == col:
            return cells[lower_i]
        if upper_i < len(cells) and cells[upper_i].row == row and cells[upper_i].col == col:
            return cells[upper_i]
        lower_i -= 1
        upper_i += 1
    return None


def is_required(ressource: str):
    for project in allProjects:
        if project.get_pending_resource(ressource) > 0:
            return True
    return False


async def split_overflow(log=None) -> Tuple[Dict[str, Dict[str, List[int]]], List[Tuple[Cell, int]]]:
    if log is None:
        log = []

    logger.debug("Splitting overflow")
    log.append("Loading overflow...")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    s = await sheet.worksheet(SHEET_OVERFLOW_NAME)
    overflow = await s.range("A2:C")
    i = -1
    total_res = [0] * len(PROJECT_RESOURCES)

    investments = {}  # type: {str: [Item]}
    changes = []
    async with projects_lock:
        for res_cell in overflow:
            i += 1
            if res_cell.col != 1:
                continue
            amount_cell = find_cell(overflow, res_cell.row, 2, i)
            player_cell = find_cell(overflow, res_cell.row, 3, i)
            if not is_required(res_cell.value):
                continue
            if not amount_cell or not player_cell:
                continue
            amount = amount_cell.numeric_value
            item = res_cell.value
            player = player_cell.value
            if item not in PROJECT_RESOURCES:
                continue
            index = PROJECT_RESOURCES.index(item)

            if type(amount) != int:
                logger.warning("Warning, value in overflow row %s is not an integer", res_cell.row)
                continue
            if player not in investments:
                investments[player] = {}
            split = Project.split_contract([Item(item, amount)], allProjects, extra_res=total_res)
            invest = Project.calc_investments(split)
            new_value = 0
            if "overflow" in invest:
                new_value = invest["overflow"][index]

            old_invest = investments[player]
            for proj, inv in invest.items():
                if proj == "overflow":
                    continue
                total_res = list(map(add, total_res, inv))
                changes.append((amount_cell, new_value))
                if proj in old_invest.keys():
                    old_invest[proj] = list(map(add, old_invest[proj], inv))
                else:
                    old_invest[proj] = inv
    log.append("Overflow recalculated")
    return investments, changes


async def apply_overflow_split(investments: Dict[str, Dict[str, List[int]]], changes: List[Tuple[Cell, int]]):
    logger.info("Inserting overflow into projects")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    s = await sheet.worksheet(SHEET_OVERFLOW_NAME)
    log = [f"Inserting investments from {len(investments)} players..."]
    logger.info("Inserting overflow investments for %s player", len(investments))
    for player, invest in investments.items():
        l, _ = await insert_investments(player, invest)
        log += l
    log.append("Investments inserted")
    batch_change = []
    logger.info("Calculating batch update for overflow, %s changes", len(changes))
    log.append(f"Calculating batch changes: {len(batch_change)} changes...")
    for cell, new_value in changes:
        log.append(f"  {cell.address}: {cell.value} -> {new_value}")
        batch_change.append({
            "range": cell.address,
            "values": [[str(new_value)]]
        })
    log.append(f"Executing {len(batch_change)} changes...")
    logger.info("Executing batch update (%s changes)", len(batch_change))
    await s.batch_update(batch_change)
    log.append("Batch update applied, overflow split completed.")
    logger.info("Batch update applied, overflow split completed")
    return log
