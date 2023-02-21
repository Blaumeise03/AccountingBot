import asyncio
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
from typing import TYPE_CHECKING, Union, Optional, Tuple, Callable, Dict, List, Any

import cv2
import numpy
import numpy as np
import requests
from PIL import Image
from discord import Message
from discord.ext import tasks
from numpy import ndarray
from pytesseract import pytesseract

from accounting_bot import utils, accounting, ocr_utils
from accounting_bot.accounting import Transaction, ConfirmOCRView
from accounting_bot.exceptions import BotOfflineException
from accounting_bot.ocr_utils import ThreadSafeList, to_relative_cords, get_center, is_within_bounds, \
    CorporationMission, MemberDonation, OCRException
from accounting_bot.utils import TransactionLike, OCRBaseData

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.ocr")
WORKING_DIR = "images"
accounting.IMG_WORKING_DIR = WORKING_DIR
STATE = None  # type: BotState | None
Path(WORKING_DIR + "/download").mkdir(parents=True, exist_ok=True)
Path(WORKING_DIR + "/transactions").mkdir(parents=True, exist_ok=True)

return_missions = ThreadSafeList()


def apply_dilation(img: ndarray,
                   block_size: int,
                   c: int,
                   rect: Tuple[int, int],
                   iterations: int,
                   debug=False) -> ndarray:

    # Apply threshold
    thresh = cv2.adaptiveThreshold(img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block_size, c)

    # Filter out horizontal and vertical lines of threshold
    thresh_h = cv2.threshold(thresh, 0, 255, cv2.THRESH_OTSU)[1]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 25))
    detected_h_lines = cv2.morphologyEx(thresh_h, cv2.MORPH_OPEN, horizontal_kernel, iterations=2)
    detected_v_lines = cv2.morphologyEx(thresh_h, cv2.MORPH_OPEN, vertical_kernel, iterations=2)
    detected_lines = detected_h_lines + detected_v_lines
    np.clip(detected_lines, 0, 255, out=detected_lines)
    cnts = cv2.findContours(detected_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = cnts[0] if len(cnts) == 2 else cnts[1]
    for cnt in cnts:
        cv2.drawContours(thresh, [cnt], -1, (0, 0, 0), 2)

    # Filter out noise (single dots)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 2))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)

    if debug:
        cv2.imwrite(WORKING_DIR + '/image_threshold.jpg', thresh)

    # Dilation of text areas
    rect_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, rect)
    dilation = cv2.dilate(thresh, rect_kernel, iterations=iterations)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_dilation.jpg', dilation)
    return dilation


def apply_lut(lut_in: List[int], lut_out: List[int], img: ndarray) -> ndarray:
    lut_8u = numpy.interp(numpy.arange(0, 256), lut_in, lut_out).astype(numpy.uint8)
    return cv2.LUT(img, lut_8u)


def preprocess_mission(img: ndarray, rect=(2, 2), debug=False) -> Tuple[ndarray, ndarray]:
    # Greyscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = (255 - gray)
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_grey.jpg', gray)

    # Color curve correction to improve text
    img_lut = apply_lut(
        lut_in=[0, 91, 144, 226, 255],
        lut_out=[0, 0, 30, 255, 255],
        img=gray
    )
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_LUT.jpg', img_lut)
    dilation = apply_dilation(
        img=gray,
        block_size=11,
        c=9,
        rect=rect,
        iterations=6,
        debug=debug
    )
    return dilation, img_lut


def preprocess_donation(img: ndarray, rect=(3, 1), debug=False) -> Tuple[ndarray, ndarray]:
    img_b, img_g, img_r = cv2.split(img)
    # Dilation image for text area detection
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = 255 - gray
    img_lut_thresh = apply_lut(
        lut_in=[0, 91, 144, 226, 255],
        lut_out=[0, 91, 144, 226, 255],
        img=gray
    )
    dilation = apply_dilation(
        img=img_lut_thresh,
        block_size=7,
        c=10,
        rect=rect,
        iterations=6,
        debug=debug
    )

    # Image for OCR with improved readability
    #img_lut = apply_lut(
    #    lut_in=[0, 59, 130, 255],
    #    lut_out=[0, 43, 217, 255],
    #    img=img
    #)
    img_b_lut = apply_lut(
        lut_in=[0, 54, 86, 255],
        lut_out=[0, 17, 153, 255],
        img=img_b
    )
    img_g_lut = apply_lut(
        lut_in=[0, 44, 73, 255],
        lut_out=[0, 8, 85, 255],
        img=img_g
    )
    img_r_lut = apply_lut(
        lut_in=[0, 59, 144, 255],
        lut_out=[0, 31, 239, 255],
        img=img_r
    )
    img_lut = cv2.merge([img_b_lut, img_g_lut, img_r_lut])
    if debug:
        cv2.imwrite(WORKING_DIR + '/image_LUT.jpg', img_lut)
    return dilation, img_lut


