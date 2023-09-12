# PluginConfig
# Name: AccountingPlugin
# Author: Blaumeise03
# Depends-On: [accounting_bot.ext.embeds, accounting_bot.ext.members, accounting_bot.ext.sheet.sheet_main]
# Localization: accounting_lang.xml
# End
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from asyncio import Lock
from datetime import datetime
from typing import Optional, Callable, TypeVar, Tuple, Dict
from typing import Union, List

import discord
import discord.ext
import mariadb
import pytz
from discord import Embed, Interaction, Color, Message, ApplicationContext, option, User, RawReactionActionEvent
from discord.ext import commands
from discord.ext.commands import Cog, CheckFailure
from discord.ui import Modal, InputText
from gspread.utils import ValueInputOption, ValueRenderOption
from numpy import ndarray

from accounting_bot import utils
from accounting_bot.exceptions import BotOfflineException, AccountingException, NoPermissionException
from accounting_bot.ext.accounting_db import AccountingDB
from accounting_bot.ext.members import MembersPlugin, member_only
from accounting_bot.ext.sheet import sheet_main
from accounting_bot.ext.sheet.sheet_main import SheetPlugin
from accounting_bot.main_bot import BotPlugin, PluginWrapper, AccountingBot
from accounting_bot.utils import AutoDisableView, ErrorHandledModal, parse_number, admin_only, \
    guild_only, online_only, CmdAnnotation

INVESTMENT_RATIO = 0.3  # percentage of investment that may be used for transactions
_T = TypeVar("_T")

logger = logging.getLogger("ext.accounting")

NAME_SHIPYARD = "Buyback Program"

# Database lock
database_lock = Lock()

CONFIG_TREE = {
    "logChannel": (int, -1),
    "adminLogChannel": (int, -1),
    "admins": (list, []),
    "shipyard_admins": (list, []),
    "menuMessage": (int, -1),
    "menuChannel": (int, -1),
    "main_guild": (int, -1),
    "timezone": (str, "Europe/Berlin"),
    "db": {
        "user": (str, "N/A"),
        "password": (str, "N/A"),
        "host": (str, "127.0.0.1"),
        "port": (int, 3306),
        "name": (str, "accounting_bot"),
    }
}


class AccountingPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.db = None  # type: AccountingDB | None
        self.config = bot.create_sub_config("accounting")
        self.config.load_tree(CONFIG_TREE)
        self.accounting_log = None  # type: int | None
        self.admin_log = None  # type: int | None
        self.admins = []  # type: List[int]
        self.admins_shipyard = []  # type: List[int]
        self.guild = None  # type: int | None
        self.user_role = None  # type: int | None
        self.timezone = "Europe/Berlin"  # type: str
        self.embeds = []  # type: List[Embed]
        self.sheet = None  # type: SheetPlugin | None
        self.member_p = None  # type: MembersPlugin | None
        self.wallet_lock = asyncio.Lock()
        self.wallets = {}  # type: {str: int}
        self.investments = {}  # type: {str: int}
        self.wallets_last_reload = 0

    def on_load(self):
        self.accounting_log = self.config["logChannel"]
        admin_log = self.config["adminLogChannel"]
        if admin_log != -1:
            self.admin_log = admin_log
        self.admins_shipyard = self.config["shipyard_admins"]
        self.admins = self.config["admins"]
        self.db = AccountingDB(
            username=self.config["db.user"],
            password=self.config["db.password"],
            port=self.config["db.port"],
            host=self.config["db.host"],
            database=self.config["db.name"]
        )
        self.guild = self.config["main_guild"]
        self.timezone = self.config["timezone"]
        if self.guild == -1:
            self.guild = None
        self.register_cog(AccountingCommands(self))
        self.embeds = [self.bot.get_plugin("EmbedPlugin").embeds["MenuEmbedInternal"]]
        self.sheet = self.bot.get_plugin("SheetMain")
        self.member_p = self.bot.get_plugin("MembersPlugin")

    def on_unload(self):
        if self.db.con is None:
            return
        logger.warning("Closing SQL connection")
        self.db.con.close()

    async def get_status(self, short=False) -> Dict[str, str]:
        result = {}
        db_ping = self.db.ping()
        if db_ping is not None:
            result["DB Ping"] = f"{db_ping} ms"
        else:
            result["DB Ping"] = "Not connected"
        # noinspection PyBroadException
        try:
            result["Sheet"] = (await self.sheet.get_sheet()).title
        except Exception:
            result["Sheet"] = "Error"
        
        return result

    async def inform_player(self, transaction, discord_id, receive):
        time_formatted = transaction.timestamp.astimezone(pytz.timezone(self.timezone)).strftime("%d.%m.%Y %H:%M")
        if not discord_id:
            logger.warning("Didn't received an ID for %s (receive=%s)", str(transaction), str(receive))
            return
        user = await self.bot.get_or_fetch_user(discord_id)
        if user is not None:
            await user.send(
                (
                    "Du hast ISK auf Deinem Accounting erhalten." if receive else "Es wurde ISK von deinem Konto abgebucht.") +
                "\nDein Kontostand beträgt `{:,} ISK`".format(
                    await self.get_balance(transaction.name_to if receive else transaction.name_from,
                                           default=-1)),
                embed=transaction.create_embed())
        elif discord_id > 0:
            logger.warning("Can't inform user %s (%s) about about the transaction %s -> %s: %s (%s)",
                           transaction.name_to if receive else transaction.name_from,
                           discord_id,
                           transaction.name_from,
                           transaction.name_to,
                           f"{transaction.amount:,} ISK",
                           time_formatted)

    async def save_transaction(self, transaction: "Transaction", msg: Message, user_id: int):
        # Check if the transaction is valid
        if transaction.amount is None or (
                not transaction.name_from and not transaction.name_to) or not transaction.purpose:
            logger.error(f"Invalid embed in message {msg.id}! Can't parse transaction data: {transaction}")
            raise AccountingException("Transaction verification failed: Invalid embed")
        time_formatted = transaction.timestamp.astimezone(pytz.timezone(self.timezone)).strftime("%d.%m.%Y %H:%M")

        # Save transaction to sheet
        await self.add_transaction(transaction=transaction)
        user = await self.bot.get_or_fetch_user(user_id)
        logger.info(f"Verified transaction {msg.id} ({time_formatted}). Verified by {user.name} ({user.id}).")

        # Set message as verified
        self.db.set_verification(msg.id, verified=1)

    async def inform_players(self, transaction: "Transaction"):
        # Update wallets
        if transaction.name_from and transaction.name_from in self.wallets:
            self.wallets[transaction.name_from] = self.wallets[transaction.name_from] - transaction.amount
        if transaction.name_to and transaction.name_to in self.wallets:
            self.wallets[transaction.name_to] = self.wallets[transaction.name_to] + transaction.amount
        await self.load_wallets()

        # Find the discord account
        if transaction.name_from:
            id_from, _, perfect = self.member_p.get_discord_id(transaction.name_from)
            if not perfect:
                id_from = None
            await self.inform_player(transaction, id_from, receive=False)
        if transaction.name_to:
            id_to, _, perfect = self.member_p.get_discord_id(transaction.name_to)
            if not perfect:
                id_to = None
            await self.inform_player(transaction, id_to, receive=True)

    async def save_embeds(self, msg, user_id):
        """
        Saves the transaction of a message into the sheet

        :param msg:     The message with the transaction-embed
        :param user_id: The user ID that verified the transaction
        """
        if not self.bot.is_online():
            raise BotOfflineException("Can't verify transactions when the bot is not online")
        if len(msg.embeds) == 0:
            return
        elif len(msg.embeds) > 1:
            logger.warning(f"Message {msg.id} has more than one embed ({msg.embeds})!")
        # Getting embed of the message should contain only one
        embed = msg.embeds[0]
        # Convert embed to Transaction
        transaction = self.transaction_from_embed(embed)
        if isinstance(transaction, PackedTransaction):
            transactions = transaction.get_transactions()
        else:
            transactions = [transaction]
        async with database_lock:
            is_unverified = self.db.is_unverified_transaction(msg.id)
            if not is_unverified:
                time_formatted = transaction.timestamp.astimezone(pytz.timezone(self.timezone)).strftime(
                    "%d.%m.%Y %H:%M")
                logger.warning(
                    f"Attempted to verify an already verified transaction {msg.id} ({time_formatted}), user: {user_id}.")
                return
            for transaction in transactions:
                await self.save_transaction(transaction, msg, user_id)
        user = await self.bot.get_or_fetch_user(user_id)
        await asyncio.gather(
            msg.edit(content=f"Verifiziert von {user.name}", view=None),
            msg.remove_reaction("⚠️", self.bot.user),
            msg.remove_reaction("❌", self.bot.user),
            *[self.inform_players(transaction) for transaction in transactions]
        )

    async def verify_transaction(self, user_id: int, message: Message, interaction: Interaction = None):
        if not self.bot.is_online():
            raise BotOfflineException()

        if self.admin_log is not None:
            admin_log_channel = self.bot.get_channel(self.admin_log)
            if admin_log_channel is None:
                admin_log_channel = await self.bot.fetch_channel(self.admin_log)
        else:
            admin_log_channel = None
        is_unverified = self.db.is_unverified_transaction(message=message.id)
        if is_unverified is None:
            if interaction:
                await interaction.followup.send(content="Error: Transaction not found", ephemeral=True)
            return
        if len(message.embeds) == 0:
            if interaction:
                await interaction.followup.send(content="Error: Embeds not found", ephemeral=True)
            return
        transaction = self.transaction_from_embed(message.embeds[0])
        if not transaction:
            if interaction:
                await interaction.followup.send(content="Error: Couldn't parse embed", ephemeral=True)
            return
        has_permissions = user_id in self.admins
        user = await self.bot.get_or_fetch_user(user_id)
        if not has_permissions and isinstance(transaction,
                                              Transaction) and transaction.name_from and transaction.name_to:
            user_from = self.member_p.get_user(transaction.name_from)
            # user_to = self.member_p.get_user(transaction.name_to)
            # Only transactions between two players can be self-verified
            if user_from.has_permissions(user_id):
                # Check if the balance is sufficient
                await self.load_wallets()
                bal = await self.get_balance(transaction.name_from)
                inv = await self.get_investments(transaction.name_from, default=0)
                if not bal:
                    if interaction:
                        await interaction.followup.send(content="Dein Kontostand konnte nicht geprüft werden.",
                                                        ephemeral=True)
                    return
                effective_bal = bal + inv * INVESTMENT_RATIO

                if effective_bal < transaction.amount:
                    if not interaction:
                        return
                    if (bal + inv) > transaction.amount:
                        await interaction.followup.send(
                            content="Warnung: Dein Konto (`{:,} ISK`) reicht nicht aus, um diese "
                                    "Transaktion (`{:,} ISK`) zu decken, mit deinen Einlagen (`{:,} ISK`) reicht es aber. "
                                    "Diese Transaktion überschreitet jedoch die **{:.0%}** Grenze und kann deshalb nur von "
                                    "einem **Admin** verifiziert werden."
                            .format(bal, transaction.amount, inv, INVESTMENT_RATIO),
                            ephemeral=True)
                    else:
                        await interaction.followup.send(
                            content="**Fehler**: Dein Konto (`{:,} ISK`) und deine Einlagen (`{:,} ISK`) reichen nicht aus, "
                                    "um diese Transaktion (`{:,} ISK`) zu decken.\n"
                                    "Dein Konto muss zunächst ausgeglichen werden, oder (falls eine Ausnahmeregelung "
                                    "besteht) ein **Admin** muss diese Transaktion verifizieren."
                            .format(bal, inv, transaction.amount),
                            ephemeral=True)
                    return
                has_permissions = True
                logger.info("User " + str(
                    user_id) + " is owner of transaction " + transaction.__str__() + " and has sufficient balance")
        if isinstance(transaction, ShipyardTransaction) and not has_permissions:
            has_permissions = user_id in self.admins_shipyard
        if not has_permissions:
            if interaction:
                await interaction.followup.send(
                    content="Fehler: Du hast dazu keine Berechtigung. Nur der Kontoinhaber und "
                            "Admins dürfen Transaktionen verifizieren.", ephemeral=True)
            return

        if not is_unverified:
            # The Message is already verified
            msg = "Fehler: Diese Transaktion wurde bereits verifiziert, sie wurde nicht " \
                  "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls " \
                  "dies nötig ist."
            if interaction:
                await interaction.followup.send(content=msg, ephemeral=True)
            else:
                await user.send(content=msg)
            return

        if message.content.startswith("Verifiziert von"):
            # The Message was already verified, but due to an Error it got not updated in the SQL DB
            author = await self.bot.get_or_fetch_user(user_id)
            msg = "Fehler: Diese Transaktion wurde bereits verifiziert, sie wurde nicht " \
                  "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls " \
                  "dies nötig ist."
            if interaction:
                await interaction.followup.send(content=msg, ephemeral=True)
            else:
                await author.send(content=msg)
            logger.warning("Transaction %s was already verified (according to message content), but not marked as "
                           "verified in the database.", transaction.__str__())
            self.db.set_verification(message.id, True)
            await message.edit(view=None)
            return

        # Save transaction
        await self.save_embeds(message, user_id)
        if admin_log_channel and (user_id not in self.admins):
            msg = "Transaction `{}` was self-verified by `{}:{}`:\nhttps://discord.com/channels/{}/{}/{}\n" \
                .format(transaction, user.name, user_id, self.guild, self.accounting_log, message.id)
            await admin_log_channel.send(msg)
        if interaction:
            await message.add_reaction("✅")
            await interaction.followup.send("Transaktion verifiziert!", ephemeral=True)

    async def get_wallet_state(self, wallet, amount) -> int:
        bal = await self.get_balance(wallet, 0)
        inv = await self.get_investments(wallet, 0)
        if bal > amount:
            return 0
        elif (bal + inv * INVESTMENT_RATIO) > amount:
            return 1
        elif (bal + inv) >= amount:
            return 2
        else:
            return 3

    def transaction_from_embed(self, embed: Embed):
        if embed.title == "Shipyard Bestellung":
            return ShipyardTransaction.from_embed(embed, self.timezone)
        return Transaction.from_embed(embed, self.timezone)

    async def add_transaction(self, transaction: 'Transaction') -> None:
        """
        Saves a transaction into the Accounting sheet.
        The usernames will be replaced with their defined replacement (if any),
        see `sheet.check_name_overwrites`.

        :param transaction: The transaction to save
        """
        if transaction is None:
            return
        # Get data from transaction
        user_f = transaction.name_from if transaction.name_from is not None else ""
        user_t = transaction.name_to if transaction.name_to is not None else ""
        transaction_time = transaction.timestamp.astimezone(pytz.timezone(self.timezone)).strftime("%d.%m.%Y %H:%M")
        amount = transaction.amount
        purpose = transaction.purpose if transaction.purpose is not None else ""
        reference = transaction.reference if transaction.reference is not None else ""

        # Applying custom username overwrites
        user_f = self.sheet.check_name_overwrites(user_f)
        user_t = self.sheet.check_name_overwrites(user_t)

        # Saving the data
        logger.info(f"Saving row [{transaction_time}; {user_f}; {user_t}; {amount}; {purpose}; {reference}]")
        sheet = await self.sheet.get_sheet()
        wk_log = await sheet.worksheet("Accounting Log")
        await wk_log.append_row([transaction_time, user_f, user_t, amount, purpose, reference],
                                value_input_option=ValueInputOption.user_entered)
        logger.debug("Saved row")

    async def load_wallets(self, force=False, validate=False):
        t = time.time()
        if (t - self.wallets_last_reload) < 60 * 60 and not force:
            return
        async with self.wallet_lock:
            self.wallets_last_reload = t
            self.wallets.clear()
            sheet = await self.sheet.get_sheet()
            wk_accounting = await sheet.worksheet("Accounting")
            user_raw = await wk_accounting.get_values(sheet_main.MEMBERS_AREA_LITE,
                                                      value_render_option=ValueRenderOption.unformatted)
            for u in user_raw:
                if len(u) >= 3:
                    bal = u[sheet_main.MEMBERS_WALLET_INDEX]
                    if type(bal) == int or type(bal) == float:
                        if validate and type(bal) == float:
                            logger.warning("Balance for %s is a float: %s", u[0], bal)
                        self.wallets[u[0]] = int(bal)
                if len(u) >= 4:
                    inv = u[sheet_main.MEMBERS_INVESTMENTS_INDEX]
                    if type(inv) == int or type(inv) == float:
                        if validate and type(inv) == float:
                            # logger.warning("Investment sum for %s is a float: %s", u[MEMBERS_NAME_INDEX], inv)
                            pass
                        self.investments[u[0]] = int(inv)

    async def get_balance(self, name: str, default: Optional[int] = None) -> int:
        async with self.wallet_lock:
            name = self.member_p.get_main_name(name)
            name = self.sheet.check_name_overwrites(name)
            if name in self.wallets:
                return self.wallets[name]
            return default

    async def get_investments(self, name: str, default: Optional[int] = None) -> int:
        async with self.wallet_lock:
            name = self.member_p.get_main_name(name)
            name = self.sheet.check_name_overwrites(name)
            if name in self.investments:
                return self.investments[name]
            return default

    def parse_player(self, string: str) -> Tuple[Optional[str], bool]:
        """
        Finds the closest playername match for a given string.
        It returns the name or None if not found, as well as a
        boolean indicating whether it was a perfect match.

        :param string: The string which should be looked up
        :return: (Playername: str or None, Perfect match: bool)
        """
        p = self.bot.get_plugin("MembersPlugin")
        return p.parse_player(string)

    async def on_enable(self):
        # Refreshing main menu
        channel = await self.bot.fetch_channel(self.config["menuChannel"])
        msg = await channel.fetch_message(self.config["menuMessage"])
        await msg.edit(view=AccountingView(self),
                       embeds=self.embeds, content="")

        # Updating shortcut menus
        shortcuts = self.db.get_shortcuts()
        logger.info(f"Found {len(shortcuts)} shortcut menus")
        for (m, c) in shortcuts:
            chan = self.bot.get_channel(c)
            if chan is None:
                chan = self.bot.fetch_channel(c)
            try:
                msg = await chan.fetch_message(m)
                await msg.edit(view=AccountingView(self),
                               embed=self.bot.embeds["MenuShortcut"], content="")
            except discord.errors.NotFound:
                logger.warning(f"Message {m} in channel {c} not found, deleting it from DB")
                self.db.delete_shortcut(m)

        await self.load_wallets(force=True, validate=True)

        # Updating unverified Accounting-log entries
        logger.info("Refreshing unverified accounting log entries")
        accounting_log = await self.bot.fetch_channel(self.config["logChannel"])
        unverified = self.db.get_unverified()
        logger.info(f"Found {len(unverified)} unverified message(s)")
        for m in unverified:
            try:
                msg = await accounting_log.fetch_message(m)
            except discord.errors.NotFound:
                self.db.delete(m)
                continue
            if msg.content.startswith("Verifiziert von"):
                logger.warning("Transaction already verified but not inside database: %s: %s", msg.id, msg.content)
                self.db.set_verification(m, 1)
                continue
            v = False  # Was the transaction verified while the bot was offline?
            user = None  # User ID who verified the message
            # Checking all the reactions below the message
            for r in msg.reactions:
                emoji = r.emoji
                if isinstance(emoji, str):
                    name = emoji
                else:
                    name = emoji.name
                if name != "✅":
                    continue
                users = await r.users().flatten()
                for u in users:
                    if u.id in self.config["admins"]:
                        # User is admin, the transaction is therefore verified
                        v = True
                        user = u.id
                        break
                break
            if v:
                # The Message was verified
                try:
                    # Saving transaction to the Google sheet
                    await self.save_embeds(msg, user)
                except mariadb.Error:
                    pass
                # Removing the View
                await msg.edit(view=None)
            else:
                # Updating the message View, so it can be used by the users
                await msg.edit(view=TransactionView(self))
                if len(msg.embeds) > 0:
                    transaction = self.transaction_from_embed(msg.embeds[0])
                    state = await transaction.get_state(self)
                    if state == 2:
                        await msg.add_reaction("⚠️")
                    elif state == 3:
                        await msg.add_reaction("❌")
                else:
                    logger.warning("Message %s is listed as transaction but does not have an embed", msg.id)
        logger.info("AccountingPlugin ready")


