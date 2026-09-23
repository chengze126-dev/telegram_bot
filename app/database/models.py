"""Plain data objects shared between the Telegram layer, services and UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

REGIONS = ("US", "EU", "Other", "Unknown")

JOINED_SOURCE_PARTICIPANT = "participant_record"  # date from Telegram's participant object
JOINED_SOURCE_EVENT = "join_event"  # date of a join/add service message or live join update

JOINED_SOURCE_LABELS = {
    JOINED_SOURCE_PARTICIPANT: "Telegram membership record",
    JOINED_SOURCE_EVENT: "Telegram join event",
}

JOINED_UNAVAILABLE = "Joined date unavailable"


@dataclass
class UserRecord:
    """A Telegram user as exposed by the API to the authenticated account."""

    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    is_bot: bool = False
    is_deleted: bool = False
    has_photo: bool = False
    photo_id: int | None = None

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or f"User {self.telegram_user_id}"


@dataclass
class MessageRecord:
    """A message visible in an authorized group. Text is analysed, never stored in full."""

    telegram_group_id: int
    message_id: int
    sender: UserRecord
    date: datetime
    text: str


@dataclass
class TelegramGroup:
    id: int
    telegram_group_id: int
    group_name: str
    username: str | None
    group_type: str
    enabled: bool
    explicit_location: str | None
    member_count: int | None
    status: str
    last_error: str | None
    last_scan_at: str | None
    last_member_sync_at: str | None
    last_message_id: int


@dataclass
class LeadCriteria:
    min_score: int
    joined_before: date


@dataclass
class RelevanceState:
    topic_hits: dict[str, int] = field(default_factory=dict)
    supporting_messages_count: int = 0
    evidence: list[dict] = field(default_factory=list)
    relevance_score: int = 0
    detected_topics: list[str] = field(default_factory=list)


@dataclass
class LeadFilter:
    group_id: int | None = None
    search: str = ""
    search_field: str = "all"  # all | username | user_id | name | group | topic
    region: str = "All"
    min_score: int = 0
    topic: str | None = None
    joined_before: date | None = None
    joined_known: str = "any"  # any | known | unknown
    new_today: bool = False
    has_avatar: bool = False
    matches_only: bool = False
    include_bots: bool = False
    limit: int = 20000


@dataclass
class LeadRow:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    group_id: int
    group_name: str
    joined_at: str | None
    joined_at_source: str | None
    first_detected_at: str
    last_seen_at: str
    left_at: str | None
    region: str
    relevance_score: int
    topics: list[str]
    supporting_messages_count: int
    is_bot: bool
    is_deleted: bool
    has_photo: bool
    avatar_path: str | None
    is_match: bool
    is_new_today: bool

    @property
    def status(self) -> str:
        if self.is_deleted:
            return "Deleted"
        if self.left_at:
            return "Left"
        if self.is_bot:
            return "Bot"
        if self.is_match:
            return "Match"
        if self.is_new_today:
            return "New"
        return "Tracked"

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or f"User {self.user_id}"


@dataclass
class DashboardStats:
    total_groups: int = 0
    enabled_groups: int = 0
    total_users: int = 0
    matching_users: int = 0
    new_today: int = 0
    business_matches: int = 0


@dataclass
class GroupStats:
    telegram_group_id: int
    group_name: str
    group_type: str
    enabled: bool
    status: str
    last_error: str | None
    last_scan_at: str | None
    member_count: int | None
    explicit_location: str | None
    users: int
    matches: int
    new_today: int
