import base64
import difflib
import itertools
import logging
import math
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable, Coroutine, Any, Union

import discord
from discord import User, Embed, Color, ApplicationContext, Message, InputTextStyle
from discord.ui import InputText, Button

from accounting_bot import sheet, utils
from accounting_bot.exceptions import PlanetaryProductionException, PiPlanerException
from accounting_bot.universe import data_utils
from accounting_bot.universe.models import PiPlanSettings, PiPlanResource
from accounting_bot.utils import ErrorHandledModal, AutoDisableView, Item

logger = logging.getLogger("data.pi")
item_prices = {}  # type: Dict[str,Dict[str, Union[int, float]]]
available_prices = []
pending_resources = {}  # type: Dict[str, float]
last_reload = datetime(1907, 1, 1)
pi_resources = []
pi_ids = {}  # type: Dict[str, int]
help_embed = None  # type: Embed | None
autoarray_help_a = "N/A"
autoarray_help_b = "N/A"


async def reload_pending_resources():
    global pending_resources, pi_resources, last_reload, pi_ids
    difference = datetime.now() - last_reload
    if difference < timedelta(minutes=15):
        return
    logger.info("Reloading pending resources")
    items = await sheet.load_pending_resources()
    pending_resources = items
    res = await data_utils.get_items_by_type("pi")
    pi_resources = list(map(lambda i: i.name, res))
    pi_ids = dict(map(lambda i: (i.name, i.id), res))
    last_reload = datetime.now()
    logger.info("Pending resources loaded")


def get_price(item: str, price_types: List[str]) -> Optional[Union[float, int]]:
    if item not in item_prices or len(price_types) == 0:
        return None
    prices = item_prices[item]
    if price_types[0].casefold() == "max".casefold():
        return max(prices.values())
    p = 0
    for t, v in prices.items():
        if v > p and t in price_types:
            p = v
    return p


def build_debug_table(debug_data: Dict):
    d_msg = "**Normalized + Ideal**\n```\nResource      Normalized Weight  Ideal"
    for n, w in debug_data["normalized"].items():
        if w <= 0:
            continue
        d_msg += f"\n{n:<21} {w:9,.0f}  {debug_data['ideal'][n]:4.2f}"
    d_msg += "\n```\n**Selection**\n```Resource                 Out  Eff   AW   RW  NRW"
    for v in debug_data["selection"]:
        d_msg += (f"\n{v['res']:<21} "
                  f"{v['out']:5.2f} "
                  f"{v['eff']:5.1%} "
                  f"{v['aweight']:4.2} "
                  f"{v['rweight']:4.2f} "
                  f"{v['nrweight']:4.2f}")
        if len(d_msg) > 900:
            d_msg += "\n(Truncated)"
            break
    d_msg += "\n```"
    return d_msg


class Array:
    def __init__(self,
                 resource: Optional[str] = None,
                 base_output: Optional[int] = None,
                 amount: Optional[int] = None,
                 locked: Optional[bool] = False) -> None:
        self.resource = resource  # type: str | None
        self.resource_id = None
        self.base_output = base_output  # type: int | None
        self.amount = amount  # type: int | None
        self.planet = None  # type: Planet | None
        self.locked = locked  # type: bool

    def __repr__(self) -> str:
        return f"Array({self.planet}:{self.resource}, out={self.base_output} {'locked ' if self.locked else ''})"

    def auto_init_planet(self, arrays: List["Array"], p_name: str, p_id: int):
        for arr in arrays:
            if arr.planet.id == p_id:
                self.planet = arr.planet
                return
        self.planet = Planet(p_id=p_id, name=p_name)

    @staticmethod
    def build_table(arrays: List["Array"], mode="LnRhdi", price_types: List[str] = None):
        if price_types is None:
            price_types = []
        msg = ""
        for m in mode:
            match m:
                # Caps = Align left, lowercase = align right
                case "L":
                    msg += "🔒"
                case "l":
                    msg += "🔒"
                case "n":
                    msg += " n"
                case "R":
                    msg += "Resource             "
                case "P":
                    msg += f"Planet     "
                case "b":
                    msg += "  Base"
                case "h":
                    msg += "items/h"
                case "d":
                    msg += "items/d"
                case "i":
                    msg += "    ISK/d"
                case _:
                    msg += m
        msg += "\n"
        for i, array in enumerate(arrays):
            for m in mode:
                match m:
                    case "L":
                        msg += "🔒" if array.locked else "  "
                    case "l":
                        msg += "🔒" if array.locked else "  "
                    case "n":
                        msg += f"{i:>2}"
                    case "R":
                        msg += f"{array.resource:<21}"
                    case "P":
                        msg += f"{array.planet.name:<11}"
                    case "b":
                        msg += f"{array.base_output:6.2f}"
                    case "h":
                        msg += f"{array.base_output * array.amount:6.1f}"
                    case "d":
                        msg += f"{array.base_output * array.amount * 24:7,.0f}"
                    case "i":
                        msg += f"{array.base_output * array.amount * 24 * get_price(array.resource, price_types):9,.0f}"
                    case _:
                        msg += m
            msg += "\n"
        return msg

    @staticmethod
    def build_income_table(
            arrays: Optional[List["Array"]] = None,
            income_sum: Optional[float] = None,
            price_types: List[str] = None):
        if price_types is None:
            price_types = []
        if arrays is None and income_sum is None:
            raise TypeError("Expected at least one argument for function build_income_table")
        if income_sum is None:
            income_sum = 0
            for array in arrays:
                income_sum += array.base_output * array.amount * get_price(array.resource, price_types)
        msg = (f"Zeitraum           Einnahmen\n"
               f"Pro Tag   {income_sum:14,.0f} ISK\n"
               f"Pro Woche {income_sum * 24 * 7:14,.0f} ISK\n"
               f"Pro Monat {income_sum * 24 * 7 * 30:14,.0f} ISK")
        return msg