def main_guild_only() -> Callable[[_T], _T]:
    def decorator(func):
        @utils.cmd_check
        async def predicate(ctx: ApplicationContext) -> bool:
            # noinspection PyTypeChecker
            bot = ctx.bot  # type: AccountingBot
            plugin = bot.get_plugin("AccountingPlugin")
            if (ctx.guild is None or ctx.guild.id != plugin.guild) and not await bot.is_owner(ctx.user):
                raise CheckFailure() from NoPermissionException("This command may only be executed in the main guild")
            return True

        CmdAnnotation.annotate_cmd(func, CmdAnnotation.main_guild)
        return commands.check(predicate)(func)

    return decorator


def get_current_time() -> str:
    """
    Returns the current time as a string with the format dd.mm.YYYY HH:MM

    :return: the formatted time
    """
    now = datetime.now()
    return now.strftime("%d.%m.%Y %H:%M")


class AccountingCommands(Cog):
    def __init__(self, plugin: AccountingPlugin):
        self.bot = plugin.bot
        self.db = plugin.db
        self.config = plugin.config
        self.plugin = plugin

    @Cog.listener()
    async def on_raw_reaction_add(self, reaction: RawReactionActionEvent):
        if reaction.emoji.name == "✅" and reaction.channel_id == self.config["logChannel"]:
            # The Message is not verified
            channel = self.bot.get_channel(self.config["logChannel"])
            msg = await channel.fetch_message(reaction.message_id)
            await self.plugin.verify_transaction(reaction.user_id, msg)

    @Cog.listener()
    async def on_raw_reaction_remove(self, reaction: RawReactionActionEvent):
        if (
                reaction.emoji.name == "✅" and
                reaction.channel_id == self.config["logChannel"] and
                reaction.user_id in self.config["admins"]
        ):
            logger.info(f"{reaction.user_id} removed checkmark from {reaction.message_id}!")

    @commands.slash_command(description="Creates the main menu for the bot and sets all required settings")
    @admin_only()
    @main_guild_only()
    @guild_only()
    async def setup(self, ctx: ApplicationContext):
        logger.info("User verified for setup-command, starting setup...")
        view = AccountingView(self.plugin)
        msg = await ctx.send(view=view, embeds=self.plugin.embeds)
        logger.info("Send menu message with id " + str(msg.id))
        self.config["menuMessage"] = msg.id
        self.config["menuChannel"] = ctx.channel.id
        self.config["main_guild"] = ctx.guild.id
        self.bot.save_config()
        logger.info("Setup completed.")
        await ctx.response.send_message("Saved config", ephemeral=True)

    @commands.slash_command(
        name="setlogchannel",
        description="Sets the current channel as the accounting log channel")
    @admin_only()
    @main_guild_only()
    @guild_only()
    async def set_log_channel(self, ctx):
        logger.info("User Verified. Setting up channel...")
        self.config["logChannel"] = ctx.channel.id
        self.bot.save_config()
        logger.info("Channel changed!")
        await ctx.respond(f"Log channel set to this channel (`{self.config['logChannel']}`)")

    # noinspection SpellCheckingInspection
    @commands.slash_command(description="Creates a new shortcut menu containing all buttons")
    @main_guild_only()
    @guild_only()
    async def createshortcut(self, ctx):
        if ctx.author.guild_permissions.administrator or ctx.author.id in self.plugin.admins or ctx.author.id == self.owner:
            view = AccountingView(self.plugin)
            msg = await ctx.send(view=view, embed=self.bot.embeds["MenuShortcut"])
            self.connector.add_shortcut(msg.id, ctx.channel.id)
            await ctx.respond("Shortcut menu posted", ephemeral=True)
        else:
            logger.info(f"User {ctx.author.id} is missing permissions to run the createshortcut command")
            await ctx.respond("Missing permissions", ephemeral=True)

    @commands.slash_command(name="balance", description="Get the balance of a user")
    @option("force", description="Enforce data reload from sheet", required=False, default=False)
    @option("user", description="The user to lookup", required=False, default=None)
    @member_only()
    @online_only()
    async def get_balance(self, ctx: ApplicationContext, force: bool = False, user: User = None):
        await ctx.defer(ephemeral=True)
        await self.plugin.load_wallets(force)
        if not user:
            user_id = ctx.user.id
        else:
            user_id = user.id

        name, _, _ = self.plugin.member_p.find_main_name(discord_id=user_id)
        if name is None:
            await ctx.followup.send("This discord account is not connected to any ingame account!", ephemeral=True)
            return
        name = self.plugin.sheet.check_name_overwrites(name)
        balance = await self.plugin.get_balance(name)
        invest = await self.plugin.get_investments(name, default=0)
        if balance is None:
            await ctx.followup.send("Konto nicht gefunden!", ephemeral=True)
            return
        await ctx.followup.send("Der Kontostand von {} beträgt `{:,} ISK`.\nDie Projekteinlagen betragen `{:,} ISK`"
                                .format(name, balance, invest), ephemeral=True)


