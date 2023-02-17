import logging
import unittest
from datetime import datetime

import pytesseract
from numpy import ndarray

from accounting_bot import corpmissionOCR, utils

# Change path if necessary
pytesseract.pytesseract.tesseract_cmd = "tesseract"


class TestState:
    def __init__(self) -> None:
        self.ocr = True

    def is_online(self):
        return True


class TestUser:
    def __init__(self, user_id, name):
        self.id = user_id
        self.name = name


class TestMessage:
    def __init__(self, msg_id, user):
        self.id = msg_id
        self.author = user


class CorpmissionOCRTest(unittest.TestCase):
    def __init__(self, methodName: str = ...) -> None:
        super().__init__(methodName)
        tesseract_version = pytesseract.pytesseract.get_tesseract_version()
        logging.info("Tesseract version " + str(tesseract_version) + " installed!")
        corpmissionOCR.STATE = TestState()

    def test_donation(self):
        utils.ingame_chars = ["Blaumeise03", "Blaumeise04"]
        utils.main_chars = ["Blaumeise03"]
        utils.ingame_twinks = {"Blaumeise04": "Blaumeise03"}
        user = TestUser(2, "TestUser")
        msg = TestMessage(1, user)

        corpmissionOCR.return_missions.list.clear()
        corpmissionOCR.handle_image("", "png", msg, 2, user, "img_donation_en.png", no_delete=True, debug=True)
        # noinspection DuplicatedCode
        channel, author, donation, img_id, img = corpmissionOCR.return_missions.list[0]  # type: object, object, corpmissionOCR.MemberDonation, str, ndarray
        self.assertEqual(corpmissionOCR.MemberDonation, donation.__class__)
        self.assertEqual(500000000, donation.amount)
        self.assertEqual("Blaumeise03", donation.main_char)
        self.assertEqual("Blaumeise03", donation.username)
        self.assertEqual(datetime.strptime("2023-02-15 16:43:24", "%Y-%m-%d %H:%M:%S"), donation.time)
        self.assertTrue(donation.is_donation)
        self.assertTrue(donation.valid)
        self.assertIsNotNone(img)

        corpmissionOCR.return_missions.list.clear()
        corpmissionOCR.handle_image("", "png", msg, 2, user, "img_donation_de.png", no_delete=True, debug=True)
        # noinspection DuplicatedCode
        channel, author, donation, img_id, img = corpmissionOCR.return_missions.list[0]  # type: object, object, corpmissionOCR.MemberDonation, str, ndarray
        self.assertEqual(corpmissionOCR.MemberDonation, donation.__class__)
        self.assertEqual(500000000, donation.amount)
        self.assertEqual("Blaumeise03", donation.main_char)
        self.assertEqual("Blaumeise03", donation.username)
        self.assertEqual(datetime.strptime("2023-02-15 16:43:24", "%Y-%m-%d %H:%M:%S"), donation.time)
        self.assertTrue(donation.is_donation)
        self.assertTrue(donation.valid)
        self.assertIsNotNone(img)

    def test_corpmission(self):
        utils.ingame_chars = ["Blaumeise03", "Blaumeise04"]
        corpmissionOCR.return_missions.list.clear()
        user = TestUser(2, "TestUser")
        msg = TestMessage(1, user)

        corpmissionOCR.handle_image("", "png", msg, 2, user, "img_mission_de.png")
        # noinspection DuplicatedCode
        self.assertEqual(1, len(corpmissionOCR.return_missions.list))
        channel, author, mission, img_id, img = corpmissionOCR.return_missions.list[0]  # type: object, object, corpmissionOCR.CorporationMission, str, ndarray
        self.assertEqual(corpmissionOCR.CorporationMission, mission.__class__)
        self.assertEqual(2, channel)
        self.assertEqual(user, author)
        self.assertEqual(True, mission.isMission)
        self.assertEqual(500000000, mission.amount)
        self.assertEqual(True, mission.pay_isk)
        self.assertEqual(True, mission.has_limit)
        self.assertEqual("Blaumeise03", mission.main_char)
        self.assertEqual("Blaumeise03", mission.username)
        self.assertEqual("Auszahlung", mission.title)
        self.assertEqual(True, mission.label)
        self.assertEqual(True, mission.valid)
        corpmissionOCR.return_missions.list.clear()
        self.assertEqual(0, len(corpmissionOCR.return_missions.list))

        corpmissionOCR.handle_image("", "png", msg, 2, user, "img_mission_en.png")
        # noinspection DuplicatedCode
        self.assertEqual(1, len(corpmissionOCR.return_missions.list))
        channel, author, mission, img_id, img = corpmissionOCR.return_missions.list[0]  # type: object, object, corpmissionOCR.CorporationMission, str, ndarray
        self.assertEqual(corpmissionOCR.CorporationMission, mission.__class__)
        self.assertEqual(2, channel)
        self.assertEqual(user, author)
        self.assertEqual(True, mission.isMission)
        self.assertEqual(2000000000, mission.amount)
        self.assertEqual(False, mission.pay_isk)
        self.assertEqual(True, mission.has_limit)
        self.assertEqual("Blaumeise03", mission.main_char)
        self.assertEqual("Blaumeise03", mission.username)
        self.assertEqual("Einzahlung", mission.title)
        self.assertEqual(True, mission.label)
        self.assertEqual(True, mission.valid)
        corpmissionOCR.return_missions.list.clear()


if __name__ == '__main__':
    unittest.main()
