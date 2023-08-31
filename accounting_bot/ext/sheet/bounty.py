# PluginConfig
# Name: BountyPlugin
# Author: Blaumeise03
# Depends-On: [accounting_bot.ext.sheet.sheet_main, accounting_bot.ext.members, accounting_bot.universe.data_utils]
# End
import datetime
import functools
import logging
from typing import List, Any, Dict, TYPE_CHECKING

import discord
from discord import option, ApplicationContext, User, Message
from discord.ext import commands
from discord.ext.commands import Cog
from discord.ui import InputText

from accounting_bot import utils
from accounting_bot.exceptions import InputException
from accounting_bot.ext.members import MembersPlugin, member_only
from accounting_bot.ext.sheet.sheet_main import SheetPlugin
from accounting_bot.ext.sheet.sheet_utils import map_cells
from accounting_bot.main_bot import BotPlugin, PluginWrapper
from accounting_bot.universe import data_utils
from accounting_bot.utils import admin_only, online_only, ErrorHandledModal

if TYPE_CHECKING:
    from bot import AccountingBot

logger = logging.getLogger("ext.sheet.bounty")
CONFIG_TREE = {
    "worksheet_name": (str, "Bounty Log"),
    "bounty_col_range": (str, "A:E"),
    "bounty_first_row": (int, 2),
    "bounty_normal": (float, 0.05),
    "bounty_tackle": (float, 0.025),
    "bounty_home": (float, 0.1),
    "bounty_max": (int, 2000000000),
    "bounty_home_regions": (list, []),
    "killmail_channel": (int, -1)
}


