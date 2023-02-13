import datetime
import difflib
import logging
import os
import random
import re
import shutil
import string
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy
import requests
from PIL import Image
from discord.ext import tasks
from pytesseract import pytesseract

from accounting_bot import utils
from accounting_bot.accounting import Transaction, ConfirmOCRView
from accounting_bot.exceptions import BotOfflineException

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.projects")
WORKING_DIR = "images"
STATE = None  # type: BotState | None
Path(WORKING_DIR + "/download").mkdir(parents=True, exist_ok=True)


def preprocess_text(img, debug=False):
    # Greyscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = (255 - gray)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_grey.jpg', gray)

    # Color curve correction to improve text
    lut_in = [0, 91, 144, 226, 255]
    lut_out = [0, 0, 30, 255, 255]
    lut_8u = numpy.interp(numpy.arange(0, 256), lut_in, lut_out).astype(numpy.uint8)
    img_lut = cv2.LUT(gray, lut_8u)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_LUT.jpg', img_lut)

    # Apply threshold
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 9)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_threshold.jpg', thresh)

    # Detect text
    rect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    dilation = cv2.dilate(thresh, rect_kernel, iterations=6)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_dilation.jpg', dilation)

    return dilation, img_lut


def extract_text(dilation, image):
    if not STATE.ocr:
        raise OCRException("OCR is not enabled!")
    contours, hierarchy = cv2.findContours(dilation, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_NONE)
    im2 = image
    height, width = im2.shape
    rect = im2.copy()
    #print(im2.shape)
    result = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < 10 or w < 5 or w > 300 or h > 100:
            continue
        # print(f"Original: ({x}, {y}), ({x + w}, {y + h}), size: {w}x{h}")
        x = max(0, x - 9)
        y = max(0, y - 9)
        w = min(w + 8, width - (x + w))
        h = min(h + 8, height - (y + h))
        # Draw the bounding box on the text area
        rect = cv2.rectangle(rect,
                             (x, y),
                             (x + w, y + h),
                             (0, 255, 0),
                             2)

        # Crop the bounding box area
        # print(f"New:      ({x}, {y}), ({x + w}, {y + h}), size: {w}x{h}")
        cropped = im2[y:y + h, x:x + w]

        cv2.imwrite(WORKING_DIR + '/image_rectanglebox.jpg', rect)

        # Using tesseract on the cropped image area to get text
        text = pytesseract.image_to_string(cropped)  # type: str
        text = text.strip()
        if len(text) == 0:
            continue

        # print(f"Found text at ({x}, {y}) to ({x + w}, {y + h}) \"{text}\"")
        result.append((
            {"x1": x, "x2": x + w, "y1": y, "y2": y + h},
            text
        ))
        # cv2.imshow(text, cropped)
        # cv2.waitKey(0)

    return result


def to_relative_cords(cords: {int}, width: int, height: int) -> {float}:
    return {
        "x1": cords["x1"] / width,
        "x2": cords["x2"] / width,
        "y1": cords["y1"] / height,
        "y2": cords["y2"] / height,
    }


class CorporationMission:
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

    @staticmethod
    def from_text(text: ({int}, str), width, height):
        mission = CorporationMission()
        isk_get_lines = []
        isk_pay_lines = []
        title_line = None

        # Find line for ISK
        for cords, t in text:  # type: (dict, str)
            rel_cords = to_relative_cords(cords, width, height)

            # Check if image contains a mission
            is_mission = max(difflib.SequenceMatcher(None, "MISSION", t).ratio(),
                             difflib.SequenceMatcher(None, "MISSIONSDETAILS", t).ratio(),
                             difflib.SequenceMatcher(None, "MISSION DETAILS", t.replace("|", "").strip()).ratio())
            if is_mission > 0.75:
                mission.isMission = True
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
                continue

            # Check the "Total Times"-Setting
            if rel_cords["x1"] > 0.45 and max(difflib.SequenceMatcher(None, "Total Times", t).ratio(),
                                              difflib.SequenceMatcher(None, "Times", t).ratio(),
                                              difflib.SequenceMatcher(None, "Gesamthäufigkeit", t).ratio()) > 0.7:
                mission.has_limit = True
                continue

            # Get the title
            matches = difflib.get_close_matches(t, ["Transfer", "Einzahlung", "Auszahlung"], 1)
            if rel_cords["x1"] < 0.3 and rel_cords["y1"] < 0.3 and len(matches) > 0:
                mission.title = str(matches[0])
                title_line = (rel_cords["y1"] + rel_cords["y2"]) / 2
                continue

            # Get the label
            label_acc = difflib.SequenceMatcher(None, "Accounting", t).ratio()
            if label_acc > 0.75:
                mission.label = True

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
        mission.validate()
        return mission


