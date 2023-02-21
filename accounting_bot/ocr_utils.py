import datetime
import difflib
import re
import threading
from typing import Optional, Dict, Union, Tuple, Callable

from accounting_bot import utils
from accounting_bot.utils import TransactionLike, OCRBaseData


def to_relative_cords(cords: Dict[str, int], width: int, height: int) -> {float}:
    return {
        "x1": cords["x1"] / width,
        "x2": cords["x2"] / width,
        "y1": cords["y1"] / height,
        "y2": cords["y2"] / height,
    }


def is_within_bounds(bounds: Dict[str, Union[int, float]], x: Union[int, float], y: Union[int, float]):
    return min(bounds["x1"], bounds["x2"]) <= x <= max(bounds["x1"], bounds["x2"]) and \
        min(bounds["y1"], bounds["y2"]) <= y <= max(bounds["y1"], bounds["y2"])


def get_center(cords: Dict[str, Union[int, float]]) -> Tuple[Union[int, float], Union[int, float]]:
    return (cords["x1"] + cords["x2"]) / 2, (cords["y1"] + cords["y2"]) / 2


def set_bounding_box(bounding_box: Dict[str, Optional[int]], cords: Dict[str, int]) -> None:
    def set_bound(cord_name: str, cord_num: str, method: Callable) -> None:
        if bounding_box[cord_name + cord_num] is not None:
            bounding_box[cord_name + cord_num] = method(bounding_box[cord_name + cord_num], cords[f"{cord_name}1"],
                                                        cords[f"{cord_name}2"])
        else:
            bounding_box[cord_name + cord_num] = method(cords[f"{cord_name}1"], cords[f"{cord_name}2"])

    set_bound("x", "1", min)
    set_bound("y", "1", min)
    set_bound("x", "2", max)
    set_bound("y", "2", max)


class ThreadSafeList:
    def __init__(self):
        self.list = list()
        self.lock = threading.Lock()

    def append(self, value):
        with self.lock:
            self.list.append(value)

    def pop(self):
        with self.lock:
            return self.list.pop()

    def get(self, index):
        with self.lock:
            return self.list[index]

    def length(self):
        with self.lock:
            return len(self.list)