class TransactionBase(ABC):
    @abstractmethod
    def has_permissions(self, user_name: str, plugin: AccountingPlugin, operation="") -> bool:
        pass


# noinspection PyMethodMayBeStatic
class TransactionLike(TransactionBase, ABC):
    def get_from(self) -> Optional[str]:
        return None

    def get_to(self) -> Optional[str]:
        return None

    @abstractmethod
    def get_amount(self) -> int:
        pass

    def get_time(self) -> Optional[datetime]:
        return None

    @abstractmethod
    def get_purpose(self) -> str:
        pass

    def get_reference(self) -> Optional[str]:
        return None


class Transaction(TransactionLike):
    """
    Represents a transaction

    Attributes
    ----------
    name_from: Union[str, None]
        the sender of the transaction or None
    name_to: Union[str, None]
        the receiver of the transaction or None
    amount: Union[int, None]
        the amount of the transaction

    """

    def has_permissions(self, user, plugin: AccountingPlugin, operation="") -> bool:
        match operation:
            case "delete":
                return (
                        self.name_from and plugin.member_p.get_main_name(self.name_from) == user or
                        self.name_to and plugin.member_p.get_main_name(self.name_to) == user
                )
            case "verify":
                return user in plugin.admins
        return False

    # Transaction types
    NAMES = {
        0: "Transfer",
        1: "Einzahlen",
        2: "Auszahlen"
    }
    # Embed colors
    COLORS = {
        0: Color.blue(),
        1: Color.green(),
        2: Color.red()
    }

    def __init__(self,
                 author: Union[str, None] = None,
                 name_from: Union[str, None] = None,
                 name_to: Union[str, None] = None,
                 amount: Union[int, None] = None,
                 purpose: Union[str, None] = None,
                 reference: Union[str, None] = None,
                 timestamp: Union[datetime, None] = None
                 ):
        self.author = author
        if timestamp is None:
            self.timestamp = datetime.now()
        else:
            self.timestamp = timestamp
        self.reference = reference
        self.purpose = purpose
        self.amount = amount
        self.name_to = name_to
        self.name_from = name_from
        self.allow_self_verification = False
        self.img = None  # type: ndarray | None

    def __str__(self):
        return f"<Transaction: time {self.timestamp}; from {self.name_from}; to {self.name_to}; amount {self.amount}; " \
               f"purpose \"{self.purpose}\"; reference \"{self.reference}\">"

    def get_from(self) -> Optional[str]:
        return self.name_from

    def get_to(self) -> Optional[str]:
        return self.name_to

    def get_amount(self) -> int:
        return self.amount

    def get_time(self) -> Optional[datetime]:
        return self.timestamp

    def get_purpose(self) -> str:
        return self.purpose

    def get_reference(self) -> Optional[str]:
        return self.reference

    def self_verification(self) -> bool:
        return self.allow_self_verification

    def detect_type(self) -> int:
        """
        Detects the type of this transaction. 0 = Transfer, 1 = Deposit, 2 = Withdraw.

        :return: 0, 1 or 3 for transfer, deposit and withdraw or -1 if unknown type
        """
        if self.name_to is not None and self.name_from is not None:
            return 0
        if self.name_to is not None and self.name_from is None:
            return 1
        if self.name_to is None and self.name_from is not None:
            return 2
        return -1

    def is_valid(self):
        if self.detect_type() == -1:
            return False
        if self.name_from == self.name_to:
            return False
        if self.amount <= 0:
            return False
        if self.purpose is None or len(self.purpose.strip()) == 0:
            return False
        return True

    def create_embed(self) -> Embed:
        """
        Creates an :class:`Embed` representing this transaction.

        :rtype: Embed
        :return: the created embed
        """
        transaction_type = self.detect_type()
        if transaction_type < 0:
            logger.error(f"Unexpected transaction type: {transaction_type}")

        embed = Embed(title=Transaction.NAMES[transaction_type],
                      color=Transaction.COLORS[transaction_type],
                      timestamp=datetime.now())
        if self.name_from is not None:
            embed.add_field(name="Von", value=self.name_from, inline=True)
        if self.name_to is not None:
            embed.add_field(name="Zu", value=self.name_to, inline=True)
        embed.add_field(name="Menge", value=f"{self.amount:,} ISK", inline=True)
        embed.add_field(name="Verwendungszweck", value=self.purpose, inline=True)
        if self.reference is not None and len(self.reference) > 0:
            embed.add_field(name="Referenz", value=self.reference, inline=True)
        embed.timestamp = self.timestamp
        if self.author is not None and len(self.author) > 0:
            embed.set_footer(text=self.author)
        return embed

    async def get_state(self, plugin: AccountingPlugin):
        if self.name_from is None:
            return None
        return await plugin.get_wallet_state(self.name_from, self.amount)

    @staticmethod
    async def from_modal(plugin: AccountingPlugin, modal: Modal, author: str, user: int = None) -> ('Transaction', str):
        """
        Creates a Transaction out of a :class:`Modal`, the Modal has to be filled out.

        All warnings that occurred during parsing the values will be returned as well.

        :param plugin: The :class:`AccountingPlugin`
        :param modal: The modal with the values for the transaction
        :param author: The author of this transaction
        :param user: The discord id of the author of the transaction
        :return: A Tuple containing the transaction (or None if the data was incorrect), as well as a string with all
        warnings.
        """
        transaction = Transaction(author=author)
        warnings = ""
        for field in modal.children:
            # Processing all fields of the modal
            name_type = -1
            if field.label.casefold() == "Von".casefold():
                name_type = 0
            elif field.label.casefold() == "Zu".casefold():
                name_type = 1
            elif field.label.casefold() == "Spieler(konto)name".casefold():
                if modal.title.casefold() == "Einzahlen".casefold():
                    name_type = 1
                if modal.title.casefold() == "Auszahlen".casefold():
                    name_type = 0
            if name_type != -1:
                name, match = plugin.parse_player(field.value.strip())
                if name is None:
                    warnings += f"Hinweis: Name \"{field.value}\" konnte nicht gefunden werden!\n"
                    return None, warnings
                name = plugin.member_p.get_main_name(name)
                if not match:
                    warnings += f"Hinweis: Name \"{field.value}\" wurde zu \"**{name}**\" geändert!\n"
                if name_type == 0:
                    transaction.name_from = name
                if name_type == 1:
                    transaction.name_to = name
                continue
            if field.label.casefold() == "Menge".casefold():
                raw = field.value

                amount, warn = parse_number(raw)
                warnings += warn
                if amount is None or amount < 1:
                    warnings += "**Fehler**: Die eingegebene Menge ist keine Zahl > 0!\n"
                    return None, warnings
                transaction.amount = amount
                continue
            if field.label.casefold() == "Verwendungszweck".casefold():
                transaction.purpose = field.value.strip()
                continue
            if field.label.casefold() == "Referenz".casefold():
                transaction.reference = field.value.strip()

        # Check wallet ownership and balance
        if transaction.name_from:
            user_id, _, _ = plugin.member_p.get_discord_id(player_name=transaction.name_from)
            await plugin.load_wallets()
            if (user_id is None or user != user_id) and user not in plugin.admins:
                warnings += "**Fehler**: Dieses Konto gehört dir nicht bzw. dein Discordaccount ist nicht " \
                            "**verifiziert** (kontaktiere in diesem Fall einen Admin). Nur der Kontobesitzer darf " \
                            "ISK von seinem Konto an andere senden.\n"
            bal = await plugin.get_balance(transaction.name_from)
            inv = await plugin.get_investments(transaction.name_from, default=0)
            if not bal:
                warnings += "Warnung: Dein Kontostand konnte nicht geprüft werden.\n"
            elif (bal + inv) < transaction.amount:
                warnings += "**Fehler**: Dein Kontostand (`{:,} ISK`) und Projekteinlagen (`{:,} ISK`) reichen nicht aus, " \
                            "um diese Transaktion zu decken. Wenn Dein Accounting nicht zuvor ausgeglichen ist (oder " \
                            "es für Dich eine Ausnahmeregelung gibt), wird die Transaktion abgelehnt.\n" \
                    .format(bal, inv)
            elif (bal + inv * INVESTMENT_RATIO) < transaction.amount:
                warnings += "Warnung: Dein Kontostand (`{:,} ISK`) reicht nicht aus, " \
                            "um diese Transaktion zu decken. Mit deinen Projekteinlagen (`{:,} ISK`) ist die " \
                            "Transaktion gedeckt, aber überschreitet die **{:.0%}** Grenze und kann deshalb nur " \
                            "von einem Admin verifiziert werden.\n" \
                    .format(bal, inv, INVESTMENT_RATIO)
        return transaction, warnings

    @staticmethod
    def from_embed(embed: Embed, timezone: str) -> Optional['Transaction']:
        """
        Creates a Transaction from an existing :class:`Embed`.

        :param embed: The embed containing the transaction data
        :param timezone: The timezone used for formatting
        :return: The transaction
        """
        transaction = Transaction()
        for field in embed.fields:
            name = field.name.casefold()
            if name == "Von".casefold():
                transaction.name_from = field.value
            if name == "Zu".casefold():
                transaction.name_to = field.value
            if name == "Menge".casefold():
                transaction.amount, _ = parse_number(field.value)
            if name == "Verwendungszweck".casefold():
                transaction.purpose = field.value
            if name == "Referenz".casefold():
                transaction.reference = field.value
        transaction.timestamp = embed.timestamp.astimezone(pytz.timezone(timezone))
        if embed.footer is not None:
            transaction.author = embed.footer.text
        if not transaction.is_valid():
            return None
        return transaction


