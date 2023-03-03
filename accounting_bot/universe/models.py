import enum
from typing import List, Optional, TYPE_CHECKING

from sqlalchemy import String, ForeignKey, Float, Enum, BigInteger, Integer, Table, Column, Boolean, \
    ForeignKeyConstraint, DateTime, func, TIMESTAMP, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Region(Base):
    # Eve Online Static Export CSV format:
    # regionID,regionName,x,y,z,xMin,xMax,yMin,yMax,zMin,zMax,factionID,nebula,radius
    __tablename__ = "region"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    x: Mapped[int] = mapped_column(BigInteger, nullable=True)
    y: Mapped[int] = mapped_column(BigInteger, nullable=True)
    z: Mapped[int] = mapped_column(BigInteger, nullable=True)
    faction_id: Mapped[int] = mapped_column(Integer, nullable=True)
    radius: Mapped[int] = mapped_column(BigInteger, nullable=True)
    constellations: Mapped[List["Constellation"]] = relationship(back_populates="region")

    def __repr__(self) -> str:
        return f"Region(id={self.id!r}, name={self.name!r})"


class Constellation(Base):
    # Eve Online Static Export CSV format:
    # regionID,constellationID,constellationName,x,y,z,xMin,xMax,yMin,yMax,zMin,zMax,factionID,radius
    __tablename__ = "constellation"
    id: Mapped[int] = mapped_column(primary_key=True)
    region_id = mapped_column(ForeignKey("region.id", name="key_const_reg", ondelete="CASCADE"))
    region: Mapped[Region] = relationship(back_populates="constellations")
    name: Mapped[str] = mapped_column(String(30))
    x: Mapped[int] = mapped_column(BigInteger, nullable=True)
    y: Mapped[int] = mapped_column(BigInteger, nullable=True)
    z: Mapped[int] = mapped_column(BigInteger, nullable=True)
    faction_id: Mapped[int] = mapped_column(Integer, nullable=True)
    radius: Mapped[int] = mapped_column(BigInteger, nullable=True)
    systems: Mapped[List["System"]] = relationship(back_populates="constellation")

    def __repr__(self) -> str:
        return f"Constellation(id={self.id!r}, name={self.name!r})"


SystemConnections = Table(
    "system_gates", Base.metadata,
    Column("a", Integer, ForeignKey("solarsystem.id", name="key_sysgates_a", ondelete="CASCADE")),
    Column("b", Integer, ForeignKey("solarsystem.id", name="key_sysgates_b", ondelete="CASCADE")))


class System(Base):
    # Eve Online Static Export CSV format:
    # regionID,constellationID,solarSystemID,solarSystemName,x,y,z,xMin,xMax,yMin,yMax,zMin,zMax,luminosity,border,fringe,corridor,hub,international,regional,constellation,security,factionID,radius,sunTypeID,securityClass
    __tablename__ = "solarsystem"
    id: Mapped[int] = mapped_column(primary_key=True)
    region_id = mapped_column(ForeignKey("region.id", name="key_sys_reg"))
    constellation_id = mapped_column(ForeignKey("constellation.id", name="key_sys_const", ondelete="CASCADE"))
    constellation: Mapped[Constellation] = relationship(back_populates="systems")
    name: Mapped[str] = mapped_column(String(30), index=True)
    x: Mapped[int] = mapped_column(BigInteger, nullable=True)
    y: Mapped[int] = mapped_column(BigInteger, nullable=True)
    z: Mapped[int] = mapped_column(BigInteger, nullable=True)
    security: Mapped[int] = mapped_column(Float, nullable=True)
    faction_id: Mapped[int] = mapped_column(Integer, nullable=True)
    radius: Mapped[int] = mapped_column(BigInteger, nullable=True)
    security_class: Mapped[str] = mapped_column(String(5), nullable=True)
    celestials: Mapped[List["Celestial"]] = relationship(back_populates="system")
    planets: List["Celestial"]
    stargates: Mapped[List["System"]] = relationship("System",
                                                     secondary=SystemConnections,
                                                     primaryjoin=SystemConnections.c.b == id,
                                                     secondaryjoin=SystemConnections.c.a == id)

    @property
    def planets(self) -> List["Celestial"]:
        planet_list = []
        for celestial in self.celestials:
            if celestial.type == Celestial.Type.planet:
                planet_list.append(celestial)
        return planet_list

    def __repr__(self) -> str:
        return f"System(id={self.id!r}, name={self.name!r})"