class Planet:
    def __init__(self, p_id: Optional[int] = None, name: Optional[str] = None) -> None:
        self.id = p_id  # type: int | None
        self.name = name  # type: str | None

    def __repr__(self) -> str:
        return f"Planet({self.name})"


class PiPlaner:
    def __init__(self,
                 user_id: Optional[int] = None,
                 plan_num: Optional[int] = 0,
                 user_name: Optional[str] = None,
                 arrays: Optional[int] = 0,
                 planets: Optional[int] = 0) -> None:
        self.user_id = user_id  # type: int | None
        self.plan_num = plan_num  # type: int
        self.user_name = user_name  # type: str | None
        self.constellation_id = None  # type: int | None
        self.constellation_name = None  # type: str | None
        self.num_arrays = arrays  # type: int
        self.num_planets = planets  # type: int
        self.arrays = []  # type: List[Array]
        self.preferred_prices = []

    async def load_settings(self, settings: Optional[PiPlanSettings] = None):
        if settings is None:
            settings = await data_utils.get_pi_plan(self.user_id, self.plan_num)
        if settings is None:
            return
        self.user_id = settings.user_id
        self.plan_num = settings.plan_num
        self.user_name = settings.user_name
        self.num_arrays = settings.arrays
        self.num_planets = settings.planets
        planets = {}  # type: Dict[int, Planet]
        self.arrays = []
        for res in settings.resources:  # type: PiPlanResource
            array = Array(res.resource.type.name, res.resource.output, res.arrays, res.locked)
            self.arrays.append(array)
            if res.planet_id in planets:
                array.planet = planets[res.planet_id]
            else:
                planet = Planet(res.resource.planet.id, res.resource.planet.name)
                planets[res.planet_id] = planet
                array.planet = planet
        if settings.constellation is not None:
            self.constellation_id = settings.constellation_id
            self.constellation_name = settings.constellation.name
        self.preferred_prices.clear()
        if settings.preferred_prices is not None:
            for price in settings.preferred_prices.split(";"):
                self.preferred_prices.append(price)

    async def save_settings(self):
        await data_utils.save_pi_plan(self)

    async def load_missing_data(self):
        missing_planets = []
        p_id = None
        for array in self.arrays:
            if p_id is None and array.planet.id is not None:
                p_id = array.planet.id
            if array.base_output is None or array.planet.name == "N/A":
                missing_planets.append(array.planet.id)
        planets = await data_utils.get_planets(missing_planets)
        for planet in planets:
            for array in self.arrays:
                if array.planet.id == planet.id:
                    array.planet.name = planet.name
                    for res in planet.resources:
                        if res.type.name == array.resource:
                            array.base_output = res.output
                            break
        if self.constellation_id is None and p_id is not None:
            const = await data_utils.get_constellation(planet_id=p_id)
            self.constellation_id = const.id
            self.constellation_name = const.name

    def sort_arrays(self):
        self.arrays = sorted(self.arrays, key=lambda a: (not a.locked, utils.resource_order.index(a.resource)))

    def get_next_best_array(self, all_planets: List[Dict[str, Any]], arrays: List[Array]):
        best_array = None
        best_price = None
        for res_name in item_prices.keys():
            price = get_price(res_name, self.preferred_prices)
            for p in all_planets:
                if p["res"] == res_name:
                    found = False
                    for arr in arrays:
                        if arr.planet.name == p["p_name"]:
                            found = True
                            break
                    if found:
                        continue
                    price = price * p["out"]
                    if best_price is None or price > best_price:
                        best_price = price
                        best_array = p
                    break
        if best_array is None:
            return None
        array = Array(
            resource=best_array["res"],
            base_output=best_array["out"],
            amount=self.num_arrays
        )
        array.auto_init_planet(arrays, p_name=best_array["p_name"], p_id=best_array["p_id"])
        return array

    async def auto_select(self):
        all_planets = await data_utils.get_all_pi_planets(self.constellation_name)
        free_planets = self.num_planets
        arrays = []
        for arr in self.arrays:
            if arr.locked:
                free_planets -= 1
                arrays.append(arr)
        if free_planets <= 0:
            return
        while len(arrays) < self.num_planets:
            # Find next array
            array = self.get_next_best_array(all_planets, arrays)
            if array is None:
                break
            arrays.append(array)
        return arrays

    async def auto_select_weighted(self, weights: Dict[str, float], debug_data=None):
        free_planets = self.num_planets
        arrays = []
        for arr in self.arrays:
            if arr.locked:
                free_planets -= 1
                arrays.append(arr)
        if free_planets <= 0:
            return

        all_planets = await data_utils.get_all_pi_planets(self.constellation_name, resource_names=list(weights.keys()))
        max_planets = await data_utils.get_max_pi_planets()
        # Normalize weights (amount of arrays*hours needed when using the max planet in New Eden)
        for item in weights:
            if item in max_planets and weights[item] > 1:
                weights[item] = weights[item] / max_planets[item]
        if debug_data is not None:
            debug_data["normalized"] = {k: v for k, v in sorted(weights.items(), key=lambda i: i[1], reverse=True)}
        # Calculate the ideal amount of planets
        sum_w = sum(filter(lambda i: i > 0, weights.values()))
        for item in weights:
            weights[item] = weights[item] / sum_w * free_planets
        if debug_data is not None:
            debug_data["ideal"] = {k: v for k, v in sorted(weights.items(), key=lambda i: i[1], reverse=True)}
            debug_data["selection"] = []
        while len(arrays) < self.num_planets:
            if len(all_planets) == 0:
                break
            # Factoring in the efficiency of the planets into the weight, as good planets should get prioritized
            for p in all_planets:
                p["eff"] = p["out"] / max_planets[p["res"]]
                p["weight"] = p["eff"] * weights[p["res"]]

            # Get the planet with the highest weight
            all_planets.sort(key=lambda p: p["weight"], reverse=True)
            next_array = all_planets.pop(0)
            debug_array = None
            if debug_data is not None:
                debug_array = {"res": next_array["res"],
                               "out": next_array["out"],
                               "eff": next_array["eff"],
                               "aweight": next_array["weight"],
                               "rweight": weights[next_array["res"]]}

            # Reduce the targeted weights
            sum_w = sum(filter(lambda i: i > 0, weights.values()))
            weights[next_array["res"]] = weights[next_array["res"]] - (1 / free_planets) * sum_w
            if debug_data is not None:
                debug_array["nrweight"] = weights[next_array["res"]]
                debug_data["selection"].append(debug_array)
            array = Array(
                resource=next_array["res"],
                base_output=next_array["out"],
                amount=self.num_arrays
            )
            array.auto_init_planet(arrays, next_array["p_name"], next_array["p_id"])
            arrays.append(array)
        return arrays

    def to_embed(self, color: Optional[Color] = Color.dark_grey()) -> Embed:
        self.sort_arrays()
        desc = f"Dies ist dein aktueller Pi Plan.\nMaximale Planeten: `{self.num_planets}`\n" \
               f"Maximale Fabriken: `{self.num_arrays}`\n"
        if self.constellation_id is not None:
            desc += f"Konstellation: `{self.constellation_name}`\n"
        if len(self.preferred_prices) > 0:
            desc += "Preispriorität: "
        else:
            desc += "*Keine Preispriorität festgelegt*"
        for price in self.preferred_prices:
            desc += f"`{price}` "
        emb = Embed(title=f"Pi Plan #{self.plan_num + 1}",
                    description=desc,
                    color=color)

        resources = {}
        for i, array in enumerate(self.arrays):
            array.amount = self.num_arrays
            if array.resource in resources:
                resources[array.resource] += array.base_output * array.amount
            else:
                resources[array.resource] = array.base_output * array.amount
        val = Array.build_table(self.arrays, mode="L n R P b h", price_types=self.preferred_prices)
        emb.add_field(name=f"Aktive Arrays", value=f"```\n{val}\n```", inline=False)
        val = f"{'Resource':<21}: items/h  items/d          ISK/d"
        resources = sorted(resources.items(), key=lambda res: utils.resource_order.index(res[0]))
        income_sum = 0
        for name, output in resources:
            income = 0
            price = get_price(name, self.preferred_prices)
            if price is not None:
                income = output * price
            income_sum += income
            val += f"\n{name:<21}: {output:7.2f}  {output * 24:7,.0f}  {income * 24:9,.0f} ISK"
        emb.add_field(name="Produktion", value=f"```\n{val}\n```", inline=False)
        emb.add_field(
            name="Einnahmen",
            value=f"```\n{Array.build_income_table(income_sum=income_sum)}\n```")
        return emb

    def encode_base64(self) -> str:
        """
        V1 binary data format (Number bits:value):

        [2:Version][5:num planets][5:num arrays] [19:planet index][5:resource index]... (for all arrays)

        The last byte gets filled up with zeros

        Note:
            planet index = planet id - 40000000
            resource index = item id - 42001000000 ( - 1000000 if it's a fuel)
        :return: the encoded data in base64
        """
        def to_bits(num: int, length: int):
            # noinspection PyStringFormat
            bits = f"{{0:0{length}b}}".format(num)
            if len(bits) > length:
                raise ArithmeticError(f"Number {num} is exceeding bit limit of {length}: {bits}")
            return bits

        def to_bytes(bits):
            done = False
            while not done:
                byte = 0
                for _ in range(0, 8):
                    try:
                        bit = next(bits)
                        if type(bit) == str:
                            if bit == "0":
                                bit = 0
                            elif bit == "1":
                                bit = 1
                            else:
                                raise TypeError(f"Expected bit, got '{bit}' instead")
                    except StopIteration:
                        bit = 0
                        done = True
                    byte = (byte << 1) | bit
                yield byte

        data = "01"
        if self.num_planets > 31:
            raise PiPlanerException(f"Number of planets {self.num_planets} is to high, max is 31")
        if self.num_arrays > 31:
            raise PiPlanerException(f"Number of arrays {self.num_arrays} is to high, max is 31")
        data += to_bits(self.num_planets, 5)
        data += to_bits(self.num_arrays, 5)
        for array in self.arrays:
            p_id = array.planet.id - 40000000
            if p_id < 0 or p_id > 524287:
                raise PiPlanerException(f"Planet id {p_id} is out of bounds")
            data += to_bits(p_id, 19)
            p_type = pi_ids[array.resource] - 42001000000
            # Pi have IDs 420010000xx
            if p_type >= 1000000:
                # Fuels have IDs with 420020000xx
                p_type -= 1000000
            if p_type > 31:
                raise PiPlanerException(f"Resource id {p_type} is to high")
            data += to_bits(p_type, 5)
        data_bytes = bytes(to_bytes(iter(data)))
        result = base64.b64encode(data_bytes)
        return result.decode("utf-8")

    @staticmethod
    def decode_base64(data: str):
        def groups_of_n(n, iterable):
            c = itertools.count()
            for _, gen in itertools.groupby(iterable, lambda x: math.floor(next(c) / n)):
                yield gen

        data_bytes = base64.b64decode(data)
        bits = "".join(["{:08b}".format(x) for x in data_bytes])
        version, bits = int(bits[:2], 2), bits[2:]
        if version != 1:
            raise PiPlanerException(f"Data format V{version} is not supported by this version")
        num_arrays, bits = int(bits[:5], 2), bits[5:]
        num_planets, bits = int(bits[:5], 2), bits[5:]
        lookup_table = dict(map(lambda t: (t[1] % 1000000, t[0]), pi_ids.items()))
        plan = PiPlaner(arrays=num_arrays, planets=num_planets)
        for raw in groups_of_n(24, bits):
            raw = "".join(raw)
            if len(raw) < 24:
                break
            p_id = int(raw[:19], 2) + 40000000
            p_res = int(raw[19:], 2)
            if p_res not in lookup_table:
                raise PiPlanerException(f"Resource ID {p_res} not found in lookup table")
            res_name = lookup_table[p_res]
            array = Array(res_name, amount=num_arrays)
            array.auto_init_planet(plan.arrays, p_name="N/A", p_id=p_id)
            plan.arrays.append(array)
        return plan


