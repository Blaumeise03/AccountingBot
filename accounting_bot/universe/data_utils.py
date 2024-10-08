# PluginConfig
# Name: DataUtilsPlugin
# Author: Blaumeise03
# End
import collections
import logging
import math
import re
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Union, Any, Coroutine, Awaitable

import networkx as nx
import numpy as np
import plotly.graph_objects as go
from discord import Embed

from accounting_bot.exceptions import InputException
from accounting_bot.ext.members import MembersPlugin
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.universe.models import System, Celestial
from accounting_bot.universe.universe_database import UniverseDatabase
from accounting_bot.utils import wrap_async

AU_RATIO = 149_597_870_700

logger = logging.getLogger("ext.data.utils")
data_plugin = None  # type: DataUtilsPlugin | None
CONFIG_TREE = {
    "db": {
        "username": (str, "root"),
        "password": (str, "root"),
        "host": (str, "127.0.0.1"),
        "port": (int, 3306),
        "database": (str, "universe"),
    }
}
CNFG_KILL_TREE = {
    "channel": (int, -1),
    "admins": (list, []),
    "home_regions": (list, []),
    "field_id": (str, "TITLE"),
    "regex_id": (str, "Kill Report #(\\d+)"),
    "field_final_blow": (str, "Pilot"),
    "regex_final_blow": (str, ".*"),
    "field_ship": (str, "Killed"),
    "regex_ship": (str, ".*"),
    "field_kill_value": (str, "ISK"),
    "regex_kill_value": (str, ".*"),
    "field_system": (str, "Location"),
    "regex_system": (str, "([-a-zA-Z0-9 ]+) < .* < .*")
}


class DataUtilsPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.db = None  # type: UniverseDatabase | None
        global data_plugin
        if data_plugin is not None:
            logger.warning("Overwriting singleton DataUtilsPlugin")
        data_plugin = self
        self.config = bot.create_sub_config("data_utils")
        self.config.load_tree(CONFIG_TREE)
        self.killmail_config = self.config.create_sub_config("killmail_parser")
        self.killmail_config.load_tree(CNFG_KILL_TREE)
        self.resource_order = {}  # type: Dict[str, int]

    def on_load(self):
        self.info("Starting database connection")
        self.db = UniverseDatabase(
            username=self.config["db.username"],
            password=self.config["db.password"],
            host=self.config["db.host"],
            port=self.config["db.port"],
            database=self.config["db.database"]
        )
        self.info("Database connected")
        self.resource_order.clear()
        with open("resources/ee_resources.plain", "r") as file:
            i = 0
            for line in file:
                line = line.strip("\n").strip()
                if len(line) == 0:
                    continue
                self.resource_order[line] = i
                i += 1
        self.info("Loaded resource table")

    def on_unload(self):
        logger.info("Closing database connection")
        self.db.engine.dispose()


