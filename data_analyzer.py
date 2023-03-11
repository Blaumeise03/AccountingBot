# Standalone program to access the database and view data

import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List

import plotly.graph_objects as go

from accounting_bot import utils
from accounting_bot.config import Config, ConfigTree
from accounting_bot.database import DatabaseConnector
from accounting_bot.universe import data_utils
from accounting_bot.universe.universe_database import UniverseDatabase

formatter = logging.Formatter(fmt="[%(asctime)s][%(levelname)s][%(name)s]: %(message)s")  # [%(threadName)s]
# Console log handler
console = logging.StreamHandler(sys.stdout)
console.setLevel(logging.DEBUG)
console.setFormatter(formatter)
logger = logging.getLogger()
# noinspection DuplicatedCode
logger.addHandler(console)
logging.root.setLevel(logging.NOTSET)
# logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)
logging.getLogger("data.db").setLevel(logging.DEBUG)

if not os.path.exists("images"):
    os.mkdir("images")

# noinspection DuplicatedCode
config_structure = {
    "db": {
        "user": (str, "N/A"),
        "password": (str, "N/A"),
        "port": (int, -1),
        "host": (str, "N/A"),
        "name": (str, "N/A"),
        "universe_name": (str, "N/A")
    },
    "project_resources": (list, [],)
}

config = Config("config.json", ConfigTree(config_structure), read_only=True)
config.load_config()
resource_order = config["project_resources"]  # type: List[str]

db = UniverseDatabase(
    username=config["db.user"],
    password=config["db.password"],
    port=config["db.port"],
    host=config["db.host"],
    database=config["db.universe_name"]
)
data_utils.db = db
utils.resource_order = resource_order


def safe_html(fig: go.Figure, path: str):
    while True:
        inp = input("Save as HTML [y/n]? ")
        if inp.casefold() == "y".casefold():
            logger.info("Writing html to %s", path)
            fig.write_html(path)
            logger.info("Saved html to %s", path)
            return
        elif inp.casefold() == "n".casefold():
            return
        print(f"Input '{inp}' not recognized, please write 'y' or 'n'")


def safe_image(fig: go.Figure, path: str, img_type: str, *arg, **kwargs):
    while True:
        inp = input(f"Save as {img_type} [y/n]? ")
        if inp.casefold() == "y".casefold():
            logger.info("Writing image to %s", path)
            fig.write_image(path, *arg, **kwargs)
            logger.info("Saved image to %s", path)
            return
        elif inp.casefold() == "n".casefold():
            return
        print(f"Input '{inp}' not recognized, please write 'y' or 'n'")


# noinspection PyTypeChecker
def main_analyze_pi():
    constellation_name = input("Enter constellation name: ").strip()
    resource_names = input("Enter resources, seperated by a ';': ").strip().split(";")
    resource_names = [r.strip() for r in resource_names]
    resource_names = list(filter(len, resource_names))
    region_names = input("Enter Regions, seperated by a ';': ").strip().split(";")
    region_names = [r.strip() for r in region_names]
    region_names = list(filter(len, region_names))
    fig, n = data_utils.create_pi_boxplot(constellation_name, resource_names, region_names)
    safe_html(fig, "images/plot.html")
    safe_image(fig, "images/plot.jpeg", "JPEG", height=600, width=n * 45)
    fig.show()


def main_generate_map():
    graph, lowsec_entries = data_utils.create_map_graph(inc_low_entries=True)
    lowsec_names = list(map(lambda s: s.name, lowsec_entries))
    data_utils.lowsec_pipe_analysis(graph, lowsec_names)
    inp = input("Please enter the node size (float): ")
    inp = float(inp)
    fig = data_utils.graph_map_to_figure(graph, False, node_size=inp)
    logger.info("Saving map")
    fig.write_html("images/map.html")
    logger.info("Saved map to images/map.html")
    inp = input("Save as SVG [y/n]? ")
    if inp.casefold() == "y".casefold():
        logger.info("Saving image to SVG")
        fig.write_image("images/map.svg", height=1024, width=2024)
        logger.info("Saved map to images/map.svg")
    inp = input("Save as JPEG [y/n]? ")
    if inp.casefold() == "y".casefold():
        logger.info("Saving image to JPEG")
        fig.write_image("images/plot.jpeg", scale=4, height=1024, width=1024)
        logger.info("Saved map to images/map.jpeg")
    fig.show(config={"scrollZoom": True})


def load_frp_csv(path):
    data = []
    i = -1
    with open(path, "r") as file:
        for line in file:
            i += 1
            if i == 0:
                continue
            line = line.split(",")
            frp = {
                "time": datetime.fromtimestamp(float(line[0])),
                "sov": line[2],
                "type": line[3],
                "player": line[4],
                "count": int(line[5])
            }
            data.append(frp)
    data.sort(key=lambda f: f["time"])

    def conv_time(day):
        match day:
            case 0:
                return "Monday"
            case 1:
                return "Tuesday"
            case 2:
                return "Wednesday"
            case 3:
                return "Thursday"
            case 4:
                return "Friday"
            case 5:
                return "Saturday"
            case 6:
                return "Sunday"

    def process_list(data, filter_key):
        return list(map(conv_time, sorted(map(lambda f: f["time"].weekday(), filter(lambda f: f["type"] == filter_key, data)))))

    times_frp = process_list(data, "FRP")
    times_sfrp = process_list(data, "SFRP")
    times_csq = process_list(data, "CSQ")

    fig = go.Figure(
        data=[
            go.Histogram(
                name="FRP",
                x=times_frp
            ),
            go.Histogram(
                name="SFRP",
                x=times_sfrp
            ),
            go.Histogram(
                name="CSQ",
                x=times_csq
            )
        ],
        layout=go.Layout(
            title=f"<b>FRP Distribution</b><br><sup><i>Sorted by day of week</i></sup>",
            titlefont_size=24,
            showlegend=True,
            hovermode="closest",
            margin=dict(b=20, l=5, r=5, t=60),
            xaxis=dict(showgrid=False, zeroline=True, showticklabels=True),
            yaxis=dict(showgrid=False, zeroline=True, showticklabels=True))
    )
    fig.update_layout(barmode='stack')
    fig.show()


if __name__ == '__main__':
    while True:
        inp = input("Please select action (help for list of available commands): ").casefold()
        if inp == "pi".casefold():
            main_analyze_pi()
            exit(0)
        elif inp == "map".casefold():
            main_generate_map()
            exit(0)
        elif inp == "frp".casefold():
            load_frp_csv("resources/frpStatistic.eve.csv")
            exit(0)
        elif inp == "help".casefold():
            print("pi: Find pi in a constellation")
        else:
            print("Error: Command not found, enter 'help' for a list of all commands")
