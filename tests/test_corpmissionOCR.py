import logging
import unittest

import pytesseract

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

    def test_handle_image(self):
        utils.ingame_chars = ["Blaumeise03", "Blaumeise04"]
        self.assertEqual(0, len(corpmissionOCR.return_missions.list))
        user = TestUser(2, "TestUser")
        msg = TestMessage(1, user)

        corpmissionOCR.handle_image("https://url.blaumeise03.de/AccTestMissionDE", "png", msg, 2, user)
        # noinspection DuplicatedCode
        self.assertEqual(1, len(corpmissionOCR.return_missions.list))
        channel, author, mission, img_id = corpmissionOCR.return_missions.list[0]  # type: object, object, corpmissionOCR.CorporationMission, str
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

        corpmissionOCR.handle_image("https://url.blaumeise03.de/AccTestMissionEN", "png", msg, 2, user)
        # noinspection DuplicatedCode
        self.assertEqual(1, len(corpmissionOCR.return_missions.list))
        channel, author, mission, img_id = corpmissionOCR.return_missions.list[0]  # type: object, object, corpmissionOCR.CorporationMission, str
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
