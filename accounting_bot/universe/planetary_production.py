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
from accounting_bot.universe.models import Region, Constellation, Planet, PlanetType, ResourceType, Resource, Richness, \
    System

logger = logging.getLogger("bot.pi")


class PlanetaryDatabase:
    def __init__(self, db: DatabaseConnector) -> None:
        super().__init__()
        self.engine = create_engine(f"mariadb+mariadbconnector://"
                                    f"{db.username}:{db.password}@{db.host}:{db.port}/{db.database}"
                                    )
        Region.__table__.create(bind=self.engine, checkfirst=True)
        Constellation.__table__.create(bind=self.engine, checkfirst=True)
        ResourceType.__table__.create(bind=self.engine, checkfirst=True)
        System.__table__.create(bind=self.engine, checkfirst=True)
        Planet.__table__.create(bind=self.engine, checkfirst=True)
        Resource.__table__.create(bind=self.engine, checkfirst=True)

    def init_db_from_csv(self,
                         path: str,
                         start: Optional[int] = None,
                         end: Optional[int] = None,
                         start_planet: Optional[int] = None,
                         auto: bool = False):
        logger.warning("Initialising planetary production database with file %s, start: %s, end: %s, start_planet: %s, auto: %s",
                       path, start, end, start_planet, auto)
        line_i = 0
        session = Session(self.engine)
        if auto:
            result = session.execute(text("SELECT MAX(id) as max_planet FROM planet;"))
            result = result.fetchone()
            if result is not None and len(result) == 1:
                start_planet = result[0]
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

                line = line.replace("\n", "").split(";")
                if len(line) < 9:
                    raise PlanetaryProductionException(f"CSV file {path} has invalid entry in line {str(line_i)}")
                p_id = int(line[0])
                reg_n = line[1].strip()
                const_n = line[2].strip()
                sys_n = line[3].strip()
                p_n = line[4].strip()
                p_t = PlanetType.from_str(line[5].strip())
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

                region = session.query(Region).filter(Region.name == reg_n).first()
                if region is None:
                    region = Region(name=res_n)
                    session.add(region)
                constellation = session.query(Constellation).filter(Constellation.name == const_n).first()
                if constellation is None:
                    constellation = Constellation(name=const_n, region=region)
                    session.add(constellation)
                system = session.query(System).filter(System.name == sys_n).first()
                if system is None:
                    system = System(name=sys_n, constellation=constellation)
                    session.add(system)
                planet = session.query(Planet).filter(Planet.id == p_id).first()
                if planet is None:
                    planet = Planet(id=p_id, name=p_n, system=system, type=p_t)
                    session.add(planet)
                resource_type = session.query(ResourceType).filter(ResourceType.name == res_n).first()
                if resource_type is None:
                    resource_type = ResourceType(name=res_n)
                    session.add(resource_type)
                resource = session.query(Resource).filter(Resource.planet == planet and Resource.type == resource_type).first()
                if resource is None:
                    resource = Resource(planet=planet, type=resource_type, output=out, richness=rich)
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

    def test(self):
        region = Region(name="Region A")
        const = Constellation(region=region, name="Constellation A")
        planet_a = Planet(constellation=const, name="Planet I", type=PlanetType.temperate)
        session = Session(self.engine)
        session.add(planet_a)
        session.commit()


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
    if not "test".casefold() in db_name.casefold():
        raise Exception(f"Database {db_name} is probably not a testdatabase, test evaluation canceled.")
    connector = DatabaseConnector(
        username=config["db.user"],
        password=config["db.password"],
        port=config["db.port"],
        host=config["db.host"],
        database=config["db.name"]
    )
    db = PlanetaryDatabase(connector)
    db.auto_init_pool("../../resources/planetary_production.csv", pool_size=20)
