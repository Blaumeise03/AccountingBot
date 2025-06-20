import csv
import json
import logging
import math
import sys
from typing_extensions import deprecated
from datetime import datetime
from multiprocessing.pool import ThreadPool
from typing import Dict, Tuple, Union, Any, TYPE_CHECKING

from dateutil import parser
from sqlalchemy import create_engine, update, between, select, delete, or_, and_
from sqlalchemy.orm import Session, joinedload, contains_eager

from accounting_bot import utils
from accounting_bot.config import Config
from accounting_bot.exceptions import PlanetaryProductionException, KillmailException, DatabaseException
from accounting_bot.ext.members import MembersPlugin
from accounting_bot.universe import models
from accounting_bot.universe.models import *

if TYPE_CHECKING:
    from pi_planer import PiPlaner

logger = logging.getLogger("ext.data.db")


# logger.setLevel(logging.DEBUG)
# logging.getLogger('sqlalchemy.engine').setLevel(logging.INFO)


def _ensure_len(string: str, length: int):
    if len(string) > length:
        return string[:length]
    return string


def get_file_len(path: str):
    def _count_generator(reader):
        b = reader(1024 * 1024)
        while b:
            yield b
            b = reader(1024 * 1024)

    with open(path, 'rb') as fp:
        # noinspection PyUnresolvedReferences
        c_generator = _count_generator(fp.raw.read)
        # count each \n
        return sum(buffer.count(b'\n') for buffer in c_generator)


