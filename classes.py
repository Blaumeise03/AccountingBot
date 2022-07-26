import difflib
import logging
import re
from datetime import datetime

import discord
import mariadb
import database
import discord.ext
from discord import Embed, Interaction, Colour, Color
from discord.ui import Modal, View, InputText

import sheet

BOT = None  # type: discord.ext.commands.bot.Bot | None
ACCOUNTING_LOG = None  # type: int | None

connector = None  # type: database.DatabaseConnector | None

admins = []


def set_up(new_connector, new_admins):
    global connector, admins
    connector = new_connector
    admins = new_admins


def get_current_time():
    now = datetime.now()
    return now.strftime("%d.%m.%Y %H:%M")


async def send_transaction(embed, ctx, interaction, note=""):
    msg = await BOT.get_channel(ACCOUNTING_LOG).send(embeds=[embed], view=TransactionView(ctx=ctx))
    try:
        connector.add_transaction(msg.id, interaction.user.id)
    except mariadb.Error as e:
        note += "\nFehler beim Eintragen in die Datenbank, die Transaktion wurde jedoch trotzdem im " \
                "Accountinglog gepostet. Solltest du sie bearbeiten/löschen wollen, " \
                f"informiere bitte einen Admin\n{e}"
    await interaction.response.send_message("Transaktion gesendet!" + note, ephemeral=True)


class AccountingView(View):
    def __init__(self, bot, accounting_log, ctx):
        super().__init__(timeout=None)
        global BOT, ACCOUNTING_LOG
        BOT = bot
        ACCOUNTING_LOG = accounting_log
        self.ctx = ctx

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.blurple)
    async def btn_transfer_callback(self, button, interaction):
        modal = TransferModal(title="Transfer", ctx=self.ctx, color=Color.blue(), modal_type=0)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Einzahlen", style=discord.ButtonStyle.green)
    async def btn_deposit_callback(self, button, interaction):
        modal = TransferModal(title="Einzahlen", ctx=self.ctx, color=Color.green(), modal_type=1)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Auszahlen", style=discord.ButtonStyle.red)
    async def btn_withdraw_callback(self, button, interaction):
        modal = TransferModal(title="Auszahlen", ctx=self.ctx, color=Color.red(), modal_type=2)
        await interaction.response.send_modal(modal)

    async def on_error(self, error: Exception, item, interaction):
        logging.error(f"Error in TransactionView: {error}")
        await interaction.response.send_message(str(error))