class PiPlanningSession:
    def __init__(self, user: User) -> None:
        self.user_id = user.id
        self.user = user
        self.plans = []  # type: List[PiPlaner]
        self._deleted = []  # type: List[PiPlaner]
        self._active = None
        self.main_view = None  # type: PiPlanningView | None
        self.message = None  # type: Message | None

    def set_active(self, plan: Union[str, int, None]):
        if len(self.plans) == 0:
            self._active = None
            return
        if plan is None or plan == "min":
            self._active = min(map(lambda p: p.plan_num, self.plans))
            return
        if type(plan) == int:
            for p in self.plans:
                if p.plan_num == plan:
                    self._active = plan
                    return
            self.set_active(None)
            return
        if self._active is None:
            self.set_active(None)
            return
        if plan == "prev":
            self._active = max(map(lambda p: p.plan_num, filter(lambda p: p.plan_num < self._active, self.plans)),
                               default=self._active)
            return
        if plan == "next":
            self._active = min(map(lambda p: p.plan_num, filter(lambda p: p.plan_num > self._active, self.plans)),
                               default=self._active)
            return

    def get_active_plan(self):
        if self._active is None:
            return None
        else:
            for plan in self.plans:
                if plan.plan_num == self._active:
                    return plan
        return None

    async def refresh_msg(self):
        if self._active is None:
            self.set_active(None)
        if self.message is not None:
            if self._active is not None:
                await self.message.edit(
                    content=f"Du hast {len(self.plans)} Pi Pläne\nAktuell is Plan #{self._active + 1} ausgewählt",
                    embed=self.get_active_plan().to_embed(Color.green()))
                return
            await self.message.edit(content="Du hast keinen Pi Plan, erstelle einen neuen.")
        else:
            logger.warning("Session %s:%s does not have an attached message", self.user_id, self.user.name)

    async def load_plans(self):
        self.plans.clear()
        plans = await data_utils.get_pi_plan(self.user_id)  # type: List[PiPlanSettings]
        if len(plans) == 0:
            self.plans.append(PiPlaner(
                user_id=self.user_id,
                user_name=self.user.name
            ))
        else:
            for settings in plans:
                plan = PiPlaner()
                await plan.load_settings(settings)
                self.plans.append(plan)

    async def save_plans(self):
        for plan in self.plans:
            await plan.save_settings()
        for plan in self._deleted:
            await data_utils.delete_pi_plan(plan)
        self._deleted.clear()

    def get_embeds(self) -> List[Embed]:
        if self._active is None:
            self.set_active(None)
        embeds = []
        if self._active is not None:
            embeds.append(self.get_active_plan().to_embed())
        return embeds

    def create_new_plan(self) -> PiPlaner:
        next_num = 0
        for plan in self.plans:
            if plan.plan_num >= next_num:
                next_num = plan.plan_num + 1
        plan = PiPlaner(self.user_id, next_num)
        self.plans.append(plan)
        return plan

    def add_plan(self, plan: PiPlaner):
        next_num = 0
        for p in self.plans:
            if p.plan_num >= next_num:
                next_num = p.plan_num + 1
        plan.user_id = self.user_id
        plan.user_name = self.user
        plan.plan_num = next_num
        self.plans.append(plan)

    def delete_plan(self, plan) -> None:
        self.plans.remove(plan)
        self._deleted.append(plan)
        self.set_active(None)


