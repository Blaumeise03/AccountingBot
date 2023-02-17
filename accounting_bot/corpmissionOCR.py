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
from typing import TYPE_CHECKING, Union, Optional, Tuple, Callable, Dict

import cv2
import numpy
import requests
from PIL import Image
from discord.ext import tasks
from numpy import ndarray
from pytesseract import pytesseract

from accounting_bot import utils, accounting
from accounting_bot.accounting import Transaction, ConfirmOCRView
from accounting_bot.exceptions import BotOfflineException
from accounting_bot.utils import TransactionLike, OCRBaseData

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.ocr")
WORKING_DIR = "images"
accounting.IMG_WORKING_DIR = WORKING_DIR
STATE = None  # type: BotState | None
Path(WORKING_DIR + "/download").mkdir(parents=True, exist_ok=True)
Path(WORKING_DIR + "/transactions").mkdir(parents=True, exist_ok=True)


def apply_dilation(img, block_size, c, rect: Tuple[int, int], iterations, debug=False):
    # Apply threshold
    thresh = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block_size, c)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_threshold.jpg', thresh)

    # Detect text
    rect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, rect)
    dilation = cv2.dilate(thresh, rect_kernel, iterations=iterations)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_dilation.jpg', dilation)
    return dilation


def preprocess_mission(img, debug=False):
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
    dilation = apply_dilation(gray, 11, 9, (2, 2), 6, debug)
    return dilation, img_lut


def preprocess_donation(img, debug=False):
    img_b, img_g, img_r = cv2.split(img)
    lut = {
        "green_in": [0, 47, 97, 176, 255],
        "green_out": [0, 4, 142, 210, 255],
        # Grayscale color curve for dilation
        "thresh_in": [0, 91, 144, 226, 255],
        "thresh_out": [0, 91, 144, 226, 255],
        # Color curves for text recognition
        "comp_r_in": [0, 115, 184, 255],
        "comp_r_out": [255, 225, 0, 0],
        "comp_g_in": [0, 99, 164, 194, 255],
        "comp_g_out": [0, 0, 224, 10, 0],
        "comp_b_in": [0, 98, 172, 207, 255],
        "comp_b_out": [0, 28, 192, 33, 0],
    }
    lut_int = {
        "green": numpy.interp(numpy.arange(0, 256), lut["green_in"], lut["green_out"]).astype(numpy.uint8),
        "thresh": numpy.interp(numpy.arange(0, 256), lut["thresh_in"], lut["thresh_out"]).astype(numpy.uint8),
        "comp_r": numpy.interp(numpy.arange(0, 256), lut["comp_r_in"], lut["comp_r_out"]).astype(numpy.uint8),
        "comp_g": numpy.interp(numpy.arange(0, 256), lut["comp_g_in"], lut["comp_g_out"]).astype(numpy.uint8),
        "comp_b": numpy.interp(numpy.arange(0, 256), lut["comp_b_in"], lut["comp_b_out"]).astype(numpy.uint8)
    }
    # img_lut_b = cv2.LUT(img_b, lut_int["blue"])
    img_lut_g = cv2.LUT(img_g, lut_int["green"])
    img_lut_g = (255 - img_lut_g)
    if debug:
        # cv2.imwrite(WORKING_DIR + '/image_LUT_b.jpg', img_lut_b)
        cv2.imwrite(WORKING_DIR + '/image_LUT_g.jpg', img_lut_g)

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = 255 - gray
    img_lut_thresh = cv2.LUT(gray, lut_int["thresh"])
    dilation = apply_dilation(img_lut_thresh, 7, 9, (3, 1), 6, debug)

    lut_in = [0, 59, 130, 255]
    lut_out = [0, 43, 217, 255]
    lut_8u = numpy.interp(numpy.arange(0, 256), lut_in, lut_out).astype(numpy.uint8)
    img_lut = cv2.LUT(img, lut_8u)

    if debug:
        cv2.imwrite(WORKING_DIR + '/image_LUT.jpg', img_lut)

    return dilation, img_lut


