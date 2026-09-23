from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import errors, types

from app.config import SettingsStore
from app.database.db import Database
from app.database.models import JOINED_SOURCE_EVENT, JOINED_SOURCE_PARTICIPANT, LeadCriteria, LeadFilter
from app.database.repositories import GroupRepository, LeadQueryRepository
from app.services.avatar_service import AvatarService
from app.services.ingest import IngestService
from app.services.notifications import NotificationService
from app.services.scanner import ScanService
from app.telegram.members import HistoryBatch, extract_message, participant_joined_at, user_to_record
from app.telegram.rate_limit import RateLimiter

GID = -1001234
CRITERIA = LeadCriteria(min_score=50, joined_before=datetime(2025, 1, 1).date())
D2024 = datetime(2024, 6, 15, 10, 0, tzinfo=timezone.utc)


def tg_user(uid, username, participant=None, photo=False, bot=False):
    u = types.User(id=uid, first_name=username.title(), username=username, bot=bot,
                   photo=types.UserProfilePhoto(photo_id=uid * 10, dc_id=2) if photo else None)
    if participant is not None:
        u.participant = participant
    return u


def msg(mid, sender, text="", action=None, entities=None):
    return SimpleNamespace(id=mid, sender=sender, action=action, date=datetime.now(timezone.utc),
                           message=text, action_entities=entities)


class _AsyncIter:
    def __init__(self, items, total=None):
        self._items = list(items)
        self.total = total

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class FakeClient:
    def __init__(self, members, messages, member_error=None):
        self.members = members
        self.messages = messages
        self.member_error = member_error
        self.member_calls = 0
        self.history_calls = []

    async def get_input_entity(self, gid):
        return f"entity:{gid}"

    def iter_participants(self, entity, limit=None):
        self.member_calls += 1
        if self.member_error:
            raise self.member_error
        return _AsyncIter(self.members, total=len(self.members))

    async def download_profile_photo(self, uid, file=None, download_big=False):
        with open(file, "wb") as fh:
            fh.write(b"jpg")
        return file

    def iter_messages(self, entity, limit=None, min_id=0):
        self.history_calls.append(min_id)
        items = [m for m in sorted(self.messages, key=lambda m: -m.id) if m.id > min_id][:limit]
        return _AsyncIter(items)


@pytest.fixture()
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    GroupRepository(db).upsert(GID, "Remote Developers", None, "supergroup", 3, enabled=True)
    settings = SettingsStore(tmp_path / "s.json")
    sent = []
    limiter = RateLimiter(0.0)
    ingest = IngestService(db)
    scanner = ScanService(db, settings, limiter, ingest, NotificationService(db, settings, sent.append),
                          AvatarService(db, tmp_path, limiter))
    yield db, scanner, sent
    db.close_all()


def test_participant_join_dates_only_from_real_records():
    assert participant_joined_at(types.ChannelParticipant(user_id=1, date=D2024)) == "2024-06-15 10:00:00"
    # Admin "date" is the promotion date, not the join date.
    admin = types.ChannelParticipantAdmin(1, 2, D2024, types.ChatAdminRights())
    assert participant_joined_at(admin) is None
    assert participant_joined_at(types.ChannelParticipantCreator(user_id=1, admin_rights=types.ChatAdminRights())) is None
    assert participant_joined_at(None) is None


def test_extract_message_handles_service_actions():
    batch = HistoryBatch()
    alice = tg_user(1, "alice")
    bob = tg_user(2, "bob")
    extract_message(msg(5, alice, action=types.MessageActionChatJoinedByLink(inviter_id=9)), GID, batch)
    extract_message(msg(6, alice, action=types.MessageActionChatAddUser(users=[2]), entities=[bob]), GID, batch)
    extract_message(msg(7, alice, action=types.MessageActionChatDeleteUser(user_id=2)), GID, batch)
    extract_message(msg(8, types.Channel(id=5, title="c", photo=types.ChatPhotoEmpty(), date=D2024), "anon"),
                    GID, batch)
    extract_message(msg(9, alice, "python developer"), GID, batch)
    assert [u.telegram_user_id for u, _ in batch.joins] == [1, 2]
    assert batch.leaves[0][0] == 2
    assert len(batch.messages) == 1 and batch.messages[0].text == "python developer"
    assert batch.max_id == 9


def test_scan_group_is_incremental_and_member_sync_is_throttled(env):
    db, scanner, sent = env
    members = [tg_user(1, "alicedev", types.ChannelParticipant(user_id=1, date=D2024), photo=True),
               tg_user(2, "bob"),  # no participant record -> join date unknown
               tg_user(3, "helperbot", bot=True)]
    text = "We're hiring a remote full stack developer for our SaaS startup (React, Node.js, AWS)"
    messages = [msg(10, members[0], text), msg(11, members[1], "hello"), msg(12, members[1], text)]
    client = FakeClient(members, messages)

    summary = asyncio.run(scanner.run_cycle(client, is_bot=False))
    assert summary.groups_scanned == 1 and summary.new_users == 3
    group = GroupRepository(db).get(GID)
    assert group.last_message_id == 12 and group.last_member_sync_at is not None

    rows = {r.username: r for r in LeadQueryRepository(db).query(LeadFilter(include_bots=True), CRITERIA)}
    assert rows["alicedev"].joined_at == "2024-06-15 10:00:00"
    assert rows["alicedev"].joined_at_source == JOINED_SOURCE_PARTICIPANT
    assert rows["bob"].joined_at is None
    assert rows["alicedev"].is_match and rows["bob"].is_match and not rows["helperbot"].is_match
    assert len(sent) == 2
    assert rows["alicedev"].avatar_path and rows["alicedev"].avatar_path.endswith("1_10.jpg")

    # Second cycle: history is read from the last id, members are not re-downloaded.
    client.messages.append(msg(13, members[1], "anyone hiring?"))
    asyncio.run(scanner.run_cycle(client, is_bot=False))
    assert client.member_calls == 1
    assert client.history_calls[-1] == 12
    assert GroupRepository(db).get(GID).last_message_id == 13
    assert len(sent) == 2  # no duplicate notifications


def test_join_event_sets_real_join_date(env):
    db, scanner, _ = env
    batch = HistoryBatch()
    batch.joins.append((user_to_record(tg_user(7, "carol")), "2024-02-01 08:00:00"))
    scanner.apply_batch(GID, batch)
    row = LeadQueryRepository(db).query(LeadFilter(), CRITERIA)[0]
    assert row.joined_at == "2024-02-01 08:00:00" and row.joined_at_source == JOINED_SOURCE_EVENT


def test_inaccessible_group_is_marked_and_others_continue(env):
    db, scanner, _ = env

    class Denied(FakeClient):
        def iter_messages(self, entity, limit=None, min_id=0):
            raise errors.ChannelPrivateError(request=None)

    client = Denied([], [], member_error=errors.ChatAdminRequiredError(request=None))
    summary = asyncio.run(scanner.run_cycle(client, is_bot=False))
    assert summary.groups_failed == 1
    assert GroupRepository(db).get(GID).status == "inaccessible"
