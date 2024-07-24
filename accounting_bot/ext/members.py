# PluginConfig
# Name: MembersPlugin
# Author: Blaumeise03
# End
import asyncio
import datetime
import difflib
import logging
from typing import Optional, List, Dict, Callable, Union, Tuple, Set, TypeVar, Awaitable

import discord
from discord import option, AutocompleteContext, ApplicationContext, User, Role, CheckFailure, DMChannel, \
    InputTextStyle, Embed, Color
from discord.ext import commands

from accounting_bot import utils
from accounting_bot.exceptions import UsernameNotFoundException, NoPermissionException
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper, PluginState
from accounting_bot.utils import admin_only, guild_only, cmd_check, CmdAnnotation
from accounting_bot.utils.ui import ModalForm, AwaitConfirmView

logger = logging.getLogger("ext.members")
_T = TypeVar("_T")
CONFIG_TREE = {
    "user_role": (int, None),
    "main_guild": (int, None)
}


class UserDataProviderException(Exception):
    pass


class MemberVerificationException(Exception):
    pass


class Player:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rank = None  # type: str | None
        self.discord_id = None  # type: int | None
        self.alts = []  # type: List[str]
        self.authorized_discord_ids = []  # type: List[int]
        self.is_abstract = False

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
        self.config = bot.create_sub_config("members")
        self.config.load_tree(CONFIG_TREE)
        self.players = set()  # type: Set[Player]
        self.main_chars = set()  # type: Set[str]
        self._name_lookup_table = {}  # type: Dict[str, Player]
        self._data_provider = None  # type: DataChain | None
        self._save_data_provider = None  # type: DataChain | None
        self._is_member_func = None  # type: Callable[[Union[User, discord.Member]], Awaitable[bool]] | None

    async def _default_member_func(self, user: Union[User, discord.Member]) -> bool:
        if self.config["user_role"] is None:
            return False
        if isinstance(user, discord.Member):
            # user = user  # type: discord.Member
            if user.guild.id == self.config["main_guild"]:
                return user.get_role(self.config["user_role"]) is not None
            else:
                user = await self.bot.get_or_fetch_user(user.id)
        if isinstance(user, User):
            # user = user  # type: User
            if self.config["main_guild"] is None:
                logger.error("main_guild is not set inside the config members.main_guild")
                return False
            guild = self.bot.get_guild(self.config["main_guild"])
            if guild is None:
                guild = await self.bot.fetch_guild(self.config["main_guild"])
            if guild is None:
                logger.error("Guild with id %s not found", self.config["main_guild"])
                return False
            member = guild.get_member(user.id)
            if member is None:
                member = guild.fetch_member(user.id)
            if member is None:
                return False
            return member.get_role(self.config["user_role"]) is not None
        return False

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
        new = {}
        for player in players.values():
            for alt in player.alts:
                if alt not in self._name_lookup_table:
                    new[alt] = player
        for name, player in new.items():
            self._name_lookup_table[name] = player
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
        if self.config["user_role"] is not None:
            self._is_member_func = self._default_member_func
            logger.info("Using config with user_role for member verification, guild %s, role %s",
                        self.config["main_guild"], self.config["user_role"])

    async def on_enable(self):
        await self._execute_chain()

    async def on_disable(self):
        self.players.clear()
        self._name_lookup_table.clear()
        self.main_chars.clear()

    async def get_status(self, short=False) -> Dict[str, str]:
        return {
            "All": f"{len(self._name_lookup_table)} Chars",
            "Players": f"{len(self.players)} Players"
        }

    def set_data_source(self):
        self._data_provider = DataChain()
        return self._data_provider

    def set_save_data_chain(self):
        self._save_data_provider = DataChain()
        return self._save_data_provider

    def set_is_user_function(self, func: Callable[[Union[User, discord.Member]], bool]):
        self._is_member_func = func

    async def is_member(self, user: Union[User, discord.Member]):
        if self._is_member_func is None:
            raise MemberVerificationException("No _is_member_func defined")
        try:
            return await self._is_member_func(user)
        except Exception as e:
            raise MemberVerificationException("_is_member_func threw an error during execution") from e

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
        Tuple[Optional[int], Optional[str], bool], Optional[int]
    ]:
        """
        Returns the discord id, username and true if the name was matched perfectly (else false)

        :param only_id:
        :param player_name:
        :return: The discord id, username and a boolean
        """
        player_name, perfect = self.parse_player(player_name)
        player = self.get_user(player_name)
        if player is None:
            if only_id:
                return None
            return None, None, False
        if not only_id:
            return player.discord_id, player.name, perfect
        else:
            return player.discord_id

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


