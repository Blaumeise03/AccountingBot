# PluginConfig
# Name: EmbedPlugin
# Author: Blaumeise03
# Depends-On: []
# End
import json
import logging
import ntpath
import os
import re
from os import PathLike
from typing import Dict, Union, List

import discord
from discord import Embed, Color, Interaction, InputTextStyle, SlashCommandGroup, ApplicationContext, EmbedField, \
    option, TextChannel
from discord.embeds import EmptyEmbed
from discord.ext import commands

from accounting_bot.main_bot import BotPlugin, AccountingBot, PluginWrapper
from accounting_bot.utils import AutoDisableView, admin_only
from accounting_bot.utils.ui import ModalForm

logger = logging.getLogger("ext.embeds")
re_file = re.compile(r"[a-zA-Z0-9-_]+")

if not os.path.exists("resources/embeds/custom"):
    os.mkdir("resources/embeds/custom")


def load_embeds(file_path: str) -> Dict[str, Embed]:
    embeds = {}
    if not os.path.exists(file_path):
        return embeds
    with open(file_path, "r", encoding="utf-8") as embed_file:
        raw = json.load(embed_file)
        logger.info("Loading %s embeds from %s", file_path, len(raw))
        for key, value in raw.items():
            if type(value) == str:
                embeds[key] = value
            else:
                embeds[key] = Embed.from_dict(value)
    return embeds


def save_new_embeds(file_path: str, embeds: Dict[str, Embed | None]):
    all_embeds = load_embeds(file_path)
    for k, v in embeds.items():
        all_embeds[k] = v
    raw = {}
    for k, v in all_embeds.items():
        raw[k] = v.to_dict() if v is not None else None
    with open(file_path, "w", encoding="utf-8") as embed_file:
        json.dump(raw, embed_file, ensure_ascii=False, indent=2)
        logger.info("Saved %s embeds to %s", len(raw), file_path)


class EmbedPlugin(BotPlugin):
    def __init__(self, bot: AccountingBot, wrapper: PluginWrapper) -> None:
        super().__init__(bot, wrapper, logger)
        self.embeds = {}  # type: Dict[str, Embed]
        self.embed_locations = {}  # type: Dict[str, Union[str, PathLike]]

    def on_load(self):
        # directory = os.fsencode("resources/embeds")

        for path, subdirs, files in os.walk("resources/embeds"):
            for file in files:
                if not file.endswith(".json"):
                    continue
                file_path = os.path.join(path, file)
                embeds = load_embeds(file_path)
                for k, v in embeds.items():
                    if v is None:
                        continue
                    self.embeds[k] = v
                    self.embed_locations[k] = file_path
                logger.info("%s embeds loaded", len(self.embeds))
        self.register_cog(EmbedCommands(self))

    def on_unload(self):
        self.embeds.clear()

    async def get_status(self, short=False) -> Dict[str, str]:
        return {
            "Loaded": str(len(self.embeds))
        }

    def get_embed(self, name: str, return_default=True, raise_warn=True) -> Union[Embed, str, None]:
        if name in self.embeds:
            return self.embeds[name]
        if raise_warn:
            logger.error("Embed with name %s not found", name)
        if return_default:
            return Embed(title="Embed not found", description=f"Embed with name `{name}` not found", colour=Color.red())
        return None


class EmbedBuilder:
    def __init__(self, plugin: EmbedPlugin):
        self.plugin = plugin
        self.user = None  # type: discord.User | None
        # Embed properties
        self.title = "N/A"  # type: str | None
        self.description = None  # type: str | None
        self.color = Color.default()  # type: Color
        self.footer = None  # type: str | None
        self.footer_icon = None  # type: str | None
        self.image = None  # type: str | None
        self.thumbnail = None  # type: str | None
        self.fields = []  # type: List[discord.EmbedField]
        self.file_name = "others"
        self.embed_name = None

    def add_field(self, name: str, value: str, inline=False):
        self.fields.append(
            discord.EmbedField(
                name=name, value=value, inline=inline
            )
        )

    def load_from_embed(self, embed: Embed):
        self.title = embed.title
        self.description = embed.description
        self.color = embed.color
        self.fields = []
        for field in embed.fields:
            self.fields.append(EmbedField(
                name=field.name, value=field.value, inline=field.inline
            ))
        self.image = embed.image.url
        if self.image == EmptyEmbed:
            self.image = None
        self.thumbnail = embed.thumbnail.url
        if self.thumbnail == EmptyEmbed:
            self.thumbnail = None
        self.footer_icon = embed.footer.icon_url
        if self.footer_icon == EmptyEmbed:
            self.footer_icon = None
        self.footer = embed.footer.text
        if self.footer == EmptyEmbed:
            self.footer = None

    def build_embed(self):
        embed = discord.Embed(
            title=self.title,
            description=self.description,
            color=self.color,
            fields=self.fields
        )
        embed.set_footer(
            text=self.footer if self.footer is not None else EmptyEmbed,
            icon_url=self.footer_icon if self.footer_icon is not None else EmptyEmbed
        )
        embed.set_thumbnail(url=self.thumbnail if self.thumbnail is not None else EmptyEmbed)
        embed.set_image(url=self.image if self.image is not None else EmptyEmbed)
        return embed

    def build_edit_embed(self):
        return discord.Embed(
            title="Embed Builder",
            description="Use the controls to edit the embed:\n"
                        "+ to add Fields\n✏ to change the basic settings\n⚙ To change the save settings\n"
                        "🖼 to change the icons and footer",
            fields=[EmbedField(
                name="Basic Settings",
                value=f"Embed name: `{self.embed_name}`\nFile name: `custom/{self.file_name}.json`"
            ), EmbedField(
                name="Info",
                value="The embed name must be *unique* or else it will overwrite other embeds."
            )]
        )


