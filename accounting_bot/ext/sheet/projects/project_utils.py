import logging
import re
import string
from enum import Enum
from typing import List, Dict, Tuple, Optional

from gspread import Cell

from accounting_bot.exceptions import GoogleSheetException, ProjectException
from accounting_bot.universe.data_utils import Item

logger = logging.getLogger("ext.sheet.project.utils")


def col2num(col):
    num = 0
    for c in col:
        if c in string.ascii_letters:
            num = num * 26 + (ord(c.upper()) - ord('A')) + 1
    return num


def process_first_column(batch_cells: [Cell], log: [str]):
    pending_cell = None
    investments_cell = None
    payout_cell = None
    for c in batch_cells:
        if c.value == "ausstehende Ressourcenkosten":
            pending_cell = c
            log.append(f"  Found ressource cost cell: {c.address}")
        if c.value == "Investitionen":
            investments_cell = c
            log.append(f"  Found investments cell: {c.address}")
        if c.value == "Auszahlung":
            payout_cell = c
            log.append(f"  Found payout cell: {c.address}")
    return pending_cell, investments_cell, payout_cell


async def find_player_row(cells, player, project, worksheet, log):
    player_row = -1
    for cell in cells:
        if cell.col != 1:
            continue
        if cell.value.casefold() == player.casefold():
            player_row = cell.row
            break
    if player_row == -1:
        log.append(f"  Investment row for player {player} not found, creating one...")
        for cell in cells:
            if cell.col == 1 and cell.value == "":
                log.append(f"    Found empty cell at {cell.address}, inserting player name...")
                await worksheet.update_cell(cell.row, cell.col, player)
                log.append("    Player name inserted!")
                player_row = cell.row
                break
    if player_row == -1:
        log.append(f"  Error! Could not insert investments for {player} into {project}:"
                   f" Could not find or create investment row!"
                   )
        raise GoogleSheetException(
            log,
            f"Error while inserting investments for {player} into project sheet {project}:"
            f" Could not find or create investment row!"
        )
    log.append(f"  Identified investment row: {player_row}")
    return player_row


def calculate_changes(project_resources: List[str], quantities: List[int],
                      player_row: int, player_row_formulas: List[str],
                      project_name: str, player: str,
                      cells: List[Cell], log: List[str],
                      project_resources_range: Tuple[str, str] = ("I", "BV")):
    changes = []
    handled_cols = []
    first_col, last_col = col2num(project_resources_range[0]), col2num(project_resources_range[1])
    log.append(f"    {project_name} has {len(project_resources)} project resources, range: {project_resources_range}=({first_col}, {last_col})")
    if len(project_resources) != len(quantities):
        log.append(f"    Warning! Length of project resources ({len(project_resources)}) does not match the length of quantities ({len(quantities)})")
    for i in range(len(project_resources)):
        cell = next(filter(lambda c: c.row == player_row and c.col == (i + first_col), cells), None)
        if cell is None:
            cell = Cell(player_row, i + first_col, "")
        if 0 < (cell.col - first_col + 1) <= len(project_resources):
            resource_name = project_resources[cell.col - 9]
            if len(player_row_formulas) < cell.col:
                quantity_formula = ""
            else:
                quantity_formula = player_row_formulas[cell.col - 1]  # type: str
            new_quantity = quantities[cell.col - first_col]
            if new_quantity <= 0:
                continue
            log.append(f"    Invested quantity for {resource_name} is {new_quantity}")
            if len(quantity_formula) == 0:
                quantity_formula = "=" + str(new_quantity)
            else:
                if re.fullmatch("=([-+*]?\\d+)+", quantity_formula) is None:
                    log.append(f"Error! Cell {cell.address} does contain an illegal formula: \"{quantity_formula}\"")
                    raise GoogleSheetException(log, "Sheet %s contains illegal formula for player %s (cell %s): \"%s\"",
                                               project_name, player, cell.address, quantity_formula)
                quantity_formula += "+" + str(new_quantity)
            handled_cols.append(i)
            log.append(f"      New quantity formula: \"{quantity_formula}\"")
            changes.append({
                "range": cell.address,
                "values": [[quantity_formula]]
            })
        else:
            log.append(f"    Error! Cell {cell.address} is out of range ({project_resources_range}) for project resources!")
            log.append(f"    ->  0 > {cell.col - first_col + 1} or {cell.col - first_col + 1} <= {len(project_resources)}")
    return changes, handled_cols


