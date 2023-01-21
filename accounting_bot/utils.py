import difflib
import io
import json
import logging
import traceback
from enum import Enum
from os.path import exists
from typing import Union, Tuple, Optional

import discord
from discord import Interaction
from discord.ext.commands import Bot
from discord.ui import View

from accounting_bot.config import Config

logger = logging.getLogger("bot.utils")
CONFIG = None  # type: Config | None
BOT = None  # type: Bot | None


def set_config(config: Config, bot):
    global CONFIG, BOT
    CONFIG = config
    BOT = bot


discord_users = {}  # type: {str: int} | None
ingame_twinks = {}
ingame_chars = []
main_chars = []

if exists("discord_ids.json"):
    with open("discord_ids.json") as json_file:
        discord_users = json.load(json_file)


# noinspection PyShadowingNames
def log_error(logger: logging.Logger, error: Exception, in_class=None):
    if error and error.__class__ == discord.errors.NotFound:
        logging.warning("discord.errors.NotFound Error in %s: %s", in_class.__name__, str(error))
        return
    full_error = traceback.format_exception(type(error), error, error.__traceback__)
    if in_class:
        logger.error("An error occurred in class %s", in_class.__name__)
    for line in full_error:
        for line2 in line.split("\n"):
            if len(line2.strip()) > 0:
                logger.exception(line2, exc_info=False)


async def send_exception(error: Exception, interaction: Interaction):
    await interaction.followup.send(f"An unexpected error occurred: \n{error.__class__.__name__}\n{str(error)}",
                                    ephemeral=True)


def string_to_file(text: str, filename="message.txt"):
    data = io.BytesIO(text.encode())
    data.seek(0)
    return discord.File(fp=data, filename=filename)


def list_to_string(line: [str]):
    res = ""
    for s in line:
        res += s + "\n"
    return res


def get_main_account(name: str = None, discord_id: int = None) -> (Union[str, None], Union[str, None], bool):
    """
    Finds the closest playername match for a given string. And returns the main account of this player, together with
    the parsed input name and the information, whether it was a perfect match.
    Alternatively searches for the character name belonging to the discord account.

    :param name: the string which should be looked up or
    :param discord_id: the id to search for
    :return:    Main Char: str or None,
                Char name: str or None,
                Perfect match: bool
    """
    if name is None and discord_id is None:
        return None, None, False
    if discord_id is not None:
        for main_char, d_id in discord_users.items():
            if d_id == discord_id:
                return main_char
        return None, None, False
    names = difflib.get_close_matches(name, ingame_chars, 1)
    if len(names) > 0:
        name = str(names[0])
        main_char = name
        if main_char in ingame_twinks:
            main_char = ingame_twinks[main_char]
        if name.casefold() == name.casefold():
            return main_char, name, True
        return main_char, name, False
    return None, None, False


def parse_player(string: str, users: [str]) -> (Union[str, None], bool):
    """
    Finds the closest playername match for a given string. It returns the name or None if not found, as well as a
    boolean indicating whether it was a perfect match.

    :param string: the string which should be looked up
    :param users: the available usernames
    :return: (Playername: str or None, Perfect match: bool)
    """
    names = difflib.get_close_matches(string, users, 1)
    if len(names) > 0:
        name = str(names[0])
        if name.casefold() == string.casefold():
            return str(names[0]), True
        return str(names[0]), False
    return None, False


async def get_or_find_discord_id(bot=None, guild=None, user_role=None, player_name="") -> Tuple[Optional[int], Optional[str], Optional[bool]]:
    if bot is None:
        bot = BOT
    if guild is None:
        guild = CONFIG["server"]
    if user_role is None:
        user_role = CONFIG["user_role"]

    discord_id = get_discord_id(player_name)
    if discord_id:
        return discord_id, player_name, True
    name, perfect, nicknames = await find_discord_id(bot, guild, user_role, player_name)
    if perfect:
        return nicknames[name], name, True
    return None, name, False


def get_discord_id(name: str):
    if name in discord_users:
        return discord_users[name]
    else:
        return None


async def find_discord_id(bot, guild, user_role, player_name):
    nicknames = dict(await bot.get_guild(guild)
                     .fetch_members()
                     .filter(lambda m: m.get_role(user_role) is not None)
                     .map(lambda m: (m.nick if m.nick is not None else m.name, m.id))
                     .flatten())
    name, perfect = parse_player(player_name, nicknames)
    return name, perfect, nicknames


def save_discord_id(name: str, discord_id: int):
    if name in discord_users and discord_users[name] == discord_id:
        return
    while discord_id in discord_users.values():
        for k, v in list(discord_users.items()):
            if v == discord_id:
                logger.warning("Deleted discord id %s (user: %s)", v, k)
                del discord_users[k]
    discord_users[name] = discord_id
    save_discord_config()


def save_discord_config():
    with open("discord_ids.json", "w") as outfile:
        json.dump(discord_users, outfile, indent=4)


class AutoDisableView(View):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def on_timeout(self) -> None:
        logger.info("View %s timed out (%s).", self.id, self.message.id if self.message is not None else "None")
        if self.message is not None:
            await self.message.edit(view=None)
        self.clear_items()
        self.disable_all_items()


class State(Enum):
    offline = 0
    preparing = 1
    starting = 2
    online = 3