def member_only() -> Callable[[_T], _T]:
    def decorator(func):
        @cmd_check
        async def predicate(ctx: ApplicationContext) -> bool:
            # noinspection PyTypeChecker
            bot = ctx.bot  # type: AccountingBot
            member_p = bot.get_plugin("MembersPlugin", require_state=PluginState.ENABLED)  # type: MembersPlugin
            if not await member_p.is_member(ctx.user):
                if isinstance(ctx.channel, DMChannel):
                    location = "DMChannel"
                else:
                    c_name = ctx.channel.name if hasattr(ctx.channel, "name") else str(ctx.channel.type)
                    location = f"channel {ctx.channel.id}:'{c_name}' in guild "
                    if ctx.guild is None:
                        location += "N/A"
                    else:
                        location += f"{ctx.guild.name}:{ctx.guild.id}"
                logger.warning("Unauthorized access attempt for command %s by user %s:%s in %s",
                               utils.get_cmd_name(ctx.command), ctx.user.name, ctx.user.id, location)
                raise CheckFailure("Can't execute command") \
                    from NoPermissionException("Only members may execute this command")
            return True

        CmdAnnotation.annotate_cmd(func, CmdAnnotation.member)
        return commands.check(predicate)(func)

    return decorator


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

    # noinspection DuplicatedCode
    @commands.slash_command(name="grant_permissions", description="Grants owner permissions to a discord account")
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The discord user to grant permissions to", required=True)
    @member_only()
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
        if (
                player.discord_id != ctx.user.id and
                not self.plugin.bot.is_admin(ctx.user.id) and
                ctx.user.id not in player.authorized_discord_ids
        ):
            raise NoPermissionException(f"Discord user {ctx.user.name}:{ctx.user.id} is not owner of player {player}")
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
    @member_only()
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
        if (
                player.discord_id != ctx.user.id and
                not self.plugin.bot.is_admin(ctx.user.id) and
                ctx.user.id not in player.authorized_discord_ids
        ):
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
    @member_only()
    async def show_permissions(self, ctx: ApplicationContext, ingame_name: str):
        if ingame_name is None:
            await ctx.respond("Ingame name is required.", ephemeral=True)
            return
        matched_name, _, _ = self.plugin.find_main_name(name=ingame_name)
        if matched_name is None:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)
        player = self.plugin.get_user(name=matched_name)
        if player is None:
            raise UsernameNotFoundException(f"User {matched_name} not found")
        if not player.is_abstract:
            msg = f"Spielerinformationen für Spieler `{player.name}`\n"
        else:
            msg = f"Informationen zum abstrakten Konto `{player.name}`\n"
        if len(player.alts) > 0:
            msg += "Alts:\n```\n"
            for alt in player.alts:
                msg += alt + "\n"
            msg += "```\n"
        msg += f"Rank: `{player.rank}`\n"
        if player.discord_id:
            msg += f"Owner: `{player.discord_id}` <@{player.discord_id}>\n"
        else:
            msg += "Spieler hat keinen eingetragenen Owner\n"
        if len(player.authorized_discord_ids) > 0:
            msg += "Authorisierte Nutzer (mit Owner-Berechtigungen):\n"
            for i in player.authorized_discord_ids:
                msg += f"`{i}`: <@{i}>\n"
        await ctx.response.send_message(msg, ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(name="listunregusers", description="Lists all unregistered users of the discord")
    @option("role", description="The role to check", required=True)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True)
    @admin_only()
    @guild_only()
    async def find_unregistered_users(self, ctx: ApplicationContext, role: Role, silent: bool):
        await ctx.defer(ephemeral=silent)
        users = await ctx.guild \
            .fetch_members() \
            .filter(lambda m: m.get_role(role.id) is not None) \
            .map(lambda m: (m.nick if m.nick is not None else m.name, m)) \
            .flatten()
        unreg_users = []
        no_rank = []
        for name, user in users:  # type: str, discord.Member
            player = self.plugin.get_user(discord_id=user.id)
            if player is None:
                unreg_users.append(user)
            elif player.rank is None or len(player.rank) == 0:
                no_rank.append((user, player))
        msg = f"Found {len(unreg_users)} unregistered users that have the specified role.\n"
        for user in unreg_users:
            msg += f"<@{user.id}> ({user.name})\n"
            if len(msg) > 1850:
                msg += "**Truncated**\n"
                break
        if len(no_rank) > 0:
            msg += f"Found {len(no_rank)} users with no active rank.\n"
        for user, player in no_rank:
            msg += f"<@{user.id}> ({user.name}): {player.name} - {player.rank}\n"
            if len(msg) > 1900:
                msg += "**Truncated**\n"
                break
        await ctx.followup.send(msg)

    # noinspection SpellCheckingInspection
    @commands.slash_command(name="listmissingplayers", description="Lists all players that are not in the discord")
    @option("role", description="The required role every user should have", required=False, default=None)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True)
    @admin_only()
    @guild_only()
    async def find_missing_players(self, ctx: ApplicationContext, role: Role, silent: bool):
        await ctx.defer(ephemeral=silent)
        users = dict(await ctx.guild
                     .fetch_members()
                     .map(lambda m: (m.id, m))
                     .flatten())  # type: Dict[int, discord.Member]
        unreg_users = []
        missing_roles = []
        for player in self.plugin.players:
            if player.is_abstract:
                continue
            if player.rank is None:
                continue
            if player.discord_id is None:
                unreg_users.append(player)
                continue
            if player.discord_id not in users:
                unreg_users.append(player)
                continue
            if role is None:
                continue
            if users[player.discord_id].get_role(role.id) is None:
                missing_roles.append(player)
        msg = f"Found {len(unreg_users)} missing players.\n"
        for player in unreg_users:
            msg += f"`{player.name}`"
            if player.discord_id is not None:
                msg += f": `{player.discord_id}` <@{player.discord_id}>"
            if len(player.alts) > 0:
                msg += f", {len(player.alts)} alts"
            msg += "\n"
            if len(msg) > 1900:
                msg += "**Truncated**\n"
                break
        if len(missing_roles) > 0:
            msg += f"Found {len(missing_roles)} players without the specified role.\n"
        for player in missing_roles:
            if len(msg) > 1900:
                msg += "**Truncated**\n"
                break
            msg += f"`{player.name}`: `{player.discord_id}` <@{player.discord_id}>\n"

        await ctx.followup.send(msg)

    @commands.slash_command(name="assign_role", description="Assign roles to player by a list of ingame names")
    @option("role", description="The role to assign", required=True)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True)
    @admin_only()
    @guild_only()
    async def assign_role(self, ctx: ApplicationContext, role: Role, silent: bool):
        modal = (
            await
            ModalForm(title="Player list", ignore_timeout=True)
            .add_field(label="Players", placeholder="Enter the list of players here", style=InputTextStyle.paragraph)
            .open_form(ctx.response)
        )
        if modal.is_timeout():
            return
        res = map(lambda s: s.strip(), modal.retrieve_result().split("\n"))
        interaction = modal.get_interaction()
        await interaction.response.defer(ephemeral=True)
        players = set()  # type: Set[int]
        missing = []
        for r in res:
            discord_id = self.plugin.get_discord_id(player_name=r, only_id=True)
            if discord_id is None:
                missing.append(r)
                continue
            players.add(discord_id)
        members = await ctx.guild.fetch_members().filter(lambda m: m.id in players).flatten()
        member_ids = list(map(lambda m: m.id, members))
        for i in players:
            if i not in member_ids:
                missing.append(self.plugin.get_user(discord_id=i).name)
        msg = "\n".join(map(lambda m: f"<@{m.id}>: {m.name}", members))
        msg_missing = ""
        if len(missing) > 0:
            msg_missing += f"\n\n**{len(missing)} Users not found:**\n"
            msg_missing += "\n".join(missing)
        embed = Embed(title=f"Assign role {role.name}", description=msg + msg_missing,
                      timestamp=datetime.datetime.now(), color=Color.red())
        confirm = await AwaitConfirmView(defer_response=True).send_view(
            interaction.followup,
            f"Do you want to assign the role {role.mention} to {len(members)} members?",
            embed=embed
        )
        if not confirm.confirmed:
            return
        logger.warning("Assigning the role %s:%s to %s users in discord server %s:%s, executed by %s:%s",
                       role.name, role.id, len(members), ctx.guild.name, ctx.guild.name, ctx.user.name, ctx.user.id)
        coros = []
        for member in members:
            coros.append(member.add_roles(role, reason=f"Assigned by {ctx.user.name}:{ctx.user.id}"))
        await asyncio.gather(*coros)
        embed = Embed(title=f"Assigned role {role.name}", description=msg,
                      timestamp=datetime.datetime.now(), color=Color.green())
        embed.set_footer(text=f"Executed by {ctx.user.name}", icon_url=ctx.user.avatar.url)
        await interaction.followup.send(f"Assigned the role {role.mention} to {len(members)} members", embed=embed,
                                        ephemeral=silent)
