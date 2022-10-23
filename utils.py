import difflib
import io
import logging
import traceback
from typing import Union

import discord
from discord import Interaction


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