def verify_batch_data(batch_data: [], log: [str]):
    """
    Verifies the batch data of a project sheet.

    :param batch_data: the batch data to verify
    :param log: the log
    :return: True if the data was verified
    :raises exceptions.GoogleSheetException: if the data could not be verified
    """
    if len(batch_data) != 4:
        log.append(f"  Unexpected batch size: {len(batch_data)}. Expected: 4")
        raise GoogleSheetException(log, f"Unexpected batch size: {len(batch_data)}. Expected: 4")
    if len(batch_data[0]) != 1:
        log.append(f"  Unexpected length of item_names: {len(batch_data[0])}. Expected: 1")
        raise GoogleSheetException(log, f"Unexpected length of {len(batch_data[0])}. Expected: 1")
    if len(batch_data[1]) != 1:
        log.append(f"  Unexpected length of item_quantities: {len(batch_data[1])}. Expected: 1")
        raise GoogleSheetException(log, f"Unexpected length of item_quantities: {len(batch_data[1])}. Expected: 1")
    if len(batch_data[3]) == 0:
        log.append(f"  Unexpected length of investments: {len(batch_data[3])}")
        raise GoogleSheetException(log, f"Unexpected length of investments: {len(batch_data[3])}")
    return True


def format_list(split: {str: [(str, int)]}, success: {str, bool}):
    res = ""
    max_num = 0
    max_project_size = 0
    for item in split:
        for (project, quantity) in split[item]:
            if quantity > max_num:
                max_num = quantity
            if len(project) > max_project_size:
                max_project_size = len(project)
    max_size = min(len(str(max_num)), 10)

    for item in split:
        res += item + "\n"
        for (project, quantity) in split[item]:
            spaces = max(max_size - len(str(quantity)), 0)
            res += f"    {quantity} {' ' * spaces}-> {project}"
            spaces = max(max_project_size - len(str(project)), 0)
            if project in success:
                if success[project]:
                    res += f"{' ' * spaces} (✓)"
                else:
                    res += f"{' ' * spaces} (FAILED)"
            else:
                res += f"{' ' * spaces} (NOT INSERTED)"
            res += "\n"
    return res


class Contract:
    def __init__(self, discord_id: int, player_name: str):
        self.discord_id = discord_id
        self.player_name = player_name
        self.contents = []  # type: List[Item]
        self.split = {}  # type: Dict[Project, List[Item]]

    def __repr__(self):
        return f"Contract({self.player_name}, {len(self.contents)} items)"

    def parse_list(self, raw_list: str):
        self.contents = Item.parse_ingame_list(raw_list)

    def get_total_item_split(self, item: Item):
        count = 0
        for items in self.split.values():
            for _item in filter(lambda i: i.name == item.name, items):  # type: Item
                count += _item.amount
        return count

    def invest_resource(self, project: "Project", item: Item, amount: int):
        if item not in self.contents:
            raise ProjectException(f"Can't split item {item} for {project} if it is not contained in the contract")
        if amount > item.amount - self.get_total_item_split(item):
            raise ProjectException(f"Can't split item {item} again, the amount {amount} is exceeding the total amount")
        if project not in self.split:
            self.split[project] = []
        for i in self.split[project]:
            if i.name == item.name:
                i.amount += amount
                return
        self.split[project].append(Item(item.name, amount))

    def get_invested_resource(self, project: "Project", item: Item):
        if project not in self.split:
            return 0
        amount = 0
        for split in filter(lambda i: i.name == item.name, self.split[project]):  # type: Item
            amount += split.amount
        return amount

    def validate_investments(self, results: Dict["Project", Optional[List[Item]]]) -> bool:
        for item in self.contents:
            left = item.amount
            for project in self.split.keys():
                quantity = self.get_invested_resource(project, item)
                if quantity == 0:
                    continue
                left -= quantity
                if results is None or project not in results:
                    return False
                if results[project] is None:
                    return False
                _item = next(filter(lambda _i: _i.name == item.name, results[project]), None)  # type: Item
                if _item is None:
                    return False
                if _item.amount != quantity:
                    return False
            if left > 0:
                return False
        return True

    def build_split_list(self, item_order: Optional[List[str]] = None, results: Optional[Dict["Project", Optional[List[Item]]]] = None):
        if item_order is not None:
            Item.sort_list(self.contents, item_order)
        msg = ""
        max_num = 0
        max_project_size = 0
        for project, split_items in self.split.items():
            for item in split_items:
                if item.amount > max_num:
                    max_num = item.amount
                if len(project.name) > max_project_size:
                    max_project_size = len(project.name)
        max_size = min(len(str(max_num)), 10)
        for item in self.contents:
            msg += item.name + "\n"
            left = item.amount
            for project in self.split.keys():
                quantity = self.get_invested_resource(project, item)
                if quantity == 0:
                    continue
                left -= quantity
                spaces = max(max_size - len(str(quantity)), 0)
                msg += f"    {quantity} {' ' * spaces}-> {project.name}"
                spaces = max(max_project_size - len(str(project.name)), 0)
                if results is not None and project in results:
                    if results[project] is None:
                        msg += f"{' ' * spaces} (FAILED)\n"
                    else:
                        _item = next(filter(lambda _i: _i.name == item.name, results[project]), None)  # type: Item
                        if _item is None:
                            msg += f"{' ' * spaces} (NOT INSERTED)\n"
                        elif _item.amount == quantity:
                            msg += f"{' ' * spaces} (✓)\n"
                        else:
                            msg += f"{' ' * spaces} (PARTIALLY INSERTED: {_item.amount})\n"
                else:
                    msg += f"{' ' * spaces} (NOT INSERTED)\n"

            if left > 0:
                msg += f"    {left}   (FAILED TO INSERT EVERYTHING)\n"
        return msg


