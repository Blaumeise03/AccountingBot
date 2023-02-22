import enum
from typing import List

from sqlalchemy import String, ForeignKey, Float, Enum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Region(Base):
    __tablename__ = "region"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    constellations: Mapped[List["Constellation"]] = relationship(back_populates="region")

    def __repr__(self) -> str:
        return f"Region(id={self.id!r}, name={self.name!r})"


class Constellation(Base):
    __tablename__ = "constellation"
    id: Mapped[int] = mapped_column(primary_key=True)
    region_id = mapped_column(ForeignKey("region.id", name="key_const_reg"))
    region: Mapped[Region] = relationship(back_populates="constellations")
    name: Mapped[str] = mapped_column(String(30))
    systems: Mapped[List["System"]] = relationship(back_populates="constellation")

    def __repr__(self) -> str:
        return f"Constellation(id={self.id!r}, name={self.name!r}, region={self.region.name!r})"


class System(Base):
    __tablename__ = "system"
    id: Mapped[int] = mapped_column(primary_key=True)
    constellation_id = mapped_column(ForeignKey("constellation.id", name="key_sys_const"))
    constellation: Mapped[Constellation] = relationship(back_populates="systems")
    name: Mapped[str] = mapped_column(String(30))
    planets: Mapped[List["Planet"]] = relationship(back_populates="system")

    def __repr__(self) -> str:
        return f"System(id={self.id!r}, name={self.name!r}, const={self.constellation.name!r})"


class PlanetType(enum.Enum):
    ice = "Ice"
    oceanic = "Oceanic"
    temperate = "Temperate"
    barren = "Barren"
    lava = "Lava"
    gas = "Gas"
    storm = "Storm"
    plasma = "Plasma"

    @staticmethod
    def from_str(label: str):
        for p_type in PlanetType:
            if p_type.value.casefold() == label.casefold():
                return p_type
        return None


class Planet(Base):
    __tablename__ = "planet"
    id: Mapped[int] = mapped_column(primary_key=True)
    system_id = mapped_column(ForeignKey("system.id", name="key_planet_sys"))
    system: Mapped[System] = relationship(back_populates="planets")
    name: Mapped[str] = mapped_column(String(30))
    type: Mapped[PlanetType] = mapped_column(Enum(PlanetType))
    resources: Mapped[List["Resource"]] = relationship(back_populates="planet")

    def __repr__(self) -> str:
        return f"Planet(id={self.id!r}, name={self.name!r}, system={self.system.name!r})"


class ResourceType(Base):
    __tablename__ = "resource_type"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    type: Mapped[str] = mapped_column(String(10), primary_key=True, nullable=True)

    def __repr__(self) -> str:
        return f"ResourceType(id={self.id!r}, name={self.name!r}, type={self.type!r})"


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
    __tablename__ = "resources"
    planet_id: Mapped[int] = mapped_column(ForeignKey("planet.id", name="key_res_planet"), primary_key=True)
    planet: Mapped[Planet] = relationship(back_populates="resources")
    type_id: Mapped[int] = mapped_column(ForeignKey("resource_type.id", name="key_res_type"), primary_key=True)
    type: Mapped[ResourceType] = relationship()
    output: Mapped[float] = mapped_column(Float())
    richness: Mapped[int] = mapped_column(Enum(Richness))
