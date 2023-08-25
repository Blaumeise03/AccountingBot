# PluginConfig
# Name: UniversePlugin
# Author: Blaumeise03
# Depends-On: [accounting_bot.universe.pi_planer]
# End
import io
import logging

import discord
import numpy as np
from discord import ApplicationContext, option, SlashCommandGroup, ChannelType, Embed, Color
from discord.ext import commands

from accounting_bot import utils
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.universe import data_utils
from accounting_bot.universe.pi_planer import PiPlanningSession, PiPlanningView

logger = logging.getLogger("ext.universe")


class UniversePlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)

    def on_load(self):
        self.register_cog((UniverseCommands(self)))

    def on_unload(self):
        super().on_unload()


class UniverseCommands(commands.Cog):
    def __init__(self, plugin: UniversePlugin):
        self.plugin = plugin

    cmd_pi = SlashCommandGroup(name="pi", description="Access planetary production data.")

    @cmd_pi.command(name="stats", description="View statistical data for pi in a selected constellation")
    @option(name="const", description="Target Constellation", type=str, required=True)
    @option(name="resources", description="List of pi, seperated by ';'", type=str, required=False)
    @option(name="compare_regions",
            description="List of regions, seperated by ';' to compare the selected constellation with",
            type=str, required=False)
    @option(name="vertical", description="Create a vertical boxplot (default false)",
            default=False, required=False)
    @option(name="full_axis", description="Makes the y-axis go from 0-100% instead of cropping it to the min/max.",
            default=False, required=False)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_const_stats(self, ctx: ApplicationContext, const: str, resources: str, compare_regions: str,
                              vertical: bool, full_axis: bool, silent: bool):
        await ctx.response.defer(ephemeral=silent)
        resource_names = utils.str_to_list(resources, ";")
        region_names = utils.str_to_list(compare_regions, ";")

        figure, n = await data_utils.create_pi_boxplot_async(const, resource_names, region_names, vertical, full_axis)
        img_binary = await data_utils.create_image(figure,
                                                   height=max(n * 45, 500) + 80 if vertical else 500,
                                                   width=700 if vertical else max(n * 45, 500))
        arr = io.BytesIO(img_binary)
        arr.seek(0)
        file = discord.File(arr, "image.jpeg")
        await ctx.followup.send(f"PI Analyse für {const} abgeschlossen:", file=file, ephemeral=silent)

    @cmd_pi.command(name="find", description="Returns a list with the best planets for selected pi")
    @option(name="const_sys", description="Target Constellation or origin system", type=str, required=True)
    @option(name="resource", description="Name of pi to search", type=str, required=True)
    @option(name="distance", description="Distance from origin system to look up",
            type=int, min_value=0, max_value=30, required=False, default=0)
    @option(name="amount", description="Number of planets to return", type=int, required=False, default=None)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_find_pi(self, ctx: ApplicationContext, const_sys: str, resource: str, distance: int, amount: int,
                          silent: bool):
        await ctx.response.defer(ephemeral=True)
        resource = resource.strip()
        const = await data_utils.get_constellation(const_sys)
        has_sys = False
        if const is not None:
            result = await data_utils.get_best_pi_planets(const.name, resource, amount)
            title = f"{resource} in {const_sys}"
        else:
            sys = await data_utils.get_system(const_sys)
            if sys is None:
                await ctx.followup.send(f"\"{const_sys}\" is not a system/constellation.", ephemeral=silent)
                return
            if distance is None:
                await ctx.followup.send("An distance of jumps from the selected system is required.", ephemeral=silent)
                return
            result = await data_utils.get_best_pi_by_planet(sys.name, distance, resource, amount)
            title = f"{resource} near {const_sys}"
            has_sys = True
        result = sorted(result, key=lambda r: r["out"], reverse=True)
        msg = "Output in units per factory per hour\n```"
        msg += f"{'Planet':<12}: {'Output':<6}" + ("  Jumps\n" if has_sys else "\n")
        for res in result:
            msg += f"\n{res['p_name']:<12}: {res['out']:6.2f}" + (f"  {res['distance']}j" if has_sys else "")
            if len(msg) > 3900:
                msg += "\n**(Truncated)**"
                break
        msg += "\n```"
        emb = discord.Embed(title=title, color=discord.Color.green(),
                            description="Kein Planet gefunden/ungültige Eingabe" if len(result) == 0 else msg)
        await ctx.followup.send(embed=emb, ephemeral=silent)

    @cmd_pi.command(name="planer", description="Opens the pi planer to manage your planets")
    async def cmd_pi_plan(self, ctx: ApplicationContext):
        plan = self.plugin.bot.get_plugin("PiPlanerPlugin").get_session(ctx.user)  # type: PiPlanningSession
        await plan.load_plans()
        if ctx.channel.type == ChannelType.private:
            interaction = await ctx.response.send_message(
                f"Du hast aktuell {len(plan.plans)} aktive Pi Pläne:",
                embeds=plan.get_embeds(),
                view=PiPlanningView(plan))
            response = await interaction.original_response()
            plan.message = await ctx.user.fetch_message(response.id)
            return
        msg = await ctx.user.send(
            f"Du hast aktuell {len(plan.plans)} aktive Pi Pläne:",
            embeds=plan.get_embeds(),
            view=PiPlanningView(plan))
        plan.message = msg
        await ctx.response.send_message("Überprüfe deine Direktnachrichten", ephemeral=True)

    @commands.slash_command(name="route", description="Finds a route between two systems")
    @option("start", description="The origin system", type=str, required=True)
    @option("end", description="The destination system", type=str, required=True)
    @option("mode", description="The autopilot mode", type=str, required=False, default="normal",
            choices=["normal", "avoid 00", "low only"])
    @option("threshold", description="The min distance between two gates for warnings", type=int, required=False,
            default=50)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_route(self, ctx: ApplicationContext, start: str, end: str, mode: str, threshold: int,
                        silent: bool = True):
        await ctx.response.defer(ephemeral=silent, invisible=False)
        sec_min = None
        sec_max = None
        mode = mode.casefold()
        if mode == "avoid 00".casefold():
            sec_min = 0
        elif mode == "low only".casefold():
            sec_min = 0
            sec_max = 0.5
        route = await data_utils.find_path(start, end, sec_min, sec_max)
        msg = "```"
        msg_crit = "```"
        first = None
        last = None
        if len(route) > 0:
            max_len = max(map(lambda r: len(r[0]), route))
        else:
            max_len = 6
        for sys, prev, dest, distance in route:
            if first is None:
                first = prev
            last = dest
            msg += f"\n{'⚠️' if distance > threshold else ' '} {sys:{max_len}}: {prev:{max_len}} -> {dest:{max_len}}: {distance:4.2f} AU "
            if distance > threshold:
                msg_crit += f"\n{sys:{max_len}}: {distance:4.2f} AU"
        msg += "\n```"
        msg_crit += "\n```"
        if len(msg_crit) > 7:
            msg_crit = f"\nAchtung, es gibt einige Warps die länger als `{threshold} AU` sind auf der Route:\n" + msg_crit
        else:
            msg_crit = f"\nEs gibt keine Warps die länger als `{threshold} AU` sind auf der Route"
        msg += msg_crit
        if len(msg) > 1800:
            files = [utils.string_to_file(msg.replace("```", ""), filename="route.txt")]
            msg = "\n**Die Route ist zu lang** für eine Nachricht, daher wurde sie in einer Textdatei gespeichert."
            if len(msg_crit) < 1800:
                msg += msg_crit
        else:
            files = []
        await ctx.followup.send(f"Route von **{first}** nach **{last}**:\n"
                                f"Min Security: `{sec_min}`\nMax Security: `{sec_max}`\n" + msg, files=files)

    @commands.slash_command(name="angle", description="Finds the angle between two gates")
    @option("system", description="The system", type=str, required=True)
    @option("start", description="The origin point", type=str, required=True)
    @option("obj_a", description="The first gate", type=str, required=True)
    @option("obj_b", description="The second gate", type=str, required=True)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_angle(self, ctx: ApplicationContext, system: str, start: str, obj_a: str, obj_b: str, silent: bool = True):
        await ctx.response.defer(ephemeral=silent, invisible=False)
        gates = await data_utils.get_gates(system)
        cel_start = None
        cel_b = None
        cel_a = None
        for g in gates:
            name = g.connected_gate.system.name
            if name.casefold() == start.casefold():
                cel_start = g
                start = name
            elif name.casefold() == obj_a.casefold():
                cel_a = g
                obj_a = name
            elif name.casefold() == obj_b.casefold():
                cel_b = g
                obj_b = name
        if cel_start is None:
            await ctx.followup.send(f"Celestial `{start}` not found")
            return
        if cel_b is None:
            await ctx.followup.send(f"Celestial `{cel_b}` not found")
            return
        if cel_a is None:
            await ctx.followup.send(f"Celestial `{cel_a}` not found")
            return
        s = np.array([cel_start.x, cel_start.y, cel_start.z])
        b = np.array([cel_b.x, cel_b.y, cel_b.z])
        a = np.array([cel_a.x, cel_a.y, cel_a.z])
        # Get direction vectors with the start celestial as their origin
        s_a = (a - s)
        s_b = (b - s)
        # Normalize vectors to fix floating point issues
        s_a = s_a / np.linalg.norm(s_a)
        s_b = s_b / np.linalg.norm(s_b)
        # Calculate the angle between both vectors
        cos_angle = np.dot(s_a, s_b)
        angle = np.arccos(cos_angle)
        angle = np.degrees(angle)
        await ctx.followup.send(
            f"Der Winkel zwischen den Gates `{obj_a}` and `{obj_b}` gesehen vom Gate `{start}` beträgt `{angle:.3f}°`."
        )

    @commands.slash_command(name="lowsec_entries", description="Finds a list of all lowsec entries with distance")
    @option("start", description="The origin system", type=str, required=True)
    @option("max_d", description="The max distance to lowsec", type=int, required=False, default=35)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            type=bool, default=True, required=False)
    async def cmd_lowsec(self, ctx: ApplicationContext, start: str, max_d: int, silent: bool = True):
        await ctx.response.defer(ephemeral=silent, invisible=False)
        result = await data_utils.find_lowsec_entries(start, int(max_d))
        msg = "```"
        if len(result) > 0:
            max_len = max(map(len, result))
        else:
            max_len = 1
            msg += "\nKeine Lowsec Systeme gefunden"
        for node, d in result.items():
            msg += f"\n{node:{max_len}}: {d:2}"
        msg += "\n```"
        embed = Embed(title=f"Lowsec Entries nach `{start}`", color=Color.green(),
                      description=f"Alle Lowsec Eingänge in den Nullsec die weniger als `{max_d}` jumps "
                                  f"von `{start}` entfernt sind.")
        embed.add_field(name="Routen", value=msg)
        await ctx.followup.send(embed=embed)