class Item(object):
    def __init__(self, name: str, amount: Union[int, float]):
        self.name = name
        self.amount = amount

    def __repr__(self):
        return f"Item({self.name}={self.amount})"

    @staticmethod
    def sort_list(items: List["Item"], order: Union[List[str], Dict[str, int]]) -> None:
        if type(order) is list:
            for item in items:  # type: Item
                if item.name not in order:
                    order.append(item.name)
            items.sort(key=lambda x: order.index(x.name) if x.name in order else math.inf)
        elif type(order) is dict:
            items.sort(key=lambda x: order[x.name] if x.name in order else math.inf)

    @staticmethod
    def sort_tuple_list(items: List[Tuple[str, Any, ...]], order: Union[List[str], Dict[str, int]]) -> None:
        if type(order) is list:
            for item in items:
                if item[0] not in order:
                    order.append(item[0])
            items.sort(key=lambda x: order.index(x[0]) if x[0] in order else math.inf)
        elif type(order) is dict:
            items.sort(key=lambda x: order[x[0]] if x[0] in order else math.inf)

    @staticmethod
    def parse_ingame_list(raw: str) -> List["Item"]:
        items = []  # type: List[Item]
        for line in raw.split("\n"):
            if re.fullmatch("[a-zA-Z ]*", line):
                continue
            line = re.sub("\t", "    ", line.strip())  # Replace Tabs with spaces
            line = re.sub("^\\d+ *", "", line.strip())  # Delete first column (numeric Index)
            if len(re.findall(r" \d+", line.strip())) > 1:
                line = re.sub(" *[0-9.]+$", "", line.strip())  # Delete last column (Valuation, decimal)
            item = re.sub(" +\\d+$", "", line)
            quantity = line.replace(item, "").strip()
            if len(quantity) == 0:
                continue
            item = item.strip()
            found = False
            for i in items:
                if i.name == item:
                    i.amount += int(quantity)
                    found = True
                    break
            if not found:
                items.append(Item(item, int(quantity)))
        Item.sort_list(items, data_plugin.resource_order)
        return items

    @staticmethod
    def parse_list(raw: str, skip_negative=False) -> List["Item"]:
        items = []  # type: List[Item]
        for line in raw.split("\n"):
            if re.fullmatch("[a-zA-Z ]*", line):
                continue
            line = re.sub("\t", "    ", line.strip())  # Replace Tabs with spaces
            line = re.sub("^\\d+ *", "", line.strip())  # Delete first column (numeric Index)
            item = re.sub(" +[0-9.]+$", "", line)
            quantity = line.replace(item, "").strip()
            if len(quantity) == 0:
                continue
            item = item.strip()
            quantity = float(quantity)
            if skip_negative and quantity < 0:
                continue
            items.append(Item(item, quantity))
        Item.sort_list(items, data_plugin.resource_order)
        return items

    @staticmethod
    def to_string(items):
        res = ""
        for item in items:
            res += f"{item.name}: {item.amount}\n"
        return res


def create_pi_boxplot(constellation_name: str,
                      resource_names: List[str],
                      region_names: Optional[List[str]] = None,
                      vertical=False,
                      full_axis=False) -> Tuple[go.Figure, int]:
    if region_names is not None and len(region_names) == 0:
        region_names = None
    logger.info("Creating boxplot for constellation %s, resources: %s", constellation_name, resource_names)
    res = data_plugin.db.fetch_resources(constellation_name, resource_names)
    res_max = data_plugin.db.fetch_max_resources(region_names)
    data = {}
    for r in res:
        if r["res"] in data:
            data[r["res"]].append(r["out"] / res_max[r["res"]])
        else:
            data[r["res"]] = [r["out"] / res_max[r["res"]]]
    # noinspection PyTypeChecker
    data = collections.OrderedDict(sorted(data.items(), key=lambda x: data_plugin.resource_order[x[0]]))
    data_keys = list(data)
    data_values = list(data.values())
    # noinspection PyPep8Naming
    N = len(data)
    c = ['hsl(' + str(h) + ',50%' + ',50%)' for h in np.linspace(0, 360, N)]

    # noinspection PyTypeChecker
    fig = go.Figure(
        data=[
            go.Box(
                x=data_values[i] if vertical else None,
                y=data_values[i] if not vertical else None,
                name=data_keys[i],
                marker_color=c[i]
            ) for i in (range(int(N)) if not vertical else range(int(N) - 1, -1, -1))
        ])

    if region_names is None:
        subtitle = "Compared to the best planet in <b>New Eden</b>."
    else:
        subtitle = "Compared to the best planet in "
        for i, region in enumerate(region_names):
            subtitle += f"<b>{region}</b>"
            if len(region_names) > 1 and i == len(region_names) - 2:
                subtitle += " and "
                if vertical:
                    subtitle += "<br>"
            elif i < len(region_names) - 2:
                subtitle += ", "
                if vertical:
                    subtitle += "<br>"
        subtitle += "."
    axis_percent = dict(zeroline=False, gridcolor="white", tickformat=",.0%")
    axis_names = dict(showgrid=False, zeroline=False, showticklabels=True)
    # format the layout
    fig.update_layout(
        xaxis=axis_names if not vertical else axis_percent,
        yaxis=axis_percent if not vertical else axis_names,
        paper_bgcolor="rgb(233,233,233)",
        plot_bgcolor="rgb(233,233,233)",
        title=go.layout.Title(
            text=f"Resources in <b>{constellation_name}</b> <br><sup><i>{subtitle}</i></sup>",
            xref="paper",
            x=0
        ),
        showlegend=False
    )
    if full_axis and not vertical:
        fig.update_yaxes(range=[0, 1])
    if full_axis and vertical:
        fig.update_xaxes(range=[0, 1])
    return fig, N