def extract_text(dilation, image, postfix="", expansion=(9, 9, 8, 8)):
    if not STATE.ocr:
        raise OCRException("OCR is not enabled!")
    contours, hierarchy = cv2.findContours(dilation, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_NONE)
    im2 = image
    height = im2.shape[0]
    width = im2.shape[1]
    rect = im2.copy()
    # print(im2.shape)
    result = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < 10 or w < 5 or w > 300 or h > 100:
            continue
        # print(f"Original: ({x}, {y}), ({x + w}, {y + h}), size: {w}x{h}")
        x = max(0, x - expansion[0])
        y = max(0, y - expansion[1])
        w = min(w + expansion[2], width - (x + w))
        h = min(h + expansion[3], height - (y + h))
        # Draw the bounding box on the text area
        rect = cv2.rectangle(rect,
                             (x, y),
                             (x + w, y + h),
                             (0, 255, 0),
                             2)

        # Crop the bounding box area
        # print(f"New:      ({x}, {y}), ({x + w}, {y + h}), size: {w}x{h}")
        cropped = im2[y:y + h, x:x + w]

        cv2.imwrite(WORKING_DIR + f"/image_rectanglebox{postfix}.jpg", rect)

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

    return result, rect


def get_image_type(text: [{str: int}, str], width: int, height: int) -> int:
    m_mission = 0
    m_donation = 0
    for cords, txt in text:  # type: dict[str: int], str
        rel_cords = to_relative_cords(cords, width, height)
        m_mission = max(m_mission,
                        difflib.SequenceMatcher(None, "MISSION DETAILS", txt).ratio(),
                        difflib.SequenceMatcher(None, "MISSION", txt).ratio(),
                        difflib.SequenceMatcher(None, "MISSIONSDETAILS", txt).ratio())
        if rel_cords["x1"] < 0.2:
            m_donation = max(m_donation,
                             difflib.SequenceMatcher(None, "Member Donation", txt).ratio(),
                             difflib.SequenceMatcher(None, "Donation", txt).ratio(),
                             difflib.SequenceMatcher(None, "Mitgliederspende", txt.split(" ")[0]).ratio())
    if m_donation > 0.75:
        return 1
    if m_mission > 0.75:
        return 0
    return -1


def to_relative_cords(cords: {int}, width: int, height: int) -> {float}:
    return {
        "x1": cords["x1"] / width,
        "x2": cords["x2"] / width,
        "y1": cords["y1"] / height,
        "y2": cords["y2"] / height,
    }


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
                    .replace("D", "0").replace("O", "0").strip()
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
                     self.amount is not None and \
                     self.time is not None


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


def handle_image(url, content_type, message, channel, author, file=None, no_delete=False, debug=True):
    if not STATE.is_online():
        raise BotOfflineException()
    img_id = "".join(random.choice(string.ascii_uppercase) for _ in range(5))
    image_name = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "__" + img_id + "." + content_type.replace(
        "image/", "")
    if file is None:
        file_name = WORKING_DIR + "/download/" + image_name
    else:
        file_name = file
        no_delete = True

    try:
        if file is None:
            res = requests.get(url, stream=True)
            logger.info("Received image (%s) from %s: %s", image_name, message.author.id, url)

            if res.status_code == 200:
                with open(file_name, "wb") as f:
                    shutil.copyfileobj(res.raw, f)
                logger.info("Image successfully downloaded from %s (%s): %s", message.author.name, message.author.id,
                            file_name)
            else:
                logger.warning("Image %s download from %s (%s) failed: %s", image_name, message.author.name,
                               message.author.id, res.status_code)

        image = Image.open(file_name)
        image.thumbnail((1500, 4000), Image.LANCZOS)
        image.save(WORKING_DIR + "/image_rescaled_" + img_id + ".png", "PNG")
        img = cv2.imread(WORKING_DIR + "/image_rescaled_" + img_id + ".png")
        dilation, img_lut = preprocess_mission(img, debug=debug)
        res = extract_text(dilation, img_lut)
        text = res[0]
        img_rect = res[1] if len(res) > 1 else None
        height, width, _ = img.shape
        img_type = get_image_type(text, width, height)
        valid = False
        data = None  # type: OCRBaseData | None
        bounds = None
        if img_type == 0:
            data = CorporationMission.from_text(text, width, height)  # type: CorporationMission
            if data.isMission:
                valid = True
                logger.info("Detected CorporationMission from %s:%s: %s", message.author.id, message.author.name,
                            image_name)
        elif img_type == 1:
            dilation, img_lut = preprocess_donation(img, debug=debug)
            res = extract_text(dilation, img_lut, expansion=(4, 7, 12, 10))
            text = res[0]
            img_rect = res[1] if len(res) > 1 else None
            data = MemberDonation.from_text(text, width, height)  # type: MemberDonation
            bounds = data.bounding_box
            if data.is_donation:
                valid = True
                logger.info("Detected MemberDonation from %s (%s): %s", message.author.id, message.author.name,
                            image_name)
        else:
            logger.info("Could not handle image from %s (%s) %s", message.author.id, message.author.name, image_name)
            return_missions.append((message.author.id, author, OCRException("Image is not a mission/donation"), img_id))

        cropped = None
        if bounds is not None and img_rect is not None:
            cropped = img_rect[bounds["y1"]:bounds["y2"], bounds["x1"]:bounds["x2"]]
            data.img = cropped
        return_missions.append((channel, author, data, img_id, cropped))

        if os.path.exists(WORKING_DIR + "/image_rescaled_" + img_id + ".png"):
            os.remove(WORKING_DIR + "/image_rescaled_" + img_id + ".png")
        if not valid and not no_delete:
            logger.warning("Received image %s from %s:%s is not a mission/donation, deleting file.",
                           file_name, message.author.name, message.author.id)
            if os.path.exists(file_name):
                logger.info("Deleting image %s", file_name)
                os.remove(file_name)
    except Exception as e:
        logger.error("OCR job for image %s (user %s) failed!", img_id, message.author.id)
        utils.log_error(logger, e, in_class="handle_image")
        return_missions.append((message.author.id, author, e, img_id))
        if os.path.exists(file_name) and not no_delete:
            logger.info("Deleting image %s", file_name)
            os.remove(file_name)


