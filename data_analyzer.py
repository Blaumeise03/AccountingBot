# Standalone program to access the database and view data

import logging
import os
import sys
from typing import List

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
        "name": (str, "N/A")
    },
    "project_resources": (list, [],)
}

config = Config("config.json", ConfigTree(config_structure), read_only=True)
config.load_config()
resource_order = config["project_resources"]  # type: List[str]
connector = DatabaseConnector(
        username=config["db.user"],
        password=config["db.password"],
        port=config["db.port"],
        host=config["db.host"],
        database=config["db.name"]
    )

db = UniverseDatabase(connector)
data_utils.db = db
data_utils.resource_order = resource_order


# noinspection PyTypeChecker
def main_analyze_pi():
    constellation_name = input("Enter constellation name: ").strip()
    resource_names = input("Enter resources, seperated by a ';': ").strip().split(";")
    resource_names = [r.strip() for r in resource_names]
    resource_names = filter(len, resource_names)
    fig = data_utils.create_pi_boxplot(constellation_name, resource_names)
    fig.write_image("images/plot.jpeg", height=600, width=len(resource_names) * 45)
    logger.info("Saved image to images/plot.jpeg")
    fig.show()


if __name__ == '__main__':
    while True:
        inp = input("Please select action (help for list of available commands): ").casefold()
        if inp == "pi".casefold():
            main_analyze_pi()
            exit()
        elif inp == "help".casefold():
            print("pi: Find pi in a constellation")
        else:
            print("Error: Command not found, enter 'help' for a list of all commands")