@wrap_async
def create_pi_boxplot_async(constellation_name: str,
                            resource_names: List[str],
                            region_names: List[str],
                            vertical=False,
                            full_axis=False):
    return create_pi_boxplot(constellation_name, resource_names, region_names, vertical, full_axis)


@wrap_async
def get_all_pi_planets(constellation_name: str,
                       resource_names: List[str] = None,
                       amount: Optional[int] = None):
    return data_plugin.db.fetch_resources(constellation_name, resource_names, amount)


@wrap_async
def get_best_pi_planets(constellation_name: str,
                        resource_name: str,
                        amount: Optional[int] = None):
    """
    Searches the best planets in a constellation for a given resource

    The list of dictionaries is as follows:
        [{
        "p_id": planet_id,
        "p_name": planet.name,
        "res": type.name,
        "out": output
        , ...]}
    :param constellation_name:
    :param resource_name:
    :param amount:
    :return:
    """
    return data_plugin.db.fetch_resources(constellation_name, [resource_name], amount)


@wrap_async
def get_best_pi_by_planet(constellation_name: str,
                          distance: int,
                          resource_name: str,
                          amount: Optional[int] = None):
    return data_plugin.db.fetch_ressource_by_planet(constellation_name, distance, resource_name, amount)


@wrap_async
def get_max_pi_planets(region_names: Optional[List[str]] = None):
    return data_plugin.db.fetch_max_resources(region_names)


@wrap_async
def get_constellation(const_name: str = None, planet_id: int = None):
    return data_plugin.db.fetch_constellation(const_name, planet_id)


@wrap_async
def get_planets(planet_ids: List[int]):
    return data_plugin.db.fetch_planets(planet_ids)


@wrap_async
def get_system(system_name: str):
    return data_plugin.db.fetch_system(system_name)


@wrap_async
def get_gates(system_name: str):
    return data_plugin.db.fetch_gates(system_name)


@wrap_async
def create_image(fig: go.Figure, *args, **kwargs):
    return fig.to_image(*args, **kwargs)


@wrap_async
def save_pi_plan(*args, **kwargs):
    return data_plugin.db.save_pi_plan(*args, **kwargs)


@wrap_async
def delete_pi_plan(*args, **kwargs):
    return data_plugin.db.delete_pi_plan(*args, **kwargs)


@wrap_async
def get_pi_plan(*args, **kwargs):
    return data_plugin.db.get_pi_plan(*args, **kwargs)


@wrap_async
def save_market_data(items):
    data_plugin.db.save_market_data(items)


async def init_market_data(items: Dict[str, Dict[str, int]]):
    await save_market_data(items)


@wrap_async
def get_market_data(
        item_names: Optional[List[str]] = None,
        item_type: Optional[str] = None):
    return data_plugin.db.get_market_data(item_names, item_type)


@wrap_async
def get_available_market_data(item_type: str):
    return data_plugin.db.get_available_market_data(item_type)


@wrap_async
def get_items_by_type(item_type: str):
    return data_plugin.db.fetch_items(item_type)


