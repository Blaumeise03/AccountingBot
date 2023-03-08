import asyncio
import collections
import functools
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from typing import List, Iterable, Iterator, Dict, Any, Tuple, Optional, Callable, TypeVar, Union

import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import networkx as nx

from accounting_bot import sheet
from accounting_bot.universe.models import System, PiPlanSettings
from accounting_bot.universe.universe_database import UniverseDatabase

logger = logging.getLogger("data.utils")

db = None  # type: UniverseDatabase | None
resource_order = []  # type: List[str]
executor = ThreadPoolExecutor(max_workers=5)
loop = asyncio.get_event_loop()
_T = TypeVar("_T")


async def execute_async(func: Callable[..., _T], *args, **kwargs) -> _T:
    return await loop.run_in_executor(executor, functools.partial(func, *args, **kwargs))


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


async def create_pi_boxplot_async(constellation_name: str,
                                  resource_names: List[str],
                                  region_names: List[str],
                                  vertical=False) -> Tuple[go.Figure, int]:
    return await execute_async(create_pi_boxplot, constellation_name, resource_names, region_names, vertical)


async def get_all_pi_planets(constellation_name: str,
                             resource_names: List[str] = None,
                             amount: Optional[int] = None) -> List[Dict[str, int]]:
    def _get_all_pi_planets(*args, **kwargs):
        return db.fetch_resources(*args, **kwargs)
    return await execute_async(_get_all_pi_planets, constellation_name, resource_names, amount)


async def get_best_pi_planets(constellation_name: str,
                              resource_name: str,
                              amount: Optional[int] = None) -> List[Dict[str, int]]:
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
    def _get_best_pi_planets(const_name: str, res_name: str, am: Optional[int] = None) -> List[Dict[str, int]]:
        return db.fetch_resources(const_name, [res_name], am)
    return await execute_async(_get_best_pi_planets, constellation_name, resource_name, amount)


async def get_best_pi_by_planet(constellation_name: str,
                                distance: int,
                                resource_name: str,
                                amount: Optional[int] = None) -> List[Dict[str, int]]:
    def _get_pi(*args, **kwargs) -> List[Dict[str, int]]:
        return db.fetch_ressource_by_planet(*args, **kwargs)
    return await execute_async(_get_pi, constellation_name, distance, resource_name, amount)


async def get_system(system_name: str):
    def _get_system(sys_name: str):
        return db.fetch_system(sys_name)
    return await execute_async(_get_system, system_name)


async def get_constellation(constellation_name: str):
    def _get_const(const_name: str):
        return db.fetch_constellation(const_name)
    return await execute_async(_get_const, constellation_name)


async def create_image(*args, **kwargs) -> bytes:
    def _create_image(fig: go.Figure, *_args, **_kwargs) -> bytes:
        return fig.to_image(*_args, **_kwargs)
    return await execute_async(_create_image, *args, **kwargs)


async def save_pi_plan(*args, **kwargs) -> None:
    def _save_pi_plan(*_args, **_kwargs):
        return db.save_pi_plan(*_args, **_kwargs)
    return await execute_async(_save_pi_plan, *args, **kwargs)


async def delete_pi_plan(*args, **kwargs) -> None:
    def _delete_pi_plan(*_args, **_kwargs):
        return db.delete_pi_plan(*_args, **_kwargs)
    return await execute_async(_delete_pi_plan, *args, **kwargs)


async def get_pi_plan(*args, **kwargs) -> Union[PiPlanSettings, List[PiPlanSettings], None]:
    def _get_pi_plan(*_args, **_kwargs):
        return db.get_pi_plan(*_args, **_kwargs)
    return await execute_async(_get_pi_plan, *args, **kwargs)


async def init_market_data() -> None:
    def _save_market_data(_items):
        db.save_market_data(_items)
    logger.info("Loading market data")
    items = await sheet.get_market_data()
    await execute_async(_save_market_data, items)
    logger.info("Market data loaded")


async def get_market_data(
        item_names: Optional[List[str]] = None,
        item_type: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    def _get_market_data(*args, **kwargs):
        return db.get_market_data(*args, **kwargs)
    return await execute_async(_get_market_data, item_names, item_type)


async def get_available_market_data(item_type: str) -> None:
    def _get_available_market_data(*args, **kwargs):
        return db.get_available_market_data(*args, **kwargs)
    return await execute_async(_get_available_market_data, item_type)


def graph_map_to_figure(graph: nx.Graph, include_highsec=True, node_size=3.5) -> go.Figure:
    edge_x = {"n": [], "l": [], "m": [], "h": []}
    edge_y = {"n": [], "l": [], "m": [], "h": []}
    edge_traces = []

    for n1, n2, data in graph.edges(data=True):
        sec1 = graph.nodes[n1]["security"]
        sec2 = graph.nodes[n2]["security"]
        s1 = graph.nodes[n1]["sucs"]
        s2 = graph.nodes[n2]["sucs"]
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
