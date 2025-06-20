import asyncio
import logging
import re
from typing import List, TYPE_CHECKING, Tuple, Dict, Optional, Generator

import gspread_asyncio
from gspread import GSpreadException, Cell
from gspread.utils import ValueRenderOption, ValueInputOption

from accounting_bot.config import Config
from accounting_bot.exceptions import BotOfflineException, GoogleSheetException, ProjectException
from accounting_bot.ext.sheet.projects.project_utils import process_first_column, verify_batch_data, find_player_row, \
    calculate_changes, Project, Contract
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
        ["I2:2",  # Item names row
         f"I{pending_cell.row}:{pending_cell.row}",  # Item quantities row (pending resources)
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
        elif exclude == "ExcludeAutoinvest".casefold():
            project.exclude = Project.ExcludeSettings.auto_split

    # Verifying resource names (top row of the sheet)
    i = 0
    for name in items_names:  # type: str
        if name not in self.project_resources:
            log.append(
                f"  Error: Unexpected item at position {i}: Found \"{name}\". "
                f"Illegal item for projects, please rename it or add it to the config")
            raise GoogleSheetException(log, f"Unexpected item at position {i} for project {project}: Found '{name}'. "
                                            f"Illegal item for projects, please rename it or add it to the config")
        i += 1
        project.resource_order.append(name)

    # Processing investments area
    investments_raw = batch_data[3]
    i = -1
    investments_range = None
    if project.exclude == Project.ExcludeSettings.none or project.exclude == Project.ExcludeSettings.auto_split:
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


async def load_project_payout(plugin: "ProjectPlugin", project: Project, log: list[str], sheet: gspread_asyncio.AsyncioGspreadSpreadsheet) -> None:
    logger.info(f"Loading project payout for {project.name}")
    log.append(f"Starting processing of project sheet \"{project.name}\"")
    s = await sheet.worksheet(project.name)

    # Scanning project sheet to find the locations of the required entries
    batch_cells = await s.findall(re.compile(r"(Steuern Corporation)|(Gesamtanteile)|(Auszahlung)"), in_column=1)
    

async def insert_investments(self: "ProjectPlugin", contract: Contract) -> Tuple[List[str], Dict[Project, List[Item]]]:
    """
    Inserts investments for a player into the Google Sheet.

    :param self:
    :param contract: The contract to split.

    :return: The log and a dictionary containing which projects succeeded and which failed (no entry = not attempted)

    :raises exception.GoogleSheetException: If any exception occurred during the insertion process.
    """
    log = []
    success = {}
    if not self.bot.is_online():
        raise BotOfflineException()
    for project in contract.split:
        log.append(f"Processing investment into {project} for player {contract.player_name}")
        try:
            success[project] = await insert_investment(self, contract.player_name, project.name, contract.split[project], log)
        except Exception as e:
            log.append("Error: " + str(e))
            success[project] = None
            logger.exception("Error while inserting investments for player %s", contract.player_name, exc_info=e)
            raise GoogleSheetException(log,
                                       f"Error while trying to insert investments for {contract.player_name}",
                                       progress=success) from e
    return log, success


async def insert_investment(self: "ProjectPlugin", player: str, project_name: str, quantities: List[Item], log=None) -> Optional[List[Item]]:
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
        return quantities if await insert_overflow(self, player, quantities, log) else None
    if project_name.casefold() == "unknown".casefold():
        log.append("Unknown items in contract detected")
        logger.warning("Unknown items in contract for player %s detected", player)
        return None
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
        raw_quantities = [0]*len(project.resource_order)
        handled_items = {}
        for i, res_name in enumerate(project.resource_order):
            for item in quantities:
                if item.name != project.resource_order[i]:
                    continue
                raw_quantities[i] += item.amount
                handled_items[i] = item
        log.append(f"  Calculated raw quantities: {raw_quantities}")
        changes, handled_cols = calculate_changes(
            project.resource_order, raw_quantities,
            player_row, player_row_formulas,
            project_name, player,
            cells, log)
        for i in handled_items.keys():
            if i in handled_cols:
                handled_cols[handled_cols.index(i)] = handled_items[i]

        log.append(f"  Applying {len(changes)} changes to {project_name}:")
        for change in changes:
            log.append(f"    {change['range']}: '{change['values'][0][0]}'")
        await worksheet.batch_update(changes, value_input_option=ValueInputOption.user_entered)
    logger.debug("Inserted investment for %s into %s!", player, project_name)
    log.append(f"Project {project_name} processed!")
    return list(filter(lambda i: type(i) is Item, handled_cols))


async def insert_overflow(self: "ProjectPlugin", player: str, items: List[Item], log=None) -> bool:
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
    log.append(f"Generating overflow for {player}: {len(items)} items")
    request = []
    for item in items:
        if item.amount > 0:
            log.append(f"  Item \"{item.name}\": {item.amount}")
            request.append([item.name, item.amount, player])
    log.append("Overflow table generated:")
    for r in request:
        log.append(f"  {r}")
    log.append("  Inserting into sheet...")
    await s.append_rows(request, value_input_option=ValueInputOption.user_entered, table_range="A2")
    log.append("Overflow inserted!")
    logger.debug("Inserted overflow for %s!", player)
    return True


def is_required(plugin: "ProjectPlugin", ressource: str):
    for project in plugin.all_projects:
        if project.get_pending_resource(ressource) > 0:
            return True
    return False


def iterate_overflow(plugin: "ProjectPlugin", overflow: List[Cell]) -> Generator[Tuple[Cell, Cell, Cell], None, None]:
    """
    A generator that iterates all entries of the project overflow. Will yield a tuple consisting of:
        - the item cell
        - the player cell
        - the amount cell
    :param plugin:
    :param overflow:
    """
    i = 0
    for res_cell in overflow:
        i += 1
        if res_cell.col != 1:
            continue
        amount_cell = find_cell(overflow, res_cell.row, 2, i)
        player_cell = find_cell(overflow, res_cell.row, 3, i)
        if not is_required(plugin, res_cell.value):
            continue
        if amount_cell is None or player_cell is None:
            continue
        yield res_cell, player_cell, amount_cell


async def split_overflow(
        plugin: "ProjectPlugin", project_resources: List[str], log=None
) -> Tuple[List[Contract], List[Tuple[Cell, int]]]:
    if log is None:
        log = []

    logger.debug("Splitting overflow")
    log.append("Loading overflow...")
    sheet = await plugin.sheet.get_sheet()
    s = await sheet.worksheet(plugin.config["sheet_overflow_name"])
    overflow = await s.range("A2:C")
    i = -1
    total_items = {}  # type: Dict[Project, List[Item]]
    overflow_contracts = []  # type: List[Contract]
    overflow_items = []  # type: List[Item]
    overflow_items_owner = {}  # type: Dict[Item, str]

    def _increment_resource(_player: str, _res: str, _amount: int):
        _contract_item = Item(_res, _amount)
        overflow_items_owner[_contract_item] = _player
        overflow_items.append(_contract_item)

    async with plugin.projects_lock:
        # Sum up resources for all players
        for item_cell, player_cell, amount_cell in iterate_overflow(plugin, overflow):
            amount = amount_cell.numeric_value
            item = item_cell.value
            player = player_cell.value
            if item not in plugin.project_resources:
                continue
            if type(amount) is not int:
                logger.warning("Warning, value in overflow row %s is not an integer", item_cell.row)
                continue
            _increment_resource(player, item, amount)

        # Split contracts
        remaining = {}
        inserted_items = {}  # type: Dict[str, int]
        for item in overflow_items:
            for project in reversed(plugin.all_projects):
                if project.exclude != Project.ExcludeSettings.none:
                    continue
                if item.name not in inserted_items:
                    inserted_items[item.name] = 0
                player = overflow_items_owner[item]
                pending = project.get_pending_resource(item.name)
                pending = max(0, pending - inserted_items[item.name])
                if pending <= 0:
                    continue
                amount = min(pending, item.amount)
                if amount == 0:
                    continue
                item.amount -= amount
                inserted_items[item.name] += amount
                contract = next(
                    filter(lambda _c: _c.player_name == player, overflow_contracts),
                    None)
                if contract is None:
                    contract = Contract(discord_id=-1, player_name=player)
                    overflow_contracts.append(contract)
                contract_item = next(filter(lambda _i: _i.name == item.name, contract.contents), None)
                if contract_item is None:
                    contract_item = Item(item.name, 0)
                    contract.contents.append(contract_item)
                contract_item.amount += amount
                contract.invest_resource(project, contract_item, amount)
                if player not in remaining:
                    remaining[player] = {}
                if item.name not in remaining[player]:
                    remaining[contract.player_name][item.name] = 0
                remaining[contract.player_name][item.name] += amount

        # Calculate cell changes
        changes = []  # type: List[Tuple[Cell, int]]
        for item_cell, player_cell, amount_cell in iterate_overflow(plugin, overflow):
            amount = amount_cell.numeric_value
            item = item_cell.value
            player = player_cell.value
            if player not in remaining:
                continue
            if item not in remaining[player]:
                continue
            if remaining[player][item] == 0:
                continue
            if remaining[player][item] < 0:
                raise ProjectException(
                    f"Overflow split failed, item {item} was split to often: {remaining[player][item]} for {player}")
            if amount > remaining[player][item]:
                amount -= remaining[player][item]
                remaining[player][item] = 0
            else:
                remaining[player][item] -= amount
                amount = 0
            if amount == amount_cell.numeric_value:
                continue
            changes.append((amount_cell, amount))
    for player, items in remaining.items():
        for item, amount in items.items():
            if amount != 0:
                raise ProjectException(
                    f"Overflow split failed, item {item} was not processed correctly for {player}, remaining {amount}")

    log.append("Overflow recalculated")
    return overflow_contracts, changes


async def apply_overflow_split(plugin: "ProjectPlugin",
                               investments: List[Contract],
                               changes: List[Tuple[Cell, int]]):
    logger.info("Inserting overflow into projects")
    sheet = await plugin.sheet.get_sheet()
    s = await sheet.worksheet(plugin.config["sheet_overflow_name"])
    log = [f"Inserting investments from {len(investments)} players..."]
    logger.info("Inserting overflow investments for %s player", len(investments))
    for contract in investments:
        l, _ = await insert_investments(plugin, contract)
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
    await s.batch_update(batch_change, value_input_option=ValueInputOption.user_entered)
    log.append("Batch update applied, overflow split completed.")
    logger.info("Batch update applied, overflow split completed")
    return log
