import unittest
from typing import List

from sqlalchemy.orm import Session

from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.universe.models import System, Celestial
from accounting_bot.universe.universe_database import UniverseDatabase

# noinspection DuplicatedCode
config_structure = {
    "db": {
        "user": (str, "N/A"),
        "password": (str, "N/A"),
        "port": (int, -1),
        "host": (str, "N/A"),
        "name": (str, "N/A"),
        "universe_name": (str, "N/A")
    },
    "google_sheet": (str, "N/A"),
    "project_resources": (list, [],),
    "pytesseract_cmd_path": (str, "N/A"),
}


class PlanetaryProductionTest(unittest.TestCase):
    config = None  # type: Config | None
    connector = None  # type: DatabaseConnector | None

    # noinspection DuplicatedCode
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = Config("test_config.json", ConfigTree(config_structure))
        cls.config.load_config()
        cls.connector = DatabaseConnector(
            username=cls.config["db.user"],
            password=cls.config["db.password"],
            port=cls.config["db.port"],
            host=cls.config["db.host"],
            database=cls.config["db.universe_name"]
        )

    def setUp(self) -> None:
        self.config = PlanetaryProductionTest.config
        self.connector = PlanetaryProductionTest.connector
        self.db = UniverseDatabase(
            username=PlanetaryProductionTest.config["db.user"],
            password=PlanetaryProductionTest.config["db.password"],
            port=PlanetaryProductionTest.config["db.port"],
            host=PlanetaryProductionTest.config["db.host"],
            database=PlanetaryProductionTest.config["db.universe_name"]
        )

    def test_system(self):
        with Session(self.db.engine) as conn:
            # logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
            with self.assertLogs("sqlalchemy.engine", level="INFO") as cm:
                # SQLAlchemy should fetch only the system
                system = conn.query(System).filter(System.name.like("IAK-JW")).first()
            self.assertGreaterEqual(len(cm.output), 3)
            self.assertIsNotNone(system)
            with self.assertNoLogs("sqlalchemy.engine", level="INFO"):
                # Accessing the basic data should not trigger a SQL query
                self.assertEqual(30000709, system.id)
                self.assertEqual("IAK-JW", system.name)
                self.assertEqual(20000104, system.constellation_id)
                self.assertEqual(10000008, system.region_id)
            with self.assertLogs("sqlalchemy.engine", level="INFO") as cm:
                # Accessing the Constellation object should trigger a SQL query
                self.assertEqual("WQZ8-4", system.constellation.name)
            self.assertGreaterEqual(len(cm.output), 2)
            with self.assertLogs("sqlalchemy.engine", level="INFO") as cm:
                # Same with accessing the Region object of the constellation
                self.assertEqual("Scalding Pass", system.constellation.region.name)
            self.assertGreaterEqual(len(cm.output), 2)
            with self.assertLogs("sqlalchemy.engine", level="INFO") as cm:
                # Accessing the hybrid_property should trigger a SQL query to load all celestials
                # noinspection PyTypeChecker
                planets = system.planets  # type: List[Celestial]
            self.assertGreaterEqual(len(cm.output), 2)
            self.assertEqual(9, len(planets))

    def test_stargates(self):
        with Session(self.db.engine) as conn:
            with self.assertLogs("sqlalchemy.engine", level="INFO") as cm:
                system = conn.query(System).filter(System.name.like("IAK-JW")).first()
                # noinspection PyTypeChecker
                gates = system.stargates  # type: List[Celestial]
                gates_names = map(lambda s: s.name, gates)
            self.assertGreaterEqual(len(cm.output), 2)
            self.assertEqual(3, len(gates))
            self.assertCountEqual(gates_names, ["KZFV-4", "RYC-19", "WO-GC0"])


if __name__ == '__main__':
    unittest.main()
