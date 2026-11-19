"""Step 1-2: choosing (or dropping) the CV and correcting the inferred profile: roles,
skills, languages, degrees, sectors, work experience, mobility, home, seniority.

There is no confirmation step: the profile is valid as soon as a CV has been analysed,
and every edit is applied at once and saved after a short pause (AUTOSAVE_MS, the
window persists it on a worker), with a discreet "Saved" indicator and an undo of the
last changes (Ctrl+Z while the tab has the focus, the undo button, the CV menu)."""

from __future__ import annotations

import re

from typing import Callable

from PySide6.QtCore import QTime, QTimer, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QLabel, QGroupBox, QHBoxLayout, QLineEdit, QListWidget, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ..geo.sectors import SECTORS
from ..i18n import t
from ..models import Evidence, CVProfile, ProfileItem
from .common import format_number
from .profile_boxes import (ExperienceBox, FileDropFilter, HomeBox, MobilityBox, SeniorityBox, TextViewer,
                            original_text, text_or_default)
from .theme import section
from .widgets import ElidedLabel, wrapped_label

LIST_MIN_LINES = 4  # each list shows at least this many rows
AUTOSAVE_MS = 500   # edits are saved after this pause
UNDO_LIMIT = 30     # profile states kept for "undo"

USER_SOURCE = "user"


def sector_name(key: str) -> str:
    """Sector name in the current language (translation file, else the English name)."""
    text = t(f"sector.{key}")
    if text == f"sector.{key}":
        sector = SECTORS.get(key)
        return sector.name_en if sector is not None else key
    return text



_PAGES_LIMIT = re.compile(r"^Only the first (\d+) of (\d+) (?:scanned|image) pages were read\.$")


def reading_warning_text(warning: str) -> str:
    """A warning of the CV reading in the interface language (known ones are translated;
    a technical one, with an error text, stays as it is)."""
    from ..cv.reading import WARNING_KEYS

    key = WARNING_KEYS.get(warning)
    if key:
        return t(key)
    m = _PAGES_LIMIT.match(warning or "")
    if m:
        return t("reading.warning.pages_limit", read=m.group(1), total=m.group(2))
    return warning