class ThreadSafeTask:
    def __init__(self, task):
        self.completed = False
        self._lock = threading.Lock()
        self.task = task
        self.res = None

    def is_completed(self):
        with self._lock:
            return self.completed

    def run(self):
        with self._lock:
            self.res = self.task()
            self.completed = True

    def yield_result(self):
        with self._lock:
            if self.completed:
                return self.res


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


return_missions = ThreadSafeList()


def handle_image(url, content_type, message, channel, author):
    if not STATE.is_online():
        raise BotOfflineException()
    img_id = "".join(random.choice(string.ascii_uppercase) for _ in range(3))
    image_name = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "__" + img_id + "." + content_type.replace(
        "image/", "")
    file_name = WORKING_DIR + "/download/" + image_name

    try:
        res = requests.get(url, stream=True)
        logger.info("Received image (%s) from %s: %s", image_name, message.author.id, url)

        if res.status_code == 200:
            with open(file_name, "wb") as f:
                shutil.copyfileobj(res.raw, f)
            logger.info("Image successfully downloaded from %s (%s): %s", message.author.name, message.author.id,
                        file_name)
        else:
            logger.warning("Image %s download from %s (%s) failed!", image_name, message.author.name, message.author.id)

        image = Image.open(file_name)
        image.thumbnail((1500, 4000), Image.LANCZOS)
        image.save(WORKING_DIR + "/" + img_id + "_rescaled.png", "PNG")
        img = cv2.imread(WORKING_DIR + "/" + img_id + "_rescaled.png")
        dilation, img_lut = preprocess_text(img, debug=True)
        text = extract_text(dilation, img_lut)
        height, width, _ = img.shape
        mission = CorporationMission.from_text(text, width, height)
        return_missions.append((channel, author, mission, img_id))
        if os.path.exists(WORKING_DIR + "/" + img_id + "_rescaled.png"):
            os.remove(WORKING_DIR + "/" + img_id + "_rescaled.png")
        if not mission.isMission:
            logger.warning("Received image %s from %s:%s is not a mission, deleting file.",
                           file_name, message.author.name, message.author.id)
            if os.path.exists(file_name):
                logger.info("Deleting image %s", file_name)
                os.remove(file_name)
    except Exception as e:
        logger.error("OCR job for image %s (user %s) failed!", img_id, message.author.id)
        logger.exception(e)
        return_missions.append((message.author.id, author, e, img_id))
        if os.path.exists(file_name):
            logger.info("Deleting image %s", file_name)
            os.remove(file_name)


@tasks.loop(seconds=3.0)
async def ocr_result_loop():
    with return_missions.lock:
        for i in range(len(return_missions.list)):
            if return_missions.list[i] is None:
                continue
            channel_id, author, mission, img_id = return_missions.list[i]  # type: int, int, CorporationMission, str
            return_missions.list[i] = None
            user = await STATE.bot.get_or_fetch_user(author) if author is not None else None
            if not user and channel_id:
                channel = STATE.bot.get_channel(channel_id)
                if channel is None:
                    channel = await STATE.bot.fetch_channel(channel_id)
                if channel is None:
                    logger.error("Channel " + str(channel_id) + " from OCR result list not found!")
                    continue
            if isinstance(mission, Exception):
                logger.error("OCR job for %s failed, img_id: %s, error: %s", author, img_id, str(mission))
                await user.send("An error occurred: " + str(mission))
                continue
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
                       " wurde, so musst Du sie manuell im Accountinglog posten.\n"
            if user is not None:
                await user.send("Bild wurde verarbeitet: \n" + msg)
            # if channel is not None:
            # await channel.send("Bild wurde verarbeitet: \n" + msg)
            if not mission.valid:
                return
            if user is None:
                logger.warning("User for OCR image %s with discord ID %s not found!", img_id, author)
                return
            transaction = Transaction.from_ocr(mission, author)
            transaction.author = user.name
            if transaction:
                ocr_view = ConfirmOCRView(transaction)
                await user.send("Willst du diese Transaktion senden?", view=ocr_view, embed=transaction.create_embed())

        while None in return_missions.list:
            return_missions.list.remove(None)


class OCRException(Exception):
    pass