class BountyPlugin(BotPlugin):
    def __init__(self, bot: "AccountingBot", wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.killmail_channel = -1  # type: int | None
        self.config = bot.create_sub_config("sheet.bounty")
        self.config.load_tree(CONFIG_TREE)
        self.sheet = None  # type: SheetPlugin | None
        self.member_p = None  # type:  MembersPlugin | None

    def on_load(self):
        self.sheet = self.bot.get_plugin("SheetMain")
        self.member_p = self.bot.get_plugin("MembersPlugin")
        self.register_cog(BountyCommands(self))
        self.killmail_channel = self.config["killmail_channel"]

    async def update_killmails(self, bounties: List[Dict[str, Any]], warnings: List[str] = None, auto_fix=False):
        """
            {"kill_id", "player", "type", "system", "region"}
        """
        if warnings is None:
            warnings = []
        logger.info("Updating %s bounties", len(bounties))
        sheet = await self.sheet.get_sheet()
        wk_bounty = await sheet.worksheet(self.config["worksheet_name"])
        bounty_area = self.config["bounty_col_range"].replace(":", f"{self.config['bounty_first_row']}:")
        data = await wk_bounty.range(bounty_area)
        data = map_cells(data)
        batch_changes = []
        new_bounties = bounties.copy()
        last_row = -1
        num_updated = 0
        num_new = 0
        for row in data:
            if len(row) < 5:
                continue
            kill_id = row[0].value
            if kill_id == "":
                kill_id = None
            elif kill_id.isnumeric():
                kill_id = int(kill_id)
            else:
                logger.warning("Bounty sheet cell %s doesn't contains a number: %s", row[0].address, kill_id)
                warnings.append(f"Bounty sheet cell {row[0].address} doesn't contains a number: {kill_id}")
                kill_id = None
            player = row[1].value
            if player == "":
                continue
            if row[0].row > last_row:
                last_row = row[0].row
            if kill_id is None and not auto_fix:
                logger.warning("Bounty sheet cell %s is empty but player cell isn't", row[0].address)
                warnings.append(f"Bounty sheet cell {row[0].address} is empty but player cell isn't")
                continue
            value = utils.parse_number(row[2].value)[0]
            tackle = row[3].value.casefold() == "TRUE".casefold()
            home = row[4].value.casefold() == "TRUE".casefold()
            bounty = None
            updated = False
            for b in bounties:
                if (not auto_fix or kill_id is not None) and b["kill_id"] == kill_id and b["player"] == player:
                    bounty = b
                    break
                if auto_fix and b["value"] == value and b["player"] == player:
                    logger.warning("Bounty sheet cell %s is empty but player cell isn't. Autofix: %s",
                                   row[0].address,
                                   b["kill_id"])
                    warnings.append(
                        f"Bounty sheet cell {row[0].address} is empty but player cell isn't. Autofix: {b['kill_id']}")
                    bounty = b
                    updated = True
                    break
            if bounty is None:
                continue
            if bounty in new_bounties:
                new_bounties.remove(bounty)
            if kill_id is not None and value != bounty["value"]:
                batch_changes.append({
                    "range": row[2].address,
                    "values": [[bounty["value"]]]
                })
                logger.info("Bounty data 'value' has changed for %s:%s", kill_id, player)
                warnings.append(f"Bounty data 'value' has changed for {kill_id}:{player}")
                updated = True
            if kill_id is not None and tackle != (bounty["type"] == "T"):
                batch_changes.append({
                    "range": row[3].address,
                    "values": [[bounty["type"] == "T"]]
                })
                logger.info("Bounty data 'type' has changed for %s:%s", kill_id, player)
                warnings.append(f"Bounty data 'type' has changed for {kill_id}:{player}")
                updated = True
            if kill_id is not None and home != (bounty["region"] in self.config["bounty_home_regions"]):
                batch_changes.append({
                    "range": row[4].address,
                    "values": [[bounty["region"] in self.config["bounty_home_regions"]]]
                })
                logger.info("Bounty data 'home' has changed for %s:%s", kill_id, player)
                warnings.append(f"Bounty data 'home' has changed for {kill_id}:{player}")
                updated = True
            if kill_id is None and auto_fix:
                batch_changes.append({
                    "range": row[0].address,
                    "values": [[bounty["kill_id"]]]
                })
                logger.info("Bounty data 'id' has changed for %s:%s", bounty["kill_id"], player)
                warnings.append(f"Bounty data 'id' has changed for {bounty['kill_id']}:{player}")
                updated = True
            if updated:
                num_updated += 1
        logger.info("Detected %s new bounties, inserting them after row %s", len(new_bounties), last_row)
        for bounty in new_bounties:
            last_row += 1
            address = self.config["bounty_col_range"].replace(":", f"{last_row}:") + str(last_row)
            batch_changes.append({
                "range": address,
                "values": [[
                    bounty["kill_id"],
                    str(bounty["player"]),
                    bounty["value"],
                    bounty["type"] == "T",
                    bounty["region"] in self.config["bounty_home_regions"]
                ]]
            })
            num_new += 1
        logger.info("Performing batch update for killmails")
        await wk_bounty.batch_update(batch_changes)
        logger.info("Bounties updated")
        return num_updated, num_new


class BountyCommands(Cog):
    def __init__(self, plugin: BountyPlugin) -> None:
        super().__init__()
        self.plugin = plugin

    @Cog.listener()
    async def on_message(self, message: Message):
        if message.channel.id == self.plugin.killmail_channel and len(message.embeds) > 0:
            logger.info("Received message %s with embed, parsing killmail", message.id)
            state = await data_utils.save_killmail(message.embeds[0], self.plugin.member_p)
            if state == 1:
                await message.add_reaction("⚠️")
            elif state == 2:
                await message.add_reaction("✅")

    @commands.slash_command(name="parse_killmails", description="Loads all killmails of the channel into the database")
    @option(name="after", description="ID of message to start the search (exclusive)", required=True)
    @admin_only("bounty")
    @online_only()
    async def cmd_parse_killmails(self, ctx: ApplicationContext, after: str):
        if not after.isnumeric():
            raise InputException("Message ID has to be a number")
        await ctx.response.defer(ephemeral=True, invisible=False)
        message = await ctx.channel.fetch_message(int(after))
        # noinspection PyTypeChecker
        messages = await ctx.channel.history(after=message, oldest_first=True).flatten()
        num = 0
        for message in messages:
            if len(message.embeds) > 0:
                state = await data_utils.save_killmail(message.embeds[0], self.plugin.member_p)
                if state > 0:
                    num += 1
                if state == 1:
                    await message.add_reaction("⚠️")
                elif state == 2:
                    await message.add_reaction("✅")
        await ctx.followup.send(f"Loaded {num} killmails into the database")

    @commands.slash_command(name="save_killmails",
                            description="Saves the killmails between the ids into the google sheet")
    @option(name="first", description="ID of first killmail", required=True)
    @option(name="last", description="ID of last killmail", required=True)
    @option(name="month", description="Number of month (1=January...)", min=1, max=12, required=False, default=None)
    @option(name="autofix", description="Automatically fixes old sheet data", required=False, default=False)
    @admin_only("bounty")
    @online_only()
    async def cmd_save_killmails(
            self,
            ctx: ApplicationContext,
            first: int,
            last: int,
            month: int = None,
            autofix: bool = False):
        # if bounty_config is None:
        #     await ctx.response.send_message("Bounty Config is not loaded, command is disabled.", ephemeral=True)
        #     logger.warning("Bounty config is not loaded, can't sync bounty DB with sheet")
        #     return
        await ctx.response.defer(ephemeral=True, invisible=False)
        time = datetime.datetime.now()
        if month is not None:
            time = datetime.datetime(time.year, month, 1)
        warnings = await data_utils.verify_bounties(self.plugin.member_p, first, last, time)
        bounties = await data_utils.get_all_bounties(first, last)
        num_updated, num_new = await self.plugin.update_killmails(bounties, warnings, autofix)
        length = sum(map(len, warnings))
        msg = f"Bounty Sheet aktualisiert, es wurden {num_updated} Einträge aktualisiert und {num_new} neue Bounties " \
              f"eingetragen. Es gab {len(warnings)} Warnungen."
        if length > 900:
            file = utils.string_to_file(utils.list_to_string(warnings), "warnings.txt")
            await ctx.followup.send(f"{msg} Siehe Anhang.", file=file)
            return
        if length == 0:
            await ctx.followup.send(msg)
            return
        await ctx.followup.send(f"{msg}. Warnungen:\n```\n{utils.list_to_string(warnings)}\n```")

    @commands.slash_command(name="showbounties", description="Shows the bounty stats of a player")
    @option("user", description="The user to look up", required=False, default=None)
    @option("silent", description="Default true, execute the command silently", required=False, default=True)
    async def cmd_show_bounties(self, ctx: ApplicationContext, user: User = None, silent: bool = True):
        # if bounty_config is None:
        #     await ctx.response.send_message("Bounty Config is not loaded, command is disabled.", ephemeral=True)
        #     logger.warning("Bounty config is not loaded, can't calculate bounties")
        #     return
        if user is None:
            user = ctx.user
        player = self.plugin.member_p.find_main_name(discord_id=user.id)[0]
        if player is None:
            await ctx.response.send_message("Kein Spieler zu diesem Discord Account gefunden", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=silent, invisible=False)
        start, end = utils.get_month_edges(datetime.datetime.now())
        res = await data_utils.get_bounties_by_player(start, end, player)
        for b in res:
            if b["type"] == "T":
                factor = self.plugin.config["bounty_tackle"]
            elif b["region"] in self.plugin.config["bounty_home_regions"]:
                factor = self.plugin.config["bounty_home"]
            else:
                factor = self.plugin.config["bounty_normal"]
            b["value"] = factor * min(b["value"], self.plugin.config["bounty_max"])
            if b["ship"] is None:
                b["ship"] = "N/A"
        msg = f"Bounties aus diesem Monat für `{player}`\n```"
        b_sum = functools.reduce(lambda x, y: x + y, map(lambda b: b["value"], res))
        i = 0
        for b in res:
            msg += f"\n{b['type']} {b['kill_id']:<7} {b['ship']:<12.12} {b['value']:11,.0f} ISK"
            if len(msg) > 1400:
                msg += f"\ntruncated {len(res) - i - 1} more killmails"
                break
            i += 1
        msg += f"\n```\nSumme: {b_sum:14,.0f} ISK\n*Hinweis: Dies ist nur eine ungefähre Vorschau*"
        await ctx.followup.send(msg)

    @commands.message_command(name="Add Tackle")
    @admin_only("bounty")
    @online_only()
    async def ctx_cmd_add_tackle(self, ctx: ApplicationContext, message: discord.Message):
        if len(message.embeds) == 0:
            await ctx.response.send_message(f"Nachricht enthält kein Embed:\n{message.jump_url}", ephemeral=True)
            return
        await ctx.response.send_modal(AddBountyModal(self.plugin, message))

    @commands.message_command(name="Show Bounties")
    @member_only()
    async def ctx_cmd_show_bounties(self, ctx: ApplicationContext, message: discord.Message):
        if len(message.embeds) == 0:
            await ctx.response.send_message(f"Nachricht enthält kein Embed:\n{message.jump_url}", ephemeral=True)
            return
        kill_id = data_utils.get_kill_id(message.embeds[0])
        await ctx.response.defer(ephemeral=True, invisible=False)
        bounties = await data_utils.get_bounties(kill_id)
        killmail = await data_utils.get_killmail(kill_id)
        msg = (f"{message.jump_url}\nKillmail `{kill_id}`:\n```\n"
               f"Spieler: {killmail.final_blow}\n" +
               (f"Schiff: {killmail.ship.name}\n" if killmail.ship is not None else "Schiff: N/A\n") +
               (f"System: {killmail.system.name}\n" if killmail.system is not None else "System: N/A\n") +
               f"Wert: {killmail.kill_value:,} ISK\nBounties:")
        for bounty in bounties:
            msg += f"\n{bounty['type']:1} {bounty['player']:10}"
        msg += "\n```"
        await ctx.followup.send(msg)


class AddBountyModal(ErrorHandledModal):
    def __init__(self, plugin: BountyPlugin, msg: discord.Message, *args, **kwargs):
        super().__init__(title="Bounty hinzufügen", *args, **kwargs)
        self.plugin = plugin
        self.msg = msg
        self.add_item(InputText(label="Tackler/Logi", placeholder="Oder \"clear\"", required=True))

    async def callback(self, ctx: ApplicationContext):
        kill_id = data_utils.get_kill_id(self.msg.embeds[0])
        if self.children[0].value.strip().casefold() == "clear".casefold():
            await ctx.response.defer(ephemeral=True, invisible=False)
            await data_utils.clear_bounties(kill_id)
            await ctx.followup.send(f"Bounties gelöscht\n{self.msg.jump_url}")
            return
        player = self.plugin.member_p.find_main_name(self.children[0].value.strip())[0]
        if player is None:
            await ctx.response.send_message(
                f"Spieler `{self.children[0].value.strip()}` nicht gefunden!\n{self.msg.jump_url}", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True, invisible=False)
        await data_utils.add_bounty(kill_id, player, "T")
        bounties = await data_utils.get_bounties(kill_id)
        msg = f"Spieler `{player}` wurde als Tackle/Logi für Kill `{kill_id}` eingetragen:\n```"
        for bounty in bounties:
            msg += f"\n{bounty['type']:1} {bounty['player']:10}"
        msg += f"\n```\n{self.msg.jump_url}"
        await ctx.followup.send(msg)
