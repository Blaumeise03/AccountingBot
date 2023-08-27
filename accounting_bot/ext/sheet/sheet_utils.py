from typing import List, Union

from gspread import Cell


def map_cells(cells: List[Cell]) -> List[List[Cell]]:
    res = {}
    for cell in cells:
        if cell.row in res:
            res[cell.row].append(cell)
        else:
            res[cell.row] = [cell]
    for r_i, val in res.items():
        val.sort(key=lambda c: c.col)
    res = list(map(lambda t: t[1], sorted(res.items(), key=lambda t: t[0])))
    return res


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