class ListEditor(QGroupBox):
    """Editable list of profile entries: add, edit, remove."""

    def __init__(self, title_key: str, on_change: Callable[[], None], parent: QWidget | None = None):
        super().__init__(parent)
        self._title_key = title_key
        self._on_change = on_change
        self.entries: list[ProfileItem] = []

        self.list = QListWidget()
        self.list.setMinimumHeight(self.list.fontMetrics().height() * LIST_MIN_LINES + 12)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.field = QLineEdit()
        self.add_button = QPushButton()
        self.edit_button = QPushButton()
        self.remove_button = QPushButton()
        self.add_button.clicked.connect(self.add)
        self.edit_button.clicked.connect(self.edit)
        self.remove_button.clicked.connect(self.remove)
        self.field.returnPressed.connect(self.add)
        self.list.currentRowChanged.connect(self._row_changed)

        # Buttons in a column beside the list: five boxes fit side by side at 1280 px.
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        for button in (self.add_button, self.edit_button, self.remove_button):
            button.setProperty("role", "small")
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout = QGridLayout(self)
        layout.addWidget(self.list, 0, 0)
        layout.addLayout(buttons, 0, 1)
        layout.addWidget(self.field, 1, 0, 1, 2)
        layout.setRowStretch(0, 1)
        layout.setColumnStretch(0, 1)

    def retranslate(self) -> None:
        self.setTitle(section(t(self._title_key)))
        self.add_button.setText(t("profile.add"))
        self.edit_button.setText(t("profile.edit"))
        self.remove_button.setText(t("profile.remove"))

    def set_entries(self, entries: list[ProfileItem]) -> None:
        self.entries = [ProfileItem.from_dict(v.to_dict()) for v in entries]
        self._redraw()

    def values(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _redraw(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for entry in self.entries:
            self.list.addItem(entry.value)
            # Translated value: the CV wording in the tooltip ("From the CV: ...").
            tooltip = "\n".join(x for x in (entry.value, original_text(entry.original)) if x)
            self.list.item(self.list.count() - 1).setToolTip(tooltip)
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)
        self.list.blockSignals(False)

    def select(self, index: int) -> None:
        self.list.setCurrentRow(index)

    def _index(self) -> int | None:
        row = self.list.currentRow()
        return row if 0 <= row < len(self.entries) else None

    def _row_changed(self, _row: int) -> None:
        i = self._index()
        if i is not None:
            self.field.setText(self.entries[i].value)

    def _text(self) -> str:
        return " ".join(self.field.text().split())

    def add(self) -> None:
        text = self._text()
        if not text or any(v.value.casefold() == text.casefold() for v in self.entries):
            return
        self.entries.append(ProfileItem(text, [Evidence(text, USER_SOURCE)]))
        self.field.clear()
        self._redraw()
        self._on_change()

    def edit(self) -> None:
        i = self._index()
        text = self._text()
        if i is None or not text or text == self.entries[i].value:
            return
        self.entries[i] = ProfileItem(text, [Evidence(text, USER_SOURCE)])
        self._redraw(select=i)
        self._on_change()

    def remove(self) -> None:
        i = self._index()
        if i is None:
            return
        del self.entries[i]
        self.field.clear()
        self._redraw()
        self._on_change()


class ProfilePanel(QWidget):
    """CV choice and editable profile, saved as it is edited.

    `on_profile_change()` is called at every edit (cheap updates: the note of tab 2);
    `on_profile_edited(profile)` once the edits pause for AUTOSAVE_MS (persist it,
    follow it in the other tabs); the window calls `show_saved()` when it is stored."""

    def __init__(self, on_choose_cv: Callable[[], None],
                 on_profile_change: Callable[[], None] | None = None,
                 parent: QWidget | None = None, *,
                 on_file_dropped: Callable[[str], None] | None = None,
                 on_use_home: Callable[[str], None] | None = None,
                 on_read_again: Callable[[], None] | None = None,
                 on_add_version: Callable[[str, str], None] | None = None):
        super().__init__(parent)
        self._on_profile_change = on_profile_change or (lambda: None)
        self.on_profile_edited: Callable[[CVProfile], None] = lambda _p: None
        self._on_file_dropped = on_file_dropped or (lambda _p: None)
        self._profile: CVProfile | None = None
        self._reading = None
        self._loading = False   # filling the boxes from a profile: not an edit
        self._undo: list[dict] = []
        self._snapshot: dict | None = None
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(AUTOSAVE_MS)
        self._autosave.timeout.connect(self.save_now)
        self._inferred: list[str] = []
        self.text_viewer: TextViewer | None = None
        self._on_add_version = on_add_version or (lambda _p, _l: None)
        self._offer_path: str | None = None

        self.cv_button = QPushButton()
        self.cv_button.clicked.connect(lambda: on_choose_cv())
        self.file_label = ElidedLabel(mode=Qt.TextElideMode.ElideMiddle)
        self.file_label.setProperty("role", "strong")
        self.text_button = QPushButton()
        self.text_button.clicked.connect(lambda: self.show_text())
        # The advanced OCR reader (gui/ocr_reader.py sets the callbacks): read the scan
        # again on request, and a non-blocking offer to download it.
        self.on_advanced_reread: Callable[[], None] = lambda: None
        self.on_advanced_accept: Callable[[], None] = lambda: None
        self.on_advanced_decline: Callable[[], None] = lambda: None
        self._advanced_offer: dict | None = None
        self.advanced_button = QPushButton()
        self.advanced_button.clicked.connect(lambda: self.on_advanced_reread())
        self.advanced_bar = QWidget()
        self.advanced_bar.setObjectName("offerBar")
        self.advanced_label = wrapped_label(role="title")
        self.advanced_download_button = QPushButton()
        self.advanced_download_button.setProperty("role", "small")
        self.advanced_download_button.clicked.connect(lambda: self._advanced_answer(True))
        self.advanced_never_button = QPushButton()
        self.advanced_never_button.setProperty("role", "small")
        self.advanced_never_button.clicked.connect(lambda: self._advanced_answer(False))
        advanced_layout = QHBoxLayout(self.advanced_bar)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(self.advanced_label, 1)
        advanced_layout.addWidget(self.advanced_download_button)
        advanced_layout.addWidget(self.advanced_never_button)
        self.advanced_bar.hide()
        self.advanced_download_button.setEnabled(False)
        self.advanced_never_button.setEnabled(False)
        # "Read again": the same CV file analysed again (choosing a file already starts
        # the analysis; there is no separate load button).
        self.reread_button = QPushButton()
        self.reread_button.clicked.connect(lambda: (on_read_again or (lambda: None))())
        self._busy = False        # a CV is being read
        self._my_cv_title: str | None = None
        from .cv_panel import PhotoLabel
        from PySide6.QtCore import QSize

        self.photo_label = PhotoLabel(size=QSize(40, 50))  # the portrait in the CV, when found
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.photo_label)
        top.addWidget(self.cv_button)
        top.addWidget(self.file_label, 1)
        top.addWidget(self.reread_button)
        top.addWidget(self.advanced_button)
        top.addWidget(self.text_button)
        # Non-blocking offer to keep a CV chosen here as a version in "My CV".
        self.offer_bar = QWidget()
        self.offer_bar.setObjectName("offerBar")
        self.offer_label = wrapped_label(role="title")
        self.offer_language = QComboBox()
        from .cv_panel import VERSION_LANGUAGES

        for code in VERSION_LANGUAGES:
            self.offer_language.addItem("", code)
        self.offer_add_button = QPushButton()
        self.offer_add_button.setProperty("role", "small")
        self.offer_add_button.clicked.connect(lambda: self.accept_offer())
        self.offer_dismiss_button = QPushButton()
        self.offer_dismiss_button.setProperty("role", "small")
        self.offer_dismiss_button.clicked.connect(lambda: self.hide_offer())
        offer_layout = QHBoxLayout(self.offer_bar)
        offer_layout.setContentsMargins(0, 0, 0, 0)
        offer_layout.addWidget(self.offer_label, 1)
        offer_layout.addWidget(self.offer_language)
        offer_layout.addWidget(self.offer_add_button)
        offer_layout.addWidget(self.offer_dismiss_button)
        self.offer_bar.hide()
        self.info_label = wrapped_label(role="muted")
        self.reading_label = wrapped_label(role="muted")
        self.warnings_label = wrapped_label(role="warning")
        # The big entry point once a CV is read: "Who could be interested in my profile?"
        # (the window binds it to the Search-menu action; gui/window_interest.py).
        self.on_interest: Callable[[], None] = lambda: None
        self.interest_button = QPushButton()
        self.interest_button.setObjectName("interestButton")
        self.interest_button.setProperty("role", "accent")
        self.interest_button.setMinimumHeight(self.fontMetrics().height() * 2 + 12)
        font = self.interest_button.font()
        font.setPointSizeF(font.pointSizeF() * 1.25 if font.pointSizeF() > 0 else 12.0)
        font.setBold(True)
        self.interest_button.setFont(font)
        self.interest_button.clicked.connect(lambda: self.on_interest())
        self.interest_hint = wrapped_label(role="muted")
        self.interest_bar = QWidget()
        interest_layout = QHBoxLayout(self.interest_bar)
        interest_layout.setContentsMargins(0, 2, 0, 2)
        interest_layout.addWidget(self.interest_button)
        interest_layout.addWidget(self.interest_hint, 1)
        self.interest_bar.hide()

        self.roles_editor = ListEditor("profile.roles", self._modified)
        self.skills_editor = ListEditor("profile.skills", self._modified)
        self.languages_editor = ListEditor("profile.languages", self._modified)
        self.degrees_editor = ListEditor("profile.degrees", self._modified)

        self.sectors_box = QGroupBox()
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(2, 2, 2, 2)
        inner_layout.setSpacing(3)
        self.sector_boxes: dict[str, QCheckBox] = {}
        for key in SECTORS:
            box = QCheckBox()
            box.toggled.connect(self._modified)
            inner_layout.addWidget(box)
            self.sector_boxes[key] = box
        inner_layout.addStretch(1)
        self._sectors_inner = inner
        self.sectors_scroll = QScrollArea()
        self.sectors_scroll.setWidgetResizable(True)
        self.sectors_scroll.setWidget(inner)
        self.sectors_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        sectors_layout = QVBoxLayout(self.sectors_box)
        sectors_layout.addWidget(self.sectors_scroll)

        self.experience_box = ExperienceBox()
        self.mobility_box = MobilityBox(self._modified)
        self.home_box = HomeBox(self._modified, on_use_home or (lambda _h: None))
        self.seniority_box = SeniorityBox(self._modified)

        # Inside a scroll area: at 1280x800 everything fits, smaller windows scroll.
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.roles_editor, 0, 0)
        grid.addWidget(self.skills_editor, 0, 1)
        grid.addWidget(self.languages_editor, 0, 2)
        grid.addWidget(self.degrees_editor, 0, 3)
        grid.addWidget(self.sectors_box, 0, 4, 2, 1)
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.addWidget(self.home_box)
        side.addWidget(self.seniority_box)
        side.addStretch(1)
        grid.addWidget(self.experience_box, 1, 0, 1, 2)
        grid.addWidget(self.mobility_box, 1, 2)
        grid.addLayout(side, 1, 3)
        for c in range(4):
            grid.setColumnStretch(c, 1)
        grid.setRowStretch(0, 2)
        grid.setRowStretch(1, 3)
        content = QWidget()
        content.setLayout(grid)
        self.page_scroll = QScrollArea()
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setWidget(content)

        # No confirmation: the state of the profile, "Saved" and the undo of the last edit.
        self.state_label = wrapped_label()
        self.saved_label = QLabel()
        self.saved_label.setProperty("role", "muted")
        self.undo_action = QAction(self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.undo_action.triggered.connect(lambda _checked=False: self.undo())
        self.addAction(self.undo_action)
        self.undo_button = QPushButton()
        self.undo_button.setProperty("role", "small")
        self.undo_button.clicked.connect(lambda: self.undo_action.trigger())
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self.state_label, 1)
        bottom.addWidget(self.saved_label)
        bottom.addWidget(self.undo_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.offer_bar)
        layout.addWidget(self.advanced_bar)
        layout.addWidget(self.info_label)
        layout.addWidget(self.interest_bar)
        layout.addWidget(self.reading_label)
        layout.addWidget(self.warnings_label)
        layout.addWidget(self.page_scroll, 1)
        layout.addLayout(bottom)
        self.drop_filter = FileDropFilter(self._file_dropped, self)
        self.drop_filter.watch(self)
        self._update_state()

    def _file_dropped(self, path: str) -> None:
        self._on_file_dropped(path)

    # --- "My CV" library

    def set_my_cv(self, title: str | None) -> None:
        """The library has a primary version (`title`): the empty tab 1 says where to
        find it (the "Use for analysis" button of the My CV tab)."""
        self._my_cv_title = title or None
        if self._profile is None and not self._busy:
            self.info_label.setText(self._empty_text())

    def _empty_text(self) -> str:
        text = t("profile.instructions")
        if self._my_cv_title:
            text += "\n" + t("profile.my_cv_hint", use=t("mycv.use"))
        return text

    def can_reread(self) -> bool:
        """"Read again" is possible: a CV file is known and nothing is being read."""
        return self._profile is not None and bool(self._profile.cv_path) and not self._busy

    def reading_done(self) -> None:
        """The reading ended without a profile (error): the buttons come back."""
        self._busy = False
        self._update_state()

    def set_photo(self, png: bytes | None) -> None:
        self.photo_label.set_photo(png, t("profile.photo_tooltip"))

    def offer_add(self, path: str, language: str) -> None:
        """Info bar: "this CV is not in My CV: add it as a version?" (language pre-selected)."""
        self._offer_path = path
        index = self.offer_language.findData(language)
        self.offer_language.setCurrentIndex(max(0, index))
        self._offer_texts()
        self.offer_bar.show()

    def hide_offer(self) -> None:
        self._offer_path = None
        self.offer_bar.hide()

    @property
    def offer_visible(self) -> bool:
        return self._offer_path is not None

    def accept_offer(self) -> None:
        path, language = self._offer_path, self.offer_language.currentData()
        self.hide_offer()
        if path:
            self._on_add_version(path, language or "en")

    def _offer_texts(self) -> None:
        from .cv_panel import language_label

        self.offer_label.setText(t("mycv.offer"))
        for i in range(self.offer_language.count()):
            self.offer_language.setItemText(i, language_label(self.offer_language.itemData(i)))
        self.offer_add_button.setText(t("mycv.offer_add"))
        self.offer_dismiss_button.setText(t("mycv.offer_dismiss"))

    # --- the advanced OCR reader

    def offer_advanced(self, size: str, source: str) -> None:
        """Info bar: "this scan was hard to read: download the advanced reader?" (non-blocking)."""
        self._advanced_offer = {"size": size, "source": source}
        self._advanced_texts()
        self.advanced_download_button.setEnabled(True)
        self.advanced_never_button.setEnabled(True)
        self.advanced_bar.show()

    def hide_advanced_offer(self) -> None:
        self._advanced_offer = None
        self.advanced_download_button.setEnabled(False)
        self.advanced_never_button.setEnabled(False)
        self.advanced_bar.hide()

    @property
    def advanced_offer_visible(self) -> bool:
        return self._advanced_offer is not None

    def _advanced_answer(self, accepted: bool) -> None:
        if self._advanced_offer is None:
            return
        self.hide_advanced_offer()
        (self.on_advanced_accept if accepted else self.on_advanced_decline)()

    def _advanced_texts(self) -> None:
        offer = self._advanced_offer or {"size": "", "source": ""}
        self.advanced_label.setText(t("profile.advanced_offer", **offer))
        self.advanced_download_button.setText(t("profile.advanced_download", size=offer["size"]))
        self.advanced_never_button.setText(t("profile.advanced_never"))
        self.advanced_button.setText(t("profile.advanced_reread"))

    def can_read_again(self) -> bool:
        """The current CV went through the OCR (an image or a scanned PDF)."""
        return (self._profile is not None and bool(self._profile.cv_path)
                and getattr(self._reading, "method", "text") in ("ocr", "mixed"))

    # --- data

    @property
    def ready(self) -> bool:
        """A CV was analysed and the profile has what the search needs."""
        return not self.missing_for_search()

    @property
    def has_profile(self) -> bool:
        return self._profile is not None

    def editors(self) -> tuple[ListEditor, ...]:
        return (self.roles_editor, self.skills_editor, self.languages_editor, self.degrees_editor)

    def set_profile(self, profile: CVProfile, inferred_sectors: list[str], reading=None, *,
                    restored: bool = False) -> None:
        """Shows a profile just read (it is valid at once and saved); `reading` is the
        cv.reading.ReadResult. `restored`: the profile saved by the previous session
        (shown as it is, not saved again)."""
        self._profile = profile
        self._reading = reading
        self._busy = False
        self._inferred = list(inferred_sectors)
        if self.text_viewer is not None:
            self.text_viewer.close()
            self.text_viewer = None
        self._fill(profile, list(profile.sectors or inferred_sectors))
        self._undo = []
        self._snapshot = self._state()
        self.retranslate()
        self._on_profile_change()
        if restored:
            self._autosave.stop()
        else:
            self._autosave.start()

    def _fill(self, profile: CVProfile, sectors: list[str]) -> None:
        """The boxes show `profile` (not reported as an edit)."""
        self._loading = True
        try:
            self.experience_box.set_history(getattr(profile, "experiences", []), getattr(profile, "gaps", []),
                                            profile.years_experience)
            self.mobility_box.set_mobility(getattr(profile, "mobility", None))
            self.home_box.set_home(getattr(profile, "home", None))
            self.seniority_box.set_seniority(getattr(profile, "seniority", None),
                                             getattr(profile, "seniority_evidence", []))
            self.roles_editor.set_entries(profile.roles)
            self.skills_editor.set_entries(profile.skills)
            self.languages_editor.set_entries(profile.spoken_languages)
            self.degrees_editor.set_entries(profile.degrees)
            chosen = set(sectors)
            for key, box in self.sector_boxes.items():
                box.blockSignals(True)
                box.setChecked(key in chosen)
                box.blockSignals(False)
        finally:
            self._loading = False

    def inferred_sectors(self) -> list[str]:
        return list(self._inferred)

    def chosen_sectors(self) -> list[str]:
        """The ticked sectors, most relevant first: the order inferred from the CV (the
        search ranks the first sector highest), then the ones the user added."""
        ticked = [k for k, box in self.sector_boxes.items() if box.isChecked()]
        order = {key: i for i, key in enumerate(self._inferred)}
        return sorted(ticked, key=lambda k: (order.get(k, len(order)), list(self.sector_boxes).index(k)))

    def current_profile(self) -> CVProfile | None:
        """Profile with the user's corrections (a copy)."""
        if self._profile is None:
            return None
        p = self._profile

        def copy(entries):
            return [ProfileItem.from_dict(v.to_dict()) for v in entries]

        return CVProfile(
            cv_path=p.cv_path,
            language=p.language,
            roles=copy(self.roles_editor.entries),
            skills=copy(self.skills_editor.entries),
            spoken_languages=copy(self.languages_editor.entries),
            degrees=copy(self.degrees_editor.entries),
            sectors=self.chosen_sectors(),
            years_experience=p.years_experience,
            text=p.text,
            experiences=list(getattr(p, "experiences", [])),
            gaps=list(getattr(p, "gaps", [])),
            mobility=self.mobility_box.mobility(),
            home=self.home_box.home(),
            seniority=self.seniority_box.seniority(),
            seniority_evidence=self.seniority_box.evidence(),
            translated=getattr(p, "translated", False),
            translation=getattr(p, "translation", ""),
            untranslated=list(getattr(p, "untranslated", []) or []),
        )

    @property
    def reading(self):
        return self._reading

    def show_text(self) -> TextViewer | None:
        """Opens the read-only viewer of the extracted text (non-modal)."""
        if self._profile is None:
            return None
        if self.text_viewer is not None:
            self.text_viewer.close()
        self.text_viewer = TextViewer(self, self._profile.text, t("profile.text_title"))
        self.text_viewer.show()
        return self.text_viewer

    def missing_for_search(self) -> list[str]:
        """What the search really needs and the profile lacks: "cv" (no CV analysed) or
        "roles" (no job role: the search ranks companies by the roles). Empty = ready."""
        if self._profile is None:
            return ["cv"]
        if not self.roles_editor.entries:
            return ["roles"]
        return []

    def focus_field(self, field: str) -> None:
        """Shows and focuses the field named by `missing_for_search`."""
        if field == "roles":
            self.page_scroll.ensureWidgetVisible(self.roles_editor)
            self.roles_editor.field.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.cv_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _state(self) -> dict | None:
        profile = self.current_profile()
        return None if profile is None else profile.to_dict()

    def _modified(self, *_args) -> None:
        """Every edit: applied at once (the search tab says what is still missing, if
        anything), kept for undo, saved after AUTOSAVE_MS."""
        if self._loading or self._profile is None:
            return
        state = self._state()
        if state == self._snapshot:
            return
        if self._snapshot is not None:
            self._undo.append(self._snapshot)
            del self._undo[:-UNDO_LIMIT]
        self._snapshot = state
        self._on_profile_change()
        self._update_state()
        self._autosave.start()

    def save_now(self) -> None:
        """Hands the current profile over to be saved (the pause after the edits ended)."""
        self._autosave.stop()
        profile = self.current_profile()
        if profile is not None:
            self.saved_label.setText(t("profile.saving"))
            self.on_profile_edited(profile)

    @property
    def save_pending(self) -> bool:
        return self._autosave.isActive()

    def show_saved(self, when: QTime | None = None) -> None:
        """"Saved ✓ 14:05": the window stored the profile."""
        when = when or QTime.currentTime()
        self.saved_label.setText(t("profile.saved", time=when.toString("HH:mm")))

    def show_not_saved(self) -> None:
        self.saved_label.setText(t("profile.not_saved"))

    def can_undo(self) -> bool:
        return bool(self._undo)

    def undo(self) -> bool:
        """Back to the profile before the last edit (it is saved again)."""
        if not self._undo or self._profile is None:
            return False
        state = self._undo.pop()
        profile = CVProfile.from_dict(state)
        self._fill(profile, list(profile.sectors))
        self._snapshot = self._state()
        self._on_profile_change()
        self._update_state()
        self._autosave.start()
        return True

    # --- texts

    def _update_state(self) -> None:
        missing = self.missing_for_search()
        if missing == ["cv"]:
            text = t("profile.no_cv")
        elif missing:
            text = t("profile.missing", field=t(f"launch.field.{missing[0]}"))
        else:
            text = t("profile.ready")
        self.state_label.setText(text)
        self.undo_action.setEnabled(self.can_undo())
        self.undo_button.setEnabled(self.can_undo())
        self.text_button.setEnabled(self._profile is not None and bool(self._profile.text))
        self.advanced_button.setEnabled(self.can_read_again() and not self._busy)
        self.reread_button.setEnabled(self.can_reread())
        self.interest_bar.setVisible(self._profile is not None and not self._busy)
        path = self._profile.cv_path if self._profile is not None else ""
        self.reread_button.setToolTip(t("profile.read_again_tip", path=path) if path else "")

    def _fit_sectors(self) -> None:
        """The sectors column is as wide as its longest name (no horizontal scrolling)."""
        width = self._sectors_inner.sizeHint().width()
        width += self.sectors_scroll.verticalScrollBar().sizeHint().width() + self.sectors_scroll.frameWidth() * 2
        self.sectors_scroll.setMinimumWidth(width)

    def _reading_text(self) -> str:
        r = self._reading
        if r is None:
            return ""
        method = getattr(r, "method", "text")
        parts = [t(f"reading.method.{method}"), t("reading.pages", pages=getattr(r, "pages", 0))]
        confidence = getattr(r, "ocr_confidence", None)
        if confidence is not None:
            parts.append(t("reading.confidence", percent=format_number(confidence * 100)))
        if getattr(r, "engine", "") == "advanced":
            parts.append(t("reading.engine.advanced"))
        return " · ".join(parts)

    def retranslate(self) -> None:
        self.cv_button.setText(t("profile.choose_cv"))
        self.reread_button.setText(t("profile.read_again"))
        self._offer_texts()
        self._advanced_texts()
        self.text_button.setText(t("profile.show_text"))
        self.interest_button.setText(t("interest.button"))
        self.interest_hint.setText(t("interest.button_hint"))
        self.reading_label.setText(self._reading_text())
        self.reading_label.setVisible(bool(self.reading_label.text()))
        warnings = list(getattr(self._reading, "warnings", []) or [])
        confidence = getattr(self._reading, "ocr_confidence", None)
        notes = []
        if confidence is not None and confidence < 0.8:
            notes.append(t("reading.low_confidence"))
        notes += [reading_warning_text(w) for w in warnings]
        self.warnings_label.setText("\n".join(notes))
        self.warnings_label.setVisible(bool(notes))
        for box in (self.experience_box, self.mobility_box, self.home_box, self.seniority_box):
            box.retranslate()
        if self._profile is None:
            self.file_label.setText(t("profile.no_file"))
            self.info_label.setText(t("profile.reading") if self._busy else self._empty_text())
        else:
            self.file_label.setText(self._profile.cv_path)
            years = self._profile.years_experience
            language = text_or_default(f"cv_language.{self._profile.language}")
            if years is None:
                info = t("profile.info_no_years", language=language)
            else:
                info = t("profile.info", language=language, years=format_number(years, 1))
            note = self.translation_note()
            self.info_label.setText(f"{info}\n{note}" if note else info)
        for editor in self.editors():
            editor.retranslate()
        self.sectors_box.setTitle(section(t("profile.sectors")))
        for key, box in self.sector_boxes.items():
            name = sector_name(key)
            if key in self._inferred:
                name += " " + t("profile.inferred")
            box.setText(name)
        self._fit_sectors()
        self.undo_action.setText(t("profile.undo"))
        self.undo_button.setText(t("profile.undo_button"))
        self.undo_button.setToolTip(t("profile.undo"))
        self._update_state()

    def translation_note(self) -> str:
        """"CV in Italian: fields translated into English (local translation)" or ""."""
        p = self._profile
        if p is None or not getattr(p, "translated", False):
            return ""
        language = text_or_default(f"cv_language.{p.language}")
        key = "profile.translated_local" if getattr(p, "translation", "") == "local" else "profile.translated_tables"
        note = text_or_default(key, language=language)
        left = len(getattr(p, "untranslated", []) or [])
        if left:
            note += " · " + text_or_default("profile.untranslated", count=left)
        return note

    def show_reading(self, path: str) -> None:
        self._busy = True
        self.file_label.setText(path)
        self.info_label.setText(t("profile.reading"))
        self._update_state()

    def show_ocr_progress(self, done: int, total: int) -> None:
        self.info_label.setText(t("profile.ocr_progress", done=min(done + 1, total), total=total))
