# PluginConfig
# Name: EchoesPlugin
# Author: Blaumeise03
# Load-After: [accounting_bot.ext.sheet.projects, accounting_bot.ext.sheet.sheet_main]
# End
import asyncio
import csv
import difflib
import io
import logging
import os
from asyncore import write
from pathlib import Path
from typing import Dict, Any, List, Optional, TYPE_CHECKING

import discord
from blue_echoes.data import Item
from blue_echoes.data.models import EstimatedMarketData
from blue_echoes.data_utils import Dialect
from discord import SlashCommandGroup, option, ApplicationContext, Embed
from discord.ext import commands
from gspread.utils import ValueInputOption
from sqlalchemy.ext.asyncio import create_async_engine

from accounting_bot import utils
from accounting_bot.exceptions import PluginNotFoundException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper, PluginState
from accounting_bot.utils import AutoDisableView
from accounting_bot.utils.ui import NumPadView, ModalForm, ConfirmView, AwaitConfirmView

if TYPE_CHECKING:
    from accounting_bot.ext.sheet.sheet_main import SheetPlugin

logger = logging.getLogger("ext.echoes_data")

try:
    from blue_echoes.db import EchoesDB
except ImportError as e:
    logger.error("Failed to import blue_echoes.db, make sure you have the blue_echoes_data package installed")
    raise e


class EchoesDataPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.engine = None
        self.db: EchoesDB = None
        self.atomic_resources: List[str] = []
        self.automatic_resources: List[str] = []

    def on_load(self):
        logger.info("Connecting to database")
        db_url = os.getenv('BLUE_ECHOES_DB')
        if db_url is None:
            raise ValueError("BLUE_ECHOES_DB environment variable is not set")
        self.engine = create_async_engine(
            db_url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=True
        )
        self.db = EchoesDB(self.engine, dialect=Dialect.from_str(os.getenv('BLUE_ECHOES_DIALECT', 'mysql')))
        logger.info("Connected to database")
        self.register_cog(EchoesDataCommands(self))
        try:
            plugin = self.bot.get_plugin(name="ProjectPlugin", require_state=PluginState.LOADED)
            self.atomic_resources = list(plugin.project_resources)
            logger.info("Loaded %s atomic resources from project plugin", len(self.atomic_resources))
        except PluginNotFoundException:
            logger.warning("ProjectPlugin not found, atomic resources could not be loaded")
        header_path = Path("config/headers.txt")
        if header_path.exists():
            with header_path.open("r", encoding="utf-8") as f:
                self.automatic_resources = [x.strip() for x in f.readlines()]
            logger.info("Loaded %s automatic resources from headers.txt", len(self.automatic_resources))

    async def on_enable(self):
        await self.db.create_tables()


def build_cost_table(item: Item, indent=0):
    msg = ""
    for cost in item.blueprint.resources:
        msg += " " * indent
        msg += f"{cost.item.name}: {cost.amount}"
        if cost.item.blueprint is not None:
            msg += f" ({cost.item.blueprint.money} ISK per unit)\n"
            msg += build_cost_table(cost.item, indent + 2)
        else:
            msg += "\n"
    return msg


def build_cost_formulars(item: Item, atomic_resources: List[str], automatic_resources: List[str], start_row=5):
    rows = []
    for cost in item.blueprint.resources:
        if cost.item.name in atomic_resources:
            continue
        current_row = len(rows) + start_row + 1
        out_num = 1 if cost.item.blueprint is None else cost.item.blueprint.output_num
        out_num_correct = "" if out_num == 1 else f"/{out_num}"
        if cost.item.name in automatic_resources:
            rows.append([
                cost.item.name,
                f'=IFERROR(C6/$C${start_row}*$B${start_row}; "")',
                f'=ROUNDUP(IFERROR(ROUNDUP(CEILING(VLOOKUP(A${start_row}; Produktionskosten!$1:2318; '
                f'MATCH(A{current_row}; Produktionskosten!$1:$1; 0); FALSE) * $D${start_row})); 0){out_num_correct})*$C${start_row}'])
        else:
            base_quantity = cost.amount
            rows.append([
                cost.item.name,
                f'=IFERROR(C6/$C${start_row}*$B${start_row}; "")',
                f'=ROUNDUP(IFERROR(ROUNDUP(CEILING({base_quantity} * $D${start_row})); 0){out_num_correct})*$C${start_row}'])
        if cost.item.blueprint is not None:
            rows.extend(build_cost_formulars(cost.item, atomic_resources, automatic_resources, start_row=current_row))
    return rows