@tasks.loop(seconds=3.0)
async def ocr_result_loop():
    with return_missions.lock:
        for i in range(len(return_missions.list)):
            if return_missions.list[i] is None:
                continue
            res = return_missions.list[
                i]  # type: Tuple[int, int, Union[CorporationMission, MemberDonation], str, ndarray]
            channel_id = res[0]
            author = res[1]
            data = res[2]
            img_id = res[3]
            img_cropped = res[4] if len(res) > 4 else None
            return_missions.list[i] = None
            user = await STATE.bot.get_or_fetch_user(author) if author is not None else None
            if not user and channel_id:
                channel = STATE.bot.get_channel(channel_id)
                if channel is None:
                    channel = await STATE.bot.fetch_channel(channel_id)
                if channel is None:
                    logger.error("Channel " + str(channel_id) + " from OCR result list not found!")
                    continue
            if isinstance(data, Exception):
                logger.error("OCR job for %s failed, img_id: %s, error: %s", author, img_id, str(data))
                await user.send("An error occurred: " + str(data))
                continue
            is_valid = False
            file = utils.image_to_file(img_cropped, ".jpg", "img_ocr_cropped.jpg")
            if isinstance(data, CorporationMission):
                msg = f"```\nIst Mission: {str(data.isMission)}\n" \
                      f"Gültig: {str(data.valid)}\nTitel: {data.title}\nNutzername: {data.username}\n" \
                      f"Main Char: {data.main_char}\nMenge: {str(data.amount)}\nErhalte ISK: {str(data.pay_isk)}" \
                      f"\nLimitiert: {str(data.has_limit)}\nLabel korrekt: {data.label}\n```\n"
                if not data.isMission:
                    msg += "**Fehler**: Das Bild ist keine Corpmission. Wenn es sich doch um eine handelt, *kontaktiere " \
                           "bitte einem Admin* und schicke ihm das Bild zu, damit die Bilderkennung verbessert werden kann.\n\n"
                if not data.label:
                    msg += "**Fehler**: Das Label wurde nicht erkannt. Für die Mission muss das Label \"Accounting\" " \
                           "ausgewählt werden.\n"
                if not data.has_limit:
                    msg += "**Fehler**: Das Limit wurde nicht erkannt. Bei der Mission muss ein \"Total Times\"-Limit " \
                           "eingestellt sein.\n"
                if not data.title or data.title == "Transfer":
                    msg += "**Fehler**: Der Titel wurde nicht erkannt. Er muss \"Einzahlung\" oder \"Auszahlung\" lauten.\n"
                if not data.main_char:
                    msg += "**Fehler**: Der Spielername wurde nicht erkannt.\n"
                if not data.amount:
                    msg += "**Fehler**: Die ISK-Menge wurde nicht erkannt.\n"
                if not data.valid:
                    msg += "\n**Fehlgeschlagen!** Die Mission ist nicht korrekt, bzw. es gab einen Fehler beim Einlesen. " \
                           "Wenn die Mission nicht korrekt erstellt wurde, lösche sie bitte und erstelle sie bitte " \
                           "entsprechend der Anleitung im Leitfaden neu. Wenn sie korrekt ist, aber nicht richtig erkannt" \
                           " wurde, so musst Du sie manuell im Accountinglog posten.\n" \
                           "Du kannst außerdem einen Admin informieren, damit die Bilderkennung verbessert werden kann.\n"
                else:
                    is_valid = True
            elif isinstance(data, MemberDonation):
                msg = f"```\nIst Spende: {str(data.is_donation)}\n" \
                      f"Gültig: {str(data.valid)}\n" \
                      f"Menge: {str(data.amount)}\n" \
                      f"Nutzer: {str(data.username)}\n" \
                      f"Main Char: {str(data.main_char)}\n" \
                      f"Zeit: {str(data.time)}\n```\n"
                if not data.is_donation:
                    msg += "**Fehler**: Das Bild enthält keine Spende\n"
                if not data.time:
                    msg += "**Fehler**: Die Zeit wurde nicht erkannt\n"
                if not data.main_char:
                    msg += "**Fehler**: Der Nutzername wurde nicht erkannt\n"
                if not data.amount:
                    msg += "**Fehler**: Die Menge wurde nicht erkannt\n"
                if not data.valid:
                    msg += "\n**Fehlgeschlagen**: Die Spende wurde nicht korrekt erkannt. Bitte sende nur unbearbeitete " \
                           "Screenshots (d.h. schneide das Bild bitte nicht passend zu). Wenn es sich um einen Fehler vom" \
                           "Bot handelt, poste die Spende manuell im Accountinglog.\n" \
                           "Du kannst außerdem einen Admin informieren, damit die Bilderkennung verbessert werden kann.\n"
                else:
                    is_valid = True
            else:
                msg = "Error, unknown result " + str(data)
                logger.error("OCR result loop received unknown data for user %s for image %s: %s",
                             author,
                             img_id,
                             data)

                if user is not None:
                    await user.send("Bild wurde verarbeitet: \n" + msg, file=file)
                continue

            transaction = Transaction.from_ocr(data, author)
            if user is not None:
                transaction.author = user.name
            if author is not None and isinstance(data, MemberDonation):
                char = utils.get_main_account(discord_id=author)[0]
                transaction.allow_self_verification = True
                if transaction.name_to != char:
                    msg += "Warnung: Diese Einzahlung stammt nicht von dir"
            if not transaction.is_valid():
                msg += "**Fehler**: Transaktion is nicht gültig.\n"
                is_valid = False
            if user is not None:
                logger.info("OCR job for image %s for user %s (%s) completed, valid: %s", img_id, user.name, author,
                            is_valid)
                await user.send("Bild wurde verarbeitet: \n" + msg, file=file)
            else:
                logger.warning("User for OCR image %s with discord ID %s not found!", img_id, author)
                continue
            if not is_valid:
                continue

            if transaction:
                ocr_view = ConfirmOCRView(transaction, img_cropped)
                msg = "Willst du diese Transaktion senden? "
                if isinstance(data, MemberDonation):
                    msg += "Stelle bitte auch sicher, dass die korrekte Zeit erkannt wurde. Die Zeit kannst Du einfach " \
                           "ändern, änderst du jedoch die Menge, so muss die Transaktion von einem Admin verifiziert werden.\n" \
                           "`Zeit: {}`".format(transaction.timestamp)
                await user.send(msg, view=ocr_view, embed=transaction.create_embed())

        while None in return_missions.list:
            return_missions.list.remove(None)


@ocr_result_loop.error
async def handle_ocr_error(error: Exception):
    utils.log_error(logger, error, in_class="ocr_result_loop")
    del error
    ocr_result_loop.restart()


class OCRException(Exception):
    pass