class CorporationMission(TransactionLike, OCRBaseData):
    def __init__(self) -> None:
        super().__init__()
        self.isMission = False  # type: bool
        self.valid = False  # type: bool
        self.title = None  # type: str  | None
        self.username = None  # type: str  | None
        self.main_char = None  # type: str  | None
        self.pay_isk = None  # type: bool | None
        self.amount = None  # type: int  | None
        self.has_limit = False  # type: bool
        self.label = False  # type: bool

    def validate(self):
        self.valid = False
        if self.amount and self.has_limit and self.title and self.label and self.main_char:
            title = self.title.casefold()
            if title == "Einzahlung".casefold() and not self.pay_isk:
                self.valid = True
            if title == "Auszahlung".casefold() and self.pay_isk:
                self.valid = True
            if title == "Transfer".casefold():
                self.valid = False

    def get_from(self) -> Optional[str]:
        if self.pay_isk and self.title == "Auszahlung":
            return self.main_char
        return None

    def get_to(self) -> Optional[str]:
        if not self.pay_isk and self.title == "Einzahlung":
            return self.main_char
        return None

    def get_amount(self) -> int:
        return self.amount

    def get_time(self) -> Optional[datetime.datetime]:
        return datetime.datetime.now()

    def get_purpose(self) -> str:
        if self.pay_isk and self.title == "Auszahlung":
            return "Auszahlung Accounting"
        if not self.pay_isk and self.title == "Einzahlung":
            return "Einzahlung Accounting"

    def get_reference(self) -> Optional[str]:
        return None

    @staticmethod
    def from_text(text: ({int}, str), width, height):
        mission = CorporationMission()
        bounding_box = {"x1": None, "x2": None, "y1": None, "y2": None}
        isk_get_lines = []
        isk_pay_lines = []
        title_line = None

        # Find line for ISK
        for cords, t in text:  # type: (dict, str)
            rel_cords = to_relative_cords(cords, width, height)
            if rel_cords["x1"] < 0.1:
                continue
            # Check if image contains a mission
            is_mission = max(difflib.SequenceMatcher(None, "MISSION", t).ratio(),
                             difflib.SequenceMatcher(None, "MISSIONSDETAILS", t).ratio(),
                             difflib.SequenceMatcher(None, "MISSION DETAILS", t.replace("|", "").strip()).ratio())
            if is_mission > 0.75:
                mission.isMission = True
                set_bounding_box(bounding_box, cords)
                continue

            # Get transaction direction and y-level of the ISK quantity
            pay = max(difflib.SequenceMatcher(None, "Corporation pays", t).ratio(),
                      difflib.SequenceMatcher(None, "pays", t.replace("|", "").strip()).ratio())
            get = max(difflib.SequenceMatcher(None, "Corporation gets", t).ratio(),
                      difflib.SequenceMatcher(None, "gets", t.replace("|", "").strip()).ratio())
            if rel_cords["x1"] < 0.3 and (pay > 0.8 or get > 0.8):
                if pay > get:
                    isk_pay_lines.append((rel_cords["y1"] + rel_cords["y2"]) / 2)
                elif get > pay:
                    isk_get_lines.append((rel_cords["y1"] + rel_cords["y2"]) / 2)
                set_bounding_box(bounding_box, cords)
                continue

            # Check the "Total Times"-Setting
            if rel_cords["x1"] > 0.45 and max(difflib.SequenceMatcher(None, "Total Times", t).ratio(),
                                              difflib.SequenceMatcher(None, "Times", t).ratio(),
                                              difflib.SequenceMatcher(None, "Gesamthäufigkeit", t).ratio()) > 0.7:
                mission.has_limit = True
                # ToDo: Check for number or "No Restrictions"
                set_bounding_box(bounding_box, cords)
                continue

            # Get the title
            matches = difflib.get_close_matches(t, ["Transfer", "Einzahlung", "Auszahlung"], 1)
            if rel_cords["x1"] < 0.3 and rel_cords["y1"] < 0.3 and len(matches) > 0:
                mission.title = str(matches[0])
                title_line = (rel_cords["y1"] + rel_cords["y2"]) / 2
                set_bounding_box(bounding_box, cords)
                continue

            # Get the label
            label_acc = difflib.SequenceMatcher(None, "Accounting", t).ratio()
            if label_acc > 0.75:
                mission.label = True
                set_bounding_box(bounding_box, cords)

        if len(isk_pay_lines) > 0 and len(isk_get_lines) > 0:
            raise Exception("Found both pay and get lines!")

        mission.pay_isk = len(isk_pay_lines) > 0
        mission.amount = None
        isk_lines = isk_pay_lines if mission.pay_isk else isk_get_lines
        name_match = -1

        # Get isk and name
        for cords, txt in text:  # type: dict, str
            rel_cords = to_relative_cords(cords, width, height)
            rel_y = (rel_cords["y1"] + rel_cords["y2"]) / 2
            best_line = None

            for line in isk_lines:
                if best_line is None or (abs(rel_y - line) < abs(rel_y - best_line)):
                    best_line = line

            if title_line and rel_cords["x1"] > 0.6 and abs(rel_y - title_line) < 0.05 and len(txt) > 3:
                name_raw = txt.split("\n")[0].strip()
                main_char, parsed_name, _ = utils.get_main_account(name_raw)
                if main_char:
                    match = difflib.SequenceMatcher(None, parsed_name, name_raw).ratio()
                    if match > name_match:
                        mission.main_char = main_char
                        mission.username = parsed_name
                        name_match = match
                set_bounding_box(bounding_box, cords)
                continue

            if best_line and abs(rel_y - best_line) < 0.05 and rel_cords["x1"] < 0.66:
                # Remove comas and the Z
                txt = re.sub("[,.;zZ\n ]", "", txt)
                # Fix numbers
                txt = re.sub("[oOD]", "0", txt)
                txt = re.sub("[Iil]", "1", txt)
                txt = txt.strip()
                if txt.isdigit():
                    mission.amount = int(txt)
                    set_bounding_box(bounding_box, cords)
        mission.validate()
        if bounding_box["x1"] and bounding_box["x2"] and bounding_box["y1"] and bounding_box["y2"]:
            mission.bounding_box = bounding_box
        return mission