# noinspection PyUnusedLocal
class PiPlanningView(AutoDisableView):
    def __init__(self, planning_session: PiPlanningSession):
        super().__init__(timeout=60 * 20)
        self.session = planning_session
        self.session.main_view = self

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.red, row=0)
    async def btn_close(self, button: Button, ctx: ApplicationContext):
        await self.message.delete()
        await ctx.response.send_message(f"Um die Änderungen zu speichern, klicke auf 💾", ephemeral=True)

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.green, row=0)
    async def btn_save(self, button: Button, ctx: ApplicationContext):
        await ctx.response.defer(ephemeral=True)
        await self.session.save_plans()
        await ctx.followup.send("Änderungen gespeichert!", ephemeral=True)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.grey, row=0)
    async def btn_prev_plan(self, button: Button, ctx: ApplicationContext):
        self.session.set_active("prev")
        await ctx.response.defer(ephemeral=True, invisible=True)
        await self.session.refresh_msg()

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.grey, row=0)
    async def btn_next_plan(self, button: Button, ctx: ApplicationContext):
        self.session.set_active("next")
        await ctx.response.defer(ephemeral=True, invisible=True)
        await self.session.refresh_msg()

    @discord.ui.button(emoji="❓", style=discord.ButtonStyle.grey, row=0)
    async def btn_help(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_message(embed=help_embed, ephemeral=True)

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.red, row=1)
    async def btn_delete(self, button: Button, ctx: ApplicationContext):
        async def _delete(_ctx: ApplicationContext):
            self.session.delete_plan(self.plan)
            self.plan = None
            self.session.isEditing = False
            await _ctx.response.send_message("Plan gelöscht", ephemeral=True)
            await self.session.refresh_msg()

        await ctx.response.send_message("Willst Du diesen Plan wirklich löschen?", view=ConfirmView(_delete),
                                        ephemeral=True)

    @discord.ui.button(emoji="➕", style=discord.ButtonStyle.blurple, row=1)
    async def btn_new(self, button: Button, ctx: ApplicationContext):
        plan = self.session.create_new_plan()
        plan.user_id = ctx.user.id
        plan.user_name = ctx.user.name
        await self.session.refresh_msg()
        await ctx.response.send_message(f"Es wurde ein neuer Plan erstellt: #{plan.plan_num + 1}", ephemeral=True)

    @discord.ui.button(emoji="✏", style=discord.ButtonStyle.blurple, row=1)
    async def btn_basic(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self.session, "basic"))

    @discord.ui.button(emoji="🗺️", style=discord.ButtonStyle.blurple, row=1)
    async def btn_const(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self.session, "const"))

    @discord.ui.button(emoji="💸", style=discord.ButtonStyle.blurple, row=1)
    async def btn_prices(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self.session, "prices"))

    @discord.ui.button(emoji="🗑️", label="Array", style=discord.ButtonStyle.red, row=2)
    async def btn_del_array(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self.session, "del_array"))

    @discord.ui.button(emoji="🔓", label="Array", style=discord.ButtonStyle.blurple, row=2)
    async def btn_lock_array(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self.session, "lock_array"))

    @discord.ui.button(emoji="➕", label="Array", style=discord.ButtonStyle.blurple, row=2)
    async def btn_add_array(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self.session, "add_array"))

    @discord.ui.button(label="Auto", style=discord.ButtonStyle.blurple, row=2)
    async def btn_auto_add_array(self, button: Button, ctx: ApplicationContext):
        plan = self.session.get_active_plan()
        if plan is None:
            await ctx.response.send_message("Es ist kein Plan ausgewählt!", ephemeral=True)
            return
        await ctx.response.send_modal(AutoSelectArrayModal(self.session, plan))

    @discord.ui.button(label="Export", style=discord.ButtonStyle.grey, row=3)
    async def btn_export(self, button: Button, ctx: ApplicationContext):
        code = self.session.get_active_plan().encode_base64()
        emb = Embed(title="Export code",
                    description="Teile diesen Code um deinen Pi Plan zu teilen. Andere können mit diesem Code deinen "
                                "Plan importieren. Außerdem kannst du dir den Code als Backup speichern um deinen Plan "
                                "zu einem späteren Zeitpunkt wiederherstellen zu können.",
                    color=Color.gold())
        emb.add_field(name="Code", value=f"```\n{code}\n```")
        await ctx.response.send_message(embed=emb, ephemeral=True)

    @discord.ui.button(label="Import", style=discord.ButtonStyle.grey, row=3)
    async def btn_import(self, button: Button, ctx: ApplicationContext):
        async def _import(code, _ctx: ApplicationContext):
            await _ctx.response.defer(ephemeral=True, invisible=False)
            plan = PiPlaner.decode_base64(code)
            await plan.load_missing_data()
            self.session.add_plan(plan)
            self.session.set_active(plan.plan_num)
            await self.session.refresh_msg()
            await _ctx.followup.send("Plan hinzugefügt und aktiviert")
        await ctx.response.send_modal(
            StringInputModal(
                title="Import Plan",
                label="Code",
                placeholder="Hier Code eingeben",
                callback=_import
            )
        )