def extract_text(dilation: ndarray,
                 image: ndarray,
                 postfix: str = "",
                 expansion: Tuple[int, int, int, int] = (9, 9, 8, 8),
                 skip_donations: bool = False,
                 rel_bounds: Dict[str, int] = None
                 ) -> Tuple[List[Tuple[Dict[str, int], str]], ndarray]:
    """
    Extracts all text of an image

    :param dilation: the dilation image
    :param image: the image to extract text from
    :param postfix: additional file ending for processed image
    :param expansion: number of pixels to expand the cropped image (left, up, right, down)
    :param skip_donations: method stops once it recognizes a CorporationDonation
    :param rel_bounds: the area of the image that contains relevant text (or None to process whole image), in relative coordinates
    :return: the recognized text with the absolute coordinates and the image with rectangles around the recognized text
    """
    if not STATE.ocr:
        raise OCRException("OCR is not enabled!")
    contours, hierarchy = cv2.findContours(dilation, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_NONE)
    im2 = image
    height = im2.shape[0]
    width = im2.shape[1]
    rect = im2.copy()
    result = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if h < 10 or w < 5 or w > 450 or h > 100:
            continue
        x = max(0, x - expansion[0])
        y = max(0, y - expansion[1])
        w = min(w + expansion[2], width - (x + w))
        h = min(h + expansion[3], height - (y + h))

        cords = {
            "x1": x,
            "x2": x + w,
            "y1": y,
            "y2": y + h
        }
        rel_cords = to_relative_cords(cords, width, height)
        cx, cy = get_center(rel_cords)
        if rel_bounds is not None and not is_within_bounds(bounds=rel_bounds, x=cx, y=cy):
            continue
        # Crop the bounding box area
        cropped = im2[y:y + h, x:x + w]

        # Using tesseract on the cropped image to get the text
        text = pytesseract.image_to_string(
            image=cropped
        )  # type: str
        text = text.strip()

        if len(text) == 0:
            continue
        # Draw the bounding box on around the text area
        rect = cv2.rectangle(rect,
                             (x, y),
                             (x + w, y + h),
                             (0, 255, 0),
                             2)
        cv2.imwrite(WORKING_DIR + f"/image_rectanglebox{postfix}.jpg", rect)
        result.append((
            {"x1": x, "x2": x + w, "y1": y, "y2": y + h},
            text
        ))

        if skip_donations and rel_cords["x1"] < 0.2 and match_donation(text) > 0.75:
            with open(WORKING_DIR + f"/image_text{postfix}.txt", mode="w", encoding="utf-8") as fp:
                for c, t in result:
                    # write each item on a new line
                    fp.write("{}:{}\n".format(c, t))
            return result, rect

    with open(WORKING_DIR + f"/image_text{postfix}.txt", 'w') as fp:
        for c, t in result:
            # write each item on a new line
            fp.write("{}:{}\n".format(c, t))
    return result, rect


def match_donation(text: str) -> float:
    """
    Matches a given string against common strings of Member Donations.

    :param text: the text to compare
    :return: the matched percentage
    """
    split = text.split(" ")
    alt_text = None
    if len(split) > 2:
        alt_text = split[0] + split[1]
    return max(difflib.SequenceMatcher(None, "Member Donation", text).ratio(),
               difflib.SequenceMatcher(None, "Member Donation", alt_text).ratio() if alt_text else 0,
               difflib.SequenceMatcher(None, "Donation", text).ratio(),
               difflib.SequenceMatcher(None, "Mitgliederspende", text.split(" ")[0]).ratio())


def get_image_type(text: List[Tuple[Dict[str, int], str]], width: int, height: int) -> int:
    """
    Checks if the image is a donation or a mission (or none of both).

    :param text: the recognized text as coordinates-text pairs
    :param width: the width of the image
    :param height: the height of the image
    :return: 1 if it's a donation, 0 for a mission and -1 if none of both
    """
    m_mission = 0
    m_donation = 0
    for cords, txt in text:  # type: dict[str: int], str
        rel_cords = to_relative_cords(cords, width, height)
        m_mission = max(m_mission,
                        difflib.SequenceMatcher(None, "MISSION DETAILS", txt).ratio(),
                        difflib.SequenceMatcher(None, "MISSION", txt).ratio(),
                        difflib.SequenceMatcher(None, "MISSIONSDETAILS", txt).ratio())
        if rel_cords["x1"] < 0.2:
            m_donation = max(m_donation, match_donation(txt))
    if m_donation > 0.75:
        return 1
    if m_mission > 0.75:
        return 0
    return -1


