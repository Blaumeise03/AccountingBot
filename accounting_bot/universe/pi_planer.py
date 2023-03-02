import logging
from typing import List, Dict, Optional, Callable, Coroutine, Any, Union

import discord
from discord import User, Embed, Color, ApplicationContext, Interaction, Message, InputTextStyle
from discord.ui import InputText, Button

from accounting_bot.exceptions import PlanetaryProductionException
from accounting_bot.universe import data_utils
from accounting_bot.universe.models import PiPlanSettings, PiPlanResource
from accounting_bot.utils import ErrorHandledModal, AutoDisableView

logger = logging.getLogger("data.pi")
item_prices = {}  # type: Dict[str,Dict[str, Union[int, float]]]
available_prices = []


def get_price(item: str, price_types: List[str]) -> Optional[Union[float, int]]:
    if item not in item_prices or len(price_types) == 0:
        return None
    prices = item_prices[item]
    if price_types[0].casefold() == "max".casefold():
        return max(prices.values())
    for t in price_types:
        if t in prices:
            return prices[t]
    return None


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


class Planet:
    def __init__(self, p_id: Optional[int] = None, name: Optional[str] = None) -> None:
        self.id = p_id  # type: int | None
        self.name = name  # type: str | None


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

    def sort_arrays(self):
        self.arrays = sorted(self.arrays, key=lambda a: (not a.locked, data_utils.resource_order.index(a.resource)))

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

        val = f"   n {'Resource':<21} Planet      Out @ Arrays = {'items/h':<6}"
        resources = {}
        for i, array in enumerate(self.arrays):
            val += (f"\n{'🔒' if array.locked else '  '}"
                    f"{i:>2} {array.resource:<21} {array.planet.name:<11} "
                    f"{array.base_output:7.2f} @ {array.amount:2} = {array.base_output * array.amount:6.1f}")
            if array.resource in resources:
                resources[array.resource] += array.base_output * array.amount
            else:
                resources[array.resource] = array.base_output * array.amount
        emb.add_field(name=f"Aktive Arrays", value=f"```\n{val}\n```", inline=False)
        val = f"{'Resource':<21}: items/h  items/d    ISK/d"
        resources = sorted(resources.items(), key=lambda res: data_utils.resource_order.index(res[0]))
        income_sum = 0
        for name, output in resources:
            income = 0
            price = get_price(name, self.preferred_prices)
            if price is not None:
                income = output * price
            income_sum += income
            val += f"\n{name:<21}: {output:7.2f}  {output*24:7,.0f}  {income*24:9,.0f} ISK"
        emb.add_field(name="Produktion", value=f"```\n{val}\n```", inline=False)
        emb.add_field(
            name="Einnahmen",
            value=f"```\nZeitraum  Einnahmen\n"
                  f"Pro Tag   {income_sum:13,.0f} ISK\n"
                  f"Pro Woche {income_sum*24*7:13,.0f} ISK\n"
                  f"Pro Monat {income_sum*24*7*30:13,.0f} ISK\n```")
        return emb


class PiPlanningSession:
    def __init__(self, user: User) -> None:
        self.user_id = user.id
        self.user = user
        self.plans = []  # type: List[PiPlaner]
        self._deleted = []  # type: List[PiPlaner]
        self._active = None
        self.edit = None  # type: EditPlanView | None
        self.message = None  # type: Message | None

    async def refresh_msg(self):
        if self.edit is not None:
            await self.edit.refresh_msg()
        if self.message is not None:
            msg = f"Du hast {len(self.plans)} Pi Pläne\n"
            if self._active is not None:
                msg += f"Aktuell is Plan #{self._active} ausgewählt"
            await self.message.edit(content=msg, embeds=self.get_embeds())
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
        embeds = []
        for plan in self.plans:
            if self._active is not None and plan.plan_num == self._active:
                emb = plan.to_embed(color=Color.green())
            else:
                emb = plan.to_embed(color=Color.red())
            embeds.append(emb)
        return embeds

    def set_active(self, num: int) -> Optional[PiPlaner]:
        for plan in self.plans:
            if plan.plan_num == num - 1:
                self._active = num - 1
                return plan
        return None

    def create_new_plan(self) -> PiPlaner:
        next_num = 0
        for plan in self.plans:
            if plan.plan_num >= next_num:
                next_num = plan.plan_num + 1
        plan = PiPlaner(self.user_id, next_num)
        self.plans.append(plan)
        return plan

    def delete_plan(self, plan) -> None:
        self.plans.remove(plan)
        self._deleted.append(plan)


