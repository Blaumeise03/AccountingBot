import asyncio
import datetime
import json
import logging
import re
from os.path import exists

import gspread_asyncio
import pytz
from google.oauth2.service_account import Credentials
from gspread import GSpreadException
from gspread.utils import ValueRenderOption, ValueInputOption

from accounting_bot import projects
from accounting_bot.project_utils import find_player_row, calculate_changes, verify_batch_data, process_first_column
from accounting_bot.exceptions import GoogleSheetException
from accounting_bot.projects import Project

logger = logging.getLogger("bot.sheet")
logger.setLevel(logging.DEBUG)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = ""
SHEET_LOG_NAME = "Accounting Log"
SHEET_ACCOUNTING_NAME = "Accounting"
SHEET_OVERFLOW_NAME = "Projektüberlauf"

wkProjectNames = []  # type: [str]
users = []
overwrites = {}
allProjects = []  # type: [Project]
lastChanges = datetime.datetime(1970, 1, 1)

MEMBERS_AREA = "A4:K"      # The area of the member list
MEMBERS_NAME_INDEX = 0     # The column index of the name
MEMBERS_ACTIVE_INDEX = 10  # The column index of the "active" column

PROJECT_RESOURCES = []

loadProject_blocked = False
loadProject_time = datetime.datetime(1970, 1, 1)
projects_lock = asyncio.Lock()


def load_config():
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


def get_creds():
    creds = Credentials.from_service_account_file("credentials.json")
    scoped = creds.with_scopes([
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return scoped


agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)


async def setup_sheet(sheet_id, project_resources):
    global SPREADSHEET_ID, PROJECT_RESOURCES, users
    logger.info("Loading google sheet...")
    agc = await agcm.authorize()
    SPREADSHEET_ID = sheet_id
    PROJECT_RESOURCES = project_resources
    load_config()
    sheet = await agc.open_by_key(sheet_id)
    wk_accounting = await sheet.worksheet("Accounting")
    user_raw = await wk_accounting.get_values("A4:K", value_render_option=ValueRenderOption.unformatted)
    for u in user_raw:
        if len(u) > MEMBERS_ACTIVE_INDEX and u[MEMBERS_ACTIVE_INDEX]:
            users.append(u[MEMBERS_NAME_INDEX])
    for u in overwrites.keys():
        u_2 = overwrites.get(u)
        if u_2 is None:
            users.append(u)
        else:
            users.append(u_2)
    await find_projects()


def check_name_overwrites(name: str):
    overwrite = overwrites.get(name, None)
    if overwrite is not None:
        name = overwrite
    return name


async def add_transaction(transaction):
    agc = await agcm.authorize()
    sheet = await agc.open_by_key(SPREADSHEET_ID)
    wk_log = await sheet.worksheet("Accounting Log")
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
    await wk_log.append_row([time, user_f, user_t, amount, purpose, reference], value_input_option=ValueInputOption.user_entered)
    logger.debug("Saved row")


async def find_projects():
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


async def load_projects():
    global lastChanges, loadProject_blocked, loadProject_time
    # Prevent parallel execution of function, instead all other calls will be delayed until the first call is finished
    # In that case, the function won't reload the projects, as they were just reloaded recently
    time = datetime.datetime.now()
    if projects_lock.locked():
        logger.debug("load_projects is locked, waiting for it to complete...")
        while projects_lock.locked():  # and (time - loadProject_time).total_seconds() / 60.0 < 2:
            await asyncio.sleep(5)
        return ["Parallel command call discovered! Method evaluation was canceled. The received data may be deprecated."]

    async with projects_lock:
        log = []
        agc = await agcm.authorize()
        sheet = await agc.open_by_key(SPREADSHEET_ID)
        lastChanges = datetime.datetime.strptime(sheet.ss.lastUpdateTime, "%Y-%m-%dT%H:%M:%S.%fZ")

        allProjects.clear()

        for project_name in wkProjectNames:
            await load_project(project_name, log, sheet)

    logger.debug("Projects loaded")
    return log


async def load_project(project_name: str, log: [str], sheet: gspread_asyncio.AsyncioGspreadSpreadsheet):
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
    if not verify_batch_data(batch_data, log):
        return

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
            logger.error("More Project resources (%s) found than expected (%s)", len(items_names),
                         len(PROJECT_RESOURCES))
            i = -1
            break
        if name.casefold() != PROJECT_RESOURCES[i].casefold():
            log.append(
                f"  Error: Unexpected item at position {i}: Found \"{name}\", expected {PROJECT_RESOURCES[i]}")
            logger.error(
                "Unexpected item at position %s: Found \"%s\", expected \"%s\"",
                i, name, PROJECT_RESOURCES[i])
            i = -1
            break
        i += 1
    if i == -1:
        return

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
        if investments_range[0] is None:
            logger.error("Investments area malformed, cell \"Gesamtanteile\" is missing")
            log.append("  ERROR: Investments area malformed, cell \"Gesamtanteile\" is missing")
            return
    log.append(f"  Data integrity of \"{project_name}\" verified!")

    for name in items_names:
        quantity = int(item_quantities[items_names.index(name)])
        if quantity > 0:
            project.pendingResources.append(Project.Item(name, quantity))
    log.append(f"\"{project_name}\" processed!")


async def insert_investments(player: str, investments: {str: [int]}):
    log = []
    for project in investments:
        log.append(f"Processing investment into {project} for player {player}")
        try:
            await insert_investment(player, project, investments[project], log)
        except GSpreadException as e:
            log.append("Error: " + str(e))
            logger.exception("Error while inserting investments for player %s", player, exc_info=e)
            raise GoogleSheetException(log, f"Error while trying to insert investments for {player}")
    return log


async def insert_investment(player: str, project_name: str, quantities: [int], log=None):
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
                f"  Error while inserting investments for {player}, project sheet {project} not found!", log)
        if project.investments_range is None:
            if project is None:
                log.append(f"  Error, project sheet {project_name} has no investment range!")
                raise GoogleSheetException(
                    log,
                    "Error while inserting investments for {player} in project sheet {project}:"
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
        player_row_formulas = await worksheet.batch_get([f"{player_row}:{player_row}"], value_render_option=ValueRenderOption.formula)
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


async def insert_overflow(player: str, quantities: [int], log=None):
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
    return log