class Celestial(Base):
    class GroupID(object):
        def __init__(self, group_id: Optional[int] = None):
            self.groupID = group_id

    class TypeID(object):
        def __init__(self, type_id: Optional[int] = None):
            self.typeID = type_id

    class TypeGroupID(TypeID, GroupID):
        def __init__(self, type_id: Optional[int] = None, group_id: Optional[int] = None):
            super().__init__(type_id)
            self.groupID = group_id

    class NamedTypeID(TypeID):
        def __init__(self, type_name: str, type_id: Optional[int] = None):
            super().__init__(type_id)
            self.type_name = type_name

    class Type(TypeGroupID, enum.Enum):
        region = 3, 3
        constellation = 4, 4
        system = 5, 5
        star = None, 6
        planet = None, 7
        moon = None, 8
        asteroid_belt = 15, 9
        stargate = None, 10
        npc_station = None, 15
        unknown_anomaly = None, 995

        def __repr__(self):
            return 'MapType(%s, type_id %r, group_id %r)' % (self.__name__, self.typeID, self.groupID)

        @staticmethod
        def from_group_id(group_id):
            for c_type in Celestial.Type:
                if c_type.groupID == group_id:
                    return c_type
            return None

    class PlanetType(NamedTypeID, enum.Enum):
        ice = "Ice", 12
        oceanic = "Oceanic", 2014
        temperate = "Temperate", 11
        barren = "Barren", 2016
        lava = "Lava", 2015
        gas = "Gas", 13
        storm = "Storm", 2017
        plasma = "Plasma", 2063
        unknown = "N/A", 30889

        @staticmethod
        def from_str(label: str):
            for p_type in Celestial.PlanetType:
                if p_type.name == label.casefold():
                    return p_type
            return None

        @staticmethod
        def from_type_id(type_id: int):
            for p_type in Celestial.PlanetType:
                if p_type.typeID == type_id:
                    return p_type
            return None

    # Eve Online Static Export CSV format:
    # itemID,typeID,groupID,solarSystemID,constellationID,regionID,orbitID,x,y,z,radius,itemName,security,celestialIndex,orbitIndex
    __tablename__ = "celestial"
    id: Mapped[int] = mapped_column(primary_key=True)
    type_id: Mapped[int] = mapped_column(Integer, nullable=True)
    group_id: Mapped[int] = mapped_column(Integer, nullable=True)
    system_id = mapped_column(ForeignKey("solarsystem.id", name="key_celest_sys", ondelete="CASCADE"))
    system: Mapped[System] = relationship(back_populates="celestials")
    orbit_id: Mapped[int] = mapped_column(Integer, nullable=True)
    x: Mapped[int] = mapped_column(BigInteger, nullable=True)
    y: Mapped[int] = mapped_column(BigInteger, nullable=True)
    z: Mapped[int] = mapped_column(BigInteger, nullable=True)
    radius: Mapped[int] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(30))
    security: Mapped[int] = mapped_column(Float, nullable=True)
    celestial_index: Mapped[int] = mapped_column(Integer, nullable=True)
    orbit_index: Mapped[int] = mapped_column(Integer, nullable=True)
    resources: Mapped[List["Resource"]] = relationship(back_populates="planet")

    @hybrid_property
    def type(self) -> Type:
        return Celestial.Type.from_group_id(self.group_id)

    @hybrid_property
    def planet_type(self) -> PlanetType:
        return Celestial.PlanetType.from_type_id(self.type_id)

    def __repr__(self) -> str:
        return "Celestial(id={id!s}, name={name!s}, system={system_name!s}, type={type!s})".format(
            id=self.id,
            name=self.name,
            system_name=self.system.name if self.system is not None else "None",
            type=self.type.name if self.type is not None else self.type_id
        )


class Item(Base):
    __tablename__ = "item"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), index=True)
    type: Mapped[str] = mapped_column(String(15), nullable=True)
    prices: Mapped[List["MarketPrice"]] = relationship()

    def __repr__(self) -> str:
        return f"Item(id={self.id!r}, name={self.name!r}, type={self.type!r})"


class MarketPrice(Base):
    __tablename__ = "price"
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", name="key_item_price"), primary_key=True)
    item: Mapped[Item] = relationship(back_populates="prices")
    price_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    price_value: Mapped[float] = mapped_column(Float)
    last_updated = mapped_column(TIMESTAMP, onupdate=func.now(), server_default=text('CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP'))


class Richness(enum.Enum):
    poor = "poor"
    medium = "medium"
    rich = "rich"
    perfect = "perfect"

    @staticmethod
    def from_str(label: str):
        for p_type in Richness:
            if p_type.value.casefold() == label.casefold():
                return p_type
        return None


class Resource(Base):
    # Eve Echoes PI Static Data Export CSV format:
    # Planet ID;Region;Constellation;System;Planet Name;Planet Type;Resource;Richness;Output
    __tablename__ = "resources"
    planet_id: Mapped[int] = mapped_column(ForeignKey("celestial.id", name="key_res_planet", ondelete="CASCADE"), primary_key=True)
    planet: Mapped[Celestial] = relationship(back_populates="resources")
    type_id: Mapped[int] = mapped_column(ForeignKey("item.id", name="key_res_item"), primary_key=True)
    type: Mapped[Item] = relationship()
    output: Mapped[float] = mapped_column(Float())
    richness: Mapped[int] = mapped_column(Enum(Richness))

    def __repr__(self) -> str:
        return f"Resource(planet={self.planet_id}, res={self.type_id})"


class PiPlanSettings(Base):
    __tablename__ = "piplan"
    user_id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    plan_num: Mapped[int] = mapped_column(Integer(), primary_key=True)
    user_name: Mapped[str] = mapped_column(String(40))
    arrays: Mapped[int] = mapped_column(Integer(), default=0)
    planets: Mapped[int] = mapped_column(Integer(), default=0)
    resources: Mapped[List["PiPlanResource"]] = relationship(back_populates="piplan", cascade="all, delete-orphan")
    constellation_id: Mapped[int] = mapped_column(ForeignKey("constellation.id", name="key_piplan_const"), nullable=True)
    constellation: Mapped[Constellation] = relationship()
    preferred_prices: Mapped[str] = mapped_column(String(100), nullable=True)


class PiPlanResource(Base):
    __tablename__ = "piplan_resource"
    user_id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    plan_num: Mapped[int] = mapped_column(Integer(), primary_key=True)
    piplan: Mapped[PiPlanSettings] = relationship(back_populates="resources")
    planet_id: Mapped[int] = mapped_column(Integer(), primary_key=True)
    type_id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    resource: Mapped[Resource] = relationship(lazy="joined")
    arrays: Mapped[int] = mapped_column(Integer(), default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (ForeignKeyConstraint([user_id, plan_num],
                                           [PiPlanSettings.user_id, PiPlanSettings.plan_num],
                                           name="fk_pires_piplan"),
                      ForeignKeyConstraint([planet_id, type_id],
                                           [Resource.planet_id, Resource.type_id],
                                           name="fk_pires_res"),
                      {})
