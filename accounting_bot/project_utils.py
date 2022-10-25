import logging
import re

from gspread import Cell

from accounting_bot.exceptions import GoogleSheetException

logger = logging.getLogger("project.utils")


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


def calculate_changes(project_resources: [str], quantities: [int],
                      player_row: int, player_row_formulas: [str],
                      project_name: str, player: str,
                      cells: [Cell], log: [str]):
    changes = []
    for i in range(len(project_resources)):
        cell = next(filter(lambda c: c.row == player_row and c.col == (i + 8), cells), None)
        if cell is None:
            cell = Cell(player_row, i + 8, "")
        if 0 < (cell.col - 7) < len(project_resources):
            resource_name = project_resources[cell.col - 8]
            if len(player_row_formulas) < cell.col:
                quantity_formula = ""
            else:
                quantity_formula = player_row_formulas[cell.col - 1]  # type: str
            new_quantity = quantities[cell.col - 8]
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
            log.append(f"      New quantity formula: \"{quantity_formula}\"")
            changes.append({
                "range": cell.address,
                "values": [[quantity_formula]]
            })
    return changes


def verify_batch_data(batch_data: [], log: []):
    if len(batch_data) != 4:
        logger.error("Unexpected batch size: %s. Expected: 4", len(batch_data))
        log.append(f"  Unexpected batch size: {len(batch_data)}. Expected: 4")
        return False
    if len(batch_data[0]) != 1:
        logger.error("Unexpected length of item_names: %s. Expected: 1", len(batch_data[0]))
        log.append(f"  Unexpected length of item_names: {len(batch_data[0])}. Expected: 1")
        return False
    if len(batch_data[1]) != 1:
        logger.error("Unexpected length of item_quantities: %s. Expected: 1", len(batch_data[1]))
        log.append(f"  Unexpected length of item_quantities: {len(batch_data[1])}. Expected: 1")
        return False
    if len(batch_data[3]) == 0:
        logger.error("Unexpected length of investments: %s", len(batch_data[3]))
        log.append(f"  Unexpected length of investments: {len(batch_data[3])}")
        return False
    return True
