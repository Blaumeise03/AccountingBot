import asyncio
import functools
from typing import Optional, Dict, Coroutine, Callable, Union, List, Self

import discord
from discord import InteractionResponse, Interaction, InputTextStyle, Button, ApplicationContext, Embed, Webhook
from discord.ui import InputText

from accounting_bot.exceptions import InputException
from accounting_bot.utils import ErrorHandledModal, AutoDisableView


class FormTimeoutException(InputException):
    pass


class ModalForm(ErrorHandledModal):
    def __init__(self, title: str, send_response: Union[str, bool, None] = None, ignore_timeout=False, *args, **kwargs):
        """

        :param title: The title for the form
        :param send_response: The response message after submitting. If None the interaction will not be completed and
                               can be retrieved by get_interaction for custom responses. If true the response will get
                               deferred. If a string is given, it will get send to the user.
        :param args:
        :param kwargs:
        """
        super().__init__(title=title, timeout=30 * 60, *args, **kwargs)
        self.ignore_timeout = ignore_timeout
        self._is_timeout = False
        self.submit_message = send_response
        self._interaction = None  # type: Interaction | None
        self._results = None  # type: Dict[str, str] | None

    async def callback(self, interaction: Interaction):
        self._interaction = interaction
        self._results = {}
        for item in self.children:
            self._results[item.label] = item.value
        if self.submit_message is None or self.submit_message is False:
            return
        if self.submit_message is True:
            await interaction.response.defer(ephemeral=True, invisible=True)
            return
        await interaction.response.send_message(self.submit_message, ephemeral=True)

    def add_field(self,
                  label: str,
                  style: InputTextStyle = InputTextStyle.short,
                  placeholder: str | None = None,
                  min_length: int | None = None,
                  max_length: int | None = None,
                  required: bool | None = True,
                  value: str | None = None):
        self.add_item(InputText(label=label,
                                style=style,
                                placeholder=placeholder,
                                min_length=min_length,
                                max_length=max_length,
                                required=required,
                                value=value))
        return self

    async def open_form(self, response: InteractionResponse):
        await response.send_modal(self)
        await self.wait()
        if self._results is None:
            self._is_timeout = True
            if not self.ignore_timeout:
                raise FormTimeoutException()
        return self

    def is_timeout(self):
        return self._is_timeout

    def retrieve_result(self, label: Optional[str] = None, index: Optional[int] = None):
        if self._results is None or len(self._results) == 0:
            if self.ignore_timeout:
                return None
            raise FormTimeoutException("No results are available")
        if label is None and index is None:
            if len(self._results) > 1:
                raise TypeError("Both label and index are None and there is more than one result")
            return next(iter(self._results.values()))
        elif label is not None:
            return self._results[label]
        elif index is not None:
            return self._results[self.children[index].label]

    def retrieve_results(self, ignore_timeout=False):
        if self._results is None and not ignore_timeout:
            if self.ignore_timeout:
                return None
            raise FormTimeoutException("No results are available")
        return self._results

    def get_response(self) -> Optional[InteractionResponse]:
        if self._interaction is None:
            return None
        # noinspection PyTypeChecker
        return self._interaction.response

    def get_interaction(self) -> Optional[Interaction]:
        return self._interaction


# noinspection PyUnusedLocal
class ConfirmView(AutoDisableView):
    def __init__(self, callback: Callable[[Interaction], Coroutine], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.function = callback

    @discord.ui.button(label="Bestätigen", style=discord.ButtonStyle.green)
    async def btn_confirm(self, button: Button, ctx: Interaction):
        await self.function(ctx)
        await self.message.delete()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.grey)
    async def btn_abort(self, button: Button, ctx: Interaction):
        await ctx.response.defer(invisible=True)
        await self.message.delete()