def graph_map_to_figure(graph: nx.Graph, include_highsec=True, node_size=3.5, show_info=False) -> go.Figure:
    edge_x = {"n": [], "l": [], "m": [], "h": []}
    edge_y = {"n": [], "l": [], "m": [], "h": []}
    edge_traces = []

    for n1, n2, data in graph.edges(data=True):
        sec1 = graph.nodes[n1]["security"]
        sec2 = graph.nodes[n2]["security"]
        if not include_highsec and sec1 > 0 and sec2 > 0:
            continue
        x0, y0 = graph.nodes[n1]["pos"]
        x1, y1 = graph.nodes[n2]["pos"]
        edge_type = "n"
        if data["routes"] > 25:
            edge_type = "h"
        elif data["routes"] > 10:
            edge_type = "m"
        elif data["routes"] > 1:
            edge_type = "l"
        edge_x[edge_type].append(x0)
        edge_x[edge_type].append(x1)
        edge_x[edge_type].append(None)
        edge_y[edge_type].append(y0)
        edge_y[edge_type].append(y1)
        edge_y[edge_type].append(None)

    edge_traces.append(go.Scatter(
        x=edge_x["n"], y=edge_y["n"],
        line=dict(width=0.5, color="#828282"),
        hoverinfo='none',
        mode='lines'))
    edge_traces.append(go.Scatter(
        x=edge_x["l"], y=edge_y["l"],
        line=dict(width=0.75, color="#e8a623"),
        hoverinfo='none',
        mode='lines'))
    edge_traces.append(go.Scatter(
        x=edge_x["m"], y=edge_y["m"],
        line=dict(width=1, color="#ba4907"),
        hoverinfo='none',
        mode='lines'))
    edge_traces.append(go.Scatter(
        x=edge_x["h"], y=edge_y["h"],
        line=dict(width=1.5, color="#d90000"),
        hoverinfo='none',
        mode='lines'))

    node_x_normal = []
    node_y_normal = []
    node_x_low_entry = []
    node_y_low_entry = []
    min_axis = 0
    max_axis = 0
    for node in graph.nodes():
        x, y = graph.nodes[node]["pos"]
        sec = graph.nodes[node]["security"]
        s = graph.nodes[node]["sucs"]
        if not include_highsec and sec > 0 and s == 0:
            continue
        if min_axis > x:
            min_axis = x
        if min_axis > y:
            min_axis = y
        if max_axis < x:
            max_axis = x
        if max_axis < y:
            max_axis = y
        if sec > 0 and s > 0:
            node_x_low_entry.append(x)
            node_y_low_entry.append(y)
        else:
            node_x_normal.append(x)
            node_y_normal.append(y)

    axis_scale = max_axis - min_axis
    max_axis += axis_scale * 0.05
    min_axis -= axis_scale * 0.05

    node_marker_normal = []
    node_marker_low_entry = []
    node_text_normal = []
    node_text_low_entry = []
    node_text_pos_low_entry = []
    max_mark = 0
    for node, data in graph.nodes(data=True):
        if not include_highsec and data["security"] > 0 and data["sucs"] == 0:
            continue

        if data["sucs"] > max_mark:
            max_mark = data["sucs"]
        if data["security"] > 0 and data["sucs"] > 0:
            node_marker_low_entry.append(data["sucs"])
            node_text_low_entry.append(f"{node}: {data['sucs']}")
            if data["pos"][1] > 0:
                node_text_pos_low_entry.append("bottom center")
            else:
                node_text_pos_low_entry.append("top center")
        else:
            node_marker_normal.append(data["sucs"])
            node_text_normal.append(f"{node}: {data['sucs']}")

    colorscale = [[0.00, "rgb(36, 36, 36)"],  # 0
                  [0.01, "rgb(36, 36, 36)"],  # < 1
                  [0.15, "rgb(88, 145, 22)"],  # 6
                  [0.3, "rgb(166, 161, 31)"],  # 12
                  [0.45, "rgb(173, 99, 14)"],  # 20
                  [0.60, "rgb(158, 52, 6)"],  # 36
                  [1.00, "rgb(232, 0, 0)"],  # 40
                  ]
    node_trace_normal = go.Scatter(
        x=node_x_normal, y=node_y_normal,
        mode="markers" if not show_info else "markers+text",
        hoverinfo="text",
        marker=dict(
            showscale=True,
            cmin=0,
            cmax=200,
            colorscale=colorscale,
            reversescale=False,
            color=[],
            size=node_size,
            colorbar=dict(
                tickvals=[0, 10, 25, 50, 100, 200],
                thickness=15,
                title="Systems covered",
                xanchor="left",
                titleside="right"
            ),
            line_width=0))
    # noinspection PyTypeChecker
    node_trace_low_entry = go.Scatter(
        x=node_x_low_entry, y=node_y_low_entry,
        mode="markers+text",
        # textsrc="text",
        textposition=node_text_pos_low_entry,
        hoverinfo="text",
        marker_symbol="diamond",
        marker=dict(
            showscale=False,
            cmin=0,
            cmax=max_mark,
            colorscale=colorscale,
            reversescale=False,
            color=[],
            size=6,
            line_width=0,
        ))

    node_trace_normal.marker.color = node_marker_normal
    node_trace_normal.text = node_text_normal

    node_trace_low_entry.marker.color = node_marker_low_entry
    node_trace_low_entry.text = node_text_low_entry

    # noinspection PyTypeChecker
    fig = go.Figure(data=edge_traces + [node_trace_normal] + [node_trace_low_entry],
                    layout=go.Layout(
                        title="Lowsec Autopilot Routes</b> <br><sup><i>Shortest route from every nullsec system to "
                              "the nearest lowsec system</i></sup>",
                        titlefont_size=16,
                        showlegend=False,
                        hovermode="closest",
                        margin=dict(b=20, l=5, r=5, t=40),
                        xaxis=dict(showgrid=False, zeroline=True, showticklabels=True, range=[min_axis, max_axis]),
                        yaxis=dict(showgrid=False, zeroline=True, showticklabels=True, range=[min_axis, max_axis]))
                    )
    return fig