class MarketGroupSelect(discord.ui.Select):
    def __init__(
            self,
            select_options: Dict[int, Dict[str, Any]],
            callback,
            parent_id: Optional[int] = None,
            *args,
            placeholder="Select a market group",
            **kwargs
    ):
        self.select_options = {group["id"]: discord.SelectOption(label=group["name"], value=str(group["id"])) for group in
                   select_options.values()}
        self._callback = callback
        self.parent_id = parent_id
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=list(self.select_options.values()),
            *args, **kwargs
        )

    async def callback(self, interaction: discord.Interaction):
        await self._callback(self, interaction)


class ItemSelectView(AutoDisableView):
    def __init__(self,
                 plugin: EchoesDataPlugin,
                 market_groups: Dict[int, Dict[str, Any]],
                 callback,
                 auto_delete=True,
                 silent=True,
                 *args, **kwargs,
                 ):
        super().__init__(*args, **kwargs)
        self.plugin = plugin
        self.market_groups = market_groups
        self.auto_delete = auto_delete
        self.silent = silent
        primary_group = dict(filter(lambda x: x[1]["parent_id"] is None, market_groups.items()))
        # self.secondary_group = dict(filter(lambda x: x[1]["parent_id"] in self.primary_group, market_groups.items()))
        # self.tertiary_group = dict(filter(lambda x: x[1]["parent_id"] in self.secondary_group, market_groups.items()))

        self.market_drawers: List[Optional[MarketGroupSelect]] = [None, None, None, None]
        self.market_drawers[0] = MarketGroupSelect(primary_group, self.select_callback, row=0)
        self.add_item(self.market_drawers[0])
        # self.market_drawers.append(MarketGroupSelect(self.secondary_group, self.select_callback))
        # self.market_drawers.append(MarketGroupSelect(self.tertiary_group, self.select_callback))
        self.market_selection: List[Optional[int]] = [None, None, None, None]
        self._callback = callback

    async def select_callback(self, select, interaction: discord.Interaction):
        if select == self.market_drawers[3]:
            task = None
            if self.auto_delete:
                task = asyncio.create_task(self.message.delete())
            await self._callback(interaction, int(select.values[0]), silent=self.silent)
            if task is not None:
                await task
            if not self.auto_delete:
                return
        else:
            await interaction.response.defer()
        market_level = self.market_drawers.index(select)
        market_group_id = int(select.values[0])
        self.market_selection[market_level] = market_group_id
        await self.update_drawer()

    async def update_drawer(self):
        # Validate parent-child relationships
        if self.market_selection[0] is None:
            self.market_selection[1] = None
        elif self.market_selection[1] is not None:
            group = self.market_groups[self.market_selection[1]]
            if group["parent_id"] != self.market_selection[0]:
                self.market_selection[1] = None

        if self.market_selection[1] is None:
            self.market_selection[2] = None
        elif self.market_selection[2] is not None:
            group = self.market_groups[self.market_selection[2]]
            if group["parent_id"] != self.market_selection[1]:
                self.market_selection[2] = None
        # Remove all drawers that are not needed or incorrect
        for i in range(1, 4):
            if self.market_selection[i - 1] is None and self.market_drawers[i] is not None:
                item = self.market_drawers[i]
                self.remove_item(item)
                self.market_drawers[i] = None
            if self.market_selection[i - 1] is not None and self.market_drawers[i] is not None:
                item = self.market_drawers[i]
                if item.parent_id != self.market_selection[i - 1]:
                    self.remove_item(item)
                    self.market_drawers[i] = None

        # Add drawers that are needed
        for i in range(1, 3):
            if self.market_selection[i - 1] is not None and self.market_drawers[i] is None:
                item = MarketGroupSelect(
                    dict(filter(lambda x: x[1]["parent_id"] == self.market_selection[i - 1],
                                self.market_groups.items())),
                    self.select_callback, self.market_selection[i - 1], row=i)
                self.market_drawers[i] = item
                self.add_item(item)
        # Add item drawer if needed
        if self.market_selection[2] is not None and self.market_drawers[3] is None:
            items = await self.plugin.db.item_repo.fetch_items(market_groups=self.market_selection[2])
            options = {item.id: {
                "id": item.id,
                "name": item.name,
            } for item in items}
            self.market_drawers[3] = MarketGroupSelect(options, self.select_callback, self.market_selection[2],
                                                       placeholder="Select Item", row=3)
            self.add_item(self.market_drawers[3])

        # Select current selection
        for i in range(4):
            if self.market_drawers[i] is None:
                continue
            for opt in self.market_drawers[i].select_options.values():
                opt.default = False
            if self.market_selection[i] is not None:
                opt = self.market_drawers[i].select_options[self.market_selection[i]]
                opt.default = True
            self.market_drawers[i].options = list(self.market_drawers[i].select_options.values())
        # debug_msg = "Selection: " + ",".join(str(x) for x in self.market_selection) + "\n"
        # for i in range(4):
        #     if self.market_drawers[i] is None:
        #         debug_msg += f"Drawer {i}: None\n"
        #     else:
        #         item = self.market_drawers[i]
        #         debug_msg += f"Drawer {i}: Parent: {item.parent_id}, options: {len(item.select_options)}\n"
        #         for opt in item.options:
        #             if not opt.default:
        #                 continue
        #             debug_msg += f"  Selected: {opt.label} ({opt.value})\n"
        await self.message.edit(
            view=self)


