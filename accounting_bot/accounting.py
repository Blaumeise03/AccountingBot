import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from asyncio import Lock
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from typing import Union, List

import cv2
import discord
import discord.ext
import mariadb
import pytz
from discord import Embed, Interaction, Color, Message
from discord.ext.commands import Bot
from discord.ui import Modal, InputText
from numpy import ndarray

from accounting_bot import sheet, utils
from accounting_bot.exceptions import BotOfflineException, AccountingException
from accounting_bot.universe import pi_planer
from accounting_bot.utils import AutoDisableView, ErrorHandledModal, TransactionLike, parse_number

if TYPE_CHECKING:
    from bot import BotState

INVESTMENT_RATIO = 0.3  # percentage of investment that may be used for transactions

logger = logging.getLogger("bot.accounting")

BOT = None  # type: discord.ext.commands.bot.Bot | None
ACCOUNTING_LOG = None  # type: int | None
ADMIN_LOG = None  # type: int | None
SHIPYARD_ADMINS = []
STATE = None  # type: BotState | None
IMG_WORKING_DIR = None  # type: str | None
NAME_SHIPYARD = "Buyback Program"

# All embeds
EMBED_MENU_INTERNAL = None  # type: Embed | None
EMBED_MENU_EXTERNAL = None  # type: Embed | None
EMBED_MENU_VCB = None  # type: Embed | None
EMBED_MENU_SHORTCUT = None  # type: Embed | None
EMBED_INDU_MENU = None  # type: Embed | None

# Database lock
database_lock = Lock()


def setup(state: "BotState") -> None:
    """
    Sets all the required variables and reloads the embeds.

    :param state: the global state of the bot
    """
    global BOT, ACCOUNTING_LOG, ADMIN_LOG, STATE, SHIPYARD_ADMINS
    global EMBED_MENU_INTERNAL, EMBED_MENU_EXTERNAL, EMBED_MENU_VCB, EMBED_MENU_SHORTCUT, EMBED_INDU_MENU
    config = state.config
    BOT = state.bot
    ACCOUNTING_LOG = config["logChannel"]
    admin_log = config["adminLogChannel"]
    if admin_log != -1:
        ADMIN_LOG = admin_log
    SHIPYARD_ADMINS = config["shipyard_admins"]
    STATE = state
    logger.info("Loading embed config")
    with open("resources/embeds.json", "r", encoding="utf8") as embed_file:
        embeds = json.load(embed_file)
        EMBED_MENU_INTERNAL = Embed.from_dict(embeds["MenuEmbedInternal"])
        EMBED_MENU_EXTERNAL = Embed.from_dict(embeds["MenuEmbedExternal"])
        EMBED_MENU_VCB = Embed.from_dict(embeds["MenuEmbedVCB"])
        EMBED_MENU_SHORTCUT = Embed.from_dict(embeds["MenuShortcut"])
        EMBED_INDU_MENU = Embed.from_dict(embeds["InduRoleMenu"])
        pi_planer.help_embed = Embed.from_dict(embeds["PiPlanerHelp"])
        pi_planer.autoarray_help_a = embeds["PiPlanAutoSelectHelpA"]
        pi_planer.autoarray_help_b = embeds["PiPlanAutoSelectHelpB"]
        logger.info("Embeds loaded.")


def get_menu_embeds() -> [Embed]:
    """
    Returns an array of all embeds for the main accounting bot menu.

    :return: an array containing all three menu embeds.
    """
    return [
        EMBED_MENU_INTERNAL,
        EMBED_MENU_EXTERNAL,
        EMBED_MENU_VCB
    ]


def get_current_time() -> str:
    """
    Returns the current time as a string with the format dd.mm.YYYY HH:MM

    :return: the formatted time
    """
    now = datetime.now()
    return now.strftime("%d.%m.%Y %H:%M")


