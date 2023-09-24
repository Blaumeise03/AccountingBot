from datetime import datetime

from dateutil import parser
from discord import ApplicationContext
from discord.ext import commands
from discord.ext.commands import Context


class LocalTimeConverter(commands.Converter):
    async def convert(self, ctx: Context, argument: str) -> datetime:
        return parser.parse(argument, parserinfo=parser.parserinfo(dayfirst=True))