class AwaitConfirmView(AutoDisableView):
    def __init__(self, defer_response=True, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.confirmed = False
        self.interaction = None  # type: Interaction | None
        self._defer_response = defer_response

    async def send_view(self,
                        response: Union[InteractionResponse, Webhook],
                        message: str, ephemeral=True,
                        embed: Embed | None = None,
                        embeds: List[Embed] | None = None) -> Self:
        if isinstance(response, InteractionResponse):
            await response.send_message(
                content=message,
                view=self,
                embed=embed,
                embeds=embeds,
                ephemeral=ephemeral)
        else:
            self.message = await response.send(
                content=message,
                view=self,
                embed=embed if embed is not None else discord.utils.MISSING,
                embeds=embeds if embeds is not None else discord.utils.MISSING,
                ephemeral=ephemeral)
        time_out = await self.wait()
        if time_out:
            self.confirmed = False
        return self

    async def defer_response(self):
        if self.interaction is None:
            return
        await self.interaction.response.defer(ephemeral=True, invisible=True)

    @discord.ui.button(label="Bestätigen", style=discord.ButtonStyle.green)
    async def btn_confirm(self, button: Button, ctx: Interaction):
        self.confirmed = True
        self.interaction = ctx
        if self._defer_response:
            await self.defer_response()
        await self.message.delete()
        self.stop()

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.grey)
    async def btn_abort(self, button: Button, ctx: Interaction):
        self.confirmed = False
        self.interaction = ctx
        if self._defer_response:
            await self.defer_response()
        await self.message.delete()
        self.stop()


class NumPadView(AutoDisableView):
    class NumberButton(discord.ui.Button):
        def __init__(self,
                     number: int,
                     callback: Callable[[int, Interaction], Coroutine],
                     *args, **kwargs):
            super().__init__(label=f"{number}",
                             style=discord.ButtonStyle.blurple,
                             *args, **kwargs)
            self.number = number
            self.function = callback

        async def callback(self, ctx: Interaction):
            await self.function(self.number, ctx)

    def __init__(self, start_row=0, consume_response=True, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.selected = None
        self.consume_response = consume_response
        self.response = None  # type: InteractionResponse | None
        for i in range(7, 10):
            self.add_item(NumPadView.NumberButton(i, self.callback, row=start_row))
        for i in range(4, 7):
            self.add_item(NumPadView.NumberButton(i, self.callback, row=start_row + 1))
        for i in range(1, 4):
            self.add_item(NumPadView.NumberButton(i, self.callback, row=start_row + 2))
        btn_custom = discord.ui.Button(
            label="...",
            style=discord.ButtonStyle.green,
            row=start_row + 3
        )
        btn_custom.callback = functools.partial(NumPadView.btn_custom, view=self)
        self.add_item(btn_custom)
        btn_cancel = discord.ui.Button(
            label="✖",
            style=discord.ButtonStyle.red,
            row=start_row + 3
        )
        btn_cancel.callback = functools.partial(NumPadView.btn_cancel, view=self)
        self.add_item(btn_cancel)

    async def callback(self, number: int, ctx: Interaction):
        self.selected = number
        self.response = ctx.response
        self.stop()
        if self.consume_response:
            await ctx.response.defer(invisible=True)
            await self.message.delete()
        else:
            asyncio.get_event_loop().create_task(self.message.delete())

    async def on_timeout(self) -> None:
        await super().on_timeout()

    @staticmethod
    async def btn_custom(ctx: Interaction, view):
        # noinspection PyTypeChecker
        modal = await (
            ModalForm(title="Insert Number", send_response=None, ignore_timeout=True)
            .add_field(label="Insert Number", placeholder="Insert Number here")
            .open_form(ctx.response)
        )
        if modal.is_timeout():
            return
        try:
            number = int(modal.retrieve_result())
        except ValueError as e:
            await modal.get_response().send_message(f"Input is not a valid number: {e}", ephemeral=True)
            return
        confirm = await (AwaitConfirmView(defer_response=False)
                         .send_view(modal.get_response(), message=f"Please confirm the number `{number}`"))
        if not confirm.confirmed:
            await confirm.defer_response()
            return
        await view.callback(number, confirm.interaction)

    @staticmethod
    async def btn_cancel(ctx: ApplicationContext, view):
        await ctx.response.defer(invisible=True)
        await view.message.delete()
