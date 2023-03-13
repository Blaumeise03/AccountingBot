import collections
import logging
import re
from datetime import datetime
from typing import List, Tuple, Optional

import networkx as nx
import numpy as np
import plotly.graph_objects as go
from discord import Embed

from accounting_bot import sheet, utils
from accounting_bot.config import ConfigTree
from accounting_bot.exceptions import InputException
from accounting_bot.universe.models import System
from accounting_bot.universe.universe_database import UniverseDatabase
from accounting_bot.utils import wrap_async, resource_order

logger = logging.getLogger("data.utils")

db = None  # type: UniverseDatabase | None
killmail_config = None  # type: ConfigTree | None
killmail_admins = []  # type: List[int]


def create_pi_boxplot(constellation_name: str,
                      resource_names: List[str],
                      region_names: Optional[List[str]] = None,
                      vertical=False) -> Tuple[go.Figure, int]:
    if region_names is not None and len(region_names) == 0:
        region_names = None
    logger.info("Creating boxplot for constellation %s, resources: %s", constellation_name, resource_names)
    res = db.fetch_resources(constellation_name, resource_names)
    res_max = db.fetch_max_resources(region_names)
    data = {}
    for r in res:
        if r["res"] in data:
            data[r["res"]].append(r["out"] / res_max[r["res"]])
        else:
            data[r["res"]] = [r["out"] / res_max[r["res"]]]
    # noinspection PyTypeChecker
    data = collections.OrderedDict(sorted(data.items(), key=lambda x: resource_order.index(x[0])))
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
    return fig, N


@wrap_async
def create_pi_boxplot_async(constellation_name: str,
                            resource_names: List[str],
                            region_names: List[str],
                            vertical=False):
    return create_pi_boxplot(constellation_name, resource_names, region_names, vertical)


@wrap_async
def get_all_pi_planets(constellation_name: str,
                       resource_names: List[str] = None,
                       amount: Optional[int] = None):
    return db.fetch_resources(constellation_name, resource_names, amount)


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
    return db.fetch_resources(constellation_name, [resource_name], amount)


@wrap_async
def get_best_pi_by_planet(constellation_name: str,
                          distance: int,
                          resource_name: str,
                          amount: Optional[int] = None):
    return db.fetch_ressource_by_planet(constellation_name, distance, resource_name, amount)


@wrap_async
def get_max_pi_planets(region_names: Optional[List[str]] = None):
    return db.fetch_max_resources(region_names)


@wrap_async
def get_system(system_name: str):
    return db.fetch_system(system_name)


def get_constellation(constellation_name: str):
    return db.fetch_constellation(constellation_name)


@wrap_async
def create_image(fig: go.Figure, *args, **kwargs):
    return fig.to_image(*args, **kwargs)


@wrap_async
def save_pi_plan(*args, **kwargs):
    return db.save_pi_plan(*args, **kwargs)


@wrap_async
def delete_pi_plan(*args, **kwargs):
    return db.delete_pi_plan(*args, **kwargs)


@wrap_async
def get_pi_plan(*args, **kwargs):
    return db.get_pi_plan(*args, **kwargs)


@wrap_async
def save_market_data(items):
    db.save_market_data(items)


async def init_market_data():
    items = await sheet.get_market_data()
    await save_market_data(items)


@wrap_async
def get_market_data(
        item_names: Optional[List[str]] = None,
        item_type: Optional[str] = None):
    return db.get_market_data(item_names, item_type)


@wrap_async
def get_available_market_data(item_type: str):
    return db.get_available_market_data(item_type)


@wrap_async
def get_items_by_type(item_type: str):
    return db.fetch_items(item_type)


