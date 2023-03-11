import io
import logging
from typing import TYPE_CHECKING, Optional, Union

import discord
from discord import Option, User, ApplicationContext, AutocompleteContext, option, Role, SlashCommand, SlashCommandGroup
from discord.ext import commands
from discord.ext.commands import Context

from accounting_bot import accounting, sheet, utils
from accounting_bot.accounting import AccountingView
from accounting_bot.config import Config
from accounting_bot.database import DatabaseConnector
from accounting_bot.universe import data_utils
from accounting_bot.universe.pi_planer import PiPlaner, PiPlanningSession, PiPlanningView
from accounting_bot.utils import State, get_cmd_name

if TYPE_CHECKING:
    from bot import BotState

logger = logging.getLogger("bot.commands")


def main_char_autocomplete(self: AutocompleteContext):
    return filter(lambda n: self.value is None or n.startswith(self.value.strip()), utils.main_chars)


class HelpCommand(commands.Cog):
    def __init__(self, state: 'BotState'):
        self.state = state

    def commands_autocomplete(self, ctx: AutocompleteContext):
        cmds = []
        for name, cog in self.state.bot.cogs.items():
            cmds.append(name)
            for cmd in cog.walk_commands():
                cmds.append(f"{get_cmd_name(cmd)}")
        for cmd in self.state.bot.commands:
            cmds.append(f"{get_cmd_name(cmd)}".strip())
        return filter(lambda n: ctx.value is None or n.casefold().startswith(ctx.value.casefold().strip()), cmds)

    @staticmethod
    def get_general_embed(bot: commands.Bot):
        emb = discord.Embed(title="Hilfe", color=discord.Color.red(),
                            description="Gib `/help <Befehl>` oder `/help <Modul>` ein für weitere Informationen")
        for name, cog in bot.cogs.items():  # type: str, commands.Cog
            cmd_desc = ""
            for cmd in cog.walk_commands():
                cmd_desc += f"`{get_cmd_name(cmd)}`: {cmd.description}\n"
            emb.add_field(name=name, value=cmd_desc, inline=False)
        cmd_desc = ""
        for cmd in bot.walk_commands():
            if not cmd.cog_name and not cmd.hidden:
                cmd_desc += f"{get_cmd_name(cmd)} - {cmd.description}\n"
        if cmd_desc:
            emb.add_field(name="Andere Befehle", value=cmd_desc)
        return emb

    @staticmethod
    def get_cog_embed(cog: commands.Cog):
        emb = discord.Embed(title=f"Hilfe zu {cog.__cog_name__}", color=discord.Color.red(),
                            description="Gib `/help <Befehl>` oder `/help <Modul>` ein für weitere Informationen.\n"
                                        "Bei den Befehlsdetails werden optionale Parameter mit `<Name: Type>` und "
                                        "verpflichtende Parameter mit `[Name: Type]` gekennzeichnet.\n\n"
                                        "Verfügbare Befehle:")
        for cmd in cog.walk_commands():
            cmd_name = get_cmd_name(cmd)
            cmd_desc = cmd.description if cmd.description is not None and len(cmd.description) > 0 else "N/A"
            if isinstance(cmd, SlashCommand):
                if len(cmd.options) > 0:
                    cmd_desc += " **Parameter**:\n"
                for opt in cmd.options:
                    # noinspection PyUnresolvedReferences
                    cmd_desc += f"`{'[' if opt.required else '<'}{opt.name}: {opt.input_type.name}" \
                                f"{']' if opt.required else '>'}`: {opt.description}\n"
            emb.add_field(name=cmd_name, value=cmd_desc, inline=False)
        return emb

    @staticmethod
    def get_command_embed(command: commands.Command):
        description = "Keine Beschreibung verfügbar"
        if command.description is not None and len(command.description) > 0:
            description = command.description
        emb = discord.Embed(title=f"Hilfe zu `{get_cmd_name(command)}`", color=discord.Color.red(),
                            description=description)
        if isinstance(command, SlashCommand):
            if len(command.options) > 0:
                description += "\nParameter:"
            for opt in command.options:
                # noinspection PyUnresolvedReferences
                emb.add_field(name=opt.name,
                              value=f"{'(Optional)' if not opt.required else '(Benötigt)'}: `{opt.input_type.name}`\n"
                                    f"Default: `{str(opt.default)}`\n{opt.description}",
                              inline=False)
        emb.description = description
        return emb

    @staticmethod
    def get_help_embed(bot: commands.Bot, selection: Optional[str] = None):
        if selection is None:
            return HelpCommand.get_general_embed(bot)
        selection = selection.strip()
        if selection in bot.cogs:
            cog = bot.cogs[selection]
            return HelpCommand.get_cog_embed(cog)
        command = None
        for cmd in bot.walk_commands():
            if f"{get_cmd_name(cmd)}".casefold() == selection.casefold():
                command = cmd
                break
        if command is None:
            for cog in bot.cogs.values():
                for cmd in cog.walk_commands():
                    if f"{get_cmd_name(cmd)}".casefold() == selection.casefold():
                        command = cmd
                        break
                if command is not None:
                    break
        if command is not None:
            return HelpCommand.get_command_embed(command)
        return discord.Embed(title=f"Hilfe", color=discord.Color.red(),
                             description=f"Befehl/Modul `{selection}` nicht gefunden. Gib `/help` ein um eine Liste "
                                         f"aller Befehle zu sehen.")

    @commands.slash_command(name="help", description="Help-Command")
    @option(name="selection", description="The command/module to get help about.", type=str, required=False,
            autocomplete=commands_autocomplete)
    @option(name="silent", description="Execute the command publicly.", type=bool, required=False, default=True,
            autocomplete=commands_autocomplete)
    @option(name="edit_msg", description="Edit this message and update the embed", type=str, required=False,
            default=None)
    async def help(self, ctx: ApplicationContext,
                   selection: str, silent: bool, edit_msg: str
                   ):
        emb = HelpCommand.get_help_embed(self.state.bot, selection)
        if edit_msg is not None:
            try:
                edit_msg = int(edit_msg.strip())
            except ValueError as e:
                await ctx.response.send_message(f"Message id `'{edit_msg}'` is not a number:\n"
                                                f"{str(e)}.", ephemeral=silent)
                return
            await ctx.response.defer(ephemeral=silent)
            try:
                msg = await ctx.channel.fetch_message(edit_msg)
                await msg.edit(embed=emb)
                await ctx.followup.send("Message edited", ephemeral=silent)
                return
            except discord.NotFound:
                await ctx.followup.send("Message not found in current channel", ephemeral=silent)
                return
        await ctx.response.send_message(embed=emb, ephemeral=silent)