def create_map_graph(inc_low_entries=False):
    def security_to_level(sec: float):
        levels = [(1, 1), (2, 0.9), (3, 0.7), (4, 0.5), (5, 0.3), (6, 0.1), (7, -0.1), (8, -0.3), (9, -0.5), (10, -0.7)]
        for level, s in levels:
            if sec > s:
                return level - 1
        return 10

    logger.debug("Loading map")
    systems = data_plugin.db.fetch_map()
    logger.debug("Map loaded")
    graph = nx.Graph()
    # max: x=319045588875206976, y=145615391401048000, z=472860102256057024
    logger.debug("Adding nodes")
    for system in systems:
        graph.add_node(system.name,
                       name=system.name,
                       security=system.security,
                       level=security_to_level(system.security),
                       pos=(system.x / 100000000000000000, system.z / 100000000000000000)
                       )

    logger.debug("Adding edges")
    lowsec_entries = []  # type: List[System]
    for system in systems:
        for sys in system.stargates:  # type: System
            graph.add_edge(system.name, sys.name, routes=0, sec_origin=system.security, sec_dest=sys.security)
            if inc_low_entries and system.security > 0 > sys.security:
                lowsec_entries.append(system)
    if not inc_low_entries:
        return graph
    return graph, lowsec_entries