@deprecated("UniverseDatabase is no longer maintained, use the BlueEchoesDB instead")
class UniverseDatabase:
    def __init__(self,
                 username: Optional[str],
                 password: Optional[str],
                 host: Optional[str],
                 port: Optional[str],
                 database: Optional[str]) -> None:
        super().__init__()
        logger.info("Creating engine")
        self.engine = create_engine("mariadb+mariadbconnector://{username}:{password}@{host}:{port}/{database}"
                                    .format(username=username,
                                            password=password,
                                            host=host,
                                            port=port,
                                            database=database),
                                    pool_pre_ping=True,
                                    pool_recycle=True
                                    )
        logger.info("Creating tables if not exist")
        Region.__table__.create(bind=self.engine, checkfirst=True)
        Constellation.__table__.create(bind=self.engine, checkfirst=True)
        Item.__table__.create(bind=self.engine, checkfirst=True)
        System.__table__.create(bind=self.engine, checkfirst=True)
        Celestial.__table__.create(bind=self.engine, checkfirst=True)
        models.SystemConnections.create(bind=self.engine, checkfirst=True)
        models.StargateConnections.create(bind=self.engine, checkfirst=True)
        Resource.__table__.create(bind=self.engine, checkfirst=True)
        PiPlanSettings.__table__.create(bind=self.engine, checkfirst=True)
        PiPlanResource.__table__.create(bind=self.engine, checkfirst=True)
        MarketPrice.__table__.create(bind=self.engine, checkfirst=True)
        Killmail.__table__.create(bind=self.engine, checkfirst=True)
        Bounty.__table__.create(bind=self.engine, checkfirst=True)
        MobiKillmail.__table__.create(bind=self.engine, checkfirst=True)
        logger.info("Database setup completed")

    def save_market_data(self, items: Dict[str, Dict[str, Any]]) -> None:
        with Session(self.engine) as conn:
            for item_name, prices in items.items():
                db_item = (
                    conn.query(Item)
                    .options(joinedload(Item.prices))
                    .filter(Item.name == item_name)
                ).first()
                if db_item is None:
                    logger.warning("Item %s from market sheet not found in database", item_name)
                    continue
                for price_type, price in prices.items():
                    found = False
                    # For some reason, the SQL statements don't work with floats. MariaDB will throw an invalid
                    # parameter error, but when executing the SQL command directly via the terminal, the UPDATE
                    # statement works perfectly fine. On top of that is the bug inconsistent between the local
                    # development environment and the production one. And going even further, the bug does not
                    # seem to be consistent on the production server as well. It does not occur always, one time
                    # updating the DB server fixed it temporarily. As the floating point problems are only relevant
                    # for one item (Tritanium) which is currently not being used by the bot in any calculation, the
                    # values will be inserted as integers into the DB.
                    for p in db_item.prices:  # type: MarketPrice
                        if p.price_type == price_type:
                            p.price_value = int(price)
                            found = True
                            break
                    if not found:
                        p = MarketPrice(price_type=price_type, price_value=int(price))
                    db_item.prices.append(p)
                conn.commit()

    def get_market_data(self,
                        item_names: Optional[List[str]] = None,
                        item_type: Optional[str] = None) -> Dict[str, Dict[str, float]]:
        with Session(self.engine) as conn:
            if item_names is None:
                if item_type is None:
                    raise TypeError("One argument is required")
                stmt = select(Item.name).filter(Item.type == item_type)
                res = conn.execute(stmt).all()
                item_names = [r[0] for r in res]
            items = {}
            for item_name in item_names:
                db_item = (
                    conn.query(Item)
                    .options(joinedload(Item.prices))
                    .filter(Item.name == item_name)
                ).first()
                prices = {}
                for price in db_item.prices:
                    prices[price.price_type] = price.price_value
                items[db_item.name] = prices
            return items

    def get_available_market_data(self, item_type: str) -> List[str]:
        with Session(self.engine) as conn:
            stmt = (
                select(MarketPrice.price_type)
                .join(MarketPrice.item)
                .where(Item.type == item_type)
                .group_by(MarketPrice.price_type)
            )
            result = conn.execute(stmt).all()
            return [r[0] for r in result]

    def fetch_system(self, system_name: str) -> Optional[System]:
        with Session(self.engine, expire_on_commit=False) as conn:
            return conn.query(System).filter(System.name == system_name).first()

    def fetch_systems(self, system_names: List[str]) -> List[System]:
        with Session(self.engine, expire_on_commit=False) as conn:
            # noinspection PyTypeChecker
            return (
                conn.query(System)
                .options(joinedload(System.celestials)
                         .subqueryload(Celestial.connected_gate)
                         .subqueryload(Celestial.system),
                         joinedload(System.stargates))
                .filter(System.name.in_(system_names)).all()
            )

    def fetch_gates(self, system_name: str) -> List[Celestial]:
        with Session(self.engine, expire_on_commit=False) as conn:
            # noinspection PyTypeChecker
            return (
                conn.query(Celestial)
                .options(joinedload(Celestial.connected_gate)
                         .subqueryload(Celestial.system))
                .join(Celestial.system)
                .filter(System.name.ilike(system_name))
                .filter(Celestial.connected_gate != None)
                .all()
            )

    def fetch_constellation(self, constellation_name: str = None, planet_id: int = None) -> Optional[Constellation]:
        with Session(self.engine, expire_on_commit=False) as conn:
            if constellation_name is not None:
                return conn.query(Constellation).filter(Constellation.name == constellation_name).first()
            if planet_id is not None:
                return (
                    conn.query(Constellation)
                    .join(Constellation.systems)
                    .join(System.celestials)
                    .filter(Celestial.id == planet_id).first()
                )

    def fetch_planet(self, planet_name: str) -> Optional[Celestial]:
        with Session(self.engine, expire_on_commit=False) as conn:
            return conn.query(Celestial).filter(Celestial.name == planet_name).first()

    def fetch_resources(self, constellation_name: str,
                        res_names: Optional[List[str]] = None,
                        amount: Optional[int] = None) -> List[Dict[str, int]]:
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
                query = (
                    conn.query(Resource)
                    .options(joinedload(Resource.planet), joinedload(Resource.type))
                    .filter(Resource.planet_id.in_(planet_ids))
                    .order_by(Resource.output.desc())
                )
            else:
                res_ids = conn.query(Item.id).filter(Item.name.in_(res_names))
                query = (
                    conn.query(Resource)
                    .options(joinedload(Resource.planet), joinedload(Resource.type))
                    .filter(
                        Resource.planet_id.in_(planet_ids),
                        Resource.type_id.in_(res_ids)
                    ).order_by(Resource.output.desc())
                )
            logger.debug("Resources loaded for %s", constellation_name)
        if amount is None:
            res = query.all()
        else:
            res = query.limit(amount).all()
        processed = list(map(lambda r: {
            "p_id": r.planet_id,
            "p_name": r.planet.name,
            "res": r.type.name,
            "out": r.output
        }, res))
        return processed

    def fetch_ressource_by_planet(self,
                                  sys_name: str,
                                  distance: int,
                                  res_name: Optional[str],
                                  amount: Optional[int] = None) -> List[Dict[str, int]]:
        if amount is None:
            amount = 20
        with Session(self.engine) as conn:
            logger.info("Loading resource for system %s distance %s: %s", sys_name, distance, res_name)
            systems = {}
            all_systems = []
            # noinspection PyTypeChecker
            system = (
                conn.query(System)
                .filter(System.name == sys_name).first()
            )  # type: System

            logger.debug("Loading item %s", res_name)
            # noinspection PyTypeChecker
            item = (
                conn.query(Item)
                .filter(Item.name.like(res_name))
            ).first()  # type: Item
            if system is None:
                raise PlanetaryProductionException(f"System '{sys_name}' not found")
            if item is None:
                raise PlanetaryProductionException(f"Resource '{res_name}' not found")
            cache = [system]  # type: List[System]
            not_cached = []  # type: List[System]
            logger.debug("Caching constellation %s", system.constellation_id)
            # noinspection PyTypeChecker
            cache.extend(conn.query(System)
                         .options(joinedload(System.stargates))
                         .filter(System.constellation_id == system.constellation_id).all())
            current_systems = [system]
            next_systems = []
            logger.debug("Loading systems into cache")
            d = 0
            while len(current_systems) > 0 and d <= distance:
                logger.debug("Processing %s systems with distance %s", len(current_systems), d)
                for s in current_systems:
                    all_systems.append(s)
                    systems[s.id] = d
                    for n in s.stargates:
                        if n in all_systems:
                            continue
                        next_systems.append(n)
                        if n not in cache:
                            not_cached.append(n)
                # Load new systems into cache
                if len(not_cached) > 0:
                    const_ids = [r.constellation_id for r in not_cached]
                    logger.debug("Loading constellations %s into cache", const_ids)
                    result = (
                        conn.query(System)
                        .options(joinedload(System.stargates))
                        .filter(System.constellation_id.in_(const_ids))
                    ).all()
                    not_cached.clear()
                    for r in result:
                        if r not in cache:
                            # noinspection PyTypeChecker
                            cache.append(r)
                    logger.debug("Systems cached")
                current_systems = next_systems
                next_systems = []
                d += 1
            logger.debug("Loading resources")
            stmt = (
                select(Resource)
                .options(joinedload(Resource.planet))
                .join(Resource.planet)
                .where(Resource.type_id == item.id)
                .where(Celestial.system_id.in_([i for i in systems]))
                .order_by(Resource.output.desc())
            )
            # noinspection PyTypeChecker
            if amount is None:
                res = conn.execute(stmt).all()
            else:
                res = conn.execute(stmt.limit(amount))
            processed = list(map(lambda r: {
                "p_id": r[0].planet_id,
                "p_name": r[0].planet.name,
                "res": r[0].type.name,
                "out": r[0].output,
                "distance": systems[r[0].planet.system.id]
            }, res))
            logger.debug("%s resources loaded", len(processed))
            return processed

    def fetch_max_resources(self, region_names: Optional[List[str]] = None):
        with Session(self.engine, expire_on_commit=False) as conn:
            if region_names is None or len(region_names) == 0:
                stmt = (
                    select(Item.name, func.max(Resource.output))
                    .select_from(Resource)
                    .join(Item, Resource.type_id == Item.id)
                    .group_by(Resource.type_id)
                )
            else:
                region_ids = (
                    select(Region.id).select_from(Region).where(Region.name.in_(region_names))
                )
                stmt = (
                    select(Item.name, func.max(Resource.output))
                    .select_from(Resource)
                    .join(Item, Resource.type_id == Item.id)
                    .join(Celestial, Resource.planet_id == Celestial.id)
                    .join(System, Celestial.system_id == System.id)
                    .where(System.region_id.in_(region_ids))
                    .group_by(Resource.type_id)
                )
            result = conn.execute(stmt).all()
        # noinspection PyTypeChecker
        return dict(result)

    def fetch_planets(self, planet_ids: List[int]) -> List[Celestial]:
        with Session(self.engine, expire_on_commit=False) as conn:
            # noinspection PyTypeChecker
            return (
                conn.query(Celestial)
                .options(joinedload(Celestial.resources)
                         .subqueryload(Resource.type))
                .filter(Celestial.id.in_(planet_ids))
            ).all()

    def fetch_map(self) -> List[System]:
        with Session(self.engine, expire_on_commit=False) as conn:
            # noinspection PyTypeChecker
            return (
                conn.query(System)
                .options(
                    joinedload(System.stargates),
                    joinedload(System.constellation).subqueryload(Constellation.region)
                ).all()
            )

    def fetch_item(self, item_name: str) -> Optional[Item]:
        with Session(self.engine, expire_on_commit=False) as conn:
            return (
                conn.query(Item)
                .filter(Item.name == item_name)
                .first()
            )

    def fetch_items(self, item_type: str) -> List[Item]:
        with Session(self.engine, expire_on_commit=False) as conn:
            # noinspection PyTypeChecker
            return (
                conn.query(Item)
                .filter(Item.type == item_type)
                .all()
            )

    def get_pi_plan(self,
                    user_id: int,
                    plan_num: Optional[int] = None) -> Union[PiPlanSettings, List[PiPlanSettings], None]:
        with Session(self.engine) as conn:
            if plan_num is not None:
                return (conn.query(PiPlanSettings)
                        .options(joinedload(PiPlanSettings.resources).subqueryload(Resource.type),
                                 joinedload(PiPlanSettings.resources).subqueryload(Resource.planet),
                                 joinedload(PiPlanSettings.constellation))
                        .filter(PiPlanSettings.user_id == user_id, PiPlanSettings.plan_num == plan_num)
                        .first())
            else:
                # noinspection PyTypeChecker
                return (conn.query(PiPlanSettings)
                        .options(
                    joinedload(PiPlanSettings.resources).subqueryload(PiPlanResource.resource).subqueryload(
                        Resource.type),
                    joinedload(PiPlanSettings.resources).subqueryload(PiPlanResource.resource).subqueryload(
                        Resource.planet),
                    joinedload(PiPlanSettings.constellation))
                        .filter(PiPlanSettings.user_id == user_id)
                        .all())

    def save_pi_plan(self, pi_plan: "PiPlaner"):
        with Session(self.engine) as conn:
            result = (
                conn.query(PiPlanSettings)
                .options(joinedload(PiPlanSettings.resources).subqueryload(PiPlanResource.resource).subqueryload(
                    Resource.type))
                .filter(PiPlanSettings.user_id == pi_plan.user_id, PiPlanSettings.plan_num == pi_plan.plan_num)
                .first()
            )  # type: PiPlanSettings | None
            if result is None:
                result = PiPlanSettings()
                conn.add(result)
            result.user_id = pi_plan.user_id
            result.plan_num = pi_plan.plan_num
            result.user_name = pi_plan.user_name
            result.arrays = pi_plan.num_arrays
            result.planets = pi_plan.num_planets
            result.constellation_id = pi_plan.constellation_id
            if len(pi_plan.preferred_prices) == 0:
                result.preferred_prices = None
            else:
                prices = ""
                for p in pi_plan.preferred_prices:
                    prices += f"{p};"
                prices = prices.strip(";")
                result.preferred_prices = prices
            arrays = []
            found_arrays = []
            for res in result.resources:  # type: PiPlanResource
                found = None
                for array in pi_plan.arrays:
                    if array.planet.id == res.planet_id:
                        if array.resource == res.resource.type.name:
                            found = array
                            res.arrays = array.amount
                            res.locked = array.locked
                            found_arrays.append(array)
                            arrays.append(res)
                            break
                if found is not None:
                    break
            for array in pi_plan.arrays:
                if array not in found_arrays:
                    if array.resource_id is None:
                        item = conn.query(Item).filter(Item.name.like(array.resource)).first()
                        if item is None:
                            raise PlanetaryProductionException(f"Resource {array.resource} not found!")
                        array.resource = item.name
                        array.resource_id = item.id
                    arrays.append(PiPlanResource(
                        user_id=pi_plan.user_id,
                        plan_num=pi_plan.plan_num,
                        planet_id=array.planet.id,
                        type_id=array.resource_id,
                        arrays=array.amount,
                        locked=array.locked
                    ))
            result.resources.clear()
            result.resources.extend(arrays)
            conn.commit()

    def delete_pi_plan(self, pi_plan: "PiPlaner"):
        with Session(self.engine) as conn:
            p = (
                conn.query(PiPlanSettings)
                .filter(PiPlanSettings.user_id == pi_plan.user_id,
                        PiPlanSettings.plan_num == pi_plan.plan_num)
                .first()
            )
            if p is None:
                raise PlanetaryProductionException(
                    f"Didn't found plan {pi_plan.user_id}:{pi_plan.plan_num} in database")
            conn.delete(p)
            conn.commit()

    def get_killmail(self, kill_id: int) -> Optional[Killmail]:
        with Session(self.engine, expire_on_commit=False) as conn:
            return (
                conn.query(Killmail)
                .options(joinedload(Killmail.system))
                .filter(Killmail.id == kill_id)
                .first()
            )

    def save_killmail(self, kill_data: Dict[str, str]):
        with Session(self.engine) as conn:
            k_id = int(kill_data["id"])
            k_player = kill_data["final_blow"]
            k_ship = conn.query(Item).filter(Item.name == kill_data["ship"]).first()
            k_value = utils.parse_number(kill_data["kill_value"])[0]
            k_system = conn.query(System).filter(System.name == kill_data["system"]).first()
            killmail = conn.query(Killmail).options(joinedload(Killmail.system)).filter(
                Killmail.id == k_id).first()
            if None in [k_id, k_player, k_value]:
                raise KillmailException(f"Invalid killmail data: {kill_data}")
            if killmail is None:
                killmail = Killmail(
                    id=k_id,
                    final_blow=k_player,
                    ship_id=k_ship.id if k_ship is not None else None,
                    kill_value=k_value,
                    system_id=k_system.id if k_system is not None else None
                )
                conn.add(killmail)
            else:
                killmail.final_blow = k_player
                killmail.ship_id = k_ship.id if k_ship is not None else None
                killmail.ship = k_ship
                killmail.kill_value = k_value
                killmail.system_id = k_system.id if k_system is not None else None
                killmail.system = k_system
            conn.commit()

    def save_bounty(self, kill_id: int, player: str, bounty_type: str):
        with Session(self.engine) as conn:
            killmail = conn.query(Killmail).filter(Killmail.id == kill_id).first()
            if killmail is None:
                raise DatabaseException(f"Killmail {kill_id} not found")
            bounty = conn.query(Bounty).filter(Bounty.kill_id == kill_id, Bounty.player == player).first()
            if bounty is None:
                bounty = Bounty(kill_id=kill_id, player=player, bounty_type=bounty_type)
                conn.add(bounty)
            else:
                bounty.bounty_type = bounty_type
            conn.commit()

    @staticmethod
    def _convert_bounties(bounties: List[Bounty]):
        res = []
        for bounty in bounties:  # type: Bounty
            res.append({
                "kill_id": bounty.kill_id,
                "player": bounty.player,
                "type": bounty.bounty_type,
                "value": bounty.killmail.kill_value,
                "ship": bounty.killmail.ship.name if bounty.killmail.ship is not None else None,
                "system": bounty.killmail.system.name if bounty.killmail.system is not None else None,
                "region": bounty.killmail.system.constellation.region.name if bounty.killmail.system is not None
                else None
            })
        return res

    @staticmethod
    def _convert_bounties_light(bounties: List[Bounty]):
        res = []
        for bounty in bounties:  # type: Bounty
            res.append({
                "kill_id": bounty.kill_id,
                "player": bounty.player,
                "type": bounty.bounty_type,
                "value": bounty.killmail.kill_value,
            })
        return res

    @staticmethod
    def _get_bounty_stmt(session: Session):
        return (session.query(Bounty)
                .join(Bounty.killmail)
                .join(Killmail.system, isouter=True)
                .join(System.constellation)
                .join(Constellation.region)
                .options(contains_eager(Bounty.killmail)
                         .contains_eager(Killmail.system)
                         .contains_eager(System.constellation)
                         .contains_eager(Constellation.region)))

    def get_bounty_by_killmail(self, kill_id: int):
        with Session(self.engine) as conn:
            res = (UniverseDatabase._get_bounty_stmt(conn)
                   .filter(Bounty.kill_id == kill_id).all())
            # noinspection PyTypeChecker
            return UniverseDatabase._convert_bounties(res)

    def get_bounty_by_player(self, player: str):
        with Session(self.engine) as conn:
            res = (UniverseDatabase._get_bounty_stmt(conn)
                   .filter(Bounty.player == player).all())
            # noinspection PyTypeChecker
            return UniverseDatabase._convert_bounties(res)

    def get_all_bounties(self, start: int, end: int):
        with Session(self.engine) as conn:
            res = (UniverseDatabase._get_bounty_stmt(conn)
                   .filter(Bounty.kill_id >= start, Bounty.kill_id <= end).all())
            # noinspection PyTypeChecker
            return UniverseDatabase._convert_bounties(res)

    def get_bounties_by_player(self, start_time: datetime, end_time: datetime, user: str):
        with Session(self.engine, expire_on_commit=False) as conn:
            logger.debug("Loading bounty")
            res = (UniverseDatabase._get_bounty_stmt(conn)
                   .filter(Killmail.inserted >= start_time,
                           Killmail.inserted <= end_time,
                           Bounty.player == user
                           )
                   .order_by(Killmail.kill_value.desc())
                   ).all()
            logger.debug("Bounty loaded")
        # noinspection PyTypeChecker
        return UniverseDatabase._convert_bounties(res)

    def clear_bounties(self, kill_id: int):
        with self.engine.begin() as conn:
            stmt = (
                delete(Bounty).where(Bounty.kill_id == kill_id, Bounty.bounty_type != "M")
            )
            conn.execute(stmt)

    def verify_bounties(self, plugin: MembersPlugin, kill_id_start: int, kill_id_end: int, time: datetime = None):
        warnings = []
        with Session(self.engine) as conn:
            killmails = conn.query(Killmail).filter(Killmail.id >= kill_id_start, Killmail.id <= kill_id_end).all()
            bounties = conn.query(Bounty).filter(Bounty.kill_id >= kill_id_start, Bounty.kill_id <= kill_id_end).all()
            for killmail in killmails:
                found_main = False
                found = False
                for bounty in bounties:
                    if bounty.kill_id == killmail.id:
                        if bounty.bounty_type == "M":
                            found_main = True
                        found = True
                    if found_main:
                        break
                if not found_main:
                    player, _, _ = plugin.get_main_name(name=killmail.final_blow)
                    if player is not None:
                        bounty = Bounty(
                            kill_id=killmail.id,
                            player=player
                        )
                        conn.add(bounty)
                        found_main = True
                        found = True
                        warnings.append(f"Killmail {killmail.id} has no main bounty, added bounty for {player}")
                    else:
                        warnings.append(
                            f"Killmail {killmail.id} has no main bounty, player {killmail.final_blow} not found")
                if not found:
                    warnings.append(
                        f"Killmail {killmail.id} has no bounty at all, player {killmail.final_blow} not found")
            conn.commit()
            if time is not None:
                start, end = utils.get_month_edges(time)
                wrong = (
                    conn.query(Killmail)
                    .filter(
                        or_(Killmail.id < kill_id_start, Killmail.id > kill_id_end),
                        Killmail.inserted.between(start, end)
                    )
                ).all()
                for kill in wrong:
                    warnings.append(f"Killmail {kill.id} is outside of selection, but was inserted this month "
                                    f"(won't be inserted)")
                for kill in killmails:
                    if start > kill.inserted or end < kill.inserted:
                        warnings.append(f"Killmail {kill.id} is inside of selection, but was not inserted this month "
                                        f"(will be inserted)")
        return warnings

    def save_killmail_csv(self, raw_csv: str, replace_tag: Optional[str] = None):
        csv_reader = csv.reader(raw_csv.split("\n"), delimiter=",")
        header = next(csv_reader)
        if len(header) == 0:
            return
        system_cache = {}  # type: Dict[str, System]
        ship_cache = {}  # type: Dict[str, Item]
        with Session(self.engine) as conn:
            for row in csv_reader:
                if len(row) < len(header):
                    continue
                kill_id = row[header.index("id")]
                kill_obj = conn.query(MobiKillmail).filter(MobiKillmail.id == kill_id).first()
                if kill_obj is None:
                    kill_obj = MobiKillmail()
                    kill_obj.id = kill_id
                kill_obj.report_id = row[header.index("report_id")] or None
                kill_obj.is_kill = row[header.index("report_type")].casefold() == "kill".casefold()
                kill_obj.killer_corp = _ensure_len(replace_tag or row[header.index("killer_corp")], 6)
                kill_obj.killer_name = row[header.index("killer_name")]
                kill_obj.victim_corp = _ensure_len(row[header.index("victim_corp")], 6)
                kill_obj.victim_name = row[header.index("victim_name")]
                kill_obj.isk = row[header.index("isk")]
                kill_obj.image_url = row[header.index("image_url")]
                kill_obj.date_killed = parser.parse(row[header.index("date_killed")])
                kill_obj.date_updated = parser.parse(row[header.index("date_updated")])
                kill_obj.date_created = parser.parse(row[header.index("date_created")])
                kill_obj.external_provider = row[header.index("external_provider")]
                kill_obj.victim_total_damage_received = row[header.index("victim_total_damage_received")] or None
                kill_obj.user_id = row[header.index("user_id")] or None
                kill_obj.guild_id = row[header.index("guild_id")] or None
                kill_obj.battle_type = row[header.index("battle_type")]

                kill_obj.killer_ship_name = row[header.index("killer_ship_type")] or None
                kill_obj.victim_ship_name = row[header.index("victim_ship_type")] or None
                system_name = row[header.index("system")]
                if system_name not in system_cache:
                    system = conn.query(System).filter(System.name == system_name).first()
                    if system is not None:
                        # noinspection PyTypeChecker
                        system_cache[system_name] = system
                if system_name in system_cache:
                    kill_obj.system_id = system_cache[system_name].id
                if kill_obj.victim_ship_name not in ship_cache or kill_obj.killer_ship_name not in ship_cache:
                    ships = conn.query(Item).filter(
                        Item.name.in_([kill_obj.victim_ship_name, kill_obj.killer_ship_name])).all()
                    for ship in ships:
                        # noinspection PyTypeChecker
                        ship_cache[ship.name] = ship
                if kill_obj.victim_ship_name in ship_cache:
                    kill_obj.victim_ship_id = ship_cache[kill_obj.victim_ship_name].id
                if kill_obj.killer_ship_name in ship_cache:
                    kill_obj.killer_ship_id = ship_cache[kill_obj.killer_ship_name].id
                conn.add(kill_obj)
            conn.commit()

    def get_killmail_leaderboard(self, killer_corp: str, amount: int) -> List[Tuple[str, int]]:
        start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with (Session(self.engine) as conn):
            # SELECT killer_name, SUM(isk)
            # FROM mobi_killmails
            # WHERE date_killed >= '2023-12-01'
            # GROUP BY killer_name
            # ORDER BY SUM(isk) DESC;
            res = (
                conn
                .query(MobiKillmail.killer_name, func.sum(MobiKillmail.isk))
                .filter(
                    and_(MobiKillmail.date_killed >= start_of_month,
                         MobiKillmail.killer_corp == killer_corp)
                ).group_by(MobiKillmail.killer_name)
                .order_by(func.sum(MobiKillmail.isk).desc())
                .limit(amount))
        result = []
        for name, isk in res:  # type: str, int
            result.append((name, isk))
        return result

    def get_top_killmails(self, killer_corp: str, amount: int) -> List[MobiKillmail]:
        start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with (Session(self.engine, expire_on_commit=False) as conn):
            res = (
                conn
                .query(MobiKillmail)
                .filter(
                    and_(MobiKillmail.date_killed >= start_of_month,
                         MobiKillmail.killer_corp == killer_corp)
                ).order_by(MobiKillmail.isk.desc())
                .limit(amount))
            return list(res)