class Project(object):
    def __init__(self, name: str):
        self.name = name  # type: str
        self.exclude = Project.ExcludeSettings.none  # type: Project.ExcludeSettings
        self.pending_resources = []  # type: List[Item]
        self.investments_range = None
        self.resource_order = []  # type: List[str]

    def __repr__(self):
        return f"Project({self.name})"

    def get_pending_resource(self, resource: str) -> int:
        resource = resource.casefold()
        for item in self.pending_resources:
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
        for r in self.pending_resources:  # type: Item
            res += f"\n{r.name}: {r.amount}"
        return res

    @staticmethod
    def split_contract(contract: Contract,
                       project_list: List['Project'],
                       project_resources: List[str] = None,
                       priority_projects: List[str] = None,
                       extra_res: Dict["Project", List[Item]] = None) -> None:
        projects_ordered = project_list[::-1]  # Reverse the list
        if priority_projects is not None:
            for p_name in reversed(priority_projects):
                for p in projects_ordered:  # type: Project
                    if p.name == p_name:
                        projects_ordered.remove(p)
                        projects_ordered.insert(0, p)
        # split = {}  # type: {str: [(str, int)]}
        contract.split.clear()
        overflow_project = Project(name="overflow")
        for item in contract.contents:
            left = item.amount
            for project in projects_ordered:  # type: Project
                if project.exclude != Project.ExcludeSettings.none:
                    continue
                pending = project.get_pending_resource(item.name)
                if extra_res is not None and project in extra_res:
                    _extra = next(filter(lambda r: r.name == item.name, extra_res[project]), None)
                    if _extra is not None:
                        pending -= _extra.amount
                amount = min(pending, left)
                if pending > 0 and amount > 0:
                    left -= amount
                    contract.invest_resource(project, item, amount)
            if left > 0:
                contract.invest_resource(overflow_project, item, left)

    @staticmethod
    def calc_investments(split: Dict[str, List[Tuple[str, int]]], project_resources: List[str]) -> Dict[str, List[int]]:
        log = []
        investments = {}  # type: Dict[str, List[int]]
        item_names = project_resources
        for item_name in split:  # type: str
            if item_name in item_names:
                index = item_names.index(item_name)
            else:
                log.append(f"Error: {item_name} is not a project resource!")
                continue
            for (pr, amount) in split[item_name]:  # type: str, int
                if pr not in investments:
                    investments[pr] = [0] * len(item_names)
                investments[pr][index] += amount
        return investments

    class ExcludeSettings(Enum):
        none = 0  # Don't exclude the project
        investments = 1  # Exclude the project from investments
        all = 2  # Completely hides the project