def lowsec_pipe_analysis(graph: nx.Graph, lowsec_entries: List[str]):
    logger.info("Analysing shortest route to lowsec for %s lowsec entry systems", len(lowsec_entries))
    all_nodes = dict()
    for node, data in graph.nodes(data=True):
        if node not in lowsec_entries:
            data["d_low"] = None
        else:
            data["d_low"] = 0
        data["suc"] = None
        data["sucs"] = 0
        if data["security"] < 0 or node in lowsec_entries:
            all_nodes[node] = data

    current_nodes = list(lowsec_entries)
    for system in lowsec_entries:
        all_nodes[system]["d_low"] = 0
    next_nodes = []
    end_notes = []
    distance = 0
    while True:
        distance += 1
        logger.info("Processing %s systems with distance=%s", len(current_nodes), distance)
        while len(current_nodes) > 0:
            node = current_nodes.pop(0)
            deleted = False
            for n in graph.neighbors(node):
                if n not in all_nodes:
                    continue
                if all_nodes[n]["d_low"] is not None:
                    continue
                if not deleted and node in end_notes:
                    end_notes.remove(node)
                    deleted = True
                all_nodes[n]["suc"] = node
                all_nodes[n]["d_low"] = distance
                next_nodes.append(n)
                end_notes.append(n)

        if len(next_nodes) == 0:
            logger.info("Processed all systems")
            break
        current_nodes = next_nodes
        next_nodes = []
    logger.info("Analysing catchment area")
    current_nodes = end_notes
    next_nodes = []
    logger.info("Found %s end systems", len(current_nodes))

    def incr_path(c_n):
        all_nodes[c_n]["sucs"] += 1
        if all_nodes[c_n]["suc"] is not None:
            edge = graph[c_n][all_nodes[c_n]["suc"]]
            edge["routes"] += 1
            incr_path(all_nodes[c_n]["suc"])

    visited = []
    while True:
        while len(current_nodes) > 0:
            node = current_nodes.pop(0)
            if all_nodes[node]["suc"] is None:
                continue
            n = all_nodes[node]["suc"]
            if n not in visited:
                next_nodes.append(n)
                visited.append(n)
            incr_path(node)
        if len(next_nodes) == 0:
            logger.info("Processed all systems")
            break
        current_nodes = next_nodes
        next_nodes = []


@wrap_async
def find_path(start_name: str, end_name: str, sec_min: float = None, sec_max: float = None):
    def filter_edges(o, d, edge):
        if sec_min is not None and edge["sec_dest"] <= sec_min:
            return None
        if sec_max is not None and edge["sec_dest"] > sec_max:
            return None
        return 1

    graph = create_map_graph()
    start = None
    end = None
    for node in graph.nodes:
        if node == start_name:
            start = node
        if node == end_name:
            end = node
        if start is not None and end is not None:
            break
    if start is None:
        raise InputException(f"Start system '{start_name}' not found")
    if end is None:
        raise InputException(f"End system '{end_name}' not found")
    try:
        path = nx.astar_path(graph, start, end, weight=filter_edges)
    except nx.NetworkXNoPath as e:
        raise InputException(
            f"No available route between {start_name} and {end_name} with security between {sec_min} and {sec_max}"
        ) from e
    systems = data_plugin.db.fetch_systems(path)
    result = []  # type: List[Tuple[str, str, str, float]]
    for prev, current, dest in zip(path, path[1:], path[2:]):
        system = None
        for s in systems:
            if s.name == current:
                system = s
                break
        prev_gate = None
        dest_gate = None
        for cel in system.celestials:  # type: Celestial
            if cel.connected_gate is not None and cel.connected_gate.system.name == prev:
                prev_gate = cel
            if cel.connected_gate is not None and cel.connected_gate.system.name == dest:
                dest_gate = cel
        distance = math.sqrt(
            (dest_gate.x - prev_gate.x) ** 2 +
            (dest_gate.y - prev_gate.y) ** 2 +
            (dest_gate.z - prev_gate.z) ** 2
        ) / AU_RATIO
        # print(f"{current}: {prev} -> {dest}: {distance:.2f} AU")
        result.append((current, prev, dest, distance))
    return result


