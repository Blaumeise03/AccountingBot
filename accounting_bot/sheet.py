import asyncio
import datetime
import json
import logging
import re
import time
from os.path import exists

import gspread_asyncio
import pytz
from google.oauth2.service_account import Credentials
from gspread import GSpreadException
from gspread.utils import ValueRenderOption, ValueInputOption

from accounting_bot import projects, utils
from accounting_bot.project_utils import find_player_row, calculate_changes, verify_batch_data, process_first_column
from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.projects import Project
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from accounting_bot import classes

logger = logging.getLogger("bot.sheet")
logger.setLevel(logging.DEBUG)

# Google Sheets API settings
SCOPES = ["https://spreadsheets.google.com/feeds",
          "https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
SPREADSHEET_ID = ""
SHEET_LOG_NAME = "Accounting Log"
SHEET_ACCOUNTING_NAME = "Accounting"
SHEET_OVERFLOW_NAME = "Projektüberlauf"
sheet_name = "N/A"

# Projekt worksheet names
wkProjectNames = []  # type: [str]
users = []
overwrites = {}
allProjects = []  # type: [Project]
lastChanges = datetime.datetime(1970, 1, 1)

wallets = {}
wallets_last_reload = 0
MEMBERS_WALLET_INDEX = 2  # The column index of the balance
MEMBERS_AREA_LITE = "A4:C"  # The reduced area of the member list
MEMBERS_AREA = "A4:O"  # The area of the member list
MEMBERS_NAME_INDEX = 0  # The column index of the name
MEMBERS_ACTIVE_INDEX = 10  # The column index of the "active" column
MEMBERS_RANK_INDEX = 8  # The column index of the "rank" column
MEMBERS_NOTE_INDEX = 14  # The column containing notes for users

# All resources that exist, will be used to verify the integrity of the received data
PROJECT_RESOURCES = []

projects_lock = asyncio.Lock()


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

    for u in user_raw:
        # Check if main account
        if len(u) > MEMBERS_ACTIVE_INDEX and u[MEMBERS_ACTIVE_INDEX]:
            users.append(u[MEMBERS_NAME_INDEX])

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


async def add_transaction(transaction: 'classes.Transaction') -> None:
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
    time = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    amount = transaction.amount
    purpose = transaction.purpose if transaction.purpose is not None else ""
    reference = transaction.reference if transaction.reference is not None else ""

    # Applying custom username overwrites
    user_f = check_name_overwrites(user_f)
    user_t = check_name_overwrites(user_t)

    # Saving the data
    logger.info(f"Saving row [{time}; {user_f}; {user_t}; {amount}; {purpose}; {reference}]")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    wk_log = await sheet.worksheet("Accounting Log")
    await wk_log.append_row([time, user_f, user_t, amount, purpose, reference],
                            value_input_option=ValueInputOption.user_entered)
    logger.debug("Saved row")


async def load_wallets(force=False):
    global wallets, wallets_last_reload
    t = time.time()
    if (t - wallets_last_reload) < 60*60*5 and not force:
        return
    wallets_last_reload = t
    wallets.clear()
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    wk_accounting = await sheet.worksheet("Accounting")
    user_raw = await wk_accounting.get_values(MEMBERS_AREA_LITE, value_render_option=ValueRenderOption.unformatted)
    for u in user_raw:
        if len(u) >= 3:
            bal = u[MEMBERS_WALLET_INDEX]
            if type(bal) == int:
                wallets[u[MEMBERS_NAME_INDEX]] = bal


def get_balance(name: str):
    if name in wallets:
        return wallets[name]
    return None


async def find_projects():
    """
    Reloads the list of available projects and loads them into the cache.
    """
    logger.debug("Reloading projects...")
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    wk_projects = []
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
            project.pendingResources.append(Project.Item(name, quantity))
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