class DatabaseInitializer(UniverseDatabase):
    regions = ["Aridia", "Black Rise", "Branch", "Cache", "Catch", "Cloud Ring", "Curse", "Deklein", "Delve", "Derelik",
               "Detorid", "Devoid", "Domain", "Esoteria", "Essence", "Everyshore", "Fade", "Feythabolis", "Fountain",
               "Geminate", "Genesis", "Great Wildlands", "Heimatar", "Immensea", "Impass", "Insmother", "Kador",
               "Khanid", "Kor-Azor", "Lonetrek", "Metropolis", "Molden Heath", "Omist", "Outer Ring", "Paragon Soul",
               "Period Basis", "Placid", "Providence", "Pure Blind", "Querious", "Region", "Scalding Pass",
               "Sinq Laison", "Solitude", "Stain", "Syndicate", "Tash-Murkon", "Tenal", "Tenerifis", "The Bleak Lands",
               "The Citadel", "The Forge", "Tribute", "Vale of the Silent", "Venal", "Verge Vendor", "Wicked Creek"]
    cleanup_celestials = [
        Celestial.Type.region.groupID,
        Celestial.Type.constellation.groupID,
        Celestial.Type.system.groupID,
        Celestial.Type.asteroid_belt.groupID,
        Celestial.Type.unknown_anomaly.groupID
    ]

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

                    if line_i % 500 == 0:
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
        file_length = get_file_len(path)
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

    def auto_init_item_types(self, item_types: Dict[str, Tuple[int, int]]):
        with self.engine.begin() as conn:
            logger.info("Initializing item types for database")
            for k, v in item_types.items():
                logger.info("Updating item type for [%s, %s] to '%s'", v[0], v[1], k)
                stmt = (update(Item)
                        .where(between(Item.id, v[0], v[1]))
                        .values(type=k)
                        )
                conn.execute(stmt)
            logger.info("Item types updated")

    def auto_init_stargates(self, file_path: str):
        file_length = get_file_len(file_path)
        with open(file_path, "r") as f:
            with Session(self.engine) as conn:
                line_i = 0
                logger.info("Loading stargates")
                for line in f:
                    line_i += 1
                    if line_i == 1:
                        continue
                    if line_i % 500 == 0:
                        logger.info("Processing line %s/%s (%s)", line_i, file_length,
                                    "{:.2%}".format(line_i / file_length))
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
                conn.commit()
            logger.info("Stargates loaded")

    def auto_cleanup_db(self):
        with self.engine.begin() as conn:
            # Delete Regions that are unavailable:
            stmt = (
                delete(Region)
                .where(Region.name.notin_(DatabaseInitializer.regions))
            )
            result = conn.execute(stmt)
            logger.info("Deleted %s inactive regions", result.rowcount)

            # Cleanup celestials
            logger.info("Deleting wrong celestials from database")
            stmt = (
                delete(Celestial)
                .where(Celestial.group_id.in_(DatabaseInitializer.cleanup_celestials))
            )
            result = conn.execute(stmt)
            logger.info("Deleted %s celestials from database", result.rowcount)

    def auto_fix_systems(self, path: str, separator: str):
        with Session(self.engine) as conn:
            logger.info("Fixing systems")
            file_length = get_file_len(path)
            with open(path, "r") as file:
                line_i = 0
                for line in file:
                    line_i += 1
                    if line_i == 1:
                        continue
                    if line_i % 3000 == 0:
                        logger.info("Processing line %s/%s (%s)", line_i, file_length,
                                    "{:.2%}".format(line_i / file_length))
                        conn.commit()
                    line = line.replace("\n", "").split(separator)
                    # Planet ID;Region;Constellation;System;Planet Name;Planet Type;Resource;Richness;Output
                    p_id = int(line[0])
                    r_name = line[1]
                    c_name = line[2]
                    s_name = line[3]
                    planet = (
                        conn.query(Celestial)
                        .options(
                            joinedload(Celestial.system)
                            .joinedload(System.constellation)
                            .joinedload(Constellation.region)
                        ).filter(Celestial.id == p_id)
                    ).first()
                    system = planet.system
                    constellation = system.constellation
                    if system.name != s_name:
                        logger.error("Planet %s system %s does not match with db %s", p_id, s_name, system.name)
                    if constellation.name != c_name:
                        logger.warning("System %s constellation %s does not match with db %s", s_name, c_name,
                                       constellation.name)
                        constellation = conn.query(Constellation).filter(Constellation.name == c_name).first()
                        system.constellation_id = constellation.id
                        system.region_id = constellation.region.id
                        conn.commit()
                        logger.info("Fixed constellation")
                    if constellation.region.name != r_name:
                        logger.error("System '%s' region '%s' does not match with db '%s'", s_name, r_name,
                                     constellation.region.name)
                    if system.region_id != constellation.region_id:
                        logger.error("System %s has wrong region id %s, expected %s", system.name, system.region_id,
                                     constellation.region_id)


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
            "universe_name": (str, "N/A")
        },
        "google_sheet": (str, "N/A"),
        "project_resources": (list, [],),
        "pytesseract_cmd_path": (str, "N/A"),
    }
    path = input("Enter path to config: ")
    if path.strip() == "":
        path = "../../config.json"
    config = Config()
    config.load_tree(config_structure)
    config.load_config(path)
    db = DatabaseInitializer(
        username=config["db.user"],
        password=config["db.password"],
        port=config["db.port"],
        host=config["db.host"],
        database=config["db.universe_name"])
    while True:
        print("Available modes:\n"
              "  i to initialize planetary production database\n"
              "  t to initialize item types\n"
              "  s to initialize system connections\n"
              "  f to fix systems (correct their constellations)\n"
              "  c to clean up database and delete wrong systems (execute 'f' beforehand!)\n"
              "  Recommended order: (i), (t), s, f, c")
        inp = input("Please enter the selection: ").casefold()
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
        elif inp == "c".casefold():
            db.auto_cleanup_db()
        elif inp == "f".casefold():
            print("Required format for planetary_production.csv")
            print("Planet ID;Region;Constellation;System;Planet Name;Planet Type;Resource;Richness;Output")
            path = input("Enter path to planetary_production.csv: ")
            db.auto_fix_systems(path, ";")
        else:
            print(f"Error, unknown input '{inp}'")
        logger.info("Completed")