@wrap_async
def find_lowsec_entries(start_name: str, max_distance: int = 35):
    graph = create_map_graph()
    start = None
    for node, data in graph.nodes(data=True):
        if node == start_name:
            start = node
        data["suc"] = None
    if start is None:
        raise InputException(f"System {start_name} not found!")
    graph.nodes[start]["suc"] = "-START-"
    current_nodes = [start]
    next_nodes = []
    lowsec_nodes = []
    distance = 0
    while len(current_nodes) > 0 and distance <= max_distance:
        distance += 1
        for node in current_nodes:
            for n in graph.neighbors(node):
                if n != node and graph.nodes[n]["suc"] is None:
                    graph.nodes[n]["suc"] = node
                    graph.nodes[n]["distance"] = distance
                    if graph.nodes[n]["security"] <= 0:
                        next_nodes.append(n)
                    else:
                        lowsec_nodes.append(n)
        current_nodes = next_nodes
        next_nodes = []
    return dict(map(lambda n: (n, graph.nodes[n]["distance"]), lowsec_nodes))


@wrap_async
def get_item(item_name: str):
    return data_plugin.db.fetch_item(item_name)


def extract_value(embed: Embed, field_name: str, field_regex: str):
    value = None
    if field_name.casefold() == "title".casefold():
        value = embed.title
    else:
        for field in embed.fields:
            if field.name == field_name:
                value = field.value
                break
    if value is None or value == Embed.Empty:
        return None
    m = re.fullmatch(field_regex, value)
    if not m:
        return None
    if len(m.groups()) == 0:
        return m.string
    return m.group(1)


@wrap_async
def save_killmail(embed: Embed, member_plugin: MembersPlugin):
    config = data_plugin.killmail_config
    if config["field_id"] == "":
        return 0
    kill_data = {}
    for key in ["id", "final_blow", "ship", "kill_value", "system"]:
        kill_data[key] = extract_value(embed, config[f"field_{key}"], config[f"regex_{key}"])
    if None in kill_data.values():
        logger.warning("Embed with title '%s' doesn't contains a valid killmail: %s", embed.title, kill_data)
        return 0
    data_plugin.db.save_killmail(kill_data)
    m = re.fullmatch(r"\[[a-zA-Z0-9]+] (.*)", kill_data["final_blow"])
    if len(m.groups()) == 0:
        return 1
    player, _, _ = member_plugin.find_main_name(name=m.group(1))
    if player is None:
        return 1
    data_plugin.db.save_bounty(int(kill_data["id"]), player, "M")
    return 2


@wrap_async
def save_mobi_csv(csv: str, replace_tag: Optional[str] = None):
    data_plugin.db.save_killmail_csv(raw_csv=csv, replace_tag=replace_tag)


@wrap_async
def get_killboard_data(corp_tag: str):
    return data_plugin.db.get_killmail_leaderboard(killer_corp=corp_tag, amount=10)


@wrap_async
def get_top_kills(corp_tag: str):
    return data_plugin.db.get_top_killmails(killer_corp=corp_tag, amount=10)


def get_kill_id(embed: Embed):
    kill_id = extract_value(embed, data_plugin.killmail_config["field_id"], data_plugin.killmail_config["regex_id"])
    if kill_id is None or not kill_id.isnumeric():
        raise InputException(f"Embed doesn't contain a valid kill id: '{kill_id}'")
    return int(kill_id)


@wrap_async
def add_bounty(kill_id: int, player: str, bounty_type: str):
    data_plugin.db.save_bounty(kill_id, player, bounty_type)


@wrap_async
def get_killmail(kill_id: int):
    return data_plugin.db.get_killmail(kill_id)


@wrap_async
def get_bounties(kill_id: int):
    return data_plugin.db.get_bounty_by_killmail(kill_id)


@wrap_async
def get_all_bounties(start: int, end: int):
    return data_plugin.db.get_all_bounties(start, end)


@wrap_async
def get_bounties_by_player(start: datetime, end: datetime, user: str):
    return data_plugin.db.get_bounties_by_player(start, end, user)


@wrap_async
def clear_bounties(kill_id: int):
    return data_plugin.db.clear_bounties(kill_id)


@wrap_async
def verify_bounties(members_plugin: MembersPlugin, first: int, last: int, time: datetime = None):
    return data_plugin.db.verify_bounties(members_plugin, first, last, time)
