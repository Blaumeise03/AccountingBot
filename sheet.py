import difflib
import logging
import os
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


def setup_sheet(sheet_id):
    global SPREADSHEET_ID, creds, users, sheet, wkAccounting, wkLog
    SPREADSHEET_ID = sheet_id

    account = gspread.service_account(filename="credentials.json")
    sheet = account.open_by_key(sheet_id)
    wkAccounting = sheet.worksheet("Accounting")
    wkLog = sheet.worksheet("Accounting Log")
    user_raw = wkAccounting.get("A4:K", value_render_option=ValueRenderOption.unformatted)
    for u in user_raw:
        if len(u) >= 11 and u[10]:
            users.append(u[0])
    users.append("Buyback Program")
    users.append("VOID Coins Bank")
    users.append("Lotterie")
    users.append("Ship Replacement Program")


def add_transaction(time: str, user_f: str, user_t: str, amount: int, purpose: str, reference: str):
    logger.info(f"Saving row [{time}; {user_f}; {user_t}; {amount}; {purpose}; {reference}]")
    wkLog.append_row([time, user_f, user_t, amount, purpose, reference], value_input_option="USER_ENTERED")