def handle_image(url: str,
                 content_type: str,
                 message: Message,
                 channel: int,
                 author: int,
                 file: Optional[str] = None,
                 no_delete: bool = False,
                 debug: bool = True) -> None:
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

        # image = Image.open(file_name)
        # image.thumbnail((1500, 4000), Image.LANCZOS)
        # image.save(WORKING_DIR + "/image_rescaled_" + img_id + ".png", "PNG")
        # img = cv2.imread(WORKING_DIR + "/image_rescaled_" + img_id + ".png")

        resolutions = [
            # res, (l, u, r, d), (l, u, r, d)
            (1500, (9, 9, 8, 8), (4, 7, 12, 9), (2, 2), (3, 1)),
            (2000, (10, 9, 9, 8), (5, 7, 13, 10), (2, 2), (4, 2)),
            (2500, (11, 9, 10, 8), (6, 7, 13, 10), (3, 2), (4, 2)),
                       ]

        img = cv2.imread(file_name)
        height, width, _ = img.shape
        exp_mission = None
        exp_donation = None
        dil_rect_mission = None
        dil_rect_donation = None
        for res, ex1, ex2, r1, r2 in resolutions:
            if res is None or width < res:
                exp_mission = ex1
                exp_donation = ex2
                dil_rect_mission = r1
                dil_rect_donation = r2
        dilation, img_lut = preprocess_mission(img, rect=dil_rect_mission, debug=debug)
        res = extract_text(
            dilation,
            img_lut,
            expansion=exp_mission,
            rel_bounds={"x1": 0.1, "x2": 0.9, "y1": 0, "y2": 1},
            skip_donations=True)
        text = res[0]
        img_rect = res[1] if len(res) > 1 else None

        img_type = get_image_type(text, width, height)
        valid = False
        data = None  # type: OCRBaseData | None
        bounds = None
        if img_type == 0:
            data = CorporationMission.from_text(text, width, height)  # type: CorporationMission
            if data.isMission:
                valid = True
                logger.info("Detected CorporationMission from %s:%s: %s", message.author.name, message.author.id,
                            image_name)
        elif img_type == 1:
            dilation, img_lut = preprocess_donation(img, rect=dil_rect_donation, debug=debug)
            res = extract_text(
                dilation,
                img_lut,
                expansion=exp_donation,
                rel_bounds={"x1": 0, "x2": 0.3, "y1": 0, "y2": 0.7}
            )
            text = res[0]
            img_rect = res[1] if len(res) > 1 else None
            data = MemberDonation.from_text(text, width, height)  # type: MemberDonation
            bounds = data.bounding_box
            if data.is_donation:
                valid = True
                logger.info("Detected MemberDonation from %s (%s): %s", message.author.name, message.author.id,
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
        utils.log_error(logger, e, location="handle_image")
        return_missions.append((message.author.id, author, e, img_id))
        if os.path.exists(file_name) and not no_delete:
            logger.info("Deleting image %s", file_name)
            os.remove(file_name)


async def handle_orc_result(res: Tuple[int, int, Union[CorporationMission, MemberDonation], str, Optional[ndarray]]) -> None:
    channel_id = res[0]
    author = res[1]
    data = res[2]
    img_id = res[3]
    img_cropped = res[4] if len(res) > 4 else None
    user = await STATE.bot.get_or_fetch_user(author) if author is not None else None
    if not user and channel_id:
        channel = STATE.bot.get_channel(channel_id)
        if channel is None:
            channel = await STATE.bot.fetch_channel(channel_id)
        if channel is None:
            logger.error("Channel " + str(channel_id) + " from OCR result list not found!")
            return
    if isinstance(data, Exception):
        logger.error("OCR job for %s failed, img_id: %s, error: %s", author, img_id, str(data))
        await user.send("An error occurred: " + str(data))
        return
    is_valid = False
    file = utils.image_to_file(img_cropped, ".jpg", "img_ocr_cropped.jpg")
    if isinstance(data, OCRBaseData):
        msg = ocr_utils.get_message(data)
        if data.valid:
            is_valid = True
    else:
        msg = "Error, unknown result " + str(data)
        logger.error("OCR result loop received unknown data for user %s for image %s: %s",
                     author,
                     img_id,
                     data)

        if user is not None:
            await user.send("Bild wurde verarbeitet: \n" + msg, file=file)
        return
    if not isinstance(data, TransactionLike):
        raise TypeError(f"TransactionLike data expected, got instead {str(type(data))}")
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
        return
    if not is_valid:
        return

    if transaction:
        ocr_view = ConfirmOCRView(transaction, img_cropped)
        msg = "Willst du diese Transaktion senden? "
        if isinstance(data, MemberDonation):
            msg += "Stelle bitte auch sicher, dass die korrekte Zeit erkannt wurde. Die Zeit kannst Du einfach " \
                   "ändern, änderst du jedoch die Menge, so muss die Transaktion von einem Admin verifiziert werden.\n" \
                   "`Zeit: {}`".format(transaction.timestamp)
        await user.send(msg, view=ocr_view, embed=transaction.create_embed())


@tasks.loop(seconds=3.0)
async def ocr_result_loop():
    try:
        with return_missions.lock:
            for i in range(len(return_missions.list)):
                if return_missions.list[i] is None:
                    continue
                res = return_missions.list[i]
                return_missions.list[i] = None
                await handle_orc_result(res)

            while None in return_missions.list:
                return_missions.list.remove(None)
    except Exception as e:
        utils.log_error(logger, e, location="ocr_result_loop")