def graph_map_to_figure(graph: nx.Graph, include_highsec=True, node_size=3.5) -> go.Figure:
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
        elif data["routes"] > 15:
            edge_type = "m"
        elif data["routes"] > 0:
            edge_type = "l"
        edge_x[edge_type].append(x0)
        edge_x[edge_type].append(x1)
        edge_x[edge_type].append(None)
        edge_y[edge_type].append(y0)
        edge_y[edge_type].append(y1)
        edge_y[edge_type].append(None)

    edge_traces.append(go.Scatter(
        x=edge_x["n"], y=edge_y["n"],
        line=dict(width=0.5, color="#e8a623"),
        hoverinfo='none',
        mode='lines'))
    edge_traces.append(go.Scatter(
        x=edge_x["l"], y=edge_y["l"],
        line=dict(width=0.75, color="#ba4907"),
        hoverinfo='none',
        mode='lines'))
    edge_traces.append(go.Scatter(
        x=edge_x["m"], y=edge_y["m"],
        line=dict(width=1, color="#d90000"),
        hoverinfo='none',
        mode='lines'))
    edge_traces.append(go.Scatter(
        x=edge_x["h"], y=edge_y["h"],
        line=dict(width=1.5, color="#ff0303"),
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
                  [0.30, "rgb(166, 161, 31)"],  # 12
                  [0.50, "rgb(173, 99, 14)"],  # 20
                  [0.90, "rgb(158, 52, 6)"],  # 36
                  [1.00, "rgb(232, 0, 0)"],  # 40
                  ]
    node_trace_normal = go.Scatter(
        x=node_x_normal, y=node_y_normal,
        mode="markers",
        hoverinfo="text",
        marker=dict(
            showscale=True,
            cmin=0,
            cmax=max_mark,
            colorscale=colorscale,
            reversescale=False,
            color=[],
            size=node_size,
            colorbar=dict(
                tickvals=[0, 5, 10, 20, 30, 40],
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
                        title=f"Lowsec Autopilot Routes</b> <br><sup><i>Shortest route from every nullsec system to "
                              f"the nearest lowsec system</i></sup>",
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

    logger.info("Loading map")
    systems = db.fetch_map()
    logger.info("Map loaded")
    graph = nx.Graph()
    # max: x=319045588875206976, y=145615391401048000, z=472860102256057024
    logger.info("Adding nodes")
    for system in systems:
        graph.add_node(system.name,
                       name=system.name,
                       security=system.security,
                       level=security_to_level(system.security),
                       pos=(system.x / 100000000000000000, system.z / 100000000000000000)
                       )

    logger.info("Adding edges")
    lowsec_entries = []  # type: List[System]
    for system in systems:
        for sys in system.stargates:  # type: System
            graph.add_edge(system.name, sys.name, routes=0)
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
    while True:
        while len(current_nodes) > 0:
            node = current_nodes.pop(0)
            if all_nodes[node]["suc"] is None:
                continue
            n = all_nodes[node]["suc"]
            next_nodes.append(n)
            all_nodes[n]["sucs"] = all_nodes[node]["sucs"] + 1
            data = graph[node][n]
            data["routes"] += 1
        if len(next_nodes) == 0:
            logger.info("Processed all systems")
            break
        current_nodes = next_nodes
        next_nodes = []
    pass


@wrap_async
def get_item(item_name: str):
    return db.fetch_item(item_name)


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
def save_killmail(embed: Embed):
    if killmail_config["field_id"] == "":
        return 0
    kill_data = {}
    for key in ["id", "final_blow", "ship", "kill_value", "system"]:
        kill_data[key] = extract_value(embed, killmail_config[f"field_{key}"], killmail_config[f"regex_{key}"])
    if None in kill_data.values():
        logger.warning("Embed with title '%s' doesn't contains a valid killmail: %s", embed.title, kill_data)
        return 0
    db.save_killmail(kill_data)
    m = re.fullmatch(r"\[[a-zA-Z0-9]+] (.*)", kill_data["final_blow"])
    if len(m.groups()) == 0:
        return 1
    player, char, _ = utils.get_main_account(name=m.group(1))
    if player is None:
        return 1
    db.save_bounty(int(kill_data["id"]), player, "M")
    return 2


def get_kill_id(embed: Embed):
    kill_id = extract_value(embed, killmail_config["field_id"], killmail_config["regex_id"])
    if kill_id is None or not kill_id.isnumeric():
        raise InputException(f"Embed doesn't contain a valid kill id: '{kill_id}'")
    return int(kill_id)


@wrap_async
def add_bounty(kill_id: int, player: str, bounty_type: str):
    utils.get_main_account(name=player)
    db.save_bounty(kill_id, player, bounty_type)


@wrap_async
def get_killmail(kill_id: int):
    return db.get_killmail(kill_id)


@wrap_async
def get_bounties(kill_id: int):
    return db.get_bounty_by_killmail(kill_id)


@wrap_async
def get_all_bounties(start: int, end: int):
    return db.get_all_bounties(start, end)


@wrap_async
def clear_bounties(kill_id: int):
    return db.clear_bounties(kill_id)


@wrap_async
def verify_bounties(first: int, last: int, time: datetime = None):
    return db.verify_bounties(first, last, time)
