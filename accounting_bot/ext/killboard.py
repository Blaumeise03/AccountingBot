# PluginConfig
# Name: KillboardPlugin
# Author: Blaumeise03
# Depends-On: [accounting_bot.ext.members, accounting_bot.universe.data_utils]
# End
import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Tuple, Union, Optional, Set

import discord
import requests
from discord import NotFound, Forbidden, Thread, Message, Embed, Color, Cog, ApplicationContext, option
from discord.abc import GuildChannel, PrivateChannel
from discord.ext import tasks, commands

from accounting_bot.exceptions import KillmailException
from accounting_bot.main_bot import BotPlugin, PluginWrapper
from accounting_bot.universe import data_utils
from accounting_bot.universe.models import MobiKillmail
from accounting_bot.utils import wrap_async, admin_only

if TYPE_CHECKING:
    from accounting_bot.main_bot import AccountingBot

logger = logging.getLogger("ext.killboard")

API_ENDPOINT_KILLS = ("https://echoes.mobi/api/killmails?"
                      "page={page}&"
                      "killer_corp={killer_corp}&"
                      "order%5Bdate_killed%5D=desc")

CONFIG_TREE = {
    "corp_tags": (list, []),
    "replace_tag": (str, None),
    "corp_tag": (str, None),
    "only_first_page": (bool, True),
    "killboards": (list, [])
}


@wrap_async
def fetch_csv(page: int, corp_tag: str):
    response = requests.request(
        method="GET",
        url=API_ENDPOINT_KILLS.format(page=page, killer_corp=corp_tag),
        headers={"accept": "text/csv"})
    return response.content.decode("utf-8")


class KillboardPlugin(BotPlugin):
    def __init__(self, bot: "AccountingBot", wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.config = bot.create_sub_config("killmails")
        self.config.load_tree(CONFIG_TREE)
        self.killboards = set()  # type: Set[Tuple[Union[GuildChannel, Thread, PrivateChannel], Message]]
        self.cog = KillboardCog(self)

    def on_load(self):
        self.register_cog(self.cog)

    async def on_enable(self):
        # await self.refresh_kill_db()
        for c, m in self.config["killboards"]:
            try:
                channel = await self.bot.get_or_fetch_channel(channel_id=c)
                if channel is None:
                    continue
                msg = await channel.fetch_message(m)
                self.killboards.add((channel, msg))
            except NotFound:
                pass
            except Forbidden:
                logger.error("Failed to access killboard in channel %s message %s: No access", c, m)
        self.save_killboards()
        logger.info("Found %s killboards", len(self.killboards))
        self.cog.update_messages.start()

    def save_killboards(self):
        self.config["killboards"].clear()
        for channel, msg in self.killboards:
            self.config["killboards"].append((channel.id, msg.id))
        self.bot.save_config()

    async def refresh_kill_db(self):
        only_first_page = self.config["only_first_page"]
        replace_tag = self.config["replace_tag"]
        for corp_tag in self.config["corp_tags"]:
            has_data = True
            page = 1
            while has_data:
                logger.info("Fetching killmail page %s for corp %s", page, corp_tag)
                csv = await fetch_csv(page=page, corp_tag=corp_tag)
                if len(csv) < 10:
                    has_data = False
                page += 1
                await data_utils.save_mobi_csv(csv, replace_tag=replace_tag)
                if page > 10:
                    raise KillmailException(f"Reached page 11 for corp {corp_tag}")
                if only_first_page:
                    break

    async def build_killboard_embed(self):
        data = await data_utils.get_killboard_data(self.config["corp_tag"])
        top_kills = await data_utils.get_top_kills(self.config["corp_tag"])
        board = ""
        max_len_name = max(map(lambda d: len(str(d[0])), data))
        max_len_isk = max(map(lambda d: len(f"{d[1]:,}"), data))
        for player, isk in data:  # type: str, int
            board += f"{player: <{max_len_name}}: {isk: >{max_len_isk},} ISK\n"
        embed = Embed(title="Killboard (Top 10)",
                      description=f"(this month)\n**Total Kills**\n```\n{board}```",
                      color=Color.red(), timestamp=datetime.now())
        msg = ""
        for i, kill in enumerate(top_kills):  # type: int, MobiKillmail
            if kill.date_killed is not None:
                kill_time = int(kill.date_killed.timestamp())
                kill_time = f"<t:{kill_time}:d>"
            else:
                kill_time = "`Unknown time`"
            msg += (f"{i + 1}. {kill_time} {kill.killer_name}:  {kill.isk:,} ISK\n"
                    # The Zero Width Space does ensure that the line will be a new line with an indent
                    f"  ​`{kill.victim_ship_name}` [{kill.victim_corp}] {kill.victim_name}\n")
        embed.add_field(name="Top 10 Kills", value=msg, inline=False)
        embed.set_footer(text="Last updated")
        return embed

    async def update_killboards(self):
        embed = await self.build_killboard_embed()
        for channel, msg in self.killboards:
            await msg.edit(embed=embed)


class KillboardCog(Cog):
    def __init__(self, plugin: KillboardPlugin):
        self.plugin = plugin

    def cog_unload(self) -> None:
        self.update_messages.cancel()

    @tasks.loop(hours=4)
    async def update_messages(self):
        await self.plugin.refresh_kill_db()
        await self.plugin.update_killboards()

    @commands.slash_command(name="killboard", description="Create a new killboard")
    @option(name="msg_id", description="The message to edit instead of posting a new one", type=str, required=False, default=None)
    @admin_only()
    async def create_killboard(self, ctx: ApplicationContext, msg_id: Optional[str] = None):
        if msg_id is not None:
            msg_id = int(msg_id)
        await ctx.response.defer(ephemeral=True)
        embed = await self.plugin.build_killboard_embed()
        if msg_id is None:
            msg = await ctx.channel.send(embed=embed)
            self.plugin.killboards.add((msg.channel, msg))
            self.plugin.save_killboards()
            await ctx.followup.send("Created a new killboard")
            return
        msg = await ctx.channel.fetch_message(msg_id)
        await msg.edit(embed=embed)
        res = next(filter(lambda k: k[1].id == msg_id, self.plugin.killboards), None)
        if res is None:
            self.plugin.killboards.add((msg.channel, msg))
            self.plugin.save_killboards()
        await ctx.followup.send(f"Added a killboard to message {msg.jump_url}")
