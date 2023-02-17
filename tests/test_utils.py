import unittest

import cv2

from accounting_bot import utils


class UtilsTest(unittest.TestCase):
    def __init__(self, methodName: str = ...) -> None:
        super().__init__(methodName)

    def setup(self):
        utils.discord_users = {
            "UserA": 123456,
            "UserB": 42
        }
        utils.ingame_chars = [
            "UserA", "UserB", "UserD", "AltAaa", "AltB", "AltC"
        ]
        utils.ingame_twinks = {
            "AltAaa": "UserA",
            "AltB": "UserB"
        }

    def test_get_discord_id(self):
        self.setup()
        self.assertEqual(123456, utils.get_discord_id("UserA"))
        self.assertEqual(42, utils.get_discord_id("UserB"))
        self.assertEqual(None, utils.get_discord_id("UserC"))

    def test_test_get_main_account(self):
        self.setup()
        self.assertEqual(("UserA", "UserA", True), utils.get_main_account(discord_id=123456))
        self.assertEqual((None, None, False), utils.get_main_account(discord_id=123))
        self.assertEqual(("UserA", "UserA", True), utils.get_main_account(name="UserA"))
        self.assertEqual(("UserB", "UserB", False), utils.get_main_account(name="UserBB"))
        self.assertEqual(("UserA", "AltAaa", False), utils.get_main_account(name="Altaaaa"))
        self.assertEqual(("UserB", "AltB", True), utils.get_main_account(name="AltB"))
        self.assertEqual((None, None, False), utils.get_main_account(name="Ajklkhhiosda"))

    def test_parse_player(self):
        self.setup()
        self.assertEqual(("AltAaa", False), utils.parse_player("Altbaa", utils.ingame_chars))
        self.assertEqual(("UserB", True), utils.parse_player("UserB", utils.ingame_chars))
        self.assertEqual((None, False), utils.parse_player("adfhjwrjtj", utils.ingame_chars))

    def test_list_to_string(self):
        self.setup()
        self.assertMultiLineEqual("Abc\ndef\n123\n", utils.list_to_string(["Abc", "def", "123"]))
        self.assertEqual("", utils.list_to_string([]))
        self.assertEqual("\n", utils.list_to_string([""]))

    def test_image_to_file(self):
        img = cv2.imread("img_donation_en.png")
        file = utils.image_to_file(img, ".jpg", "result.jpg")
        self.assertIsNotNone(file)


if __name__ == '__main__':
    unittest.main()
