# PluginConfig
# Name: MembersPlugin
# Author: Blaumeise03
# End
import asyncio
import difflib
import logging
from typing import Optional, List, Dict, Callable, Union, Tuple, Set

import discord
from discord import option, AutocompleteContext, ApplicationContext, User, Role
from discord.ext import commands

from accounting_bot.exceptions import UsernameNotFoundException, NoPermissionException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import admin_only, guild_only

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

    def __repr__(self):
        return f"Player(name='{self.name}')"


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
        self.players = set()  # type: Set[Player]
        self.main_chars = set()  # type: Set[str]
        self._name_lookup_table = {}  # type: Dict[str, Player]
        self._data_provider = None  # type: DataChain | None
        self._save_data_provider = None  # type: DataChain | None

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
        self.players = set(self._name_lookup_table.values())
        self.main_chars = set(map(lambda p: p.name, self.players))
        logger.info("Loaded %s players", len(self.players))

    async def _execute_save_chain(self):
        players = self.players.copy()
        current = self._save_data_provider
        while current is not None and current.func is not None:
            if asyncio.iscoroutinefunction(current.func):
                players = await current.func(players)
            else:
                players = current.func(players)
            current = current.next
        logger.info("Saved %s players", len(self.players))

    def on_load(self):
        self.register_cog(MembersCommands(self))

    async def on_enable(self):
        await self._execute_chain()

    async def on_disable(self):
        self.players.clear()
        self._name_lookup_table.clear()
        self.main_chars.clear()

    def set_data_source(self):
        self._data_provider = DataChain()
        return self._data_provider

    def set_save_data_chain(self):
        self._save_data_provider = DataChain()
        return self._save_data_provider

    def get_user(self, name: Optional[str] = None, discord_id: Optional[int] = None) -> Optional[Player]:
        if name is not None and name in self._name_lookup_table:
            return self._name_lookup_table[name]
        if discord_id is not None:
            for p in self.players:
                if p.discord_id == discord_id:
                    return p
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

        :param only_id:
        :param player_name:
        :return: The discord id, username and a boolean
        """
        player_name, perfect = self.parse_player(player_name)
        player = self.get_user(player_name)
        if not only_id:
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

    async def save_discord_id(self, name: str, discord_id: int):
        player = self.get_user(name)
        if player is None:
            player = Player(name)
        player.discord_id = discord_id
        await self.save_user_list()

    async def save_user_list(self):
        await self._execute_save_chain()


def main_char_autocomplete(self: AutocompleteContext):
    # noinspection PyTypeChecker
    bot = self.bot  # type: AccountingBot
    member_p = bot.get_plugin("MembersPlugin")  # type: MembersPlugin
    return filter(lambda n: self.value is None or n.startswith(self.value.strip()), member_p.main_chars)


class MembersCommands(commands.Cog):
    def __init__(self, plugin: MembersPlugin) -> None:
        super().__init__()
        self.plugin = plugin

    @commands.slash_command(name="registeruser", description="Registers a user to a discord ID")
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The discord user to register", required=True)
    @admin_only()
    async def register_user(self, ctx: ApplicationContext, ingame_name: str, user: User):
        if user is None:
            await ctx.respond("A user is required.", ephemeral=True)
            return
        user_id = user.id
        if ingame_name is None or ingame_name == "":
            await ctx.respond("Ingame name is required.", ephemeral=True)
            return
        matched_name, _, _ = self.plugin.find_main_name(name=ingame_name)

        if matched_name is not None:
            old_id = self.plugin.get_discord_id(player_name=matched_name, only_id=True)
            old_user = self.plugin.get_user(discord_id=user.id)
            if old_user is not None:
                old_user.discord_id = None
            await self.plugin.save_discord_id(matched_name, int(user_id))
            logger.info("(%s) Saved discord id %s to player %s, old id %s", ctx.user.id, user_id, matched_name, old_id)
            await ctx.response.send_message(
                f"Spieler `{matched_name}` wurde zur ID `{user_id}` (<@{user_id}>) eingespeichert!\n" +
                ("" if not old_id else f"Die alte ID war `{old_id}` (<@{old_id}>).\n") +
                ("" if not old_user else f"Der alter Nutzer war `{old_user}`."),
                ephemeral=True)
        else:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)

    @commands.slash_command(name="grant_permissions", description="Grants owner permissions to a discord account")
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The discord user to grant permissions to", required=True)
    async def grant_permissions(self, ctx: ApplicationContext, ingame_name: str, user: User):
        if user is None:
            await ctx.respond("A user is required.", ephemeral=True)
            return
        if ingame_name is None or ingame_name == "":
            await ctx.respond("Ingame name is required.", ephemeral=True)
            return
        matched_name, _, _ = self.plugin.find_main_name(name=ingame_name)
        if matched_name is None:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)
        player = self.plugin.get_user(name=matched_name)
        if player is None:
            raise UsernameNotFoundException(f"User {matched_name} not found")
        if player.discord_id != ctx.user.id and not self.plugin.bot.is_admin(ctx.user.id):
            raise NoPermissionException(f"Disord user {ctx.user.name}:{ctx.user.id} is not owner of player {player}")
        player.authorized_discord_ids.append(user.id)
        await self.plugin.save_user_list()
        logger.info("User %s:%s granted owner permissions for %s to %s:%s",
                    ctx.user.name, ctx.user.id, player, user.name, user.id)
        await ctx.response.send_message(f"Dem Nutzer `{user.name}:{user.id}` (<@{user.id}>) wurden volle Owner-Rechte "
                                        f"für den Spieleraccount {player.name} erteilt.", ephemeral=True)

    # noinspection DuplicatedCode
    @commands.slash_command(name="revoke_permissions", description="Revokes owner permissions from a discord account")
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The discord user to grant permissions to", required=True)
    async def revoke_permissions(self, ctx: ApplicationContext, ingame_name: str, user: User):
        if user is None:
            await ctx.respond("A user is required.", ephemeral=True)
            return
        if ingame_name is None or ingame_name == "":
            await ctx.respond("Ingame name is required.", ephemeral=True)
            return
        matched_name, _, _ = self.plugin.find_main_name(name=ingame_name)
        if matched_name is None:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)
        player = self.plugin.get_user(name=matched_name)
        if player is None:
            raise UsernameNotFoundException(f"User {matched_name} not found")
        if player.discord_id != ctx.user.id and not self.plugin.bot.is_admin(ctx.user.id):
            raise NoPermissionException(f"Disord user {ctx.user.name}:{ctx.user.id} is not owner of player {player}")
        if user.id not in player.authorized_discord_ids:
            await ctx.respond(f"Der Nutzer `{user.name}:{user.id}` (<@{user.id}>) hat keine Rechte für den "
                              f"Spieleraccount {player.name}.", ephemeral=True)
            return
        player.authorized_discord_ids.remove(user.id)
        await self.plugin.save_user_list()
        logger.info("User %s:%s revoked owner permissions for %s from %s:%s",
                    ctx.user.name, ctx.user.id, player, user.name, user.id)
        await ctx.response.send_message(f"Dem Nutzer `{user.name}:{user.id}` (<@{user.id}>) wurden die Owner-Rechte "
                                        f"für den Spieleraccount {player.name} entzogen.", ephemeral=True)

    # noinspection DuplicatedCode
    @commands.slash_command(name="show_permissions", description="Shows all users with permissions for a player")
    @option("ingame_name", description="The main character name of the player", required=True,
            autocomplete=main_char_autocomplete)
    async def revoke_permissions(self, ctx: ApplicationContext, ingame_name: str):
        if ingame_name is None:
            await ctx.respond("Ingame name is required.", ephemeral=True)
            return
        matched_name, _, _ = self.plugin.find_main_name(name=ingame_name)
        if matched_name is None:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)
        player = self.plugin.get_user(name=matched_name)
        if player is None:
            raise UsernameNotFoundException(f"User {matched_name} not found")
        msg = f"Spielerinformationen für Spieler `{player.name}`\n"
        if len(player.alts) > 0:
            msg += "Alts:\n```\n"
            for alt in player.alts:
                msg += alt + "\n"
            msg += "```\n"
        if player.discord_id:
            msg += f"Owner: `{player.discord_id}` <@{player.discord_id}>\n"
        else:
            msg += "Spieler hat keinen eingetragenen Owner\n"
        if len(player.authorized_discord_ids) > 0:
            msg += "Authorisierte Spieler (mit Owner-Berechtigungen):\n"
            for i in player.authorized_discord_ids:
                msg += f"`{i}`: <@{i}>\n"
        await ctx.response.send_message(msg, ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(name="listunregusers", description="Lists all unregistered users of the discord")
    @option("role", description="The role to check", required=True)
    @admin_only()
    @guild_only()
    async def find_unregistered_users(self, ctx: ApplicationContext, role: Role):
        await ctx.defer(ephemeral=True)
        users = await ctx.guild \
            .fetch_members() \
            .filter(lambda m: m.get_role(role.id) is not None) \
            .map(lambda m: (m.nick if m.nick is not None else m.name, m)) \
            .flatten()
        unreg_users = []
        for name, user in users:  # type: str, discord.Member
            if not self.plugin.get_user(discord_id=user.id):
                unreg_users.append(user)
        msg = f"Found {len(unreg_users)} unregistered users that have the specified role.\n"
        for user in unreg_users:
            msg += f"<@{user.id}> ({user.name})\n"
            if len(msg) > 1900:
                msg += "**Truncated**\n"
                break
        await ctx.followup.send(msg, ephemeral=True)
