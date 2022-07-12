import re
from datetime import datetime

import discord
from discord import Embed, Interaction, Colour, Color
from discord.ui import Modal, View, InputText

BOT = None
ACCOUNTING_LOG = None


class AccountingView(View):
    def __init__(self, bot, accounting_log, ctx):
        super().__init__(timeout=None)
        global BOT, ACCOUNTING_LOG
        BOT = bot
        ACCOUNTING_LOG = accounting_log
        self.ctx = ctx

    @discord.ui.button(label="Transfer", style=discord.ButtonStyle.blurple, custom_id="a_btn_1")
    async def btn_transfer_callback(self, button, interaction):
        modal = TransferModal(title="Transfer", ctx=self.ctx, color=Color.blue())
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Einzahlen", style=discord.ButtonStyle.green, custom_id="a_btn_2")
    async def btn_deposit_callback(self, button, interaction):
        modal = ExternalModal(title="Einzahlen", ctx=self.ctx, color=Color.green())
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Auszahlen", style=discord.ButtonStyle.red, custom_id="a_btn_3")
    async def btn_withdraw_callback(self, button, interaction):
        modal = ExternalModal(title="Auszahlen", ctx=self.ctx, color=Color.red())
        await interaction.response.send_modal(modal)

    async def on_timeout(self):
        print("timeout")

    async def on_error(self, error: Exception, item, interaction):
        print(error)
        await interaction.response.send_message(str(error))


class TransferModal(Modal):
    def __init__(self, ctx, color: Color, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.color = color
        self.ctx = ctx
        self.add_item(InputText(label="Von", placeholder="Von", required=True))
        self.add_item(InputText(label="Zu", placeholder="Zu", required=True))
        self.add_item(InputText(label="Menge", placeholder="Menge", required=True))
        self.add_item(InputText(label="Verwendungszweck", placeholder="Verwendungszweck", required=True))

    async def callback(self, interaction: Interaction):
        embed = Embed(title="Transfer", color=self.color, timestamp=datetime.now())
        embed.add_field(name="Von:", value=self.children[0].value)
        embed.add_field(name="Zu:", value=self.children[1].value)
        amount = self.children[2].value.replace(" ", "")
        note = ""
        if ("," in amount) or ("." in amount):
            note = "\nAchtung, es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach dem Format " \
                   "\"1,000,000.00 ISK\" geparsed."
        if bool(re.match(r"[0-9]+(,[0-9]+)*(\.[0-9]+)?[a-zA-Z]*", amount)):
            amount = re.sub(r"[,a-zA-Z]", "", amount).split(".", 1)[0]
            embed.add_field(name="Menge:", value="{:,} ISK".format(int(amount)))
        else:
            embed.add_field(name="Menge:", value=self.children[2].value)
        embed.add_field(name="Verwendungszweck:", value=self.children[3].value)
        embed.set_footer(text=interaction.user.name)
        await BOT.get_channel(ACCOUNTING_LOG).send(embeds=[embed])
        await interaction.response.send_message("Transaktion gesendet!" + note, ephemeral=True)

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        print(error)
        await interaction.response.send_message(str(error))


class ExternalModal(Modal):
    def __init__(self, ctx, color: Color, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.color = color
        self.ctx = ctx
        self.add_item(InputText(label="Spieler(konto)name", placeholder="z.B. \"KjinaDeNiel\"", required=True))
        self.add_item(InputText(label="Menge", placeholder="Menge", required=True))
        self.add_item(InputText(label="Verwendungszweck", placeholder="z.B \"Einzahlung\"", required=True))
        self.add_item(InputText(label="Referenz", placeholder="z.B \"voidcoin.app/contract/20577\"", required=False))

    async def callback(self, interaction: Interaction):
        embed = Embed(title=self.title, color=self.color, timestamp=datetime.now())
        embed.add_field(name="Konto:", value=self.children[0].value)
        amount = self.children[1].value.replace(" ", "")
        note = ""
        if ("," in amount) or ("." in amount):
            note = "\nAchtung, es wurden Punkte und/oder Kommas erkannt, die Zahl wird automatisch nach dem Format " \
                   "\"1,000,000.00 ISK\" geparsed."
        if bool(re.match(r"[0-9]+(,[0-9]+)*(\.[0-9]+)?[a-zA-Z]*", amount)):
            amount = re.sub(r"[,a-zA-Z]", "", amount).split(".", 1)[0]
            embed.add_field(name="Menge:", value="{:,} ISK".format(int(amount)))
        else:
            note = note + "\nAchtung: Eingabe \"" + amount + "\" konnte nicht geparsed werden, die originale Eingabe " \
                                                             "wird weitergeleitet."
            embed.add_field(name="Menge:", value=self.children[1].value)
        embed.add_field(name="Verwendungszweck:", value=self.children[2].value)
        if self.children[3].value:
            embed.add_field(name="Referenz:", value=self.children[3].value)
        else:
            embed.add_field(name="Referenz:", value="-")
        embed.set_footer(text=interaction.user.name)
        await BOT.get_channel(ACCOUNTING_LOG).send(embeds=[embed])
        await interaction.response.send_message("Transaktion gesendet!" + note, ephemeral=True)

    async def on_error(self, error: Exception, interaction: Interaction) -> None:
        print(error)
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
                                               "<:Freighter:974612564707274752> Hauling-Service\n"
                                               ":tools: Schiff-(Fitting)-Service")


def get_embeds():
    return [
        MenuEmbedInternal(),
        MenuEmbedExternal(),
        MenuEmbedVCB()
    ]