# noinspection PyUnusedLocal
class SelectArrayView(AutoDisableView):
    class NumberButton(discord.ui.Button):
        def __init__(self,
                     number: Union[str, int],
                     callback: Callable[[Union[str, int], ApplicationContext], Coroutine],
                     *args, **kwargs):
            super().__init__(label=f"{number}",
                             style=discord.ButtonStyle.blurple,
                             *args, **kwargs)
            self.number = number
            self.function = callback

        async def callback(self, ctx: ApplicationContext):
            await self.function(self.number, ctx)
            await self.view.message.edit(embed=self.view.build_embed())

    def __init__(self, planning_session: PiPlanningSession, plan: PiPlaner, title: str,
                 resources: List[Dict[str, Any]]):
        async def save_array(value: Union[str, int], ctx: ApplicationContext):
            if type(value) == int:
                if len(self.plan.arrays) >= self.plan.num_planets:
                    await ctx.response.send_message("Du hast bereits die maximale Anzahl an Planeten", ephemeral=True)
                    return

                num = int(value)
                res = resources[num]
                array = Array(resource=res["res"], base_output=res["out"], amount=plan.num_arrays)
                array.auto_init_planet(self.plan.arrays, p_name=res["p_name"], p_id=res["p_id"])
                for arr in self.plan.arrays:
                    if arr.planet.id == array.planet.id:
                        await ctx.response.send_message("Du hast bereits ein Array auf diesem Planeten",
                                                        ephemeral=True)
                        return
                plan.arrays.append(array)
                await ctx.response.send_message("Array wurde hinzugefügt", ephemeral=True)
                await planning_session.refresh_msg()
            else:
                await ctx.response.send_modal(
                    NumberInputModal(title="Nummer eingeben",
                                     label="Nummer",
                                     placeholder="Nummer des Planeten",
                                     callback=save_array))

        super().__init__(timeout=60 * 20)
        self.session = planning_session
        self.plan = plan
        self.title = title
        self.resources = resources
        amount = len(resources)
        for i in range(min(7, amount)):
            self.add_item(SelectArrayView.NumberButton(i, save_array))
        self.add_item(SelectArrayView.NumberButton("...", save_array))

    @discord.ui.button(emoji="🆗", style=discord.ButtonStyle.green)
    async def btn_close(self, button: Button, ctx: ApplicationContext):
        await self.message.delete()
        await ctx.response.defer(ephemeral=True)

    def build_embed(self) -> Embed:
        msg = "```\nA n Planet     : Output"
        for i, res in enumerate(self.resources):
            duplicate = False
            for array in self.plan.arrays:
                if array.planet.id == res["p_id"]:
                    duplicate = True
                    break
            msg += f"\n{'A' if duplicate else ' '}{i:>2} {res['p_name']:<11}: {res['out']:6.2f}"
        msg += "\n```"
        emb = Embed(title=f"{self.title}",
                    description="Drücke auf den Knopf mit der entsprechenden Zahl um diesen Planeten auszuwählen oder "
                                f"drücke auf `...` um eine Zahl einzugeben. Es wurden {len(self.resources)} Planeten "
                                f"gefunden. Planeten mit einem A vor der Zeile sind bereits aktiv")
        emb.add_field(name="Planeten", value=msg)
        return emb


