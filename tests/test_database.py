import unittest

import mariadb

from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector

config_structure = {
    "db": {
        "user": (str, "N/A"),
        "password": (str, "N/A"),
        "port": (int, -1),
        "host": (str, "N/A"),
        "name": (str, "N/A")
    },
    "google_sheet": (str, "N/A"),
    "project_resources": (list, [],),
    "pytesseract_cmd_path": (str, "N/A"),
}


class DatabaseTest(unittest.TestCase):
    config = None  # type: Config | None
    connector = None  # type: DatabaseConnector | None

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = Config("test_config.json", ConfigTree(config_structure))
        cls.config.load_config()
        db_name = cls.config["db.name"]  # type: str
        if not "test".casefold() in db_name.casefold():
            raise Exception(f"Database {db_name} is probably not a testdatabase, test evaluation canceled.")
        cls.connector = DatabaseConnector(
            username=cls.config["db.user"],
            password=cls.config["db.password"],
            port=cls.config["db.port"],
            host=cls.config["db.host"],
            database=cls.config["db.name"]
        )

    def setUp(self) -> None:
        self.config = DatabaseTest.config
        self.connector = DatabaseTest.connector
        # noinspection SqlWithoutWhere
        self.connector.cursor.execute("DELETE FROM messages")
        # noinspection SqlWithoutWhere
        self.connector.cursor.execute("DELETE FROM shortcuts")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connector.con.close()

    def test_transactions(self):
        con = self.connector
        # Database should be empty
        self.assertEqual(0, len(con.get_unverified()))
        # Adding new transaction with message 1234 and user 5678
        con.add_transaction(1234, 5678)
        # Verifying correct insertion
        self.assertEqual(1, len(con.get_unverified()))
        self.assertEqual((5678, False), con.get_owner(1234))
        self.assertEqual(True, con.is_unverified_transaction(1234))
        # Setting state of transaction, it should return the amount of affected rows (=1)
        self.assertEqual(1, con.set_state(1234, 3))
        self.assertEqual(3, con.get_state(1234))
        self.assertEqual(None, con.get_owner(1234242))
        # Same for the verification
        self.assertEqual(1, con.set_verification(1234, True))
        self.assertEqual(0, len(con.get_unverified()))
        self.assertEqual(False, con.is_unverified_transaction(1234))
        self.assertEqual(None, con.is_unverified_transaction(1331))
        # Deleting transaction
        with self.assertLogs("database", level="INFO") as cm:
            con.delete(1234)
            con.delete(1234)
        self.assertEqual(["WARNING:database:Deletion of message 1234 affected 0 rows, expected was 1 row"], cm.output)
        con.add_transaction(1234, 5678)
        con.add_transaction(4321, 5678)
        con.add_transaction(1111, 42)
        with self.assertRaises(mariadb.Error) as cm:
            con.add_transaction(1234, 5678)
        self.assertEqual(3, len(con.get_unverified()))
        con.set_verification(4321, True)
        res = con.get_unverified()
        self.assertEqual(2, len(res))
        self.assertTrue(1234 in res)
        self.assertFalse(4321 in res)
        self.assertTrue(1111 in res)
        res = con.get_unverified(include_user=True)
        self.assertEqual(2, len(res))
        self.assertTrue((1234, 5678) in res)
        self.assertTrue((1111, 42) in res)

    def test_shortcuts(self):
        con = self.connector
        # Database should be empty
        self.assertEqual(0, len(con.get_shortcuts()))
        with self.assertLogs("database", level="INFO") as cm:
            con.add_shortcut(4242, 1234)
            self.assertEqual(1, len(con.get_shortcuts()))
            con.add_shortcut(123, 9999)
        self.assertEqual(["INFO:database:Inserted shortcut message 4242, affected 1 rows",
                          "INFO:database:Inserted shortcut message 123, affected 1 rows"], cm.output)
        res = con.get_shortcuts()
        self.assertEqual(2, len(res))
        self.assertTrue((123, 9999) in res)
        self.assertTrue((4242, 1234) in res)
        with self.assertRaises(mariadb.Error) as cm:
            con.add_shortcut(123, 9999)
        with self.assertLogs("database", level="INFO") as cm:
            con.delete_shortcut(123)
            con.delete_shortcut(4242)
            con.delete_shortcut(123)
        self.assertEqual(["INFO:database:Deleted shortcut message 123, affected 1 rows",
                          "INFO:database:Deleted shortcut message 4242, affected 1 rows",
                          "WARNING:database:Deletion of shortcut message 123 affected 0 rows, expected was 1 row"], cm.output)
        self.assertEqual(0, len(con.get_shortcuts()))

    # noinspection PyTypeChecker
    def test_errors(self):
        con = self.connector
        con.add_transaction(123, 33)
        with self.assertRaises(mariadb.Error):
            con.set_state(123, "abc")
        with self.assertRaises(mariadb.Error):
            con.set_verification(123, "abc")
        with self.assertRaises(mariadb.Error):
            con.add_shortcut("", 3)
        con.delete(123)


if __name__ == '__main__':
    unittest.main()
