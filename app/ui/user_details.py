"""Right-side detail drawer for a single user."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.database.db import Database, from_db_ts
from app.database.models import JOINED_SOURCE_LABELS, JOINED_UNAVAILABLE, LeadCriteria, LeadRow
from app.database.repositories import LeadQueryRepository, UserRepository
from app.ui.theme import STATUS_COLORS, C, icon, score_color
from app.ui.widgets import AvatarLabel, Badge, FlowLayout, divider, make_button

DRAWER_WIDTH = 440
MANUAL_REGIONS = [("Not tagged", None), ("US", "US"), ("EU", "EU"), ("Other", "Other")]


def fmt_local(ts: str | None, with_time: bool = True) -> str:
    dt = from_db_ts(ts)
    if dt is None:
        return "—"
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M") if with_time else local.strftime("%Y-%m-%d")


def _kv(label: str, value: str, muted: bool = False) -> QWidget:
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 2, 0, 2)
    k = QLabel(label)
    k.setObjectName("Muted")
    k.setMinimumWidth(110)
    k.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    v = QLabel(value)
    v.setWordWrap(True)
    v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if muted:
        v.setStyleSheet(f"color: {C.MUTED}; font-style: italic;")
    row.addWidget(k)
    row.addWidget(v, 1)
    return w


def _section(title: str) -> QLabel:
    label = QLabel(title.upper())
    label.setObjectName("SectionLabel")
    return label


class ScoreBar(QProgressBar):
    def __init__(self, score: int) -> None:
        super().__init__()
        self.setRange(0, 100)
        self.setValue(score)
        self.setTextVisible(False)
        self.setFixedHeight(6)
        self.setStyleSheet(
            f"QProgressBar {{ background: {C.BORDER}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {score_color(score)}; border-radius: 3px; }}"
        )


class UserDetailDrawer(QFrame):
    closed = Signal()
    region_changed = Signal(int)
    profile_requested = Signal(int)

    def __init__(self, db: Database, criteria_provider, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Drawer")
        self.db = db
        self.criteria_provider = criteria_provider
        self.leads = LeadQueryRepository(db)
        self.users = UserRepository(db)
        self.user_id: int | None = None
        self.focus_group: int | None = None
        self.setMaximumWidth(0)
        self.setMinimumWidth(0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        top = QHBoxLayout()
        top.setContentsMargins(20, 16, 12, 8)
        title = QLabel("User details")
        title.setObjectName("CardTitle")
        top.addWidget(title, 1)
        close = QToolButton()
        close.setIcon(icon("close", C.TEXT_2, 18))
        close.setToolTip("Close (Esc)")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close_drawer)
        top.addWidget(close)
        outer.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {C.SURFACE}; }}")
        outer.addWidget(self.scroll, 1)

        self._anim = QPropertyAnimation(self, b"maximumWidth", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._anim_finished)

    # ------------------------------------------------------------------ #

    @property
    def is_open(self) -> bool:
        return self.maximumWidth() > 0 and self.user_id is not None

    def open_user(self, user_id: int, group_id: int | None = None) -> None:
        self.user_id = user_id
        self.focus_group = group_id
        self.refresh()
        if self.maximumWidth() < DRAWER_WIDTH:
            self.setMinimumWidth(0)
            self._anim.stop()
            self._anim.setStartValue(self.maximumWidth())
            self._anim.setEndValue(DRAWER_WIDTH)
            self._anim.start()

    def _anim_finished(self) -> None:
        # Pin the width once open so the table cannot squeeze the drawer.
        if self._anim.endValue() == DRAWER_WIDTH:
            self.setMinimumWidth(DRAWER_WIDTH)

    def close_drawer(self) -> None:
        self.setMinimumWidth(0)
        self._anim.stop()
        self._anim.setStartValue(self.maximumWidth())
        self._anim.setEndValue(0)
        self._anim.start()
        self.user_id = None
        self.closed.emit()

    def refresh(self) -> None:
        if self.user_id is None:
            return
        criteria: LeadCriteria = self.criteria_provider()
        user = self.users.get_row(self.user_id)
        memberships = self.leads.user_memberships(self.user_id, criteria)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 6, 20, 24)
        layout.setSpacing(14)
        if user is None:
            layout.addWidget(QLabel("This user is no longer stored."))
            layout.addStretch(1)
            self.scroll.setWidget(body)
            return
        self._build(layout, user, memberships)
        layout.addStretch(1)
        self.scroll.setWidget(body)

    # ------------------------------------------------------------------ #

    def _build(self, layout: QVBoxLayout, user, memberships: list[tuple[LeadRow, list[dict]]]) -> None:
        uid = user["telegram_user_id"]
        username = user["username"]
        name = " ".join(p for p in (user["first_name"], user["last_name"]) if p)
        display = f"@{username}" if username else (name or f"User {uid}")

        # Identity header
        head = QHBoxLayout()
        head.setSpacing(14)
        avatar = AvatarLabel(72)
        avatar.set_avatar(user["avatar_path"], name or username or str(uid), uid)
        head.addWidget(avatar)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        n = QLabel(name or display)
        n.setStyleSheet("font-size: 17px; font-weight: 700;")
        n.setWordWrap(True)
        titles.addWidget(n)
        sub = QLabel(f"@{username}" if username else "No public username")
        sub.setObjectName("Muted")
        titles.addWidget(sub)
        badges = QHBoxLayout()
        badges.setSpacing(6)
        focus = next((m for m, _ in memberships if m.group_id == self.focus_group), None)
        focus = focus or (memberships[0][0] if memberships else None)
        if focus is not None:
            badges.addWidget(Badge(focus.status, STATUS_COLORS.get(focus.status, C.MUTED)))
        badges.addWidget(Badge("Bot" if user["is_bot"] else "User", C.WARNING if user["is_bot"] else C.INFO))
        if user["is_deleted"]:
            badges.addWidget(Badge("Deleted account", C.DANGER))
        badges.addStretch(1)
        titles.addLayout(badges)
        head.addLayout(titles, 1)
        layout.addLayout(head)

        actions_host = QWidget()
        actions = FlowLayout(actions_host, spacing=8)
        copy_btn = make_button("Copy ID", "copy")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(str(uid)))
        actions.addWidget(copy_btn)
        if username:
            open_btn = make_button("Open in Telegram", "link")
            open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(f"https://t.me/{username}")))
            actions.addWidget(open_btn)
        profile_btn = make_button("Public profile", "user",
                                  tooltip="Reads the public bio and explicit Telegram Business location once.")
        profile_btn.clicked.connect(lambda: self.profile_requested.emit(uid))
        actions.addWidget(profile_btn)
        policy = actions_host.sizePolicy()
        policy.setHeightForWidth(True)
        actions_host.setSizePolicy(policy)
        layout.addWidget(actions_host)
        layout.addWidget(divider())

        # Identity
        layout.addWidget(_section("Identity"))
        layout.addWidget(_kv("User ID", str(uid)))
        layout.addWidget(_kv("Username", f"@{username}" if username else "—"))
        layout.addWidget(_kv("First name", user["first_name"] or "—"))
        layout.addWidget(_kv("Last name", user["last_name"] or "—"))
        layout.addWidget(_kv("Account type", "Bot" if user["is_bot"] else "User"))
        layout.addWidget(_kv("Public profile photo", "Yes" if user["has_photo"] else "No"))
        if user["profile_fetched_at"]:
            layout.addWidget(_kv("Public bio", user["about"] or "—", muted=not user["about"]))

        # Region
        layout.addWidget(divider())
        layout.addWidget(_section("Region"))
        explicit = user["explicit_location"]
        layout.addWidget(_kv("Explicit location", explicit or "Not exposed by Telegram", muted=not explicit))
        effective = user["manual_region"] or user["explicit_region"] or "Unknown"
        layout.addWidget(_kv("Effective region", effective))
        tag_row = QHBoxLayout()
        tag_label = QLabel("Manual tag")
        tag_label.setObjectName("Muted")
        tag_row.addWidget(tag_label)
        tag_row.addStretch(1)
        combo = QComboBox()
        combo.setMinimumWidth(150)
        for label, value in MANUAL_REGIONS:
            combo.addItem(label, value)
        combo.setCurrentIndex(max(0, combo.findData(user["manual_region"])))
        combo.currentIndexChanged.connect(lambda _i: self._set_region(uid, combo.currentData()))
        tag_row.addWidget(combo)
        layout.addLayout(tag_row)
        note = QLabel("Region is never inferred from names, language, avatars or phone numbers — "
                      "only explicit Telegram data or your manual tag is used.")
        note.setObjectName("Help")
        note.setWordWrap(True)
        layout.addWidget(note)

        # Memberships (strictly per group)
        layout.addWidget(divider())
        layout.addWidget(_section(f"Groups ({len(memberships)})"))
        ordered = sorted(memberships, key=lambda m: (m[0].group_id != self.focus_group, -m[0].relevance_score))
        for lead, evidence in ordered:
            layout.addWidget(self._membership_card(lead, evidence))

        foot = QLabel("Only information visible to your account inside authorized groups is shown. "
                      "Evidence snippets are short and have contact details redacted.")
        foot.setObjectName("Help")
        foot.setWordWrap(True)
        layout.addWidget(foot)

    def _membership_card(self, lead: LeadRow, evidence: list[dict]) -> QFrame:
        card = QFrame()
        card.setObjectName("EvidenceCard")
        if lead.group_id == self.focus_group:
            card.setStyleSheet(f"QFrame#EvidenceCard {{ border: 1px solid {C.PRIMARY}; }}")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(6)
        top = QHBoxLayout()
        gname = QLabel(lead.group_name)
        gname.setStyleSheet("font-weight: 700; font-size: 13px;")
        top.addWidget(gname, 1)
        top.addWidget(Badge(lead.status, STATUS_COLORS.get(lead.status, C.MUTED)))
        v.addLayout(top)
        gid = QLabel(f"Group ID: {lead.group_id}")
        gid.setObjectName("Muted")
        v.addWidget(gid)
        joined = from_db_ts(lead.joined_at)
        if joined:
            source = JOINED_SOURCE_LABELS.get(lead.joined_at_source or "", "Telegram")
            v.addWidget(_kv("Joined date", f"{joined.astimezone().strftime('%Y-%m-%d')}  ·  {source}"))
        else:
            v.addWidget(_kv("Joined date", JOINED_UNAVAILABLE, muted=True))
        v.addWidget(_kv("First detected", fmt_local(lead.first_detected_at)))
        v.addWidget(_kv("Last seen by app", fmt_local(lead.last_seen_at)))
        if lead.left_at:
            v.addWidget(_kv("Left group", fmt_local(lead.left_at)))
        score_row = QHBoxLayout()
        sl = QLabel("Business relevance")
        sl.setObjectName("Muted")
        score_row.addWidget(sl)
        score_row.addStretch(1)
        sv = QLabel(f"{lead.relevance_score}%")
        sv.setStyleSheet(f"font-weight: 700; color: {score_color(lead.relevance_score)};")
        score_row.addWidget(sv)
        v.addLayout(score_row)
        v.addWidget(ScoreBar(lead.relevance_score))
        v.addWidget(_kv("Supporting messages", str(lead.supporting_messages_count)))
        if lead.topics:
            chips = QWidget()
            flow = FlowLayout(chips, spacing=6)
            for topic in lead.topics:
                flow.addWidget(Badge(topic, C.PRIMARY))
            v.addWidget(chips)
        if evidence:
            ev_label = QLabel("Evidence from public group messages")
            ev_label.setObjectName("FieldLabel")
            v.addWidget(ev_label)
            for item in evidence:
                snip = QLabel(f"“{item.get('snippet', '')}”")
                snip.setWordWrap(True)
                snip.setStyleSheet(
                    f"background: {C.SURFACE}; border-radius: 8px; padding: 8px; color: {C.TEXT_2};"
                )
                v.addWidget(snip)
                meta = QLabel(f"{item.get('date', '')}  ·  {', '.join(item.get('topics', []))}")
                meta.setObjectName("Help")
                meta.setWordWrap(True)
                v.addWidget(meta)
        else:
            none = QLabel("No relevant public messages observed yet.")
            none.setObjectName("Help")
            v.addWidget(none)
        return card

    def _set_region(self, uid: int, value: str | None) -> None:
        self.users.set_manual_region(uid, value)
        self.region_changed.emit(uid)
        self.refresh()
