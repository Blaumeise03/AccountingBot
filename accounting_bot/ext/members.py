# PluginConfig
# Name: MembersPlugin
# Author: Blaumeise03
# End
import asyncio
import difflib
import logging
from typing import Optional, List, Dict, Callable, Union, Tuple

from accounting_bot.exceptions import UsernameNotFoundException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper

logger = logging.getLogger("sheet.member")


class UserDataProviderException(Exception):
    pass


class Player:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rank = None  # type: str | None
        self.discord_id = None  # type: int | None
        self.alts = []  # type: List[str]
        self.authorized_discord_ids = []  # type: List[int]

    def has_permissions(self, discord_id: int):
        return discord_id == self.discord_id or discord_id in self.authorized_discord_ids


class DataChain:
    def __init__(self, parent: Optional["DataChain"] = None) -> None:
        super().__init__()
        self.func = None
        self.parent = parent  # type: DataChain | None
        self.next = None  # type: DataChain | None

    def map_data(self, func: Callable[[Dict[str, Player]], Dict[str, Player]]):
        self.func = func
        self.next = DataChain(parent=self)
        return self.next


class MembersPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.players = []  # type: List[Player]
        self._name_lookup_table = {}  # type: Dict[str, Player]
        self._data_provider = None  # type: DataChain | None

    async def _execute_chain(self):
        players = {}
        current = self._data_provider
        while current is not None and current.func is not None:
            if asyncio.iscoroutinefunction(current.func):
                players = await current.func(players)
            else:
                players = current.func(players)
            current = current.next
        self._name_lookup_table = players
        self.players = list(self._name_lookup_table.keys())
        logger.info("Loaded %s players", len(self.players))

    async def on_enable(self):
        await self._execute_chain()

    async def on_disable(self):
        self.players.clear()
        self._name_lookup_table.clear()

    def set_data_source(self):
        self._data_provider = DataChain()
        return self._data_provider

    def get_user(self, name: str) -> Optional[Player]:
        if name in self._name_lookup_table:
            return self._name_lookup_table[name]
        return None

    def get_main_name(self, name: str) -> str:
        if name in self._name_lookup_table:
            return self._name_lookup_table[name].name
        raise UsernameNotFoundException(f"User '{name}' not found")

    def parse_player(self, string: str) -> Tuple[Optional[str], bool]:
        """
        Finds the closest playername match for a given string. It returns the name or None if not found, as well as a
        boolean indicating whether it was a perfect match.

        :param string: The string which should be looked up
        :return: tuple(Playername: str or None, Perfect match: bool)
        """
        names = difflib.get_close_matches(string, self._name_lookup_table.keys(), 1)
        if len(names) > 0:
            name = str(names[0])
            if name.casefold() == string.casefold():
                return str(names[0]), True
            return str(names[0]), False
        return None, False

    def get_discord_id(self, player_name: str, only_id=False) -> Union[
        Tuple[Optional[int], Optional[str], bool], int
    ]:
        """
        Returns the discord id, username and true if the name was matched perfectly (else false)

        :param player_name:
        :return: The discord id, username and a boolean
        """
        player_name, perfect = self.parse_player(player_name)
        player = self.get_user(player_name)
        if only_id:
            return player.discord_id if player else None, player.name, perfect
        else:
            return player.discord_id if player else None

    def find_main_name(self, name: str = None, discord_id: int = None) -> Tuple[Optional[str], Optional[str], bool]:
        """
        Finds the closest playername match for a given string. And returns the main account of this player, together with
        the parsed input name and the information, whether it was a perfect match.
        Alternatively, searches for the character name belonging to the discord account.

        :param name: The string which should be looked up or
        :param discord_id: The id to search for
        :return:    Main Char: str or None,
                    Char name: str or None,
                    Perfect match: bool
        """
        if name is None and discord_id is None:
            return None, None, False
        if discord_id is not None:
            for main_char, user in self._name_lookup_table.items():
                if user.discord_id == discord_id:
                    return main_char, main_char, True
            return None, None, False
        name, perfect = self.parse_player(name)
        player = self.get_user(name)
        return player.name if player else None, name, perfect

    def has_permissions(self, discord_id: int, player_name: str):
        user = self.get_user(player_name)
        if user is None:
            return False
        return discord_id == user.discord_id or discord_id in user.authorized_discord_ids
