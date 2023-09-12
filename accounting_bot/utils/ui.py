from typing import Optional, Dict

from discord import InteractionResponse, Interaction, InputTextStyle
from discord.ui import Modal, InputText

from accounting_bot.exceptions import InputException
from accounting_bot.utils import ErrorHandledModal


class FormTimeoutException(InputException):
    pass


class ModalForm(ErrorHandledModal):
    def __init__(self, title: str, submit_message: Optional[str] = None, *args, **kwargs):
        """

        :param title: The title for the form
        :param submit_message: The response message after submitting. If None the interaction will not be completed and
                               can be retrieved by get_interaction for custom responses.
        :param args:
        :param kwargs:
        """
        super().__init__(title=title, timeout=30 * 60, *args, **kwargs)
        self.submit_message = submit_message
        self._interaction = None  # type: Interaction | None
        self._results = None  # type: Dict[str, str] | None

    async def callback(self, interaction: Interaction):
        self._interaction = interaction
        self._results = {}
        for item in self.children:
            self._results[item.label] = item.value
        if self.submit_message is not None:
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
            raise FormTimeoutException()
        return self

    async def retrieve_result(self, label: Optional[str] = None, index: Optional[int] = None):
        if self._results is None or len(self._results) == 0:
            raise FormTimeoutException("No results are available")
        if label is None and index is None:
            if len(self._results) > 1:
                raise TypeError("Both label and index are None and there is more than one result")
            return next(iter(self._results.values()))

    async def retrieve_results(self, ignore_timeout=False):
        if self._results is None and not ignore_timeout:
            raise FormTimeoutException("No results are available")
        return self._results