class BaseCommands(commands.Cog):
    def __init__(self, config: Config, connector: DatabaseConnector, state: 'BotState'):
        self.config = config
        self.guild = config["server"]
        self.admins = config["admins"]
        self.owner = config["owner"]
        self.connector = connector
        self.state = state

    def has_permissions(self, ctx: ApplicationContext):
        return (ctx.guild and self.guild == ctx.guild.id and ctx.user.guild_permissions.administrator) \
            or ctx.user.id in self.admins or ctx.user.id == self.owner

    @commands.slash_command(description="Creates the main menu for the bot and sets all required settings.")
    async def setup(self, ctx):
        logging.info("Setup command called by user " + str(ctx.author.id))
        if ctx.guild is None:
            await ctx.respond("Can only be executed inside a guild")
            return
        if ctx.guild.id != self.config["server"] and ctx.author.id != self.config["owner"]:
            await ctx.respond("Wrong server", ephemeral=True)
            return

        if ctx.author.guild_permissions.administrator or \
                ctx.author.id in self.config["admins"] or \
                ctx.author.id == self.config["owner"]:
            # Running setup
            logger.info("User verified for setup-command, starting setup...")
            view = AccountingView()
            msg = await ctx.send(view=view, embeds=accounting.get_menu_embeds())
            logger.info("Send menu message with id " + str(msg.id))
            self.config["menuMessage"] = msg.id
            self.config["menuChannel"] = ctx.channel.id
            self.config["server"] = ctx.guild.id
            self.config.save_config()
            logger.info("Setup completed.")
            await ctx.respond("Saved config", ephemeral=True)
        else:
            logger.info(f"User {ctx.author.id} is missing permissions to run the setup command")
            await ctx.respond("Missing permissions", ephemeral=True)

    @commands.slash_command(
        name="setlogchannel",
        description="Sets the current channel as the accounting log channel.")
    async def set_log_channel(self, ctx):
        logger.info("SetLogChannel command received.")
        if ctx.guild is None:
            logger.info("Command was send via DM!")
            await ctx.respond("Only available inside a guild")
            return
        if ctx.guild.id != self.config["server"]:
            logger.info("Wrong server!")
            await ctx.respond("Can only used inside the defined discord server", ephemeral=True)
            return

        if ctx.author.id == self.config["owner"] or ctx.author.guild_permissions.administrator:
            logger.info("User Verified. Setting up channel...")
            self.config["logChannel"] = ctx.channel.id
            self.config.save_config()
            logger.info("Channel changed!")
            await ctx.respond("Log channel set to this channel (" + str(self.config["logChannel"]) + ")")
        else:
            logger.info(f"User {ctx.author.id} is missing permissions to run the setlogchannel command")
            await ctx.respond("Missing permissions", ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(description="Creates a new shortcut menu containing all buttons.")
    async def createshortcut(self, ctx):
        if ctx.guild is None:
            await ctx.respond("Can only be executed inside a guild")
            return
        if ctx.guild.id != self.guild and ctx.author.id != self.owner:
            logging.info("Wrong server!")
            await ctx.respond("Wrong server", ephemeral=True)
            return

        if ctx.author.guild_permissions.administrator or ctx.author.id in self.admins or ctx.author.id == self.owner:
            view = AccountingView()
            msg = await ctx.send(view=view, embed=accounting.EMBED_MENU_SHORTCUT)
            self.connector.add_shortcut(msg.id, ctx.channel.id)
            await ctx.respond("Shortcut menu posted", ephemeral=True)
        else:
            logging.info(f"User {ctx.author.id} is missing permissions to run the createshortcut command")
            await ctx.respond("Missing permissions", ephemeral=True)

    @commands.slash_command(
        name="balance",
        description="Get your current accounting balance."
    )
    async def get_balance(self, ctx: ApplicationContext,
                          force: Option(bool, "Force sheet reload", required=False, default=False),
                          user: Option(User, "The user to look up", required=False, default=None)):
        await ctx.defer(ephemeral=True)
        await sheet.load_wallets(force)
        if not user:
            user_id = ctx.user.id
        else:
            user_id = user.id

        name, _, _ = utils.get_main_account(discord_id=user_id)
        if name is None:
            await ctx.followup.send("This discord account is not connected to any ingame account!", ephemeral=True)
            return
        name = sheet.check_name_overwrites(name)
        balance = await sheet.get_balance(name)
        investments = await sheet.get_investments(name, default=0)
        if balance is None:
            await ctx.followup.send("Konto nicht gefunden!", ephemeral=True)
            return
        await ctx.followup.send("Der Kontostand von {} beträgt `{:,} ISK`.\nDie Projekteinlagen betragen `{:,} ISK`"
                                .format(name, balance, investments), ephemeral=True)

    @commands.slash_command(
        name="registeruser",
        description="Registers a user to a discord ID"
    )
    @option("ingame_name", description="The main character name of the user", required=True,
            autocomplete=main_char_autocomplete)
    @option("user", description="The user to register", required=True)
    async def register_user(self, ctx: ApplicationContext, ingame_name: str, user: User):
        if not self.has_permissions(ctx):
            await ctx.respond("You don't have the permission to use this command.", ephemeral=True)
            return
        if user is None:
            await ctx.respond("Either a user is required.", ephemeral=True)
            return
        user_id = user.id
        if ingame_name is None or ingame_name == "":
            await ctx.respond("Ingame name is required!", ephemeral=True)
            return
        matched_name, _, _ = utils.get_main_account(ingame_name)

        if matched_name is not None:
            old_id = utils.get_discord_id(matched_name)
            utils.save_discord_id(matched_name, int(user_id))
            logger.info("(%s) Saved discord id %s to player %s, old id %s", ctx.user.id, user_id, matched_name, old_id)
            await ctx.response.send_message(
                f"Spieler `{matched_name}` wurde zur ID `{user_id}` (<@{user_id}>) eingespeichert!\n" +
                ("" if not old_id else f"Die alte ID war `{old_id}` (<@{old_id}>)."),
                ephemeral=True)
        else:
            await ctx.response.send_message(f"Fehler, Spieler {ingame_name} nicht gefunden!", ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(
        name="listunregusers",
        description="Lists all unregistered users of the discord"
    )
    @option("role", description="The role to check", required=True)
    async def find_unregistered_users(self, ctx: ApplicationContext, role: Role):
        if not self.has_permissions(ctx):
            await ctx.respond("You don't have the permission to use this command.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        users = await ctx.guild \
            .fetch_members() \
            .filter(lambda m: m.get_role(role.id) is not None) \
            .map(lambda m: (m.nick if m.nick is not None else m.name, m)) \
            .flatten()
        unreg_users = []
        old_users = []
        for name, user in users:  # type: str, discord.Member
            if user.id not in utils.discord_users.values():
                unreg_users.append(user)
            elif utils.get_main_account(discord_id=user.id)[0] not in utils.main_chars:
                old_users.append((utils.get_main_account(discord_id=user.id)[0], user))

        msg = f"Found {len(unreg_users)} unregistered users that have the specified role.\n"
        for user in unreg_users:
            msg += f"<@{user.id}> ({user.name})\n"
            if len(msg) > 1900:
                msg += "**Truncated**\n"
                break
        if len(old_users) > 0:
            msg += f"Found {len(old_users)} users that have no active (main) character inside the corp.\n"
            for name, user in old_users:
                msg += f"<@{user.id}> ({user.name}): Ingame: {name}\n"
                if len(msg) > 1900:
                    msg += "**Truncated**\n"
                    break
        await ctx.followup.send(msg, ephemeral=True)

    # noinspection SpellCheckingInspection
    @commands.slash_command(description="Posts a menu with all available manufacturing roles.")
    async def indumenu(self, ctx, msg: Option(str, "Message ID", required=False, default=None)):
        if msg is None:
            logger.info("Sending role menu...")
            await ctx.send(embeds=[accounting.EMBED_INDU_MENU])
            await ctx.respond("Neues Menü gesendet.", ephemeral=True)
        else:
            logger.info("Updating role menu " + str(msg))
            msg = await ctx.channel.fetch_message(int(msg))
            await msg.edit(embeds=[accounting.EMBED_INDU_MENU])
            await ctx.respond("Menü geupdated.", ephemeral=True)

    @commands.slash_command(description="Shuts down the discord bot, if set up properly, it will restart.")
    async def stop(self, ctx: ApplicationContext):
        if ctx.user.id == self.owner:
            logger.critical("Shutdown Command received, shutting down bot in 10 seconds")
            await ctx.respond("Bot wird in 10 Sekunden gestoppt...")
            self.state.state = State.terminated
            await utils.terminate_bot(connector=self.connector)
        else:
            await ctx.respond("Fehler! Berechtigungen fehlen.", ephemeral=True)


class UniverseCommands(commands.Cog):
    def __init__(self, state: 'BotState'):
        self.state = state

    cmd_pi = SlashCommandGroup(name="pi", description="Access planetary production data.")

    @cmd_pi.command(name="stats", description="View statistical data for pi in a selected constellation.")
    @option(name="const", description="Target Constellation", type=str, required=True)
    @option(name="resources", description="List of pi, seperated by ';'.", type=str, required=False)
    @option(name="compare_regions",
            description="List of regions, seperated by ';' to compare the selected constellation with.",
            type=str, required=False)
    @option(name="vertical", description="Create a vertical boxplot (default false)",
            default=False, required=False)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly.",
            default=True, required=False)
    async def cmd_const_stats(self, ctx: ApplicationContext, const: str, resources: str, compare_regions: str,
                              vertical: bool, silent: bool):
        await ctx.response.defer(ephemeral=silent)
        resource_names = utils.str_to_list(resources, ";")
        region_names = utils.str_to_list(compare_regions, ";")

        figure, n = await data_utils.create_pi_boxplot_async(const, resource_names, region_names, vertical)
        img_binary = await data_utils.create_image(figure,
                                                   height=max(n * 45, 500) + 80 if vertical else 500,
                                                   width=700 if vertical else max(n * 45, 500))
        arr = io.BytesIO(img_binary)
        arr.seek(0)
        file = discord.File(arr, "image.jpeg")
        await ctx.followup.send(f"PI Analyse für {const} abgeschlossen:", file=file, ephemeral=silent)

    @cmd_pi.command(name="find", description="Find a list with the best planets for selected pi.")
    @option(name="const_sys", description="Target Constellation or origin system", type=str, required=True)
    @option(name="resource", description="Name of pi to search", type=str, required=True)
    @option(name="distance", description="Distance from origin system to look up",
            type=int, min_value=0, max_value=30, required=False, default=None)
    @option(name="amount", description="Number of planets to return", type=int, required=False)
    @option(name="silent", description="Default false, if set to true, the command will be executed publicly",
            default=True, required=False)
    async def cmd_find_pi(self, ctx: ApplicationContext, const_sys: str, resource: str, distance: int, amount: int, silent: bool):
        await ctx.response.defer(ephemeral=True)
        resource = resource.strip()
        const = await data_utils.get_constellation(const_sys)
        has_sys = False
        if const is not None:
            result = await data_utils.get_best_pi_planets(const.name, resource, amount)
            title = f"{resource} in {const_sys}"
        else:
            sys = await data_utils.get_system(const_sys)
            if sys is None:
                await ctx.followup.send(f"\"{const_sys}\" is not a system/constellation.", ephemeral=silent)
                return
            if distance is None:
                await ctx.followup.send(f"An distance of jumps from the selected system is required.", ephemeral=silent)
                return
            result = await data_utils.get_best_pi_by_planet(sys.name, distance, resource, amount)
            title = f"{resource} near {const_sys}"
            has_sys = True
        result = sorted(result, key=lambda r: r["out"], reverse=True)
        msg = "Output in units per factory per hour\n```"
        msg += f"{'Planet':<12}: {'Output':<6}" + (f"  Jumps\n" if has_sys else "\n")
        for res in result:
            msg += f"\n{res['p_name']:<12}: {res['out']:6.2f}" + (f"  {res['distance']}j" if has_sys else "")
            if len(msg) > 3900:
                msg += "\n**(Truncated)**"
                break
        msg += "\n```"
        emb = discord.Embed(title=title, color=discord.Color.green(),
                            description="Kein Planet gefunden/ungültige Eingabe" if len(result) == 0 else msg)
        await ctx.followup.send(embed=emb, ephemeral=silent)

    @cmd_pi.command(name="planer", description="Open the pi planer to manage your planets")
    async def cmd_pi_plan(self, ctx: ApplicationContext):
        plan = PiPlanningSession(ctx.user)
        await plan.load_plans()
        msg = await ctx.user.send(
            f"Du hast aktuell {len(plan.plans)} aktive Pi Pläne:",
            embeds=plan.get_embeds(),
            view=PiPlanningView(plan))
        plan.message = msg
        await ctx.response.send_message("Überprüfe deine Direktnachrichten", ephemeral=True)

    @commands.command(name="pi", hidden=True)
    @commands.dm_only()
    async def cmd_dm_pi_plan(self, ctx: Context):
        plan = PiPlanningSession(ctx.author)
        await plan.load_plans()
        msg = await ctx.send(
            f"Du hast aktuell {len(plan.plans)} aktive Pi Pläne:",
            embeds=plan.get_embeds(),
            view=PiPlanningView(plan))
        plan.message = msg