class MemberDonation(TransactionLike, OCRBaseData):
    def __init__(self) -> None:
        super().__init__()
        self.valid = False  # type: bool
        self.username = None  # type: str  | None
        self.main_char = None  # type: str  | None
        self.amount = None  # type: int  | None
        self.is_donation = False  # type: bool
        self.time = None  # type: datetime.datetime | None

    def get_to(self) -> Optional[str]:
        return self.main_char

    def get_amount(self) -> int:
        return self.amount

    def get_time(self) -> Optional[datetime.datetime]:
        return self.time

    def get_purpose(self) -> str:
        return "Einzahlung Accounting"

    @staticmethod
    def from_text(text: ({int}, str), width, height):
        donation = MemberDonation()
        bounding_box = {"x1": None, "x2": None, "y1": None, "y2": None}
        quantity_line = None
        type_line = None
        time_line = None

        for cords, txt in text:  # type: dict, str
            rel_cords = to_relative_cords(cords, width, height)
            if rel_cords["x1"] > 0.25:
                continue
            split = txt.split(" ", 2)
            if len(split) >= 3 and difflib.SequenceMatcher(None, "Member Donation",
                                                           (split[0] + " " + split[1])).ratio() > 0.75:
                main_char, parsed_name, _ = utils.get_main_account(re.sub("[()\[\]]", "", split[2]))
                if main_char:
                    donation.main_char = main_char
                    donation.username = parsed_name
                donation.is_donation = True
                set_bounding_box(bounding_box, cords)
            elif len(split) >= 2 and difflib.SequenceMatcher(None, "Mitgliederspende", split[0]).ratio() > 0.75:
                main_char, parsed_name, _ = utils.get_main_account(re.sub("[()\[\]]", "", split[1]))
                if main_char:
                    donation.main_char = main_char
                    donation.username = parsed_name
                donation.is_donation = True
                set_bounding_box(bounding_box, cords)
            elif max(difflib.SequenceMatcher(None, "Quantity", txt).ratio(),
                     difflib.SequenceMatcher(None, "Betrag", txt).ratio()) > 0.75:
                quantity_line = (rel_cords["y1"] + rel_cords["y2"]) / 2
                set_bounding_box(bounding_box, cords)
            elif max(difflib.SequenceMatcher(None, "Type", txt).ratio(),
                     difflib.SequenceMatcher(None, "Kategori...", txt).ratio()) > 0.75:
                type_line = (rel_cords["y1"] + rel_cords["y2"]) / 2
                set_bounding_box(bounding_box, cords)
            elif max(difflib.SequenceMatcher(None, "Time", txt).ratio(),
                     difflib.SequenceMatcher(None, "Zeit", txt).ratio()) > 0.75:
                time_line = (rel_cords["y1"] + rel_cords["y2"]) / 2
                set_bounding_box(bounding_box, cords)

        for cords, txt in text:  # type: dict, str
            rel_cords = to_relative_cords(cords, width, height)
            if rel_cords["x1"] > 0.25:
                continue
            # The label is always offset down by some pixels
            rel_y = (rel_cords["y1"] + rel_cords["y2"]) / 2

            if type_line and abs(rel_y - type_line) < 0.05 \
                    and max(difflib.SequenceMatcher(None, "Member Donation", txt).ratio(),
                            difflib.SequenceMatcher(None, "Mitgliederspende", txt).ratio()) > 0.75:
                donation.is_donation = True
                set_bounding_box(bounding_box, cords)
            elif quantity_line and abs(rel_y - quantity_line) < 0.05:
                quantity_raw = re.sub("[.,]", "", txt).upper() \
                    .replace("D", "0").replace("O", "0").replace("$", "5").strip()
                if quantity_raw.isdigit():
                    donation.amount = int(quantity_raw)
                    set_bounding_box(bounding_box, cords)
            elif time_line and abs(rel_y - time_line) < 0.05:
                time_raw = re.sub("[.,:;]", "", txt.split("\n")[0])
                try:
                    time = datetime.datetime.strptime(time_raw, "%Y-%m-%d %H%M%S")
                    donation.time = time
                    set_bounding_box(bounding_box, cords)
                except ValueError:
                    pass

        donation.validate()
        if bounding_box["x1"] and bounding_box["x2"] and bounding_box["y1"] and bounding_box["y2"]:
            bounding_box["x1"] = max(bounding_box["x1"] - 8, 0)
            bounding_box["x2"] += 8
            bounding_box["y1"] = max(bounding_box["y1"] - 16, 0)
            bounding_box["y2"] += 16
            donation.bounding_box = bounding_box
        return donation

    def validate(self):
        self.valid = self.is_donation and \
                     self.main_char is not None and \
                     self.amount is not None


