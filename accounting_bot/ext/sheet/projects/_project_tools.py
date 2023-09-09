import asyncio
import logging
import re
from operator import add
from typing import List, TYPE_CHECKING, Tuple, Dict

import gspread_asyncio
from gspread import GSpreadException, Cell
from gspread.utils import ValueRenderOption, ValueInputOption

from accounting_bot.config import Config
from accounting_bot.exceptions import BotOfflineException, GoogleSheetException
from accounting_bot.ext.sheet.projects.project_utils import process_first_column, verify_batch_data, find_player_row, \
    calculate_changes, Project
from accounting_bot.ext.sheet.sheet_utils import find_cell
from accounting_bot.universe.data_utils import Item

if TYPE_CHECKING:
    from accounting_bot.ext.sheet.projects import ProjectPlugin

logger = logging.getLogger("ext.sheet.project.tools")


async def load_pending_resources(sheet: gspread_asyncio.AsyncioGspreadSpreadsheet, config: Config):
    logger.info("Loading pending resources")
    wk_overview = await sheet.worksheet(config["sheet_overview_name"])
    data = await wk_overview.get_values(config["overview_area"], value_render_option=ValueRenderOption.unformatted)
    items = {}
    row_i = -1
    for row in data:
        row_i += 1
        if row_i == 0:
            continue
        if len(row) < max(config["overview_item_index"], config["overview_item_index"]):
            continue
        items[row[config["overview_item_index"]]] = float(row[config["overview_quantity_index"]])
    return items


async def find_projects(self: "ProjectPlugin"):
    """
    Reloads the list of available projects.
    """
    logger.debug("Reloading projects")
    wk_projects = []
    sheet = await self.sheet.get_sheet()
    self.wk_project_names.clear()
    for s in await sheet.worksheets():
        if s.title.startswith("Project"):
            wk_projects.append(s)
            self.wk_project_names.append(s.title)
    names = ""
    for n in self.wk_project_names:
        names += n
        if self.wk_project_names.index(n) < len(self.wk_project_names) - 1:
            names += ", "
    logger.info(f"Found {len(self.wk_project_names)} project sheets: " + names)


async def load_projects(self: "ProjectPlugin") -> [str]:
    """
    Reloads the projects, parallel execution of the function is prevented, instead all other calls will be delayed until
    the first call is finished.

    :return: The reloading log.
    """
    # Prevent parallel execution of function, instead all other calls will be delayed until the first call is finished
    # In that case, the function won't reload the projects, as they were just reloaded recently
    if self.projects_lock.locked():
        logger.debug("load_projects is locked, waiting for it to complete...")
        while self.projects_lock.locked():  # and (time - loadProject_time).total_seconds() / 60.0 < 2:
            await asyncio.sleep(5)
        return [
            "Parallel command call discovered! Method evaluation was canceled. The received data may be deprecated."]

    async with self.projects_lock:
        log = []
        sheet = await self.sheet.get_sheet()
        # Clear all current project
        self.all_projects.clear()

        for project_name in self.wk_project_names:
            # Load projects
            await load_project(self, project_name, log, sheet)

    logger.debug("Projects loaded")
    return log


async def load_project(self: "ProjectPlugin", project_name: str, log: [str], sheet: gspread_asyncio.AsyncioGspreadSpreadsheet) -> None:
    """
    Loads a specific project from the Google sheet. It will load all pending resources and save them into the cache. It
    will take a given log and extend it.

    :param self: The ProjectPlugin
    :param project_name: the name of the project sheet
    :param log: the current reloading log
    :param sheet: the current AsyncioGspreadSpreadsheet instance
    """
    # if not self.bot.is_online():
    #     raise BotOfflineException()
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

    project = Project(project_name)
    self.all_projects.append(project)

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
        if i >= len(self.project_resources):
            log.append(
                f"  Error: More Project resources ({len(items_names)}) found than expected "
                f"({len(self.project_resources)})")
            raise GoogleSheetException(log, f"More Project resources ({len(items_names)}) found than expected "
                                            f"({len(self.project_resources)})")
        if name.casefold() != self.project_resources[i].casefold():
            log.append(
                f"  Error: Unexpected item at position {i}: Found \"{name}\", expected {self.project_resources[i]}")
            raise GoogleSheetException(log, f"Unexpected item at position {i} in {project_name}: Found \"{name}\", "
                                            f"expected \"{self.project_resources[i]}\"")
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
            project.pending_resources.append(Item(name, quantity))
    log.append(f"\"{project_name}\" processed!")


async def insert_investments(self: "ProjectPlugin",
                             player: str,
                             investments: Dict[str, List[int]]) -> Tuple[List[str], Dict[str, bool]]:
    """
    Inserts investments for a player into the Google Sheet.

    :param self:
    :param player: The player name.
    :param investments: The investments as a dictionary, with the keys being the project names and the values being an
    array of ints that represent the quantities.

    :return: The log and a dictionary containing which projects succeeded and which failed (no entry = not attempted)

    :raises exception.GoogleSheetException: If any exception occurred during the insertion process.
    """
    log = []
    success = {}
    if not self.bot.is_online():
        raise BotOfflineException()
    for project in investments:
        log.append(f"Processing investment into {project} for player {player}")
        try:
            success[project] = await insert_investment(self, player, project, investments[project], log)
        except Exception as e:
            log.append("Error: " + str(e))
            success[project] = False
            logger.exception("Error while inserting investments for player %s", player, exc_info=e)
            raise GoogleSheetException(log,
                                       f"Error while trying to insert investments for {player}",
                                       progress=success) from e
    return log, success


