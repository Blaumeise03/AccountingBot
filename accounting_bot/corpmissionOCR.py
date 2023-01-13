import difflib
import logging
import queue
import random
import re
import shutil
import string
import threading
import time

import cv2
import numpy
import requests
from PIL import Image
from pytesseract import pytesseract

logger = logging.getLogger("bot.projects")
WORKING_DIR = "images"
OCR_ENABLED = False


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
    if not OCR_ENABLED:
        raise OCRException("OCR is not enabled!")
    contours, hierarchy = cv2.findContours(dilation, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_NONE)
    im2 = image
    height, width = im2.shape
    rect = im2.copy()
    print(im2.shape)
    result = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < 10 or w < 5 or w > 300 or h > 100:
            continue
        # print(f"Original: ({x}, {y}), ({x + w}, {y + h}), size: {w}x{h}")
        x = max(0, x - 6)
        y = max(0, y - 6)
        w = min(w + 5, width - (x + w))
        h = min(h + 5, height - (y + h))
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
        self.valid = False  # type: bool
        self.title = None  # type: str  | None
        self.username = None  # type: str  | None
        self.pay_isk = None  # type: bool | None
        self.amount = None  # type: int  | None
        self.has_limit = False  # type: bool

    def validate(self):
        self.valid = False
        if self.amount and self.has_limit and self.title:
            title = self.title.casefold()
            if title == "Einzahlung".casefold() and not self.pay_isk:
                self.valid = True
            if title == "Auszahlung".casefold() and self.pay_isk:
                self.valid = True
            if title == "Transfer".casefold():
                self.valid = True

    @staticmethod
    def from_text(text: ({int}, str), width, height, usernames):
        mission = CorporationMission()
        isk_get_lines = []
        isk_pay_lines = []
        title_line = None

        # Find line for ISK
        for cords, t in text:  # type: (dict, str)
            rel_cords = to_relative_cords(cords, width, height)
            # Get transaction direction and y-level of the ISK quantity
            pay = difflib.SequenceMatcher(None, "Corporation pays", t).ratio()
            get = difflib.SequenceMatcher(None, "Corporation erhält", t).ratio()
            if rel_cords["x1"] < 0.3 and (pay > 0.8 or get > 0.8):
                if pay > get:
                    isk_pay_lines.append((rel_cords["y1"] + rel_cords["y2"]) / 2)
                elif get > pay:
                    isk_get_lines.append((rel_cords["y1"] + rel_cords["y2"]) / 2)
                continue

            # Check the "Total Times"-Setting
            if rel_cords["x1"] > 0.45 and max(difflib.SequenceMatcher(None, "Total Times", t).ratio(),
                                              difflib.SequenceMatcher(None, "Gesamthäufigkeit", t).ratio()) > 0.7:
                mission.has_limit = True
                continue

            # Get the title
            matches = difflib.get_close_matches(t, ["Transfer", "Einzahlung", "Auszahlung"], 1)
            if rel_cords["x1"] < 0.3 and rel_cords["y1"] < 0.3 and len(matches) > 0:
                mission.title = str(matches[0])
                title_line = (rel_cords["y1"] + rel_cords["y2"]) / 2
                continue

        if len(isk_pay_lines) > 0 and len(isk_get_lines) > 0:
            raise Exception("Found both pay and get lines!")

        mission.pay_isk = len(isk_pay_lines) > 0
        mission.amount = None
        isk_lines = isk_pay_lines if mission.pay_isk else isk_get_lines

        # Get isk
        for cords, txt in text:  # type: dict, str
            rel_cords = to_relative_cords(cords, width, height)
            rel_y = (rel_cords["y1"] + rel_cords["y2"]) / 2
            best_line = None

            for l in isk_lines:
                if best_line is None or (abs(rel_y - l) < abs(rel_y - best_line)):
                    best_line = l

            if title_line and rel_cords["x1"] > 0.6 and abs(rel_y - title_line) < 0.05:
                name_raw = txt.split("\n")[0].strip()
                if usernames is not None:
                    matches = difflib.get_close_matches(name_raw, usernames, 1)
                    if len(matches) > 0:
                        mission.username = matches[0]
                else:
                    mission.username = name_raw
                continue

            if abs(rel_y - best_line) < 0.05 and rel_cords["x1"] < 0.66:
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


def handle_image(url, content_type, message):
    try:
        img_id = "".join(random.choice(string.ascii_uppercase) for i in range(10))
        res = requests.get(url, stream=True)
        logging.info("Received image (" + content_type + "), ID " + img_id)
        file_name = "images/download/" + str(message.author.id) + "_" + img_id + "." + content_type.replace("image/",
                                                                                                            "")
        if res.status_code == 200:
            with open(file_name, "wb") as f:
                shutil.copyfileobj(res.raw, f)
            logging.info("Image successfully downloaded from %s (%s): %s", message.author.name, message.author.id,
                         file_name)
        else:
            logging.info("Image successfully downloaded from %s (%s): %s", message.author.name, message.author.id,
                         file_name)

        image = Image.open(file_name)
        image.thumbnail((1000, 4000), Image.LANCZOS)
        image.save("image_rescaled.png", "PNG")
        img = cv2.imread("image_rescaled.png")
        dilation, img_lut = preprocess_text(img, debug=True)
        text = extract_text(dilation, img_lut)
        height, width, _ = img.shape
        mission = CorporationMission.from_text(text, width, height, None)
        return_missions.append((message.author.id, mission))
    except Exception as e:
        logging.error("OCR job failed!")
        logging.exception(e)
        return_missions.append((message.author.id, e))


class OCRException(Exception):
    pass