class TransactionView(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(label="Löschen", style=discord.ButtonStyle.red)
    async def btn_delete_callback(self, button, interaction):
        (owner, verified) = connector.get_owner(interaction.message.id)
        if not verified and (owner == interaction.user.id or interaction.user.id in admins):
            await interaction.message.delete()
            await interaction.response.send_message("Transaktion Gelöscht!", ephemeral=True)
            connector.delete(interaction.message.id)
        elif not owner == interaction.user.id:
            await interaction.response.send_message("Dies ist nicht deine Transaktion, wenn du ein Admin bist, lösche "
                                                    "die Nachricht bitte eigenständig.", ephemeral=True)
        else:
            await interaction.response.send_message("Bereits verifiziert!", ephemeral=True)

    async def on_error(self, error: Exception, item, interaction):
        logging.error(f"Error in TransactionView: {error}", error)
        await interaction.response.send_message(str(error))


class ConfirmView(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(label="Ja", style=discord.ButtonStyle.green)
    async def btn_confirm_callback(self, button, interaction):
        await send_transaction(interaction.message.embeds[0], self.ctx, interaction)

    async def on_error(self, error: Exception, item, interaction):
        logging.error(f"Error in ConfirmView: {error}", error)
        await interaction.response.send_message(str(error), ephemeral=True)


class TransferModal(Modal):
    def __init__(self, ctx, color: Color, modal_type: int, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.modal_type = modal_type
        self.color = color
        self.ctx = ctx
        if modal_type == 0:
            self.add_item(InputText(label="Von", placeholder="Von", required=True))
            self.add_item(InputText(label="Zu", placeholder="Zu", required=True))
        else:
            self.add_item(InputText(label="Spieler(konto)name", placeholder="z.B. \"KjinaDeNiel\"", required=True))
        self.add_item(InputText(label="Menge", placeholder="Menge", required=True))
        self.add_item(InputText(label="Verwendungszweck", placeholder="Verwendungszweck", required=True))
        self.add_item(InputText(label="Referenz", placeholder="z.B \"voidcoin.app/contract/20577\"", required=False))

    async def callback(self, interaction: Interaction):
        embed = Embed(title=self.title, color=self.color, timestamp=datetime.now())
        if self.modal_type == 0:
            u_from = self.children[0].value.strip()
            u_to = self.children[1].value.strip()
            amount = self.children[2].value.replace(" ", "")
            purpose = self.children[3].value
            if self.children[4].value:
                reference = self.children[4].value
            else:
                reference = None
        else:
            if self.modal_type == 1:
                u_to = self.children[0].value.strip()
                u_from = None
            elif self.modal_type == 2:
                u_from = self.children[0].value.strip()
                u_to = None
            else:
                logging.error(f"Modal Type is not within expected range [0-2], got {self.modal_type}")
                await interaction.response.send_message(
                    f"Error: Modal Type is not within expected range [0-2], got {self.modal_type}")
                return
            amount = self.children[1].value.replace(" ", "")
            purpose = self.children[2].value
            if self.children[3].value:
                reference = self.children[3].value
            else:
                reference = None
        if u_from is not None:
            f_list = difflib.get_close_matches(u_from, sheet.users, 1)
        else:
            f_list = [None]
        if u_to is not None:
            t_list = difflib.get_close_matches(u_to, sheet.users, 1)
        else:
            t_list = [None]

        if len(f_list) > 0 and len(t_list) > 0:
            if self.modal_type == 0:
                f = str(f_list[0])
                t = str(t_list[0])
                embed.add_field(name="Von:", value=f)
                embed.add_field(name="Zu:", value=t)
            else:
                if u_from is not None:
                    f = str(f_list[0])
                    embed.add_field(name="Von:", value=f)
                else:
                    f = ""
                if u_to is not None:
                    t = str(t_list[0])
                    embed.add_field(name="Zu:", value=t)
                else:
                    t = ""
        else:
            await interaction.response.send_message(f"Namen konnten nicht gefunden werden: Von: {f_list} Zu: {t_list}",
                                                    ephemeral=True)
            return

        note = ""
        if ("," in amount) or ("." in amount):
            note = "\nHinweis: Es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach dem Format " \
                   "\"1,000,000.00 ISK\" geparsed."
        if bool(re.match(r"[0-9]+(,[0-9]+)*(\.[0-9]+)?[a-zA-Z]*", amount)):
            amount = re.sub(r"[,a-zA-Z]", "", amount).split(".", 1)[0]
            amount_int = int(amount)
            embed.add_field(name="Menge:", value="{:,} ISK".format(amount_int))
        else:
            await interaction.response.send_message(f"Eingabe \"{amount}\" ist weder eine Zahl, noch entspricht sie dem Format \"1,000,000.00 ISK\"!")
            return
            # embed.add_field(name="Menge:", value=self.children[2].value)
        embed.add_field(name="Verwendungszweck:", value=purpose)

        if reference is not None:
            embed.add_field(name="Referenz:", value=reference)
        embed.set_footer(text=interaction.user.name)
        if (u_from is not None and u_from.casefold() != f.casefold()) or (u_to is not None and u_to.casefold() != t.casefold()):
            await interaction.response.send_message(f"Meintest du '{f}' und '{t}'? Ansonsten wiederhole bitte deine Eingabe.", embeds=[embed], ephemeral=True, view=ConfirmView(ctx=self.ctx))
        else:
            await send_transaction(embed, self.ctx, interaction, note)

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        logging.error(f"Error on Transaction Modal: {error}", error)
        await interaction.response.send_message(str(error))


class MenuEmbedInternal(Embed):
    def __init__(self):
        super().__init__(color=Colour.red(), title="Interner Handel")
        self.add_field(name="(zwischen Spielern)", value="Für Handel zwischen zwei Spielern bitte \"Transfer\" "
                                                         "verwenden. Zum Ein/Auszahlen bitte den jeweiligen Knopf "
                                                         "nutzen.\n\n**Hinweis:** Die Zahl entweder als reine Zahl "
                                                         "(z.B.\"1000000\") oder im Format \"1,000,000.00 ISK\" "
                                                         "oder ähnlich eingeben. Kommas werden als Tausender-Seperator "
                                                         "erkannt, Buchstaben werden gelöscht.")


class MenuEmbedExternal(Embed):
    def __init__(self):
        super().__init__(color=Colour.red(), title="VoidCoins")
        self.add_field(name="VC-Verträge", value="Wenn ihr über VOID gehandelt habt, bitte ebenfalls die "
                                                 "\"Einzahlen/Auszahlen\"-Knöpfe nehmen. Wenn ihr VC erhalten habt "
                                                 "(z.B. SRP) \"Einzahlen\", wenn ihr VC ausgegeben habt "
                                                 "(z.B. Shipyard Kauf) \"Auszahlen\"\n\n"
                                                 "Den Vertraglink bitte im Feld \"Referenz\" eintragen."
                       )


class MenuEmbedVCB(Embed):
    def __init__(self):
        super().__init__(color=Colour.red(), title="VCB Kontodaten")
        self.add_field(name="Link", value="https://voidcoin.app/pilot/1421", inline=True)
        self.add_field(name="Kontoname", value="[V2] Massive Dynamic LLC", inline=True)


class InduRoleMenu(Embed):
    def __init__(self):
        super().__init__(color=Colour.green(), title="Industrierollen")
        self.add_field(name="Schiffbauskills", value="<:Frigate:974611741633806376> Frigates\n"
                                                     "<:Destroyer:974611810453958699> Destroyer\n"
                                                     "<:Cruiser:974611846566936576> Cruiser\n"
                                                     "<:BC:974611889982173193> Battlecruiser\n"
                                                     "<:BS:974611977626329139> Battleship\n"
                                                     "<:Industrial:974612368061517824> Industrial\n", inline=True)
        self.add_field(name="Sonstiges", value=":regional_indicator_n: Nanocores\n"
                                               ":regional_indicator_b: B-Type Module\n"
                                               # "<:Freighter:974612564707274752> Hauling-Service\n"
                                               ":regional_indicator_f: Schiff-(Fitting)-Service")


def get_embeds():
    return [
        MenuEmbedInternal(),
        MenuEmbedExternal(),
        MenuEmbedVCB()
    ]