def get_weights(raw: str):
    items = Item.parse_ingame_list(raw)
    if len(items) == 0:
        items = Item.parse_list(raw, skip_negative=True)
    return dict(map(lambda i: (i.name, i.amount), items))


class AutoSelectArrayModal(ErrorHandledModal):
    def __init__(self,
                 session: PiPlanningSession,
                 plan: PiPlaner,
                 title="Autoselect Arrays",
                 *args, **kwargs):
        super().__init__(title=title, *args, **kwargs)
        self.session = session
        self.plan = plan
        self.add_item(InputText(style=InputTextStyle.multiline,
                                label="Modus",
                                placeholder="\"ISK\", \"Projekte\" oder Itemliste\nOptional \"Debug\"",
                                required=True))

    async def callback(self, ctx: ApplicationContext):
        async def save(_ctx: ApplicationContext):
            self.session.get_active_plan().arrays = arrays
            await _ctx.response.send_message("Arrays geändert", ephemeral=True)
            await self.session.refresh_msg()

        await ctx.response.defer(ephemeral=True, invisible=False)
        inp = self.children[0].value
        debug_data = None
        re_debug = re.compile(re.escape('debug'), re.IGNORECASE)
        if re_debug.search(inp):
            debug_data = {}
            inp = re_debug.sub("", inp, 1)
        if inp.strip().casefold() == "ISK".casefold():
            arrays = await self.plan.auto_select()
        elif difflib.SequenceMatcher(None, "Projekt".casefold(), inp.strip().casefold()).ratio() > 0.75:
            await reload_pending_resources()
            weights = {k: v for k, v in pending_resources.items() if k in pi_resources}
            if len(weights) == 0:
                await ctx.followup.send("Ressourcenbedarf nicht gefunden.")
                return
            arrays = await self.plan.auto_select_weighted(weights, debug_data)
        else:
            weights = get_weights(inp)
            if len(weights) == 0:
                await ctx.followup.send("Gewichtung nicht erkannt, bitte Eingabe überprüfen.")
                return
            arrays = await self.plan.auto_select_weighted(weights, debug_data)
        msg = Array.build_table(arrays, mode="L n: R P d i", price_types=self.plan.preferred_prices)
        emb = Embed(title="Auto Array",
                    description="Es wurden die besten Planeten gesucht. Willst Du diese "
                                "in den Plan übernehmen?")
        emb.add_field(name="Arrays", value=f"```\n{msg}```\n", inline=False)
        emb.add_field(name="Einnahmen",
                      value=f"```\n{Array.build_income_table(arrays, price_types=self.plan.preferred_prices)}\n```",
                      inline=False)
        if debug_data is not None and len(debug_data) > 0:
            emb.add_field(name="Debug", value=build_debug_table(debug_data), inline=False)
            emb.add_field(name="Erklärung", value=autoarray_help_a)
            emb.add_field(name="Finale Auswahl", value=autoarray_help_b)
        view = ConfirmView(callback=save)
        msg = await ctx.followup.send(
            embed=emb,
            view=view
        )
        if view.message is None:
            view.message = msg