async def insert_investment(self: "ProjectPlugin", player: str, project_name: str, quantities: [int], log=None) -> bool:
    """
    Inserts an investment (for a specific project) into according Worksheet.

    :param self:
    :param player:          The player name.
    :param project_name:    The project sheet name.
    :param quantities:      The array of quantities which should be inserted.
    :param log:             The log that will be filled out during the process.
    :return:                True if the insertion was successful.
    """
    if log is None:
        log = []
    if project_name.casefold() == "overflow".casefold():
        return await insert_overflow(self, player, quantities, log)
    async with self.projects_lock:
        logger.debug("Inserting investment for %s into %s", player, project_name)
        sheet = await self.sheet.get_sheet()
        log.append(f"  Quantities: {quantities}")
        log.append("  Loading project sheet...")
        worksheet = await sheet.worksheet(project_name)
        project = None  # type: Project | None
        for project in self.all_projects:
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
        log.append("  Loading raw formulas")
        # worksheet.get throws a TypeError for unknown reasons, that's why worksheet.batch_get is used
        player_row_formulas = await worksheet.batch_get([f"{player_row}:{player_row}"],
                                                        value_render_option=ValueRenderOption.formula)
        if len(player_row_formulas) == 0 or len(player_row_formulas[0]) == 0:
            log.append("  Error while loading raw formulas: Not found")
            raise GSpreadException(
                log,
                f"Error while fetching investment row for player {player} in {project} (row {player_row})"
            )
        # Extracting row from the returned array
        player_row_formulas = player_row_formulas[0][0]

        log.append("  Calculating changes...")
        changes = calculate_changes(
            self.project_resources, quantities,
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


async def insert_overflow(self: "ProjectPlugin", player: str, quantities: [int], log=None) -> bool:
    """
    Inserts the overflow for a player into the Overflow sheet.

    :param self:
    :param player:      The player name.
    :param quantities:  The quantities.
    :param log:         The log that will be extended.
    :return:            True if the insertion was successful.
    """
    if log is None:
        log = []
    logger.debug("Inserting overflow for %s", player)
    sheet = await self.sheet.get_sheet()
    s = await sheet.worksheet(self.config["sheet_overflow_name"])
    log.append(f"Generating overflow for {player}: {quantities}")
    request = []
    for item, quantity in zip(self.project_resources, quantities):
        if quantity > 0:
            log.append(f"  Item \"{item}\": {quantity}")
            request.append([item, quantity, player])
    log.append("Overflow table generated:")
    for r in request:
        log.append(f"  {r}")
    log.append("  Inserting into sheet...")
    await s.append_rows(request, value_input_option=ValueInputOption.user_entered)
    log.append("Overflow inserted!")
    logger.debug("Inserted overflow for %s!", player)
    return True


def is_required(self: "ProjectPlugin", ressource: str):
    for project in self.all_projects:
        if project.get_pending_resource(ressource) > 0:
            return True
    return False


async def split_overflow(self: "ProjectPlugin", project_resources: List[str], log=None) -> Tuple[Dict[str, Dict[str, List[int]]], List[Tuple[Cell, int]]]:
    if log is None:
        log = []

    logger.debug("Splitting overflow")
    log.append("Loading overflow...")
    sheet = await self.sheet.get_sheet()
    s = await sheet.worksheet(self.config["sheet_overflow_name"])
    overflow = await s.range("A2:C")
    i = -1
    total_res = [0] * len(self.project_resources)

    investments = {}  # type: {str: [Item]}
    changes = []
    async with self.projects_lock:
        for res_cell in overflow:
            i += 1
            if res_cell.col != 1:
                continue
            amount_cell = find_cell(overflow, res_cell.row, 2, i)
            player_cell = find_cell(overflow, res_cell.row, 3, i)
            if not is_required(self, res_cell.value):
                continue
            if not amount_cell or not player_cell:
                continue
            amount = amount_cell.numeric_value
            item = res_cell.value
            player = player_cell.value
            if item not in self.project_resources:
                continue
            index = self.project_resources.index(item)

            if type(amount) != int:
                logger.warning("Warning, value in overflow row %s is not an integer", res_cell.row)
                continue
            if player not in investments:
                investments[player] = {}
            split = Project.split_contract(
                [Item(item, amount)],
                self.all_projects,
                self.project_resources,
                extra_res=total_res)
            invest = Project.calc_investments(split, project_resources)
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


async def apply_overflow_split(self: "ProjectPlugin",
                               investments: Dict[str, Dict[str, List[int]]],
                               changes: List[Tuple[Cell, int]]):
    logger.info("Inserting overflow into projects")
    sheet = await self.sheet.get_sheet()
    s = await sheet.worksheet(self.config["sheet_overflow_name"])
    log = [f"Inserting investments from {len(investments)} players..."]
    logger.info("Inserting overflow investments for %s player", len(investments))
    for player, invest in investments.items():
        l, _ = await insert_investments(self, player, invest)
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
