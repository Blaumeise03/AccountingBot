import json
import logging
import math
import sys
from multiprocessing.pool import ThreadPool
from typing import Optional, Dict, Tuple, List

from sqlalchemy import create_engine, update, between, func, select
from sqlalchemy.orm import Session, joinedload

from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.exceptions import PlanetaryProductionException
from accounting_bot.universe import models
from accounting_bot.universe.models import Region, Constellation, Celestial, Item, Resource, Richness, System

logger = logging.getLogger("data.db")


class UniverseDatabase:
    def __init__(self, db: DatabaseConnector) -> None:
        super().__init__()
        self.engine = create_engine(f"mariadb+mariadbconnector://"
                                    f"{db.username}:{db.password}@{db.host}:{db.port}/{db.database}"
                                    )
        Region.__table__.create(bind=self.engine, checkfirst=True)
        Constellation.__table__.create(bind=self.engine, checkfirst=True)
        Item.__table__.create(bind=self.engine, checkfirst=True)
        System.__table__.create(bind=self.engine, checkfirst=True)
        models.SystemConnections.create(bind=self.engine, checkfirst=True)
        Celestial.__table__.create(bind=self.engine, checkfirst=True)
        Resource.__table__.create(bind=self.engine, checkfirst=True)

    def fetch_system(self, system_name: str) -> Optional[System]:
        with Session(self.engine, expire_on_commit=False) as conn:
            return conn.query(System).filter(System.name == system_name).first()

    def fetch_planet(self, planet_name: str) -> Optional[Celestial]:
        with Session(self.engine, expire_on_commit=False) as conn:
            return conn.query(Celestial).filter(Celestial.name == planet_name).first()

    def fetch_resources(self, constellation_name: str, res_names: Optional[List[str]] = None):
        with Session(self.engine, expire_on_commit=False) as conn:
            logger.debug("Loading resources for constellation %s: %s", constellation_name, res_names)
            const_id_q = conn.query(Constellation.id).filter(Constellation.name.like(constellation_name))
            system_ids_q = conn.query(System.id).filter(System.constellation_id.in_(const_id_q))
            planet_ids = (
                conn.query(Celestial.id)
                .filter(
                    Celestial.system_id.in_(system_ids_q),
                    Celestial.group_id == Celestial.Type.planet.groupID
                )
            )
            if res_names is None or len(res_names) == 0:
                # noinspection PyTypeChecker
                res = (
                    conn.query(Resource)
                    .options(joinedload(Resource.planet), joinedload(Resource.type))
                    .filter(Resource.planet_id.in_(planet_ids)).all())
            else:
                res_ids = conn.query(Item.id).filter(Item.name.in_(res_names))
                # noinspection PyTypeChecker
                res = (
                    conn.query(Resource)
                    .options(joinedload(Resource.planet), joinedload(Resource.type))
                    .filter(
                        Resource.planet_id.in_(planet_ids),
                        Resource.type_id.in_(res_ids)
                    )
                    .all()
                )  # type: List[Resource]
            logger.debug("Resources loaded for %s", constellation_name)
        processed = list(map(lambda r: {
            "p_id": r.planet_id,
            "p_name": r.planet.name,
            "res": r.type.name,
            "out": r.output
        }, res))
        return processed

    def fetch_max_resources(self):
        with Session(self.engine, expire_on_commit=False) as conn:
            stmt = (
                select(Item.name, func.max(Resource.output))
                .select_from(Resource)
                .join(Item, Resource.type_id == Item.id)
                .group_by(Resource.type_id)
            )
            result = conn.execute(stmt).all()
        return dict(result)


class DatabaseInitializer(UniverseDatabase):
    def __init__(self, db: DatabaseConnector) -> None:
        super().__init__(db)

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
                                        "{:.2%}".format((line_i - start) / size))
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

    def auto_init_item_types(self, types: Dict[str, Tuple[int, int]]):
        with self.engine.begin() as conn:
            logger.info("Initializing item types for database")
            for k, v in types.items():
                logger.info("Updating item type for [%s, %s] to '%s'", v[0], v[1], k)
                stmt = (update(Item)
                        .where(between(Item.id, v[0], v[1]))
                        .values(type=k)
                        )
                result = conn.execute(stmt)
                pass
            logger.info("Item types updated")

    def auto_init_stargates(self, file_path: str):
        with open(file_path, "r") as f:
            with Session(self.engine) as conn:
                line_i = 0
                logger.info("Loading stargates")
                for line in f:
                    line_i += 1
                    if line_i == 1:
                        continue
                    if line_i % 100 == 0:
                        logger.info("Processing line %s", line_i)
                        conn.commit()
                    line = line.replace("\n", "").split(",")
                    gate_a_id = int(line[0])
                    gate_b_id = int(line[1])
                    gate_a = (
                        conn.query(Celestial)
                        .options(joinedload(Celestial.system))
                        .filter(Celestial.id == gate_a_id)
                        .first()
                    )  # type: Celestial | None
                    system_a = gate_a.system  # type: System
                    gate_b = (
                        conn.query(Celestial)
                        .options(joinedload(Celestial.system))
                        .filter(Celestial.id == gate_b_id)
                        .first()
                    )  # type: Celestial | None
                    system_b = gate_b.system  # type: System
                    system_a.stargates.append(system_b)
                    pass
                conn.commit()
            logger.info("Stargates loaded")


if __name__ == '__main__':
    formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s][%(threadName)s]: %(message)s")
    # Console log handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(formatter)
    logger.addHandler(console)
    logging.root.setLevel(logging.NOTSET)
    logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)
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
    path = input("Enter path to config: ")
    config = Config(path, ConfigTree(config_structure))
    config.load_config()

    connector = DatabaseConnector(
        username=config["db.user"],
        password=config["db.password"],
        port=config["db.port"],
        host=config["db.host"],
        database=config["db.name"]
    )
    db = DatabaseInitializer(connector)
    inp = input(
        "Press enter 'i' to load the planetary production database. Enter 't' to initialize the item types. "
        "Enter s to initialize stargates: ").casefold()
    if inp == "i".casefold():
        print("Required format for planetary_production.csv")
        print("Planet ID;Region;Constellation;System;Planet Name;Planet Type;Resource;Richness;Output")
        path = input("Enter path to planetary_production.csv: ")
        db.auto_init_pool(path, pool_size=5)
    elif inp == "t".casefold():
        with open("../../resources/item_types.json") as f:
            data = f.read()
        types = json.loads(data)
        db.auto_init_item_types(types)
    elif inp == "s".casefold():
        db.auto_init_stargates("../../resources/mapJumps.csv")
    else:
        print(f"Error, unknown input '{inp}'")