class BlueprintInfoView(AutoDisableView):

    def __init__(self, item: Item, plugin: EchoesDataPlugin, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.item = item
        self.plugin = plugin

    async def build_embed(self) -> Embed:
        item_ids = [self.item.id, self.item.blueprint.blueprint_item.id if self.item.blueprint is not None else None]
        if None in item_ids:
            item_ids.remove(None)
        prices = await self.plugin.db.market_repo.fetch_last_estimated_prices(item_ids)
        price_item: EstimatedMarketData = next(filter(lambda p: p.type_id == self.item.id, prices), None)
        if self.item.blueprint is not None:
            price_bp: EstimatedMarketData = next(filter(lambda p: p.type_id == self.item.blueprint.blueprint_item.id, prices), None)
        else:
            price_bp = None
        embed_desc = f"Item ID: `{self.item.id}`\n"
        latest_time = None
        if price_item is not None:
            embed_desc += f"Item Price: `{price_item.average_price_no_outliers:,} ISK`\n"
            latest_time = price_item.week_of_patch
        if price_bp is not None:
            embed_desc += f"Blueprint Price: `{price_bp.average_price_no_outliers:,} ISK`\n"
            latest_time = max(latest_time, price_bp.week_of_patch)
        if latest_time is not None:
            embed_desc += f"-# Time of price data: <t:{int(latest_time.timestamp())}:f>\n"
        embed = Embed(
            title=f"Item {self.item.name}",
            description=embed_desc,
            color=discord.Color.red()
        )
        if self.item.blueprint is not None:
            embed_desc = f"Blueprint: `{self.item.blueprint.blueprint_item.name}`\n"
            embed_desc += f"Station Price: `{self.item.blueprint.money:,} ISK`\n"
            embed_desc += f"Time: `{self.item.blueprint.time}`\n"
            embed_desc += f"Output num: `{self.item.blueprint.output_num}`\n"
            embed.add_field(name="Blueprint", value=embed_desc)
        else:
            embed.add_field(name="Blueprint", value="No blueprint found")
        return embed

    @discord.ui.button(label="Google Sheet", style=discord.ButtonStyle.primary, emoji="💾")
    async def btn_sheet(self, button: discord.ui.Button, interaction: discord.Interaction):
        if not self.plugin.bot.is_admin(interaction.user):
            await interaction.response.send_message("You are not authorized to use this button", ephemeral=True)
            return

        from gspread.utils import ValueRenderOption
        model = ModalForm(title="Insert data into Google Sheet", send_response=False)
        model.add_field(
            label="Sheet Name",
            placeholder="Name of the sheet to insert the data into",
        )
        model.add_field(
            label="Row",
            placeholder="Row to insert the data into",
        )
        # noinspection PyTypeChecker
        await model.open_form(interaction.response)
        result = model.retrieve_results()
        interaction = model.get_interaction()
        await interaction.response.defer(ephemeral=True)
        sheet_name = result["Sheet Name"]
        start_row = int(result["Row"].strip())
        sheet_plugin: SheetPlugin = self.plugin.bot.get_plugin("SheetMain")
        sheet = await sheet_plugin.get_sheet()
        worksheets = await sheet.worksheets()
        sheet_names = [ws.title for ws in worksheets]
        if sheet_name in sheet_names:
            ws = await sheet.worksheet(sheet_name)
        else:
            matches = difflib.get_close_matches(sheet_name, sheet_names, n=1)
            if len(matches) == 0:
                await interaction.followup.send("No matching sheet found")
                return
            ws = await sheet.worksheet(matches[0])

        formulas = [[self.item.name, 0, 1]]
        formulas.extend(build_cost_formulars(self.item, self.plugin.atomic_resources,
                                             self.plugin.automatic_resources, start_row=start_row))
        req_rows = len(formulas)

        cells = await ws.get(f"A{start_row}:C{start_row + req_rows}", value_render_option=ValueRenderOption.formula)
        is_empty = len(cells) == 0
        cells_pos = None
        if len(cells) > 0:
            for r, row in enumerate(cells):
                if len(row) == 0:
                    continue
                for c, cell in enumerate(row):
                    if cell != '':
                        is_empty = False
                        cells_pos = (r, c + 1)
                        break
                if not is_empty:
                    break
        if not is_empty:
            await interaction.followup.send(
                f"The selected range is not empty (at `{cells_pos[0] + start_row}:{cells_pos[1]}`), "
                f"please clear the range.", ephemeral=True)
            return
        confirm_view = AwaitConfirmView(defer_response=True)
        await confirm_view.send_view(
            interaction.followup,
            message=f"Do you want to insert {req_rows} rows into the sheet {ws.title} at row {start_row}, "
                    f"column A to C? The area is currently empty and will be overwritten.",
        )
        if not confirm_view.confirmed:
            await interaction.followup.send("Operation cancelled")
            return
        formulas = [['' if col is None else str(col) for col in row] for row in formulas]
        logger.info("User %s:%s is inserting %s rows into sheet %s at row %s for item %s:%s",
                    interaction.user.name, interaction.user.id, req_rows, ws.title, start_row,
                    self.item.name, self.item.id)
        await ws.update(range_name=f"A{start_row}:C{start_row + req_rows}", values=formulas,
                        value_input_option=ValueInputOption.user_entered)
        await interaction.followup.send(
            f"Inserted {req_rows} rows into the sheet {ws.title} at row {start_row}, column A to C", ephemeral=True)


class EchoesDataCommands(commands.Cog):
    group = SlashCommandGroup(name="eve", description="Eve Echoes Data Tools")

    def __init__(self, plugin: EchoesDataPlugin):
        self.plugin = plugin

    @group.command(name="blueprint", description="Tools for blueprint data")
    @option(name="item_name", description="The name of the item", type=str, required=False, default=None)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True)
    async def cmd_bp(self, ctx: ApplicationContext, item_name: str, silent: bool):
        await ctx.defer(ephemeral=True)
        market_groups = await self.plugin.db.item_repo.fetch_market_groups()

        view = ItemSelectView(self.plugin, market_groups,
                              callback=self.cmd_bp_callback, auto_delete=False, silent=silent)
        msg = await ctx.followup.send("Select an item", view=view)
        view.message = msg

    async def cmd_bp_callback(self, interaction: discord.Interaction, item_id: int, silent=True):
        await interaction.response.defer(ephemeral=silent)
        item = await self.plugin.db.item_repo.fetch_item(item_id=item_id)
        await self.plugin.db.item_repo.fetch_blueprint_data(item, recursive=True)
        view = BlueprintInfoView(item, self.plugin)
        embed = await view.build_embed()
        msg = await interaction.followup.send(ephemeral=silent, view=view, embed=embed)
        view.message = msg
