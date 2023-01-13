import difflib
import io
import json
import logging
import traceback
from os.path import exists
from typing import Union

import discord
from discord import Interaction
from discord.ui import View

logger = logging.getLogger("bot.utils")

discord_users = {}
if exists("discord_ids.json"):
    with open("discord_ids.json") as json_file:
        discord_users = json.load(json_file)


def log_error(logger: logging.Logger, error):
    full_error = traceback.format_exception(type(error), error, error.__traceback__)
    for line in full_error:
        for l in line.split("\n"):
            if len(l.strip()) > 0:
                logger.exception(l, exc_info=False)


async def send_exception(error: Exception, interaction: Interaction):
    await interaction.followup.send(f"An unexpected error occurred: \n{error.__class__.__name__}\n{str(error)}",
                                    ephemeral=True)


def string_to_file(text: str, filename="message.txt"):
    data = io.BytesIO(text.encode())
    data.seek(0)
    return discord.File(fp=data, filename=filename)


def list_to_string(l: [str]):
    res = ""
    for s in l:
        res += s + "\n"
    return res


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