class PackedTransaction(ABC):
    @abstractmethod
    def get_transactions(self) -> List[Transaction]:
        pass

    @abstractmethod
    def self_verification(self) -> bool:
        pass

    @abstractmethod
    def to_embed(self) -> Embed:
        pass

    @abstractmethod
    async def get_state(self, plugin: AccountingPlugin) -> int:
        pass

    @staticmethod
    @abstractmethod
    def from_embed(embed: Embed, timezone: str) -> "PackedTransaction":
        pass


class ShipyardTransaction(PackedTransaction):
    def __init__(self,
                 author: Union[str, None] = None,
                 buyer: Union[str, None] = None,
                 price: Union[int, None] = None,
                 ship: Union[str, None] = None,
                 station_fees: Union[int, None] = None,
                 builder: Union[str, None] = None,
                 timestamp: Union[datetime, None] = None
                 ):
        self.author = author
        if timestamp is None:
            self.timestamp = datetime.now()
        else:
            self.timestamp = timestamp
        self.ship = ship
        self.price = price
        self.station_fees = station_fees
        self.buyer = buyer
        self.builder = builder
        self.authorized = False

    def __str__(self):
        return f"ShipyardTransaction(from={self.buyer}, price={self.price}, station={self.station_fees}" \
               f"ship={self.ship}; builder=\"{self.builder}\"time={self.timestamp})"

    def get_transactions(self) -> List[Transaction]:
        transactions = [
            Transaction(
                name_from=self.buyer,
                name_to=NAME_SHIPYARD,
                amount=self.price,
                purpose=f"Kauf {self.ship}",
                timestamp=self.timestamp,
                author=self.author
            ),
            Transaction(
                name_from=NAME_SHIPYARD,
                amount=self.station_fees,
                purpose=f"Stationsgebühren {self.ship}",
                timestamp=self.timestamp,
                author=self.author
            )]
        slot_price = min(int(self.price * 0.02 / 100000) * 100000, 50000000)
        if self.builder is not None and slot_price >= 1000000:
            transactions.append(
                Transaction(
                    name_from=NAME_SHIPYARD,
                    name_to=self.builder,
                    amount=slot_price,
                    purpose=f"Slotgebühr {self.ship}",
                    timestamp=self.timestamp,
                    author=self.author
                )
            )
        return transactions

    async def get_state(self, plugin: AccountingPlugin) -> Optional[int]:
        if self.buyer is None:
            return None
        return await plugin.get_wallet_state(self.buyer, self.price)

    def self_verification(self):
        return self.authorized

    def to_embed(self):
        embed = Embed(
            title="Shipyard Bestellung",
            color=Color.orange()
        )
        embed.add_field(name="Käufer", value=self.buyer, inline=True)
        embed.add_field(name="Produkt", value=self.ship, inline=True)
        embed.add_field(name="Preis", value="{:,} ISK".format(self.price), inline=True)
        embed.add_field(name="Stationsgebühr", value="{:,} ISK".format(self.station_fees), inline=True)
        if self.builder is not None:
            embed.add_field(name="Bauer", value=self.builder, inline=True)
        embed.timestamp = self.timestamp
        if self.author is not None and len(self.author) > 0:
            embed.set_footer(text=self.author)
        return embed

    @staticmethod
    def from_embed(embed: Embed, timezone: str) -> "ShipyardTransaction":
        transaction = ShipyardTransaction()
        for field in embed.fields:
            name = field.name.casefold()
            if name == "Käufer".casefold():
                transaction.buyer = field.value
            if name == "Produkt".casefold():
                transaction.ship = field.value
            if name == "Preis".casefold():
                transaction.price = parse_number(field.value)[0]
            if name == "Stationsgebühr".casefold():
                transaction.station_fees = parse_number(field.value)[0]
            if name == "Bauer".casefold():
                transaction.builder = field.value
        transaction.timestamp = embed.timestamp.astimezone(pytz.timezone(timezone))
        if embed.footer is not None:
            transaction.author = embed.footer.text
        return transaction