# noinspection PyUnusedLocal
class ConfirmView(AutoDisableView):
    def __init__(self, callback: Callable[[ApplicationContext], Coroutine], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.function = callback

    @discord.ui.button(label="Bestätigen", style=discord.ButtonStyle.green)
    async def btn_confirm(self, button: Button, ctx: ApplicationContext):
        await self.function(ctx)
        await self.message.delete()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.grey)
    async def btn_abort(self, button: Button, ctx: ApplicationContext):
        await ctx.response.defer(invisible=True)
        await self.message.delete()


class NumberInputModal(ErrorHandledModal):
    def __init__(self,
                 label: str,
                 placeholder: str,
                 callback: Callable[[int, ApplicationContext], Coroutine],
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(InputText(label=label, placeholder=placeholder, required=True))
        self.function = callback

    async def callback(self, ctx: ApplicationContext):
        in1 = self.children[0].value
        if in1 is None or not in1.strip().isnumeric():
            await ctx.response.send_message(f"Eingabe `{in1}` ist keine Zahl", ephemeral=True)
            return
        in1 = int(in1)
        await self.function(in1, ctx)


class StringInputModal(ErrorHandledModal):
    def __init__(self,
                 label: str,
                 placeholder: str,
                 callback: Callable[[str, ApplicationContext], Coroutine],
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_item(InputText(label=label, placeholder=placeholder, required=True))
        self.function = callback

    async def callback(self, ctx: ApplicationContext):
        in1 = self.children[0].value
        await self.function(in1, ctx)


class EditPlanModal(ErrorHandledModal):
    def __init__(self, session: PiPlanningSession, data: str, *args, **kwargs):
        super().__init__(title="Pi Plan bearbeiten", *args, **kwargs)
        self.session = session
        self.plan = session.get_active_plan()
        self.data = data
        match data:
            case "basic":
                self.add_item(InputText(label="Planeten", placeholder="Deine maximalen Planeten",
                                        required=False))
                self.add_item(InputText(label="Arrays", placeholder="Deine maximalen Fabriken pro Planet",
                                        required=False))
            case "const":
                self.add_item(InputText(label="Konstellation", placeholder="Die Konstellation des Plans oder leer",
                                        required=False))
            case "del_array":
                self.add_item(InputText(label="Array", placeholder="Nummern der zu löschenden Arrays (mit ; getrennt)",
                                        required=True))
            case "lock_array":
                self.add_item(InputText(label="Array", placeholder="Nummern der zu sperrenden Arrays (mit ; getrennt)",
                                        required=True))
            case "add_array":
                self.add_item(InputText(label="Resource", placeholder="Name der Resource", required=True))
            case "prices":
                placeholder = ""
                for p in available_prices:
                    placeholder += f"{p}; "
                placeholder.strip().strip(",")
                self.add_item(InputText(
                    style=InputTextStyle.multiline,
                    label="Marktpreispriorität",
                    placeholder=placeholder,
                    value=placeholder,
                    required=False))
            case _:
                raise PlanetaryProductionException(f"Unknown data {self.data} for EditPlanModal")

    async def callback(self, interaction: ApplicationContext):
        in1 = self.children[0].value
        match self.data:
            case "basic":
                in2 = self.children[1].value
                if in1 is not None and not in1.strip().isnumeric():
                    await interaction.response.send_message(f"\"{in1}\" is keine Zahl!", ephemeral=True)
                    return
                if in2 is not None and not in2.strip().isnumeric():
                    await interaction.response.send_message(f"\"{in2}\" is keine Zahl!", ephemeral=True)
                    return
                if in1 is not None:
                    in1 = int(in1)
                    self.plan.num_planets = in1
                if in2 is not None:
                    in2 = int(in2)
                    self.plan.num_arrays = in2
                await interaction.response.send_message(
                    f"Die Einstellungen wurden verändert. Maximale Planeten: {self.plan.num_planets}, maximale "
                    f"Fabriken: {self.plan.num_arrays}.", ephemeral=True)
                await self.session.refresh_msg()
                return
            case "const":
                if in1 is None or in1.strip() == "":
                    self.plan.constellation_id = None
                    self.plan.constellation_name = None
                    await interaction.response.send_message(f"Konstellation gelöscht", ephemeral=True)
                    await self.session.refresh_msg()
                    return
                await interaction.response.defer(ephemeral=True)
                const = await data_utils.get_constellation(in1.strip())
                if const is None:
                    await interaction.followup.send(f"Konstellation `{in1}` nicht gefunden", ephemeral=True)
                    return
                self.plan.constellation_name = const.name
                self.plan.constellation_id = const.id
                await interaction.followup.send(f"Konstellation `{const.name}` ausgewählt", ephemeral=True)
                await self.session.refresh_msg()
                return
            case "del_array":
                to_delete = []
                msg = ""
                for num in in1.split(";"):
                    try:
                        num = int(num.strip())
                    except ValueError:
                        await interaction.response.send_message(f"`{num}` ist keine Nummer", ephemeral=True)
                        return
                    if num >= len(self.plan.arrays) or num < 0:
                        await interaction.response.send_message(
                            f"`{num}` ist keine Zahl zwischen 0 und {len(self.plan.arrays)}",
                            ephemeral=True)
                        return
                    to_delete.append(self.plan.arrays[num])
                    msg += f"{num}, "
                for arr in to_delete:
                    self.plan.arrays.remove(arr)
                await interaction.response.send_message(
                    f"`Array(s) {msg.strip().strip(',')}` wurde(n) gelöscht",
                    ephemeral=True)
                await self.session.refresh_msg()
                return
            case "lock_array":
                for num in in1.split(";"):
                    try:
                        num = int(num.strip())
                    except ValueError:
                        await interaction.response.send_message(f"`{num}` ist keine Nummer", ephemeral=True)
                        await self.session.refresh_msg()
                        return
                    if num >= len(self.plan.arrays) or num < 0:
                        await interaction.response.send_message(
                            f"`{num}` ist keine Zahl zwischen 0 und {len(self.plan.arrays)}",
                            ephemeral=True)
                        await self.session.refresh_msg()
                        return
                    self.plan.arrays[num].locked = not self.plan.arrays[num].locked
                await interaction.response.send_message("Arrays wurden gesperrt", ephemeral=True)
                await self.session.refresh_msg()
                return
            case "add_array":
                if self.plan.constellation_name is None:
                    await interaction.response.send_message("Keine Konstellation ausgewählt!", ephemeral=True)
                    return
                resources = await data_utils.get_best_pi_planets(self.plan.constellation_name, in1.strip())
                if len(resources) == 0:
                    await interaction.response.send_message(f"Resource '{in1}' nicht (in der Konstellation) gefunden!",
                                                            ephemeral=True)
                    return
                else:
                    in1 = resources[0]["res"]
                view = SelectArrayView(self.session, self.plan, f"{in1} in {self.plan.constellation_name}",
                                       resources)
                await interaction.response.send_message(embed=view.build_embed(), view=view)
                return
            case "prices":
                if in1 is None:
                    self.plan.preferred_prices.clear()
                    await interaction.response.send_message("Preispriorität gelöscht", ephemeral=True)
                    await self.session.refresh_msg()
                    return
                self.plan.preferred_prices.clear()
                if in1.strip().casefold() == "max".casefold():
                    self.plan.preferred_prices = ["MAX"]
                    await interaction.response.send_message(
                        "Es wird nun der Bestpreis für deine Berechnungen zugrunde gelegt.", ephemeral=True)
                    await self.session.refresh_msg()
                    return
                for p in in1.split(";"):
                    p = p.strip()
                    if p not in available_prices:
                        if p.strip() == "":
                            continue
                        await interaction.response.send_message(f"Preis '{p}' nicht gefunden", ephemeral=True)
                        return
                    self.plan.preferred_prices.append(p)
                msg = ""
                for p in self.plan.preferred_prices:
                    msg += f"{p} > "
                msg = msg.strip().strip(">").strip()
                await interaction.response.send_message(f"Preispriorität festgelegt:\n`{msg}`", ephemeral=True)
                await self.session.refresh_msg()
                return
            case _:
                raise PlanetaryProductionException(f"Unknown data {self.data} for EditPlanModal")