class EmbedBuilderView(AutoDisableView):
    def __init__(self, embed_builder: EmbedBuilder, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.builder = embed_builder

    async def refresh_message(self):
        await self.message.edit(embeds=[self.builder.build_embed(), self.builder.build_edit_embed()])

    @discord.ui.button(label="➕", style=discord.ButtonStyle.blurple, row=0)
    async def btn_add(self, button: discord.Button, ctx: Interaction):
        form = await (
            ModalForm(title="Add field", send_response=True)
            .add_field(label="Title", placeholder="The title of the field")
            .add_field(label="Content", placeholder="The content of the field", style=InputTextStyle.paragraph)
            .add_field(label="Inline", placeholder="[Y]es or [N]o", value="No")
            .open_form(ctx.response)
        )
        if form.is_timeout():
            return
        self.builder.add_field(
            name=form.retrieve_result(label="Title"),
            value=form.retrieve_result(label="Content"),
            inline=form.retrieve_result(label="Inline").casefold().startswith("y".casefold())
        )
        await self.refresh_message()

    @discord.ui.button(label="✏", style=discord.ButtonStyle.blurple, row=0)
    async def btn_edit(self, button: discord.Button, ctx: Interaction):
        form = await (
            ModalForm(title="Edit embed", send_response=True)
            .add_field(label="Title", placeholder="The title of the embed", value=self.builder.title)
            .add_field(label="Description", placeholder="The description of the embed", required=False,
                       style=InputTextStyle.paragraph, value=self.builder.description)
            .add_field(label="Color", placeholder="Color hex code",
                       value="#%02x%02x%02x" % self.builder.color.to_rgb())
            .open_form(ctx.response)
        )
        if form.is_timeout():
            return
        self.builder.title = form.retrieve_result(label="Title")
        self.builder.description = form.retrieve_result(label="Description")
        color_hex = form.retrieve_result(label="Color").lstrip("#")
        self.builder.color = Color.from_rgb(*tuple(int(color_hex[i:i + 2], 16) for i in (0, 2, 4)))
        await self.refresh_message()

    @discord.ui.button(label="🖼", style=discord.ButtonStyle.blurple, row=0)
    async def btn_edit_icon(self, button: discord.Button, ctx: Interaction):
        form = await (
            ModalForm(title="Edit embed icons", send_response=True)
            .add_field("Footer", placeholder="Footer text", required=False, value=self.builder.footer)
            .add_field("Footer Icon URL", placeholder="https://...", required=False, value=self.builder.footer_icon)
            .add_field("Image", placeholder="https://...", required=False, value=self.builder.image)
            .add_field("Thumbnail", placeholder="https://...", required=False, value=self.builder.thumbnail)
            .open_form(ctx.response)
        )
        if form.is_timeout():
            return
        self.builder.footer = form.retrieve_result(label="Footer")
        self.builder.footer_icon = form.retrieve_result(label="Footer Icon URL")
        self.builder.image = form.retrieve_result(label="Image")
        self.builder.thumbnail = form.retrieve_result(label="Thumbnail")
        await self.refresh_message()

    @discord.ui.button(label="⚙", style=discord.ButtonStyle.blurple, row=0)
    async def btn_config(self, button: discord.Button, ctx: Interaction):
        form = await (
            ModalForm(title="Edit settings", send_response=True)
            .add_field(label="Embed name", placeholder="The name of the embed", value=self.builder.embed_name)
            .add_field(label="File name", placeholder="The description of the embed", value=self.builder.file_name)
            .open_form(ctx.response)
        )
        if form.is_timeout():
            return
        self.builder.embed_name = form.retrieve_result(label="Embed name")
        self.builder.file_name = form.retrieve_result(label="File name")
        if self.builder.embed_name in self.builder.plugin.embeds:
            await ctx.followup.send(f"An embed with name {self.builder.embed_name} already exists. Please "
                                    f"use `/embed show embed_name:{self.builder.embed_name}` to check the embed."
                                    f"If you proceed, the old embed with this name will get overwritten. However "
                                    f"it might not get overwritten in the file itself.", ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(label="💾", style=discord.ButtonStyle.blurple, row=1)
    async def btn_save(self, button: discord.Button, ctx: Interaction):
        if not re_file.match(self.builder.file_name):
            await ctx.followup.send_message(
                f"Invalid file name {self.builder.file_name}: File name may only include "
                f"Letters (A-Z), numbers (0-9), underscore (_) and hyphen (-).", ephemeral=True)
            return
        if self.builder.embed_name is None:
            await ctx.response.send_message("No embed name specified", ephemeral=True)
            return
        await ctx.response.defer(ephemeral=True)

        file_path = f"resources/embeds/custom/{self.builder.file_name}.json"
        save_new_embeds(file_path,
                        {self.builder.embed_name: self.builder.build_embed()})
        await ctx.followup.send(f"Saved embed to `{file_path}` (already existing embeds in that file were NOT "
                                f"overwritten, as long as they had another name.", ephemeral=True)

    @discord.ui.button(label="✖", style=discord.ButtonStyle.red, row=1)
    async def btn_close(self, button: discord.Button, ctx: Interaction):
        await ctx.response.defer(ephemeral=True, invisible=True)
        await self.message.delete()


class EmbedCommands(commands.Cog):
    group = SlashCommandGroup(name="embed", description="Tools for creating and managing embeds")

    def __init__(self, plugin: EmbedPlugin):
        self.plugin = plugin

    @group.command(name="builder", description="Open the embed builder")
    @option(name="embed_name", description="The name of the embed", type=str, required=False, default=None)
    @admin_only()
    async def cmd_builder(self, ctx: ApplicationContext, embed_name: str):
        embed = self.plugin.get_embed(embed_name, return_default=False, raise_warn=False)
        builder = EmbedBuilder(self.plugin)
        if embed is not None:
            builder.load_from_embed(embed)
            if embed_name in self.plugin.embed_locations:
                builder.file_name = ntpath.basename(self.plugin.embed_locations[embed_name]).replace(".json", "")
        builder.embed_name = embed_name
        view = EmbedBuilderView(builder)
        await ctx.user.send("Create a new embed", view=view, embeds=[builder.build_embed(), builder.build_edit_embed()])
        await ctx.response.defer(ephemeral=True, invisible=True)

    @group.command(name="show", description="Shows a preview of an embed")
    @option(name="embed_name", description="The name of the embed", type=str, required=False, default=None)
    @option(name="silent", description="Execute the command silently", type=bool, required=False, default=True)
    @admin_only()
    async def cmd_show(self, ctx: ApplicationContext, embed_name: str, silent: bool):
        if embed_name is None:
            msg = "```"
            for k, v in self.plugin.embeds.items():
                if not isinstance(v, Embed):
                    continue
                loc = ntpath.basename(self.plugin.embed_locations[k]).replace(".json", "")
                msg += f"\n{k:20}: {loc}"
            await ctx.response.send_message(f"Available embeds:\n{msg}\n```", ephemeral=silent)
            return
        embed = self.plugin.get_embed(embed_name, raise_warn=False)
        await ctx.response.send_message(embed=embed, ephemeral=silent)

    @group.command(name="send", description="Sends an embed into a channel")
    @option(name="embed_name", description="The name of the embed", type=str)
    @option(name="channel", description="The target channel (or empty)", type=TextChannel, required=False, default=None)
    @admin_only()
    async def cmd_send(self, ctx: ApplicationContext, embed_name: str, channel: TextChannel):
        embed = self.plugin.get_embed(embed_name, return_default=False, raise_warn=False)
        if embed is None:
            await ctx.response.send_message(f"Unknown embed `{embed_name}`. Use `/embed show` to see all available "
                                            f"embeds (case-sensitive).", ephemeral=True)
        if channel is None:
            channel = ctx.channel
        await channel.send(embed=embed)
        await ctx.response.send_message(f"Embed sent into channel `{channel.name}:{channel.id}`", ephemeral=True)
        logger.info("User %s:%s sent the embed %s into channel %s:%s",
                    ctx.user.name, ctx.user.id, embed_name, channel.name, channel.id)