def get_message(data: OCRBaseData) -> str:
    def get_message_corporation_mission(mission: CorporationMission) -> str:
        msg = f"```\nIst Mission: {str(mission.isMission)}\n" \
              f"Gültig: {str(mission.valid)}\nTitel: {mission.title}\nNutzername: {mission.username}\n" \
              f"Main Char: {mission.main_char}\nMenge: {str(mission.amount)}\nErhalte ISK: {str(mission.pay_isk)}" \
              f"\nLimitiert: {str(mission.has_limit)}\nLabel korrekt: {mission.label}\n```\n"
        if not mission.isMission:
            msg += "**Fehler**: Das Bild ist keine Corpmission. Wenn es sich doch um eine handelt, *kontaktiere " \
                   "bitte einem Admin* und schicke ihm das Bild zu, damit die Bilderkennung verbessert werden kann.\n\n"
        if not mission.label:
            msg += "**Fehler**: Das Label wurde nicht erkannt. Für die Mission muss das Label \"Accounting\" " \
                   "ausgewählt werden.\n"
        if not mission.has_limit:
            msg += "**Fehler**: Das Limit wurde nicht erkannt. Bei der Mission muss ein \"Total Times\"-Limit " \
                   "eingestellt sein.\n"
        if not mission.title or mission.title == "Transfer":
            msg += "**Fehler**: Der Titel wurde nicht erkannt. Er muss \"Einzahlung\" oder \"Auszahlung\" lauten.\n"
        if not mission.main_char:
            msg += "**Fehler**: Der Spielername wurde nicht erkannt.\n"
        if not mission.amount:
            msg += "**Fehler**: Die ISK-Menge wurde nicht erkannt.\n"
        if not mission.valid:
            msg += "\n**Fehlgeschlagen!** Die Mission ist nicht korrekt, bzw. es gab einen Fehler beim Einlesen. " \
                   "Wenn die Mission nicht korrekt erstellt wurde, lösche sie bitte und erstelle sie bitte " \
                   "entsprechend der Anleitung im Leitfaden neu. Wenn sie korrekt ist, aber nicht richtig erkannt" \
                   " wurde, so musst Du sie manuell im Accountinglog posten.\n" \
                   "Du kannst außerdem einen Admin informieren, damit die Bilderkennung verbessert werden kann.\n"
        return msg

    def get_message_member_donation(donation: MemberDonation) -> str:
        msg = f"```\nIst Spende: {str(donation.is_donation)}\n" \
              f"Gültig: {str(donation.valid)}\n" \
              f"Menge: {str(donation.amount)}\n" \
              f"Nutzer: {str(donation.username)}\n" \
              f"Main Char: {str(donation.main_char)}\n" \
              f"Zeit: {str(donation.time)}\n```\n"
        if not donation.is_donation:
            msg += "**Fehler**: Das Bild enthält keine Spende\n"
        if not donation.time:
            msg += "**Fehler**: Die Zeit wurde nicht erkannt, verwende aktuelle Zeit\n"
            donation.time = datetime.datetime.now()
        if not donation.main_char:
            msg += "**Fehler**: Der Nutzername wurde nicht erkannt\n"
        if not donation.amount:
            msg += "**Fehler**: Die Menge wurde nicht erkannt\n"
        if not donation.valid:
            msg += "\n**Fehlgeschlagen**: Die Spende wurde nicht korrekt erkannt. Bitte sende nur unbearbeitete " \
                   "Screenshots (d.h. schneide das Bild bitte nicht passend zu). Wenn es sich um einen Fehler vom " \
                   "Bot handelt, poste die Spende manuell im Accountinglog.\n" \
                   "Du kannst außerdem einen Admin informieren, damit die Bilderkennung verbessert werden kann.\n"
        return msg

    if isinstance(data, CorporationMission):
        return get_message_corporation_mission(data)
    elif isinstance(data, MemberDonation):
        return get_message_member_donation(data)
    else:
        raise TypeError(f"Unknown OCR data: {str(type(data))}")


class OCRException(Exception):
    pass