def parse_player(string: str) -> (Union[str, None], bool):
    """
    Finds the closest playername match for a given string. It returns the name or None if not found, as well as a
    boolean indicating whether it was a perfect match.

    :param string: the string which should be looked up
    :return: (Playername: str or None, Perfect match: bool)
    """
    return utils.parse_player(string, sheet.users)


async def inform_player(transaction, discord_id, receive):
    time_formatted = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    if not discord_id:
        logger.warning("Didn't received an ID for %s (receive=%s)", str(transaction), str(receive))
        return
    user = await BOT.get_or_fetch_user(discord_id)
    if user is not None:
        await user.send(
            ("Du hast ISK auf Deinem Accounting erhalten." if receive else "Es wurde ISK von deinem Konto abgebucht.") +
            "\nDein Kontostand beträgt `" +
            "{:,} ISK".format(
                await sheet.get_balance(transaction.name_to if receive else transaction.name_from, default=-1)) + "`",
            embed=transaction.create_embed())
    elif discord_id > 0:
        logger.warning("Can't inform user %s (%s) about about the transaction %s -> %s: %s (%s)",
                       transaction.name_to if receive else transaction.name_from,
                       discord_id,
                       transaction.name_from,
                       transaction.name_to,
                       "{:,} ISK".format(transaction.amount),
                       time_formatted)


async def save_transaction(transaction: "Transaction", msg: Message, user_id: int):
    # Check if transaction is valid
    if transaction.amount is None or (not transaction.name_from and not transaction.name_to) or not transaction.purpose:
        logger.error(f"Invalid embed in message {msg.id}! Can't parse transaction data: {transaction}")
        raise AccountingException("Transaction verification failed: Invalid embed")
    time_formatted = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")

    # Save transaction to sheet
    await sheet.add_transaction(transaction=transaction)
    user = await BOT.get_or_fetch_user(user_id)
    logger.info(f"Verified transaction {msg.id} ({time_formatted}). Verified by {user.name} ({user.id}).")

    # Set message as verified
    STATE.db_connector.set_verification(msg.id, verified=1)


async def inform_players(transaction: "Transaction"):
    # Update wallets
    if transaction.name_from and transaction.name_from in sheet.wallets:
        sheet.wallets[transaction.name_from] = sheet.wallets[transaction.name_from] - transaction.amount
    if transaction.name_to and transaction.name_to in sheet.wallets:
        sheet.wallets[transaction.name_to] = sheet.wallets[transaction.name_to] + transaction.amount
    await sheet.load_wallets()

    # Find discord account
    if transaction.name_from:
        id_from, _, perfect = await utils.get_or_find_discord_id(BOT, STATE.guild, STATE.user_role, transaction.name_from)
        if not perfect:
            id_from = None
        await inform_player(transaction, id_from, receive=False)
    if transaction.name_to:
        id_to, _, perfect = await utils.get_or_find_discord_id(BOT, STATE.guild, STATE.user_role, transaction.name_to)
        if not perfect:
            id_to = None
        await inform_player(transaction, id_to, receive=True)


async def save_embeds(msg, user_id):
    """
    Saves the transaction of a message into the sheet

    :param msg:     The message with the transaction embed
    :param user_id: The user ID that verified the transaction
    """
    if not STATE.is_online():
        raise BotOfflineException("Can't verify transactions when the bot is not online")
    if len(msg.embeds) == 0:
        return
    elif len(msg.embeds) > 1:
        logging.warning(f"Message {msg.id} has more than one embed ({msg.embeds})!")
    # Getting embed of the message, should contain only one
    embed = msg.embeds[0]
    # Convert embed to Transaction
    transaction = transaction_from_embed(embed)
    if isinstance(transaction, PackedTransaction):
        transactions = transaction.get_transactions()
    else:
        transactions = [transaction]
    async with database_lock:
        is_unverified = STATE.db_connector.is_unverified_transaction(msg.id)
        if not is_unverified:
            time_formatted = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
            logger.warning(
                f"Attempted to verify an already verified transaction {msg.id} ({time_formatted}), user: {user_id}.")
            return
        for transaction in transactions:
            await save_transaction(transaction, msg, user_id)
    user = await BOT.get_or_fetch_user(user_id)
    await asyncio.gather(
        msg.edit(content=f"Verifiziert von {user.name}", view=None),
        msg.remove_reaction("⚠️", BOT.user),
        msg.remove_reaction("❌", BOT.user),
        *[inform_players(transaction) for transaction in transactions]
    )