async def send_transaction(plugin: AccountingPlugin,
                           embeds: List[Union[Embed, Transaction, PackedTransaction]],
                           interaction: Interaction,
                           note="") -> Optional[Message]:
    """
    Sends the embeds into the accounting log channel. Will send a response to the :class:`Interaction` containing the
    note and an error message in case any :class:`mariadb.Error` occurred.

    :param plugin: The :class:`AccountingBot` plugin
    :param embeds: the embeds to send
    :param interaction: discord interaction for the response
    :param note: the note which should be sent
    """
    msg = None
    for embed in embeds:
        if embed is None:
            continue
        transaction = None
        if isinstance(embed, Transaction):
            transaction = embed
            embed = embed.create_embed()
        if isinstance(embed, PackedTransaction):
            transaction = embed
            embed = transaction.to_embed()
        msg = await plugin.bot.get_channel(plugin.accounting_log).send(embeds=[embed], view=TransactionView(plugin))
        try:
            plugin.db.add_transaction(msg.id, interaction.user.id)
            if transaction is None:
                transaction = plugin.transaction_from_embed(embed)
                if isinstance(transaction, ShipyardTransaction):
                    transaction = ShipyardTransaction.from_embed(embed, plugin.timezone)
                    if interaction.user.id in plugin.admins_shipyard:
                        transaction.authorized = True
                        note += "\nDu kannst diese Transaktion selbst verifizieren"
            if not transaction:
                logger.warning("Embed in message %s is not a transaction", msg.id)
                continue

            state = await transaction.get_state(plugin)
            if state == 2:
                await msg.add_reaction("⚠️")
            elif state == 3:
                await msg.add_reaction("❌")
            if state is not None:
                plugin.db.set_state(msg.id, state)
            if transaction.self_verification():
                plugin.db.set_ocr_verification(msg.id, True)
        except mariadb.Error as e:
            note += "\nFehler beim Eintragen in die Datenbank, die Transaktion wurde jedoch trotzdem im " \
                    f"Accountinglog gepostet. Informiere bitte einen Admin, danke.\n{e}"
    await interaction.followup.send("Transaktion gesendet!" + note, ephemeral=True)
    return msg


