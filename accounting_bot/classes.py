import json
import logging
import re
from datetime import datetime
from typing import Union, List

import discord
import discord.ext
import mariadb
import pytz
from discord import Embed, Interaction, Color, Message
from discord.ui import Modal, InputText

from accounting_bot import sheet, utils
from accounting_bot.corpmissionOCR import CorporationMission
from accounting_bot.database import DatabaseConnector
from accounting_bot.utils import send_exception, AutoDisableView

logger = logging.getLogger("bot.classes")

BOT = None  # type: discord.ext.commands.bot.Bot | None
ACCOUNTING_LOG = None  # type: int | None
SERVER = None  # type: int | None
ADMINS = []  # type: [int]
CONNECTOR = None  # type: DatabaseConnector | None
USER_ROLE = None  # type: int | None

# All embeds
EMBED_MENU_INTERNAL = None  # type: Embed | None
EMBED_MENU_EXTERNAL = None  # type: Embed | None
EMBED_MENU_VCB = None  # type: Embed | None
EMBED_MENU_SHORTCUT = None  # type: Embed | None
EMBED_INDU_MENU = None  # type: Embed | None


def set_up(new_connector: DatabaseConnector,
           new_admins: List[int],
           bot: discord.ext.commands.bot.Bot,
           acc_log: int, server: int, user_role: int) -> None:
    """
    Sets all the required variables and reloads the embeds.

    :param new_connector: the new DatabaseConnector
    :param new_admins: the list of the ids of all admins
    :param bot: the discord bot instance
    :param acc_log: the id of the accounting log channel
    :param server: the id of the server
    :param user_role: the role id of users
    """
    global CONNECTOR, ADMINS, BOT, ACCOUNTING_LOG, SERVER, USER_ROLE
    global EMBED_MENU_INTERNAL, EMBED_MENU_EXTERNAL, EMBED_MENU_VCB, EMBED_MENU_SHORTCUT, EMBED_INDU_MENU
    CONNECTOR = new_connector
    ADMINS = new_admins
    BOT = bot
    ACCOUNTING_LOG = acc_log
    SERVER = server
    USER_ROLE = user_role
    logger.info("Loading embed config...")
    with open("embeds.json", "r") as embed_file:
        embeds = json.load(embed_file)
        EMBED_MENU_INTERNAL = Embed.from_dict(embeds["MenuEmbedInternal"])
        EMBED_MENU_EXTERNAL = Embed.from_dict(embeds["MenuEmbedExternal"])
        EMBED_MENU_VCB = Embed.from_dict(embeds["MenuEmbedVCB"])
        EMBED_MENU_SHORTCUT = Embed.from_dict(embeds["MenuShortcut"])
        EMBED_INDU_MENU = Embed.from_dict(embeds["InduRoleMenu"])
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


def parse_number(string: str) -> (int, str):
    """
    Converts a string into an integer. It ignores all letters, spaces and commas. A dot will be interpreted as a
    decimal seperator. Everything after the first dot will be discarded.

    :param string: the string to convert
    :return: the number or None if it had an invalid format
    """
    warnings = ""
    dots = string.count(".")
    comma = string.count(",")
    if dots > 1 >= comma:
        string = string.replace(",", ";")
        string = string.replace(".", ",")
        string = string.replace(";", ".")
        warnings += "Warnung: Es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach " \
                    "dem Format \"1.000.000,00 ISK\" geparsed. " \
                    "Bitte zur Vermeidung von Fehlern das Englische Zahlenformat verwenden!\n"
    elif ("," in string) or ("." in string):
        warnings += "Hinweis: Es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach " \
                    "dem Format \"1,000,000.00 ISK\" geparsed.\n"

    if bool(re.match(r"[0-9]+(,[0-9]+)*(\.[0-9]+)?[a-zA-Z]*", string)):
        number = re.sub(r"[,a-zA-Z ]", "", string).split(".", 1)[0]
        return int(number), warnings
    else:
        return None, ""


