# PluginConfig
# Name: HelpCommand
# Author: Blaumeise03
# Depends-On: []
# Localization: help_command_lang.xml
# End
import logging
from typing import Union, Callable, Optional

import discord
from discord import ApplicationContext, SlashCommand, ContextMenuCommand, AutocompleteContext, \
    MessageCommand, UserCommand, option
from discord.ext import commands
from discord.ext.commands import Command

from accounting_bot import utils
from accounting_bot.localization import t_
from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import CmdAnnotation

logger = logging.getLogger("test.plugin_test")


class HelpPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)

    def on_load(self):
        self.register_cog(HelpCommand(self.bot))


def get_cmd_help(cmd: Union[Callable, Command], opt: str = None, long=False, fallback=None):
    if isinstance(cmd, (Command, SlashCommand, ContextMenuCommand)):
        cmd_name = utils.get_cmd_name(cmd).replace(" ", "_")
    else:
        return fallback

    result = None
    # ToDo: Improve localisation for plugins
    extra = "_long" if long else ""
    if opt is None:
        result = t_(f"help_{cmd_name}{extra}")
        if result is None:
            result = t_(f"help_{cmd_name}", fallback=cmd.description)
    if result is None and opt is not None:
        result = t_(f"help_{cmd_name}_{opt}{extra}")
        if result is None:
            result = t_(f"help_{cmd_name}_{opt}")
        if result is None:
            result = t_(f"opt_{opt}{extra}")
        if result is None:
            result = t_(f"opt_{opt}")
    if result is None:
        return fallback
    return result


class HelpCommand(commands.Cog):
    def __init__(self, bot: AccountingBot):
        self.bot = bot

    def commands_autocomplete(self, ctx: AutocompleteContext):
        cmds = []
        for name, cog in self.bot.cogs.items():
            cmds.append(name)
            for cmd in cog.walk_commands():
                cmds.append(f"{utils.get_cmd_name(cmd)}")
        for cmd in self.bot.commands:
            cmds.append(f"{utils.get_cmd_name(cmd)}".strip())
        return filter(lambda n: ctx.value is None or n.casefold().startswith(ctx.value.casefold().strip()), cmds)

    @staticmethod
    def get_general_embed(bot: commands.Bot):
        emb = discord.Embed(title=t_("help"), color=discord.Color.red(),
                            description=t_("emb_help_general_desc"))
        for name, cog in bot.cogs.items():  # type: str, commands.Cog
            cmd_desc = ""
            for cmd in cog.walk_commands():
                desc = get_cmd_help(cmd, fallback=cmd.description)
                cmd_desc += f"`{utils.get_cmd_name(cmd)}`: {desc}\n"
            emb.add_field(name=name, value=cmd_desc, inline=False)
        cmd_desc = ""
        for cmd in bot.walk_commands():
            if not cmd.cog_name and not cmd.hidden:
                desc = get_cmd_help(cmd, fallback=cmd.description)
                cmd_desc += f"{utils.get_cmd_name(cmd)} - {desc}\n"
        if cmd_desc:
            emb.add_field(name=t_("other_cmds"), value=cmd_desc)
        return emb

    @staticmethod
    def get_cog_embed(cog: commands.Cog):
        emb = discord.Embed(title=t_("help_about").format(cog.__cog_name__), color=discord.Color.red(),
                            description=t_("emb_help_cog_desc"))
        for cmd in cog.walk_commands():
            cmd_name = utils.get_cmd_name(cmd)
            cmd_desc = get_cmd_help(cmd, fallback=cmd.description)
            cmd_details = CmdAnnotation.get_cmd_details(cmd.callback)
            extra = ""
            if isinstance(cmd, ContextMenuCommand):
                extra = t_("ctx_command") + ". "
            if cmd_details is not None:
                cmd_desc = f"*{cmd_details}*\n{extra}{cmd_desc}\n"
            if isinstance(cmd, SlashCommand):
                if len(cmd.options) > 0:
                    cmd_desc += f"\n*{t_('parameter')}*:\n"
                for opt in cmd.options:
                    # noinspection PyUnresolvedReferences
                    cmd_desc += f"`{'[' if opt.required else '<'}{opt.name}: {opt.input_type.name}" \
                                f"{']' if opt.required else '>'}`: " \
                                f"{get_cmd_help(cmd, opt.name, fallback=opt.description)}\n"
            emb.add_field(name=f"**{cmd_name}**", value=cmd_desc, inline=False)
        return emb

    @staticmethod
    def get_command_embed(command: commands.Command):
        description = get_cmd_help(command, long=True, fallback=command.description)
        if description is None or len(description) == 0:
            description = t_("no_desc_available")
        cmd_details = CmdAnnotation.get_cmd_details(command.callback)
        if cmd_details is not None:
            description = f"*{t_('restrictions')}*: *{cmd_details}*\n{description}"
        emb = discord.Embed(title=t_("help_about").format(utils.get_cmd_name(command)), color=discord.Color.red(),
                            description=description)
        if isinstance(command, MessageCommand):
            description += "\n" + t_("ctx_command_info").format(t_("message"))
        elif isinstance(command, UserCommand):
            description += "\n" + t_("ctx_command_info").format(t_("user"))
        if isinstance(command, SlashCommand):
            if len(command.options) > 0:
                description += f"\n\n**{t_('parameter')}**:"
            for opt in command.options:
                # noinspection PyUnresolvedReferences
                emb.add_field(name=opt.name,
                              value=f"({t_('optional') if not opt.required else t_('required')}):"
                                    f" `{opt.input_type.name}`\n"
                                    f"Default: `{str(opt.default)}`\n"
                                    f"{get_cmd_help(command, opt.name, fallback=opt.description)}",
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
            if f"{utils.get_cmd_name(cmd)}".casefold() == selection.casefold():
                command = cmd
                break
        if command is None:
            for cog in bot.cogs.values():
                for cmd in cog.walk_commands():
                    if f"{utils.get_cmd_name(cmd)}".casefold() == selection.casefold():
                        command = cmd
                        break
                if command is not None:
                    break
        if command is not None:
            return HelpCommand.get_command_embed(command)
        return discord.Embed(title=t_("help"), color=discord.Color.red(),
                             description=t_("cmd_not_found").format(selection=selection))

    @commands.slash_command(name="help", description="Help-Command")
    @option(name="selection", description="The command/module to get help about", type=str, required=False,
            autocomplete=commands_autocomplete)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True,
            autocomplete=commands_autocomplete)
    @option(name="edit_msg", description="Edit this message and update the embed", type=str, required=False,
            default=None)
    async def cmd_help(self, ctx: ApplicationContext,
                       selection: str, silent: bool, edit_msg: str):
        emb = HelpCommand.get_help_embed(self.bot, selection)
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