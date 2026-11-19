"""What happened when a draft was opened, and what the user still has to do."""

from dataclasses import dataclass, field

from reversehire.mail.texts import Text, Translate

STATUS_OPENED = "opened"            # a compose window (or draft file) was launched
STATUS_PENDING = "pending"          # still starting after the short timeout (Outlook COM)
STATUS_ASSISTED = "assisted"        # no mail program: clipboard and/or folder only
STATUS_FAILED = "failed"            # the method was tried and did not work
STATUS_UNAVAILABLE = "unavailable"  # the method is not available here; nothing was tried

OK_STATUSES = (STATUS_OPENED, STATUS_PENDING, STATUS_ASSISTED)


@dataclass(frozen=True)
class Outcome:
    """Result of ``MailMethod.open_draft``. Nothing was ever sent: the user sends.

    ``attached``: the PDF is already in the draft. ``body_complete``: the full text is in
    the draft (False when a link had to be shortened). ``clipboard_used`` /
    ``folder_revealed``: the assisted steps that were done. ``error``: technical detail
    for the log or a "details" line (may be empty).
    """

    method: str
    status: str
    message: Text
    steps: tuple[Text, ...] = ()
    client: str = ""
    attached: bool = False
    body_complete: bool = True
    clipboard_used: bool = False
    folder_revealed: bool = False
    error: str = ""
    extra: dict = field(default_factory=dict)  # e.g. {"eml_path": "..."} for the eml method

    @property
    def ok(self) -> bool:
        """True when something useful happened (a window, or the assisted steps)."""
        return self.status in OK_STATUSES

    def message_text(self, translate: Translate | None = None) -> str:
        return self.message.render(translate)

    def step_texts(self, translate: Translate | None = None) -> list[str]:
        return [step.render(translate) for step in self.steps]

    def full_text(self, translate: Translate | None = None) -> str:
        """Message followed by the steps, one per line (for a simple message box)."""
        lines = [self.message_text(translate)]
        lines += ["• " + line for line in self.step_texts(translate)]
        return "\n".join(lines)