# noinspection PyUnusedLocal
class PiPlanningView(AutoDisableView):
    def __init__(self, planning_session: PiPlanningSession):
        super().__init__(timeout=60*20)
        self.session = planning_session

    @discord.ui.button(emoji="💾", style=discord.ButtonStyle.green)
    async def btn_save(self, button: Button, ctx: ApplicationContext):
        await ctx.response.defer(ephemeral=True)
        await self.session.save_plans()
        await ctx.followup.send("Änderungen gespeichert!", ephemeral=True)

    @discord.ui.button(emoji="✏", style=discord.ButtonStyle.blurple)
    async def btn_edit(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(ChangePlanModal(self.session))

    @discord.ui.button(emoji="➕", style=discord.ButtonStyle.blurple)
    async def btn_new(self, button: Button, ctx: ApplicationContext):
        plan = self.session.create_new_plan()
        plan.user_id = ctx.user.id
        plan.user_name = ctx.user.name
        await self.session.refresh_msg()
        await ctx.response.send_message(f"Es wurde ein neuer Plan erstellt: #{plan.plan_num + 1}", ephemeral=True)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.red)
    async def btn_close(self, button: Button, ctx: ApplicationContext):
        await self.message.delete()
        await ctx.response.send_message(f"Um die Änderungen zu speichern, klicke auf 💾", ephemeral=True)


class ChangePlanModal(ErrorHandledModal):
    def __init__(self, session: PiPlanningSession, *args, **kwargs):
        super().__init__(title="Pi Plan auswählen", *args, **kwargs)
        self.session = session
        self.add_item(InputText(label="Nummer", placeholder="Die Nummer des Plans eingeben", required=True))

    async def callback(self, interaction: Interaction):
        num_raw = self.children[0].value
        try:
            num = int(num_raw.strip())
        except ValueError:
            await interaction.response.send_message(f"Die Eingabe \"{num_raw}\" ist keine Nummer", ephemeral=True)
            return
        plan = self.session.set_active(num)
        if plan is not None:
            view = EditPlanView(self.session, plan)
            await interaction.response.send_message(f"Pi Plan #{num} wurde als aktiv gesetzt", view=view,
                                                    embed=plan.to_embed(), ephemeral=False)
            if self.session.edit is not None:
                await self.session.edit.message.delete()
            self.session.edit = view
            await self.session.refresh_msg()
            return
        await interaction.response.send_message(f"Pi Plan #{num} wurde nicht gefunden", ephemeral=True)


# noinspection PyUnusedLocal
class EditPlanView(AutoDisableView):
    def __init__(self, planning_session: PiPlanningSession, plan: PiPlaner):
        super().__init__(timeout=60 * 20)
        self.session = planning_session
        self.plan = plan

    async def refresh_msg(self):
        if self.plan is not None:
            await self.message.edit(f"Pi Plan #{self.plan.plan_num + 1} bearbeiten", embed=self.plan.to_embed())
        else:
            await self.message.delete()

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.red)
    async def btn_delete(self, button: Button, ctx: ApplicationContext):
        async def _delete(_ctx: ApplicationContext):
            self.session.delete_plan(self.plan)
            self.plan = None
            await _ctx.response.send_message("Plan gelöscht", ephemeral=True)
            await self.session.refresh_msg()
        await ctx.response.send_message("Willst Du diesen Plan wirklich löschen?", view=ConfirmView(_delete), ephemeral=True)

    @discord.ui.button(emoji="✏", style=discord.ButtonStyle.blurple)
    async def btn_basic(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self, self.plan, "basic"))

    @discord.ui.button(emoji="🗺️", style=discord.ButtonStyle.blurple)
    async def btn_const(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self, self.plan, "const"))

    @discord.ui.button(emoji="💸", style=discord.ButtonStyle.blurple)
    async def btn_prices(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self, self.plan, "prices"))

    @discord.ui.button(emoji="🗑️", label="Array", style=discord.ButtonStyle.red)
    async def btn_del_array(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self, self.plan, "del_array"))

    @discord.ui.button(emoji="🔓", label="Array", style=discord.ButtonStyle.blurple)
    async def btn_lock_array(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self, self.plan, "lock_array"))

    @discord.ui.button(emoji="➕", label="Array", style=discord.ButtonStyle.blurple)
    async def btn_add_array(self, button: Button, ctx: ApplicationContext):
        await ctx.response.send_modal(EditPlanModal(self, self.plan, "add_array"))

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.grey)
    async def btn_close(self, button: Button, ctx: ApplicationContext):
        self.session.edit = None
        await self.message.delete()
        # await ctx.response.send_message(f"Um die Änderungen zu speichern, klicke auf 💾", ephemeral=True)
        await ctx.response.defer(ephemeral=True)


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

    def __init__(self, planning_session: PiPlanningSession, plan: PiPlaner, title: str, resources: List[Dict[str, Any]]):
        async def save_array(value: Union[str, int], ctx: ApplicationContext):
            if type(value) == int:
                num = int(value)
                res = resources[num]
                array = Array(resource=res["res"], base_output=res["out"], amount=plan.num_arrays)
                array.planet = Planet(p_id=res["p_id"], name=res["p_name"])
                plan.arrays.append(array)
                await ctx.response.send_message("Array wurde hinzugefügt", ephemeral=True)
                await planning_session.refresh_msg()
            else:
                await ctx.response.send_modal(
                    NumberInputModal(title="Nummer eingeben",
                                     label="Nummer",
                                     placeholder="Nummer des Planeten",
                                     callback=save_array)
                )
        super().__init__(timeout=60 * 20)
        self.session = planning_session
        self.plan = plan
        self.title = title
        self.resources = resources
        amount = len(resources)
        for i in range(min(7, amount)):
            self.add_item(SelectArrayView.NumberButton(i, save_array))
        self.add_item(SelectArrayView.NumberButton("...", save_array))

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.green)
    async def btn_close(self, button: Button, ctx: ApplicationContext):
        await self.message.delete()
        await ctx.response.defer(ephemeral=True)

    def build_embed(self) -> Embed:
        msg = "```\nA n: Planet    : Output"
        for i, res in enumerate(self.resources):
            duplicate = False
            for array in self.plan.arrays:
                if array.planet.id == res["p_id"]:
                    duplicate = True
                    break
            msg += f"\n{'A' if duplicate else ' '}{i:>2}: {res['p_name']:<10}: {res['out']:6.2f}"
        msg += "\n```"
        emb = Embed(title=f"{self.title}",
                    description="Drücke auf den Knopf mit der entsprechenden Zahl um diesen Planeten auszuwählen oder "
                                f"drücke auf `...` um eine Zahl einzugeben. Es wurden {len(self.resources)} Planeten "
                                f"gefunden. Planeten mit einem A vor der Zeile sind bereits aktiv")
        emb.add_field(name="Planeten", value=msg)
        return emb


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
                 *args,  **kwargs):
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