def parse_player(string: str) -> (Union[str, None], bool):
    """
    Finds the closest playername match for a given string. It returns the name or None if not found, as well as a
    boolean indicating whether it was a perfect match.

    :param string: the string which should be looked up
    :return: (Playername: str or None, Perfect match: bool)
    """
    return utils.parse_player(string, sheet.users)


async def inform_player(transaction, discord_id, receive):
    user = await BOT.get_or_fetch_user(discord_id)
    if user is not None:
        await user.send(
            "Du hast ISK auf Deinem Accounting erhalten:" if receive else "Es wurde ISK von deinem Konto abgebucht:",
            embed=transaction.create_embed())
    else:
        time_formatted = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
        logging.warning("Can't inform user " + str(discord_id) + " about about the transaction " +
                        time_formatted)


async def save_embeds(msg, user_id):
    """
    Saves the transaction of a message into the sheet

    :param msg:     The message with the transaction embed
    :param user_id: The user ID that verified the transaction
    """
    if len(msg.embeds) == 0:
        return
    elif len(msg.embeds) > 1:
        logging.warning(f"Message {msg.id} has more than one embed ({msg.embeds})!")
    # Getting embed of the message, should contain only one
    embed = msg.embeds[0]
    # Convert embed to Transaction
    transaction = Transaction.from_embed(embed)
    # Check if transaction is valid
    if transaction.amount is None or (not transaction.name_from and not transaction.name_to) or not transaction.purpose:
        logging.error(f"Invalid embed in message {msg.id}! Can't parse transaction data: {transaction}")
        return
    time_formatted = transaction.timestamp.astimezone(pytz.timezone("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")
    # Save transaction to sheet
    await sheet.add_transaction(transaction=transaction)
    user = await BOT.get_or_fetch_user(user_id)
    logging.info(f"Verified transaction {msg.id} ({time_formatted}. Verified by {user.name} ({user.id}).")
    # Set message as verified
    CONNECTOR.set_verification(msg.id, verified=1)
    await msg.edit(content=f"Verifiziert von {user.name}", view=None)

    # Find discord account
    if transaction.name_from:
        id_from, _, perfect = await utils.get_or_find_discord_id(BOT, SERVER, USER_ROLE, transaction.name_from)
        if not perfect:
            id_from = None
        await inform_player(transaction, id_from, receive=False)
    if transaction.name_to:
        id_to, _, perfect = await utils.get_or_find_discord_id(BOT, SERVER, USER_ROLE, transaction.name_to)
        if not perfect:
            id_to = None
        await inform_player(transaction, id_to, receive=True)


async def verify_transaction(user_id: int, message: Message, interaction: Interaction = None):
    is_unverified = CONNECTOR.is_unverified_transaction(message=message.id)
    if is_unverified is None:
        if interaction:
            await interaction.followup.send(content="Error: Transaction not found", ephemeral=True)
        return
    if len(message.embeds) == 0:
        if interaction:
            await interaction.followup.send(content="Error: Embeds not found", ephemeral=True)
        return
    transaction = Transaction.from_embed(message.embeds[0])
    if not transaction:
        if interaction:
            await interaction.followup.send(content="Error: Couldn't parse embed", ephemeral=True)
        return
    has_permissions = user_id in ADMINS
    if not has_permissions and transaction.name_from and transaction.name_to:
        # Only transactions between two players can be self-verified
        owner_id, _, _ = await utils.get_or_find_discord_id(player_name=transaction.name_from)
        has_permissions = owner_id and user_id == owner_id
        logger.info("User " + str(user_id) + " is owner of transaction " + transaction.__str__())

    if not has_permissions:
        if interaction:
            await interaction.followup.send(content="Fehler: Du hast dazu keine Berechtigung. Nur der Kontoinhaber und "
                                                    "Admins dürfen Transaktionen verifizieren.", ephemeral=True)
        return

    if not is_unverified:
        # Message is already verified
        author = await BOT.get_or_fetch_user(user_id)
        msg = "Fehler: Diese Transaktion wurde bereits verifiziert, sie wurde nicht " \
              "erneut im Sheet eingetragen. Bitte trage sie selbstständig ein, falls " \
              "dies nötig ist."
        if interaction:
            await interaction.followup.send(content=msg, ephemeral=True)
        else:
            await author.send(content=msg)
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
        CONNECTOR.set_verification(message.id, True)
        await message.edit(view=None)
        return
    else:
        # Save transaction
        await save_embeds(message, user_id)
        if interaction:
            await message.add_reaction("✅")
            await interaction.followup.send("Transaktion verifiziert!", ephemeral=True)


class Transaction:
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

    def __str__(self):
        return f"<Transaction time: {self.timestamp} from: {self.name_from} to: {self.name_to} amount: {self.amount} " \
               f"purpose: {self.purpose} reference: {self.reference}>"

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
                    warnings += "Fehler: Die eingegebene Menge ist keine Zahl > 0!\n"
                    return None, warnings
                transaction.amount = amount
                continue
            if field.label.casefold() == "Verwendungszweck".casefold():
                transaction.purpose = field.value.strip()
                continue
            if field.label.casefold() == "Referenz".casefold():
                transaction.reference = field.value.strip()
                continue

        # Check wallet ownership
        if transaction.name_from:
            user_id, _, _ = await utils.get_or_find_discord_id(player_name=transaction.name_from)
            if user_id is None or user != user_id and user not in ADMINS:
                warnings += "Fehler: Dieses Konto gehört dir nicht bzw. dein Discordaccount ist nicht " \
                            "**verifiziert** (kontaktiere in diesem Fall einen Admin). Nur der Kontobesitzer darf " \
                            "ISK von seinem Konto an andere senden."
        return transaction, warnings

    @staticmethod
    def from_embed(embed: Embed) -> 'Transaction':
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
        return transaction

    @staticmethod
    def from_ocr(mission: CorporationMission, user_id: int):
        transaction = Transaction()
        if mission.pay_isk and mission.title == "Auszahlung":
            transaction.name_from = mission.main_char
            transaction.purpose = "Auszahlung Accounting"
        elif not mission.pay_isk and mission.title == "Einzahlung":
            transaction.name_to = mission.main_char
            transaction.purpose = "Einzahlung Accounting"
        else:
            return None
        transaction.amount = mission.amount
        return transaction


async def send_transaction(embeds: List[Embed], interaction: Interaction, note=""):
    """
    Sends the embeds into the accounting log channel. Will send a response to the :class:`Interaction` containing the
    note and an error message in case any :class:`mariadb.Error` occurred.

    :param embeds: the embeds to send
    :param interaction: discord interaction for the response
    :param note: the note which should be sent
    """
    for embed in embeds:
        if embed is None:
            continue
        msg = await BOT.get_channel(ACCOUNTING_LOG).send(embeds=[embed], view=TransactionView())
        try:
            CONNECTOR.add_transaction(msg.id, interaction.user.id)
        except mariadb.Error as e:
            note += "\nFehler beim Eintragen in die Datenbank, die Transaktion wurde jedoch trotzdem im " \
                    "Accountinglog gepostet. Informiere bitte einen Admin, danke.\n{e}"
    await interaction.response.send_message("Transaktion gesendet!" + note, ephemeral=True)
    try:
        await interaction.message.edit(view=None)
    except discord.errors.NotFound:
        pass


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
        modal = TransferModal(title="Transfer", color=Color.blue())
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Einzahlen", style=discord.ButtonStyle.green)
    async def btn_deposit_callback(self, button, interaction):
        modal = TransferModal(title="Einzahlen", color=Color.green(), special=True, purpose="Einzahlung Accounting")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Auszahlen", style=discord.ButtonStyle.red)
    async def btn_withdraw_callback(self, button, interaction):
        modal = TransferModal(title="Auszahlen", color=Color.red(), special=True, purpose="Auszahlung Accounting")
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Shipyard", style=discord.ButtonStyle.grey)
    async def btn_shipyard_callback(self, button, interaction):
        modal = ShipyardModal(title="Schiffskauf", color=Color.red())
        await interaction.response.send_modal(modal)

    @discord.ui.button(emoji="🖨️", style=discord.ButtonStyle.grey)
    async def btn_list_transactions_callback(self, button, interaction):
        unverified = CONNECTOR.get_unverified(include_user=True)
        msg = "Unverifizierte Transaktionen:"
        if len(unverified) == 0:
            msg += "\nKeine"
        i = 0
        for (msgID, userID) in unverified:
            if len(msg) < 1900 or True:
                msg += f"\nhttps://discord.com/channels/{SERVER}/{ACCOUNTING_LOG}/{msgID} von <@{userID}>"
                i += 1
            else:
                msg += f"\nUnd {len(unverified) - i} weitere..."
        await interaction.response.send_message(msg, ephemeral=True)

    async def on_error(self, error: Exception, item, interaction):
        interaction.response.send("Error", ephemeral=True)
        logger.exception(f"Error in AccountingView: {error}")
        await send_exception(error, interaction)


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
        await interaction.response.defer()
        await verify_transaction(interaction.user.id, interaction.message, interaction)

    @discord.ui.button(label="Löschen", style=discord.ButtonStyle.red)
    async def btn_delete_callback(self, button, interaction):
        (owner, verified) = CONNECTOR.get_owner(interaction.message.id)
        if not verified and (owner == interaction.user.id or interaction.user.id in ADMINS):
            await interaction.message.delete()
            await interaction.response.send_message("Transaktion Gelöscht!", ephemeral=True)
            CONNECTOR.delete(interaction.message.id)
        elif not owner == interaction.user.id:
            await interaction.response.send_message("Dies ist nicht deine Transaktion, wenn du ein Admin bist, lösche "
                                                    "die Nachricht bitte eigenständig.", ephemeral=True)
        else:
            await interaction.response.send_message("Bereits verifiziert!", ephemeral=True)

    @discord.ui.button(label="Bearbeiten", style=discord.ButtonStyle.blurple)
    async def btn_edit_callback(self, button, interaction):
        (owner, verified) = CONNECTOR.get_owner(interaction.message.id)
        if not verified and (owner == interaction.user.id or interaction.user.id in ADMINS):
            embed = interaction.message.embeds[0]
            await interaction.response.send_modal(EditModal(interaction.message, title=embed.title))
        elif not owner == interaction.user.id:
            await interaction.response.send_message("Dies ist nicht deine Transaktion, wenn du ein Admin bist, lösche "
                                                    "die Nachricht bitte eigenständig.", ephemeral=True)
        else:
            await interaction.response.send_message("Bereits verifiziert!", ephemeral=True)

    async def on_error(self, error: Exception, item, interaction):
        logger.exception(f"Error in TransactionView: {error}", error)
        await send_exception(error, interaction)


class ConfirmView(AutoDisableView):
    """
    A :class:`discord.ui.View` for confirming new transactions. It adds one button 'Send', which will send all embeds of
    the message into the accounting log channel.
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        await send_transaction(interaction.message.embeds, interaction)

    async def on_error(self, error: Exception, item, interaction):
        logger.error(f"Error in ConfirmView: %s", error)
        await send_exception(error, interaction)


class ConfirmEditView(AutoDisableView):
    """
    A :class:`discord.ui.View` for confirming edited transactions. It adds one button 'Save', which will update the
    embeds of the original message according to the edited embeds.

    Attributes
    ----------
    message: discord.Message
        the original message which should be edited.
    """

    def __init__(self, message: Message):
        """
        Creates a new ConfirmEditView.

        :param message: the original message which should be edited.
        """
        super().__init__()
        self.message = message

    @discord.ui.button(label="Speichern", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        await self.message.edit(embeds=interaction.message.embeds)
        await interaction.response.send_message("Transaktion bearbeitet!", ephemeral=True)

    async def on_error(self, error: Exception, item, interaction):
        logger.error(f"Error in ConfirmEditView: {error}", error)
        await send_exception(error, interaction)


class ConfirmOCRView(AutoDisableView):
    def __init__(self, transaction: Transaction, note: str = ""):
        super().__init__()
        self.transaction = transaction
        self.note = note

    @discord.ui.button(label="Senden", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        await send_transaction([self.transaction.create_embed()], interaction, self.note)

    async def on_error(self, error: Exception, item, interaction):
        logger.error(f"Error in ConfirmOCRView: {error}", error)
        await send_exception(error, interaction)


class TransferModal(Modal):
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
        transaction, warnings = await Transaction.from_modal(self, interaction.user.name, interaction.user.id)
        if transaction is None:
            await interaction.response.send_message(warnings, ephemeral=True)
            return
        await interaction.response.send_message(
            warnings, embed=transaction.create_embed(),
            ephemeral=True, view=ConfirmView())

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        logger.error(f"Error on Transfer Modal: {error}", error)
        await send_exception(error, interaction)


class EditModal(TransferModal):
    def __init__(self, message: Message, *args, **kwargs):
        embed = message.embeds[0]
        # noinspection PyTypeChecker
        super().__init__(color=embed.color, default=False, *args, **kwargs)
        for field in embed.fields:
            self.add_item(InputText(label=field.name, required=True, value=field.value))

    async def callback(self, interaction: Interaction):
        transaction, warnings = await Transaction.from_modal(self, interaction.user.name, interaction.user.id)
        if transaction is not None and len(warnings) > 0:
            await interaction.response.send_message(
                warnings, embed=transaction.create_embed(),
                ephemeral=True, view=ConfirmEditView(message=interaction.message))
            return
        if transaction is None:
            await interaction.response.send_message(warnings, ephemeral=True)
            return
        await interaction.message.edit(embed=transaction.create_embed())
        await interaction.response.send_message(f"Transaktionen wurde editiert!\n{warnings}", ephemeral=True)


class ShipyardModal(Modal):
    def __init__(self, color: Color, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.color = color
        self.add_item(InputText(label="Käufer", placeholder="Käufer", required=True))
        self.add_item(InputText(label="Schiff", placeholder="Schiffsname", required=True))
        self.add_item(InputText(label="Preis", placeholder="Gesamtkosten", required=True))
        self.add_item(InputText(label="Davon Stationsgebühren", placeholder="(Klickkosten)", required=True))
        self.add_item(InputText(label="Bauer", placeholder="Manufacturer", required=False))

    async def callback(self, interaction: Interaction):
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

        embeds = []
        transaction_ship = Transaction(
            name_from=buyer,
            name_to="Buyback Program",
            amount=price,
            purpose=f"Kauf {ship}",
            author=interaction.user.name
        )
        embeds.append(transaction_ship.create_embed())

        transaction_fees = Transaction(
            name_from="Buyback Program",
            amount=station_fees,
            purpose=f"Stationsgebühren {ship}",
            author=interaction.user.name
        )
        embeds.append(transaction_fees.create_embed())

        slot_price = min(int(price * 0.02 / 100000) * 100000, 50000000)
        if slot_price < 1000000:
            warnings += f"Warnung: Slotgebühr ist mit {slot_price} zu gering, sie wird nicht eingetragen."
        if builder is not None and slot_price >= 1000000:
            transaction_builder = Transaction(
                name_from="Buyback Program",
                name_to=builder,
                amount=slot_price,
                purpose=f"Slotgebühr {ship}",
                author=interaction.user.name
            )
            embeds.append(transaction_builder.create_embed())

        if len(warnings) > 0:
            await interaction.response.send_message(warnings, embeds=embeds, ephemeral=True, view=ConfirmView())
        else:
            await send_transaction(embeds, interaction, "")

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        logger.error(f"Error on Shipyard Modal: {error}", error)
        await send_exception(error, interaction)
