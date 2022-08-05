import difflib
import json
import logging
import os
from os.path import exists

import gspread
from gspread.utils import ValueRenderOption

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SPREADSHEET_ID = ""
SHEET_LOG_NAME = "Accounting Log"
SHEET_ACCOUNTING_NAME = "Accounting"
creds = None
sheet = None
wkAccounting = None  # type: gspread.worksheet.Worksheet | None
wkLog = None  # type: gspread.worksheet.Worksheet | None
users = []
overwrites = {}


def load_config():
    global overwrites
    if exists("user_overwrites.json"):
        with open("user_overwrites.json") as json_file:
            overwrites = json.load(json_file)

    else:
        config = {}
        with open("user_overwrites.json", "w") as outfile:
            json.dump(config, outfile, indent=4)
            logging.warning("ERROR: User overwrite config not found, created new one.")


def setup_sheet(sheet_id):
    global SPREADSHEET_ID, creds, users, sheet, wkAccounting, wkLog
    SPREADSHEET_ID = sheet_id
    load_config()

    account = gspread.service_account(filename="credentials.json")
    sheet = account.open_by_key(sheet_id)
    wkAccounting = sheet.worksheet("Accounting")
    wkLog = sheet.worksheet("Accounting Log")
    user_raw = wkAccounting.get("A4:K", value_render_option=ValueRenderOption.unformatted)
    for u in user_raw:
        if len(u) >= 11 and u[10]:
            users.append(u[0])
    for u in overwrites.keys():
        u_2 = overwrites.get(u)
        if u_2 is None:
            users.append(u)
        else:
            users.append(u_2)


def add_transaction(time: str, user_f: str, user_t: str, amount: int, purpose: str, reference: str):
    overwrite_f = overwrites.get(user_f, None)
    if overwrite_f is not None:
        user_f = overwrite_f
    overwrite_t = overwrites.get(user_t, None)
    if overwrite_t is not None:
        user_t = overwrite_t
    logger.info(f"Saving row [{time}; {user_f}; {user_t}; {amount}; {purpose}; {reference}]")
    wkLog.append_row([time, user_f, user_t, amount, purpose, reference], value_input_option="USER_ENTERED")
