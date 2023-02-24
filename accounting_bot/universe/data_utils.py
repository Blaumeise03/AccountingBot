import asyncio
import collections
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Iterable, Iterator

import numpy as np
import plotly.express as px
import plotly.graph_objects as go

from accounting_bot.universe.universe_database import UniverseDatabase

logger = logging.getLogger("data.utils")

db = None  # type: UniverseDatabase | None
resource_order = []  # type: List[str]
executor = ThreadPoolExecutor(max_workers=5)
loop = asyncio.get_event_loop()


def create_pi_boxplot(constellation_name: str, resource_names: List[str]) -> go.Figure:
    logger.info("Creating boxplot for constellation %s, resources: %s", constellation_name, resource_names)
    res = db.fetch_resources(constellation_name, resource_names)
    res_max = db.fetch_max_resources()
    data = {}
    for r in res:
        if r["res"] in data:
            data[r["res"]].append(r["out"] / res_max[r["res"]])
        else:
            data[r["res"]] = [r["out"] / res_max[r["res"]]]
    data = collections.OrderedDict(sorted(data.items(), key=lambda x: resource_order.index(x[0])))
    data_keys = list(data)
    data_values = list(data.values())
    N = len(data)
    c = ['hsl(' + str(h) + ',50%' + ',50%)' for h in np.linspace(0, 360, N)]

    # noinspection PyTypeChecker
    fig = go.Figure(data=[go.Box(
        y=data_values[i],
        name=data_keys[i],
        marker_color=c[i]
    ) for i in range(int(N))])

    # format the layout
    fig.update_layout(
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=True),
        yaxis=dict(zeroline=False, gridcolor="white", tickformat=",.0%"),
        paper_bgcolor="rgb(233,233,233)",
        plot_bgcolor="rgb(233,233,233)",
        title=go.layout.Title(
            text=f"Resources in <b>{constellation_name}</b> <br><sup><i>Compared to the best planet in New Eden</i></sup>",
            xref="paper",
            x=0
        ),
        showlegend=False
    )
    return fig


async def create_pi_boxplot_async(constellation_name: str, resource_names: List[str]) -> go.Figure:
    return await loop.run_in_executor(executor, functools.partial(create_pi_boxplot, constellation_name, resource_names))


async def create_image(*args, **kwargs) -> bytes:
    def _create_image(fig: go.Figure, *_args, **_kwargs) -> bytes:
        return fig.to_image(*_args, **_kwargs)
    return await loop.run_in_executor(executor, functools.partial(_create_image, *args, **kwargs))