async def get_wallet_state(wallet, amount) -> int:
    bal = await sheet.get_balance(wallet, 0)
    inv = await sheet.get_investments(wallet, 0)
    if bal > amount:
        return 0
    elif (bal + inv * INVESTMENT_RATIO) > amount:
        return 1
    elif (bal + inv) >= amount:
        return 2
    else:
        return 3


async def verify_transaction(user_id: int, message: Message, interaction: Interaction = None):
    if not STATE.is_online():
        raise BotOfflineException()

    if ADMIN_LOG is not None:
        admin_log_channel = BOT.get_channel(ADMIN_LOG)
        if admin_log_channel is None:
            admin_log_channel = await BOT.fetch_channel(ADMIN_LOG)
    else:
        admin_log_channel = None
    is_unverified = STATE.db_connector.is_unverified_transaction(message=message.id)
    if is_unverified is None:
        if interaction:
            await interaction.followup.send(content="Error: Transaction not found", ephemeral=True)
        return
    if len(message.embeds) == 0:
        if interaction:
            await interaction.followup.send(content="Error: Embeds not found", ephemeral=True)
        return
    transaction = transaction_from_embed(message.embeds[0])
    if not transaction:
        if interaction:
            await interaction.followup.send(content="Error: Couldn't parse embed", ephemeral=True)
        return
    has_permissions = user_id in STATE.admins
    user = await BOT.get_or_fetch_user(user_id)
    if not has_permissions and isinstance(transaction, Transaction) and transaction.name_from and transaction.name_to:
        # Only transactions between two players can be self-verified
        owner_id, _, _ = await utils.get_or_find_discord_id(player_name=transaction.name_from)
        if owner_id and user_id == owner_id:
            # Check if balance is sufficient
            await sheet.load_wallets()
            bal = await sheet.get_balance(transaction.name_from)
            inv = await sheet.get_investments(transaction.name_from, default=0)
            if not bal:
                if interaction:
                    await interaction.followup.send(content="Dein Kontostand konnte nicht gepüft werden.",
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
    ocr_verified = False
    if isinstance(transaction, Transaction) and transaction.detect_type() == 1:
        owner_id, _, _ = await utils.get_or_find_discord_id(player_name=transaction.name_to)
        if owner_id and user_id == owner_id:
            ocr_verified = STATE.db_connector.get_ocr_verification(message.id)
            has_permissions = has_permissions or ocr_verified
    if isinstance(transaction, ShipyardTransaction) and not has_permissions:
        has_permissions = user_id in SHIPYARD_ADMINS
    if not has_permissions:
        if interaction:
            await interaction.followup.send(content="Fehler: Du hast dazu keine Berechtigung. Nur der Kontoinhaber und "
                                                    "Admins dürfen Transaktionen verifizieren.", ephemeral=True)
        return

    if not is_unverified:
        # Message is already verified
        msg = "Fehler: Diese Transaktion wurde bereits verifiziert, sie wurde nicht " \
              "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls " \
              "dies nötig ist."
        if interaction:
            await interaction.followup.send(content=msg, ephemeral=True)
        else:
            await user.send(content=msg)
        return

    if message.content.startswith("Verifiziert von"):
        # Message was already verified, but due to an Error it got not updated in the SQL DB
        author = await BOT.get_or_fetch_user(user_id)
        msg = "Fehler: Diese Transaktion wurde bereits verifiziert, sie wurde nicht " \
              "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls " \
              "dies nötig ist."
        if interaction:
            await interaction.followup.send(content=msg, ephemeral=True)
        else:
            await author.send(content=msg)
        logger.warning("Transaction %s was already verified (according to message content), but not marked as "
                       "verified in the database.", transaction.__str__())
        STATE.db_connector.set_verification(message.id, True)
        await message.edit(view=None)
        return
    else:
        # Save transaction
        await save_embeds(message, user_id)
        if admin_log_channel and (user_id not in STATE.admins or ocr_verified):
            file = None
            if os.path.exists(IMG_WORKING_DIR + f"/transactions/{str(message.id)}.jpg"):
                file = discord.File(IMG_WORKING_DIR + f"/transactions/{str(message.id)}.jpg")
            msg = "Transaction `{}` was self-verified by `{}:{}`:\nhttps://discord.com/channels/{}/{}/{}\n" \
                .format(transaction, user.name, user_id, STATE.guild, ACCOUNTING_LOG, message.id)
            if ocr_verified:
                msg += "*Transaction was OCR verified*"
            await admin_log_channel.send(msg, file=file)
            if os.path.exists(IMG_WORKING_DIR + f"/transactions/{str(message.id)}.jpg"):
                os.remove(IMG_WORKING_DIR + f"/transactions/{str(message.id)}.jpg")
                logger.info("Deleted file %s", IMG_WORKING_DIR + f"/transactions/{str(message.id)}.jpg")
        if interaction:
            await message.add_reaction("✅")
            await interaction.followup.send("Transaktion verifiziert!", ephemeral=True)


def transaction_from_embed(embed: Embed):
    if embed.title == "Shipyard Bestellung":
        return ShipyardTransaction.from_embed(embed)
    return Transaction.from_embed(embed)


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

    def has_permissions(self, user, operation="") -> bool:
        match operation:
            case "delete":
                if self.name_from and utils.get_main_account(self.name_from) == user:
                    return True
                elif self.name_to and utils.get_main_account(self.name_to) == user:
                    return True
                return False
            case "verify":
                return user in STATE.admins
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
        embed.add_field(name="Menge", value="{:,} ISK".format(self.amount), inline=True)
        embed.add_field(name="Verwendungszweck", value=self.purpose, inline=True)
        if self.reference is not None and len(self.reference) > 0:
            embed.add_field(name="Referenz", value=self.reference, inline=True)
        embed.timestamp = self.timestamp
        if self.author is not None and len(self.author) > 0:
            embed.set_footer(text=self.author)
        return embed

    async def get_state(self):
        if self.name_from is None:
            return None
        return await get_wallet_state(self.name_from, self.amount)

    @staticmethod
    async def from_modal(modal: Modal, author: str, user: int = None) -> ('Transaction', str):
        """
        Creates a Transaction out of a :class:`Modal`, the Modal has to be filled out.

        All warnings that occurred during parsing the values will be returned as well.

        :param modal: the modal with the values for the transaction
        :param author: the author of this transaction
        :param user: the discord id of the author
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
                name, match = parse_player(field.value.strip())
                if name is None:
                    warnings += f"Hinweis: Name \"{field.value}\" konnte nicht gefunden werden!\n"
                    return None, warnings
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
                continue

        # Check wallet ownership and balance
        if transaction.name_from:
            user_id, _, _ = await utils.get_or_find_discord_id(player_name=transaction.name_from)
            await sheet.load_wallets()
            if (user_id is None or user != user_id) and user not in STATE.admins:
                warnings += "**Fehler**: Dieses Konto gehört dir nicht bzw. dein Discordaccount ist nicht " \
                            "**verifiziert** (kontaktiere in diesem Fall einen Admin). Nur der Kontobesitzer darf " \
                            "ISK von seinem Konto an andere senden.\n"
            bal = await sheet.get_balance(transaction.name_from)
            inv = await sheet.get_investments(transaction.name_from, default=0)
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
    def from_embed(embed: Embed) -> Optional['Transaction']:
        """
        Creates a Transaction from an existing :class:`Embed`.

        :param embed: the embed containing the transaction data
        :return: the transaction
        """
        transaction = Transaction()
        for field in embed.fields:
            name = field.name.casefold()
            if name == "Von".casefold():
                transaction.name_from = field.value
            if name == "Zu".casefold():
                transaction.name_to = field.value
            if name == "Menge".casefold():
                transaction.amount, warn = parse_number(field.value)
            if name == "Verwendungszweck".casefold():
                transaction.purpose = field.value
            if name == "Referenz".casefold():
                transaction.reference = field.value
        transaction.timestamp = embed.timestamp.astimezone(pytz.timezone("Europe/Berlin"))
        if embed.footer is not None:
            transaction.author = embed.footer.text
        if not transaction.is_valid():
            return None
        return transaction

    @staticmethod
    def from_ocr(mission: TransactionLike, user_id: int):
        transaction = Transaction()
        transaction.name_from = mission.get_from()
        transaction.name_to = mission.get_to()
        transaction.purpose = mission.get_purpose()
        transaction.reference = mission.get_reference()
        transaction.amount = mission.get_amount()
        transaction.timestamp = mission.get_time()
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
    async def get_state(self) -> int:
        pass

    @staticmethod
    @abstractmethod
    def from_embed(embed: Embed) -> "PackedTransaction":
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

    async def get_state(self) -> Optional[int]:
        if self.buyer is None:
            return None
        return await get_wallet_state(self.buyer, self.price)

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
    def from_embed(embed: Embed) -> "ShipyardTransaction":
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
        transaction.timestamp = embed.timestamp.astimezone(pytz.timezone("Europe/Berlin"))
        if embed.footer is not None:
            transaction.author = embed.footer.text
        return transaction


async def send_transaction(embeds: List[Union[Embed, Transaction, PackedTransaction]], interaction: Interaction,
                           note="") -> Optional[Message]:
    """
    Sends the embeds into the accounting log channel. Will send a response to the :class:`Interaction` containing the
    note and an error message in case any :class:`mariadb.Error` occurred.

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
        msg = await BOT.get_channel(ACCOUNTING_LOG).send(embeds=[embed], view=TransactionView())
        try:
            STATE.db_connector.add_transaction(msg.id, interaction.user.id)
            if transaction is None:
                transaction = transaction_from_embed(embed)
                if isinstance(transaction, ShipyardTransaction):
                    transaction = ShipyardTransaction.from_embed(embed)
                    if interaction.user.id in SHIPYARD_ADMINS:
                        transaction.authorized = True
                        note += "\nDu kannst diese Transaktion selbst verifizieren"
            if not transaction:
                logger.warning("Embed in message %s is not a transaction", msg.id)
                continue

            state = await transaction.get_state()
            if state == 2:
                await msg.add_reaction("⚠️")
            elif state == 3:
                await msg.add_reaction("❌")
            if state is not None:
                STATE.db_connector.set_state(msg.id, state)
            if transaction.self_verification():
                STATE.db_connector.set_ocr_verification(msg.id, True)
        except mariadb.Error as e:
            note += "\nFehler beim Eintragen in die Datenbank, die Transaktion wurde jedoch trotzdem im " \
                    f"Accountinglog gepostet. Informiere bitte einen Admin, danke.\n{e}"
    await interaction.followup.send("Transaktion gesendet!" + note, ephemeral=True)
    return msg


# noinspection PyUnusedLocal
class AccountingView(AutoDisableView):
    """
    A :class:`discord.ui.View` with four buttons: 'Transfer', 'Deposit', 'Withdraw', 'Shipyard' and a printer button.
    The first 4 buttons will open the corresponding modal (see :class:`TransferModal` and :class:`ShipyardModal`),
    the printer button responds with a list of all links to all unverified transactions.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.blurple)
    async def btn_transfer_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        modal = TransferModal(title="Transfer", color=Color.blue())
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Einzahlen", style=discord.ButtonStyle.green)
    async def btn_deposit_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        modal = TransferModal(title="Einzahlen", color=Color.green(), special=True, purpose="Einzahlung Accounting")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Auszahlen", style=discord.ButtonStyle.red)
    async def btn_withdraw_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        modal = TransferModal(title="Auszahlen", color=Color.red(), special=True, purpose="Auszahlung Accounting")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Shipyard", style=discord.ButtonStyle.grey)
    async def btn_shipyard_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        modal = ShipyardModal(title="Schiffskauf", color=Color.red())
        await interaction.response.send_modal(modal)

    @discord.ui.button(emoji="🖨️", style=discord.ButtonStyle.grey)
    async def btn_list_transactions_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        unverified = STATE.db_connector.get_unverified(include_user=True)
        msg = "Unverifizierte Transaktionen:"
        if len(unverified) == 0:
            msg += "\nKeine"
        i = 0
        for (msgID, userID) in unverified:
            if len(msg) < 1900 or True:
                msg += f"\nhttps://discord.com/channels/{STATE.guild}/{ACCOUNTING_LOG}/{msgID} von <@{userID}>"
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

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verifizieren", style=discord.ButtonStyle.green)
    async def btn_verify_callback(self, button: discord.Button, interaction: Interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        await interaction.response.defer(ephemeral=True, invisible=False)
        await verify_transaction(interaction.user.id, interaction.message, interaction)

    @discord.ui.button(label="Löschen", style=discord.ButtonStyle.red)
    async def btn_delete_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        (owner, verified) = STATE.db_connector.get_owner(interaction.message.id)
        transaction = transaction_from_embed(interaction.message.embeds[0])
        user_name = utils.get_main_account(discord_id=interaction.user.id)
        has_perm = owner == interaction.user.id or interaction.user.id in STATE.admins
        if not has_perm:
            has_perm = transaction.has_permissions(user_name, "delete")
        if not verified and has_perm:
            await interaction.message.delete()
            STATE.db_connector.delete(interaction.message.id)
            await interaction.response.send_message("Transaktion Gelöscht!", ephemeral=True)
            logger.info("User %s deleted message %s", interaction.user.id, interaction.message.id)
        elif not owner == interaction.user.id:
            await interaction.response.send_message("Dies ist nicht deine Transaktion, wenn du ein Admin bist, lösche "
                                                    "die Nachricht bitte eigenständig.", ephemeral=True)
        else:
            await interaction.response.send_message("Bereits verifiziert!", ephemeral=True)

    @discord.ui.button(label="Bearbeiten", style=discord.ButtonStyle.blurple)
    async def btn_edit_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        (owner, verified) = STATE.db_connector.get_owner(interaction.message.id)
        if not verified and (owner == interaction.user.id or interaction.user.id in STATE.admins):
            embed = interaction.message.embeds[0]
            await interaction.response.send_modal(EditModal(interaction.message, title=embed.title))
        elif not owner == interaction.user.id:
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

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction: Interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        await interaction.response.defer(ephemeral=True, invisible=False)
        await send_transaction(interaction.message.embeds, interaction)
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

    def __init__(self, message: Message, original: Transaction):
        """
        Creates a new ConfirmEditView.

        :param message: the original message which should be edited.
        """
        super().__init__()
        self.message = message
        self.original = original

    @discord.ui.button(label="Speichern", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        transaction = transaction_from_embed(interaction.message.embeds[0])
        if not isinstance(transaction, Transaction):
            raise TypeError(f"Expected Transaction, but got {type(transaction)}")
        warnings = ""
        if self.original.name_to != transaction.name_to or self.original.name_from != transaction.name_from or \
                self.original.amount != transaction.amount or self.original.purpose != transaction.purpose:
            ocr_verified = STATE.db_connector.get_ocr_verification(interaction.message.id)
            if ocr_verified:
                STATE.db_connector.set_ocr_verification(interaction.message.id, False)
                warnings += "Warnung: Du kannst diese Transaktion nicht mehr selbst verifizieren.\n"
        await self.message.edit(embeds=interaction.message.embeds)
        await interaction.response.send_message(f"Transaktion bearbeitet!\n{warnings}", ephemeral=True)


# noinspection PyUnusedLocal
class ConfirmOCRView(AutoDisableView):
    def __init__(self, transaction: Transaction, img: ndarray, note: str = ""):
        super().__init__()
        self.transaction = transaction
        self.note = note
        self.img = img

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        await interaction.response.defer(ephemeral=True, invisible=False)
        msg = await send_transaction([self.transaction], interaction, self.note)
        if msg is not None:
            res_msg = f"Transaktion versendet: https://discord.com/channels/{STATE.guild}/{ACCOUNTING_LOG}/{msg.id}"
        else:
            res_msg = "Transaktion versendet!"
        if self.transaction.allow_self_verification:
            res_msg += "\nDu kannst diese Transaktion selbstständig verifizieren, klicke dazu im Accountinglog unter" \
                       "der Transaktion auf \"Verifizieren\"."
        await interaction.response.send_message(res_msg)
        if self.img is not None:
            cv2.imwrite(IMG_WORKING_DIR + f"/transactions/{str(msg.id)}.jpg", self.img)

    @discord.ui.button(label="Ändern", style=discord.ButtonStyle.blurple)
    async def btn_change_callback(self, button, interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        await interaction.response.send_modal(EditOCRModal(self.transaction, self.message))


class EditOCRModal(ErrorHandledModal):
    def __init__(self, transaction: Transaction, message: Message, *args, **kwargs):
        super().__init__(title="Transaktion ändern", *args, **kwargs)
        self.transaction = transaction
        self.message = message
        amount = "{:,} ISK".format(transaction.amount) if transaction.amount is not None else None
        self.add_item(InputText(label="Datum/Uhrzeit", placeholder="z.B. 25.03.2023 13:40", required=True,
                                value=transaction.timestamp.strftime("%d.%m.%Y %H:%M")))
        self.add_item(InputText(label="Menge", placeholder="z.B. 100,000,000 ISK", required=True, value=amount))

    async def callback(self, interaction: Interaction):
        time_raw = self.children[0].value
        amount_raw = self.children[1].value
        amount, warn = parse_number(amount_raw)
        try:
            time = datetime.strptime(time_raw, "%d.%m.%Y %H:%M")
            self.transaction.timestamp = time
        except ValueError:
            warn += f"**Fehler**: \"{time_raw}\" entspricht nicht dem Format \"25.03.2023 13:40\"\n"
        if amount is None:
            warn += f"**Fehler**: \"{amount_raw}\" ist keine Zahl, bzw. entspricht nicht dem Format \"1,000,000,000.00 ISK\"\n"
        elif self.transaction.amount != amount:
            warn += "Warnung: Die Menge wurde verändert, eine Verifizierung ist nur durch einen Admin möglich.\n"
            self.transaction.allow_self_verification = False
            self.transaction.amount = amount
        await self.message.edit(embed=self.transaction.create_embed())
        if len(warn) == 0:
            warn += "Transaktion geändert!\n"
        await interaction.response.send_message(warn)


class TransferModal(ErrorHandledModal):
    def __init__(self, color: Color, special: bool = False,
                 name_from: str = None,
                 name_to: str = None,
                 amount: str = None,
                 purpose: str = None,
                 reference: str = None,
                 default: bool = True,
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
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
            self.add_item(InputText(label="Referenz", placeholder="z.B \"voidcoin.app/contract/20577\"", required=False,
                                    value=reference))

    async def callback(self, interaction: Interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        transaction, warnings = await Transaction.from_modal(self, interaction.user.name, interaction.user.id)
        if transaction is None:
            await interaction.response.send_message(warnings, ephemeral=True)
            return
        if transaction.name_from:
            f = transaction.name_from
            transaction.name_from = sheet.check_name_overwrites(transaction.name_from)
            if f != transaction.name_from:
                warnings += f"Info: Der Sender wurde zu \"{transaction.name_from}\" geändert.\n"
        if transaction.name_to:
            t = transaction.name_to
            transaction.name_to = sheet.check_name_overwrites(transaction.name_to)
            if t != transaction.name_to:
                warnings += f"Info: Der Empfänger wurde zu \"{transaction.name_to}\" geändert.\n"
        await interaction.response.send_message(
            warnings, embed=transaction.create_embed(),
            ephemeral=True, view=ConfirmView())


class EditModal(TransferModal):
    def __init__(self, message: Message, *args, **kwargs):
        embed = message.embeds[0]
        # noinspection PyTypeChecker
        super().__init__(color=embed.color, default=False, *args, **kwargs)
        for field in embed.fields:
            self.add_item(InputText(label=field.name, required=True, value=field.value))

    async def callback(self, interaction: Interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        transaction, warnings = await Transaction.from_modal(self, interaction.user.name, interaction.user.id)
        original = transaction_from_embed(interaction.message.embeds[0])
        if not isinstance(original, Transaction):
            await interaction.response.send_message("Nur normale Transaktionen können aktuell bearbeitet werden",
                                                    ephemeral=True)
            return
        if transaction is not None and len(warnings) > 0:
            await interaction.response.send_message(
                warnings, embed=transaction.create_embed(),
                ephemeral=True, view=ConfirmEditView(message=interaction.message, original=original))
            return
        if transaction is None:
            await interaction.response.send_message(warnings, ephemeral=True)
            return
        if original.name_to != transaction.name_to or original.name_from != transaction.name_from or \
                original.amount != transaction.amount or original.purpose != transaction.purpose:
            ocr_verified = STATE.db_connector.get_ocr_verification(interaction.message.id)
            if ocr_verified:
                STATE.db_connector.set_ocr_verification(interaction.message.id, False)
                warnings += "Warnung: Du kannst diese Transaktion nicht mehr selbst verifizieren.\n"
        await interaction.message.edit(embed=transaction.create_embed())
        await interaction.response.send_message(f"Transaktionen wurde editiert!\n{warnings}", ephemeral=True)


class ShipyardModal(ErrorHandledModal):
    def __init__(self, color: Color, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.color = color
        self.add_item(InputText(label="Käufer", placeholder="Käufer", required=True))
        self.add_item(InputText(label="Schiff", placeholder="Schiffsname", required=True))
        self.add_item(InputText(label="Preis", placeholder="Gesamtkosten", required=True))
        self.add_item(InputText(label="Davon Stationsgebühren", placeholder="(Klickkosten)", required=True))
        self.add_item(InputText(label="Bauer", placeholder="Manufacturer", required=False))

    async def callback(self, interaction: Interaction):
        if not STATE.is_online():
            raise BotOfflineException()
        buyer, buyer_is_match = parse_player(self.children[0].value)
        ship = self.children[1].value.strip()
        price, warn_price = parse_number(self.children[2].value)
        station_fees, warn_fees = parse_number(self.children[3].value)
        if self.children[4].value is not None:
            builder, builder_is_match = parse_player(self.children[4].value)
        else:
            builder = None
            builder_is_match = True

        # Datavalidation warnings
        warnings = warn_price + warn_fees
        if buyer is None:
            await interaction.response.send_message(
                f"Spieler \"{self.children[0].value}\" konnte nicht gefunden werden!", ephemeral=True)
            return
        if not buyer_is_match:
            warnings += f"Hinweis: Käufer \"{self.children[0].value}\" wurde zu \"**{buyer}**\" geändert!\n"
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
            view=ConfirmView())
