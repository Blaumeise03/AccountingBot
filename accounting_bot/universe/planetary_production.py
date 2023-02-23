import logging
import math
import multiprocessing
import os
import sys
from multiprocessing.pool import ThreadPool
from typing import Optional

from sqlalchemy import create_engine, Table, MetaData, Column, Integer, String, text
from sqlalchemy.orm import Session

from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import PlanetaryProductionException
from accounting_bot.universe.models import Region, Constellation, Celestial, Item, Resource, Richness, System

logger = logging.getLogger("bot.pi")


class PlanetaryDatabase:
    def __init__(self, db: DatabaseConnector) -> None:
        super().__init__()
        self.engine = create_engine(f"mariadb+mariadbconnector://"
                                    f"{db.username}:{db.password}@{db.host}:{db.port}/{db.database}"
                                    )
        Region.__table__.create(bind=self.engine, checkfirst=True)
        Constellation.__table__.create(bind=self.engine, checkfirst=True)
        Item.__table__.create(bind=self.engine, checkfirst=True)
        System.__table__.create(bind=self.engine, checkfirst=True)
        Celestial.__table__.create(bind=self.engine, checkfirst=True)
        Resource.__table__.create(bind=self.engine, checkfirst=True)

    def init_db_from_csv(self,
                         path: str,
                         start: Optional[int] = None,
                         end: Optional[int] = None,
                         separator: str = ";",
                         start_planet: Optional[int] = None):
        logger.warning("Initialising planetary production database with file %s, start: %s, end: %s, start_planet: %s",
                       path, start, end, start_planet)
        line_i = 0
        with Session(self.engine) as session:
            size = None  # type: int | None
            if start is not None and end is not None:
                size = end - start
            with open(path, "r") as file:
                for line in file:
                    line_i += 1
                    if line_i == 1:
                        continue
                    if start is not None and line_i < start:
                        continue
                    if end is not None and line_i > end:
                        logger.warning("Line %s reached, stopping initialization", end)
                        break

                    line = line.replace("\n", "").split(separator)
                    if len(line) < 9:
                        raise PlanetaryProductionException(f"CSV file {path} has invalid entry in line {str(line_i)}")
                    p_id = int(line[0])
                    res_n = line[6].strip()
                    rich = Richness.from_str(line[7].strip())
                    out = float(line[8].replace(",", "."))

                    if start_planet is not None and p_id < start_planet:
                        continue

                    if line_i % 100 == 0:
                        if size is None or start is None:
                            logger.info("Processing line %s", line_i)
                        else:
                            logger.info("Processing line %s, %s/%s (%s)",
                                        line_i, line_i - start, size,
                                        "{:.2%}".format((line_i - start)/size))
                        session.commit()

                    resource_type = session.query(Item).filter(Item.name == res_n).first()
                    if resource_type is None:
                        raise PlanetaryProductionException("Ressource Type %s not found in database", resource_type)
                    resource = Resource(planet_id=p_id, type=resource_type, output=out, richness=rich)
                    session.add(resource)
                session.commit()
            logger.info("Database initialized")

    def auto_init_pool(self, path: str, pool_size=20):
        pool = ThreadPool(processes=pool_size)

        def _count_generator(reader):
            b = reader(1024 * 1024)
            while b:
                yield b
                b = reader(1024 * 1024)

        with open(path, 'rb') as fp:
            # noinspection PyUnresolvedReferences
            c_generator = _count_generator(fp.raw.read)
            # count each \n
            file_length = sum(buffer.count(b'\n') for buffer in c_generator)

        # Splitting file onto thread pool
        logger.info("Found %s lines in file %s, preparing thread pool with %s threads",
                    file_length, path, pool_size)
        args = []
        last_line = 0
        lines_per_thread = math.floor(file_length / pool_size)
        for i in range(pool_size):
            if i == pool_size - 1:
                args.append((path, last_line + 1, file_length))
            else:
                args.append((path, last_line + 1, last_line + lines_per_thread))
            last_line += lines_per_thread
        logger.info("Starting threadpool")
        pool.starmap(self.init_db_from_csv, args)
        logger.info("Threadpool finished, database initialized")


if __name__ == '__main__':
    formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s][%(threadName)s]: %(message)s")
    # Console log handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    logger.addHandler(console)
    logging.root.setLevel(logging.NOTSET)
    # noinspection DuplicatedCode
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

    config = Config("../../tests/test_config.json", ConfigTree(config_structure))
    config.load_config()
    db_name = config["db.name"]  # type: str
    # if not "test".casefold() in db_name.casefold():
        # raise Exception(f"Database {db_name} is probably not a testdatabase, test evaluation canceled.")
    connector = DatabaseConnector(
        username=config["db.user"],
        password=config["db.password"],
        port=config["db.port"],
        host=config["db.host"],
        database=config["db.name"]
    )
    db = PlanetaryDatabase(connector)
    input("Press enter to process resource file (this may take a while time)...")
    db.auto_init_pool("../../resources/planetary_production.csv", pool_size=5)