class EditPlanModal(ErrorHandledModal):
    def __init__(self, edit: EditPlanView, plan: PiPlaner, data: str, *args, **kwargs):
        super().__init__(title="Pi Plan bearbeiten", *args, **kwargs)
        self.edit = edit
        self.plan = plan
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
                self.add_item(InputText(label="Array", placeholder="Nummern der zu löschenden Arrays (mit ; getrennt)", required=True))
            case "lock_array":
                self.add_item(InputText(label="Array", placeholder="Nummern der zu sperrenden Arrays (mit ; getrennt)", required=True))
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
                return
            case "const":
                if in1 is None or in1.strip() == "":
                    self.plan.constellation_id = None
                    self.plan.constellation_name = None
                    await interaction.response.send_message(f"Konstellation gelöscht", ephemeral=True)
                    await self.edit.session.refresh_msg()
                    return
                await interaction.response.defer(ephemeral=True)
                const = await data_utils.get_constellation(in1.strip())
                if const is None:
                    await interaction.followup.send(f"Konstellation `{in1}` nicht gefunden", ephemeral=True)
                    return
                self.plan.constellation_name = const.name
                self.plan.constellation_id = const.id
                await interaction.followup.send(f"Konstellation `{const.name}` ausgewählt", ephemeral=True)
                await self.edit.session.refresh_msg()
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
                await self.edit.session.refresh_msg()
                return
            case "lock_array":
                for num in in1.split(";"):
                    try:
                        num = int(num.strip())
                    except ValueError:
                        await interaction.response.send_message(f"`{num}` ist keine Nummer", ephemeral=True)
                        await self.edit.session.refresh_msg()
                        return
                    if num >= len(self.plan.arrays) or num < 0:
                        await interaction.response.send_message(
                            f"`{num}` ist keine Zahl zwischen 0 und {len(self.plan.arrays)}",
                            ephemeral=True)
                        await self.edit.session.refresh_msg()
                        return
                    self.plan.arrays[num].locked = not self.plan.arrays[num].locked
                await interaction.response.send_message("Arrays wurden gesperrt", ephemeral=True)
                await self.edit.session.refresh_msg()
                return
            case "add_array":
                if self.plan.constellation_name is None:
                    await interaction.response.send_message("Keine Konstellation ausgewählt!", ephemeral=True)
                    return
                resources = await data_utils.get_best_pi_planets(self.plan.constellation_name, in1.strip())
                if len(resources) == 0:
                    await interaction.response.send_message(f"Resource '{in1}' nicht (in der Konstellation) gefunden!", ephemeral=True)
                    return
                else:
                    in1 = resources[0]["res"]
                view = SelectArrayView(self.edit.session, self.plan, f"{in1} in {self.plan.constellation_name}", resources)
                await interaction.response.send_message(embed=view.build_embed(), view=view)
                return
            case "prices":
                if in1 is None:
                    self.plan.preferred_prices.clear()
                    await interaction.response.send_message("Preispriorität gelöscht", ephemeral=True)
                    await self.edit.session.refresh_msg()
                    return
                self.plan.preferred_prices.clear()
                if in1.strip().casefold() == "max".casefold():
                    self.plan.preferred_prices = ["MAX"]
                    await interaction.response.send_message(
                        "Es wird nun der Bestpreis für deine Berechnungen zugrunde gelegt.", ephemeral=True)
                    await self.edit.session.refresh_msg()
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
                await self.edit.session.refresh_msg()
                return
            case _:
                raise PlanetaryProductionException(f"Unknown data {self.data} for EditPlanModal")