# noinspection PyUnusedLocal
class AccountingView(AutoDisableView):
    """
    A :class:`discord.ui.View` with four buttons: 'Transfer', 'Deposit', 'Withdraw', 'Shipyard' and a printer button.
    The first four buttons will open the corresponding modal (see :class:`TransferModal` and :class:`ShipyardModal`),
    the printer button responds with a list of all links to all unverified transactions.
    """

    def __init__(self, plugin: AccountingPlugin):
        super().__init__(timeout=None)
        self.plugin = plugin

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.blurple)
    async def btn_transfer_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        modal = TransferModal(title="Transfer", color=Color.blue(), plugin=self.plugin)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Einzahlen", style=discord.ButtonStyle.green)
    async def btn_deposit_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        modal = TransferModal(title="Einzahlen", color=Color.green(), plugin=self.plugin,
                              special=True, purpose="Einzahlung Accounting")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Auszahlen", style=discord.ButtonStyle.red)
    async def btn_withdraw_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        modal = TransferModal(plugin=self.plugin, title="Auszahlen", color=Color.red(),
                              special=True, purpose="Auszahlung Accounting")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Shipyard", style=discord.ButtonStyle.grey)
    async def btn_shipyard_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        modal = ShipyardModal(title="Schiffskauf", color=Color.red(), plugin=self.plugin)
        await interaction.response.send_modal(modal)

    @discord.ui.button(emoji="🖨️", style=discord.ButtonStyle.grey)
    async def btn_list_transactions_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        unverified = self.plugin.db.get_unverified(include_user=True)
        msg = "Unverifizierte Transaktionen:"
        if len(unverified) == 0:
            msg += "\nKeine"
        i = 0
        for (msg_id, user_id) in unverified:
            if len(msg) < 1900:
                msg += f"\nhttps://discord.com/channels/{self.plugin.guild}/{self.plugin.accounting_log}/{msg_id} von <@{user_id}>"
                i += 1
            else:
                msg += f"\nUnd {len(unverified) - i} weitere..."
        await interaction.response.send_message(msg, ephemeral=True)


# noinspection PyUnusedLocal
class TransactionView(AutoDisableView):
    """
    A :class:`discord.ui.View` for transaction messages with two buttons: 'Delete' and 'Edit'. The 'Delete' button will
    delete the message if the user is the author of the transaction, or an administrator. The 'Edit' button allows the
    author and administrators to edit the transaction, see :class:`EditModal`.
    """

    def __init__(self, plugin: AccountingPlugin):
        super().__init__(timeout=None)
        self.plugin = plugin

    @discord.ui.button(label="Verifizieren", style=discord.ButtonStyle.green)
    async def btn_verify_callback(self, button: discord.Button, interaction: Interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        await interaction.response.defer(ephemeral=True, invisible=False)
        await self.plugin.verify_transaction(interaction.user.id, interaction.message, interaction)

    @discord.ui.button(label="Löschen", style=discord.ButtonStyle.red)
    async def btn_delete_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        (owner, verified) = self.plugin.db.get_owner(interaction.message.id)
        transaction = self.plugin.transaction_from_embed(interaction.message.embeds[0])
        user_name = self.plugin.member_p.find_main_name(discord_id=interaction.user.id)[0]
        has_perm = owner == interaction.user.id or interaction.user.id in self.plugin.admins
        if not has_perm:
            has_perm = transaction.has_permissions(user_name, self.plugin, "delete")
        if not verified and has_perm:
            await interaction.message.delete()
            self.plugin.db.delete(interaction.message.id)
            await interaction.response.send_message("Transaktion Gelöscht!", ephemeral=True)
            logger.info("User %s deleted message %s", interaction.user.id, interaction.message.id)
        elif owner != interaction.user.id:
            await interaction.response.send_message("Dies ist nicht deine Transaktion, wenn du ein Admin bist, lösche "
                                                    "die Nachricht bitte eigenständig.", ephemeral=True)
        else:
            await interaction.response.send_message("Bereits verifiziert!", ephemeral=True)

    @discord.ui.button(label="Bearbeiten", style=discord.ButtonStyle.blurple)
    async def btn_edit_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        (owner, verified) = self.plugin.db.get_owner(interaction.message.id)
        if not verified and (owner == interaction.user.id or interaction.user.id in self.plugin.admins):
            embed = interaction.message.embeds[0]
            await interaction.response.send_modal(EditModal(plugin=self.plugin, message=interaction.message, title=embed.title))
        elif owner != interaction.user.id:
            await interaction.response.send_message("Dies ist nicht deine Transaktion, wenn du ein Admin bist, lösche "
                                                    "die Nachricht bitte eigenständig.", ephemeral=True)
        else:
            await interaction.response.send_message("Bereits verifiziert!", ephemeral=True)


# noinspection PyUnusedLocal
class ConfirmView(AutoDisableView):
    """
    A :class:`discord.ui.View` for confirming new transactions. It adds one button 'Send', which will send all embeds of
    the message into the accounting log channel.
    """

    def __init__(self, plugin: AccountingPlugin):
        super().__init__(timeout=300)
        self.plugin = plugin

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction: Interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        await interaction.response.defer(ephemeral=True, invisible=False)
        await send_transaction(self.plugin, interaction.message.embeds, interaction)
        await self.message.delete()


# noinspection PyUnusedLocal
class ConfirmEditView(AutoDisableView):
    """
    A :class:`discord.ui.View` for confirming edited transactions. It adds one button 'Save', which will update the
    embeds of the original message according to the edited embeds.

    Attributes
    ----------
    message: discord.Message
        the original message which should be edited.
    """

    def __init__(self, plugin: AccountingPlugin, message: Message, original: Transaction):
        """
        Creates a new ConfirmEditView.

        :param message: The original message which should be edited.
        """
        super().__init__()
        self.message = message
        self.original = original
        self.plugin = plugin

    @discord.ui.button(label="Speichern", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        transaction = self.plugin.transaction_from_embed(interaction.message.embeds[0])
        if not isinstance(transaction, Transaction):
            raise TypeError(f"Expected Transaction, but got {type(transaction)}")
        warnings = ""
        if self.original.name_to != transaction.name_to or self.original.name_from != transaction.name_from or \
                self.original.amount != transaction.amount or self.original.purpose != transaction.purpose:
            ocr_verified = self.plugin.db.get_ocr_verification(interaction.message.id)
            if ocr_verified:
                self.plugin.db.set_ocr_verification(interaction.message.id, False)
                warnings += "Warnung: Du kannst diese Transaktion nicht mehr selbst verifizieren.\n"
        await self.message.edit(embeds=interaction.message.embeds)
        await interaction.response.send_message(f"Transaktion bearbeitet!\n{warnings}", ephemeral=True)


class TransferModal(ErrorHandledModal):
    def __init__(self, color: Color,
                 plugin: AccountingPlugin,
                 special: bool = False,
                 name_from: str = None,
                 name_to: str = None,
                 amount: str = None,
                 purpose: str = None,
                 reference: str = None,
                 default: bool = True,
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plugin = plugin
        self.color = color
        if default:
            if not special:
                self.add_item(InputText(label="Von", placeholder="Von", required=True, value=name_from))
                self.add_item(InputText(label="Zu", placeholder="Zu", required=True, value=name_to))
            else:
                self.add_item(InputText(label="Spieler(konto)name", placeholder="z.B. \"KjinaDeNiel\"", required=True,
                                        value=name_from))
            self.add_item(InputText(label="Menge", placeholder="Menge", required=True, value=amount))
            self.add_item(
                InputText(label="Verwendungszweck", placeholder="Verwendungszweck", required=True, value=purpose))
            self.add_item(InputText(label="Referenz", placeholder="Optional", required=False,
                                    value=reference))

    async def callback(self, interaction: ApplicationContext):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        await interaction.response.defer(ephemeral=True, invisible=False)
        transaction, warnings = await Transaction.from_modal(self.plugin, self, interaction.user.name,
                                                             interaction.user.id)
        if transaction is None:
            await interaction.followup.send(warnings, ephemeral=True)
            return
        if transaction.name_from:
            f = transaction.name_from
            transaction.name_from = self.plugin.sheet.check_name_overwrites(
                self.plugin.member_p.get_main_name(transaction.name_from)
            )
            if f != transaction.name_from:
                warnings += f"Info: Der Sender wurde zu \"{transaction.name_from}\" geändert.\n"
        if transaction.name_to:
            t = transaction.name_to
            transaction.name_to = self.plugin.sheet.check_name_overwrites(
                self.plugin.member_p.get_main_name(transaction.name_to)
            )
            if t != transaction.name_to:
                warnings += f"Info: Der Empfänger wurde zu \"{transaction.name_to}\" geändert.\n"
        view = ConfirmView(self.plugin)
        msg = await interaction.followup.send(
            warnings, embed=transaction.create_embed(),
            ephemeral=True, view=view)
        view.message = msg


class EditModal(TransferModal):
    def __init__(self, plugin: AccountingPlugin, message: Message, *args, **kwargs):
        self.plugin = plugin
        embed = message.embeds[0]
        # noinspection PyTypeChecker
        super().__init__(color=embed.color, plugin=plugin, default=False, *args, **kwargs)
        for field in embed.fields:
            self.add_item(InputText(label=field.name, required=True, value=field.value))

    async def callback(self, interaction: Interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        transaction, warnings = await Transaction.from_modal(self.plugin, self, interaction.user.name,
                                                             interaction.user.id)
        original = self.plugin.transaction_from_embed(interaction.message.embeds[0])
        if not isinstance(original, Transaction):
            await interaction.response.send_message("Nur normale Transaktionen können aktuell bearbeitet werden",
                                                    ephemeral=True)
            return
        if transaction is not None and len(warnings) > 0:
            await interaction.response.send_message(
                warnings, embed=transaction.create_embed(),
                ephemeral=True,
                view=ConfirmEditView(message=interaction.message, original=original, plugin=self.plugin))
            return
        if transaction is None:
            await interaction.response.send_message(warnings, ephemeral=True)
            return
        if original.name_to != transaction.name_to or original.name_from != transaction.name_from or \
                original.amount != transaction.amount or original.purpose != transaction.purpose:
            ocr_verified = self.plugin.db.get_ocr_verification(interaction.message.id)
            if ocr_verified:
                self.plugin.db.set_ocr_verification(interaction.message.id, False)
                warnings += "Warnung: Du kannst diese Transaktion nicht mehr selbst verifizieren.\n"
        await interaction.message.edit(embed=transaction.create_embed())
        await interaction.response.send_message(f"Transaktionen wurde editiert!\n{warnings}", ephemeral=True)


class ShipyardModal(ErrorHandledModal):
    def __init__(self, plugin: AccountingPlugin, color: Color, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plugin = plugin
        self.color = color
        self.add_item(InputText(label="Käufer", placeholder="Käufer", required=True))
        self.add_item(InputText(label="Schiff", placeholder="Schiffsname", required=True))
        self.add_item(InputText(label="Preis", placeholder="Gesamtkosten", required=True))
        self.add_item(InputText(label="Davon Stationsgebühren", placeholder="(Klickkosten)", required=True))
        self.add_item(InputText(label="Bauer", placeholder="Manufacturer", required=False))

    async def callback(self, interaction: Interaction):
        if not self.plugin.bot.is_online():
            raise BotOfflineException()
        buyer, buyer_is_match = self.plugin.parse_player(self.children[0].value)
        ship = self.children[1].value.strip()
        price, warn_price = parse_number(self.children[2].value)
        station_fees, warn_fees = parse_number(self.children[3].value)
        if self.children[4].value is not None:
            builder, builder_is_match = self.plugin.parse_player(self.children[4].value)
        else:
            builder = None
            builder_is_match = True

        # Datavalidation warnings
        warnings = warn_price + warn_fees
        if buyer is None:
            await interaction.response.send_message(
                f"Spieler \"{self.children[0].value}\" konnte nicht gefunden werden!", ephemeral=True)
            return
        buyer = self.plugin.sheet.check_name_overwrites(self.plugin.member_p.get_main_name(buyer))
        if not buyer_is_match:
            warnings += f"Hinweis: Käufer \"{self.children[0].value}\" wurde zu \"**{buyer}**\" geändert!\n"
        if builder is not None:
            builder = self.plugin.sheet.check_name_overwrites(self.plugin.member_p.get_main_name(builder))
        if not builder_is_match and len(self.children[4].value) > 0:
            warnings += f"Warnung: Bauer \"{self.children[4].value}\" wurde zu \"**{builder}**\" geändert!\n"
        if price is None:
            await interaction.response.send_message(
                f"\"{self.children[2].value}\" ist keine gültige Zahl! Erlaube Formate (Beispiele):\n"
                "1,000,000 ISK\n100000\n1 000 000 ISK\n1,000,000.00", ephemeral=True)
            return
        if station_fees is None:
            await interaction.response.send_message(
                f"\"{self.children[3].value}\" ist keine gültige Zahl! Erlaube Formate (Beispiele):\n"
                "1,000,000 ISK\n100000\n1 000 000 ISK\n1,000,000.00", ephemeral=True)
            return

        slot_price = min(int(price * 0.02 / 100000) * 100000, 50000000)
        if slot_price < 1000000:
            warnings += f"Warnung: Slotgebühr ist mit {slot_price} zu gering, sie wird nicht eingetragen."
            builder = None

        transaction = ShipyardTransaction(
            author=interaction.user.name,
            buyer=buyer,
            price=price,
            ship=ship,
            station_fees=station_fees,
            builder=builder)

        if len(warnings) == 0:
            warnings = "Möchtest du diese Transaktion abschicken?"
        await interaction.response.send_message(
            warnings,
            embed=transaction.to_embed(),
            ephemeral=True,
            view=ConfirmView(self.plugin))
