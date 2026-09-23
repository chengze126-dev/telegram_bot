from __future__ import annotations

import csv
from datetime import date, datetime, timezone

import pytest

from app.config import SettingsStore
from app.database.db import Database
from app.database.models import JOINED_SOURCE_PARTICIPANT, LeadCriteria, LeadFilter, MessageRecord, UserRecord
from app.database.repositories import GroupRepository, LeadQueryRepository, UserRepository
from app.services.export import export_csv
from app.services.ingest import IngestService
from app.services.notifications import NotificationService
from app.services.region import region_from_explicit_location
from app.services.relevance import RelevanceClassifier, redact

G1, G2 = -100111, -100222
CRITERIA = LeadCriteria(min_score=50, joined_before=date(2025, 1, 1))


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "t.db")
    groups = GroupRepository(database)
    groups.upsert(G1, "Remote Developers", None, "supergroup", 10, enabled=True)
    groups.upsert(G2, "Startup Founders", None, "supergroup", 10, enabled=True)
    yield database
    database.close_all()


def user(uid, username=None, **kw):
    return UserRecord(telegram_user_id=uid, username=username, first_name=kw.get("first"), last_name=None,
                      is_bot=kw.get("bot", False), has_photo=kw.get("photo", False), photo_id=kw.get("photo_id"))


def msg(gid, mid, sender, text):
    return MessageRecord(gid, mid, sender, datetime(2025, 5, 1, tzinfo=timezone.utc), text)


def test_classifier_scores_relevant_text_higher():
    c = RelevanceClassifier()
    strong = c.score({t: 1 for t in c.analyze_text("We're hiring a senior React developer, fully remote").topics})
    weak = c.score({t: 1 for t in c.analyze_text("Good morning everyone").topics})
    assert strong >= 60
    assert weak == 0


def test_classifier_keyword_boundaries():
    c = RelevanceClassifier()
    assert "Frontend" in c.analyze_text("I love javascript").topics
    assert "Software Development" not in c.analyze_text("I love javascript").topics  # java != javascript
    assert "AI / ML" not in c.analyze_text("said the maid").topics
    assert "AI / ML" in c.analyze_text("Building an AI agent with LLMs").topics
    assert "Backend" in c.analyze_text("Node.js and .NET backend").topics


def test_profile_topics_from_camel_case_username():
    assert "Software Development" in RelevanceClassifier().profile_topics("JohnDev", None, None)


def test_snippets_are_redacted():
    text = "Contact me at john@example.com or +1 555 123 4567, see https://example.com"
    red = redact(text)
    assert "john@example.com" not in red and "555" not in red and "https" not in red


def test_groups_never_mixed(db):
    ingest = IngestService(db)
    u = user(1, "alice")
    ingest.ingest_messages([msg(G1, 10, u, "Hiring React developers, remote SaaS startup")])
    ingest.ingest_messages([msg(G2, 10, u, "hello")])  # same message id in another group is distinct
    rows = LeadQueryRepository(db).query(LeadFilter(), CRITERIA)
    by_group = {r.group_id: r for r in rows}
    assert set(by_group) == {G1, G2}
    assert by_group[G1].relevance_score > 50
    assert by_group[G2].relevance_score == 0
    assert by_group[G1].group_name == "Remote Developers"
    assert by_group[G2].group_name == "Startup Founders"


def test_messages_are_not_double_counted(db):
    ingest = IngestService(db)
    u = user(1, "alice")
    m = msg(G1, 5, u, "python backend developer")
    ingest.ingest_messages([m])
    ingest.ingest_messages([m])
    row = LeadQueryRepository(db).query(LeadFilter(group_id=G1), CRITERIA)[0]
    assert row.supporting_messages_count == 1


def test_joined_date_never_fabricated(db):
    ingest = IngestService(db)
    ingest.ingest_members(G1, [(user(1, "a"), None, None)])
    row = LeadQueryRepository(db).query(LeadFilter(), CRITERIA)[0]
    assert row.joined_at is None
    assert row.first_detected_at is not None


def test_real_join_date_not_overwritten_by_null(db):
    ingest = IngestService(db)
    ingest.ingest_members(G1, [(user(1, "a"), "2024-06-15 10:00:00", JOINED_SOURCE_PARTICIPANT)])
    ingest.ingest_members(G1, [(user(1, "a"), None, None)])
    row = LeadQueryRepository(db).query(LeadFilter(), CRITERIA)[0]
    assert row.joined_at == "2024-06-15 10:00:00"
    assert row.joined_at_source == JOINED_SOURCE_PARTICIPANT


def test_joined_before_filter_only_applies_to_known_dates(db):
    ingest = IngestService(db)
    text = "Hiring remote full stack developer for our SaaS startup, React + Node.js on AWS"
    ingest.ingest_members(G1, [
        (user(1, "old"), "2024-06-15 10:00:00", JOINED_SOURCE_PARTICIPANT),
        (user(2, "new"), "2025-03-01 10:00:00", JOINED_SOURCE_PARTICIPANT),
        (user(3, "unknown"), None, None),
    ])
    ingest.ingest_messages([msg(G1, i, user(i, n), text) for i, n in ((1, "old"), (2, "new"), (3, "unknown"))])
    repo = LeadQueryRepository(db)
    matches = {r.username for r in repo.query(LeadFilter(matches_only=True), CRITERIA)}
    assert matches == {"old", "unknown"}
    filtered = {r.username for r in repo.query(LeadFilter(joined_before=date(2025, 1, 1)), CRITERIA)}
    assert filtered == {"old", "unknown"}
    unknown = {r.username for r in repo.query(LeadFilter(joined_known="unknown"), CRITERIA)}
    assert unknown == {"unknown"}


def test_notifications_deduplicated_per_user_and_group(db, tmp_path):
    ingest = IngestService(db)
    text = "Hiring remote full stack developer for our SaaS startup, React + Node.js on AWS"
    u = user(1, "JohnDev")
    ingest.ingest_messages([msg(G1, 1, u, text), msg(G2, 1, u, text)])
    sent = []
    notifier = NotificationService(db, SettingsStore(tmp_path / "s.json"), sent.append)
    assert notifier.flush_pending() == 2
    assert notifier.flush_pending() == 0
    ingest.ingest_messages([msg(G1, 2, u, text)])
    assert notifier.flush_pending() == 0
    assert all(n.title == "New Telegram Lead Found" for n in sent)
    assert "Joined date unavailable" in sent[0].body
    assert "Relevance:" in sent[0].body


def test_region_only_from_explicit_or_manual(db):
    ingest = IngestService(db)
    ingest.ingest_members(G1, [(user(1, "a"), None, None)])
    repo = LeadQueryRepository(db)
    assert repo.query(LeadFilter(), CRITERIA)[0].region == "Unknown"
    ingest.apply_public_profile(1, "Python developer", "Berlin, Germany")
    assert repo.query(LeadFilter(), CRITERIA)[0].region == "EU"
    UserRepository(db).set_manual_region(1, "US")
    assert repo.query(LeadFilter(region="US"), CRITERIA)[0].region == "US"
    assert region_from_explicit_location(None) is None
    assert region_from_explicit_location("Austin, Texas") == "US"
    assert region_from_explicit_location("Tokyo, Japan") == "Other"


def test_search_and_export(db, tmp_path):
    ingest = IngestService(db)
    ingest.ingest_messages([msg(G1, 1, user(42, "=evil"), "DevOps with Kubernetes and Terraform")])
    repo = LeadQueryRepository(db)
    assert repo.query(LeadFilter(search="42", search_field="user_id"), CRITERIA)
    assert repo.query(LeadFilter(search="devops", search_field="topic"), CRITERIA)
    assert repo.query(LeadFilter(search="remote dev"), CRITERIA)  # group name
    out = tmp_path / "x.csv"
    assert export_csv(repo.query(LeadFilter(), CRITERIA), out) == 1
    with open(out, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "User ID"
    assert rows[1][1] == "'=evil"
    assert rows[1][6] == "Joined date unavailable"


def test_dashboard_and_group_stats(db):
    ingest = IngestService(db)
    text = "Hiring remote full stack developer for our SaaS startup, React + Node.js on AWS"
    ingest.ingest_messages([msg(G1, 1, user(1, "a"), text), msg(G1, 2, user(2, "b"), "hi"),
                            msg(G2, 1, user(3, "c", bot=True), text)])
    repo = LeadQueryRepository(db)
    stats = repo.dashboard_stats(CRITERIA)
    assert stats.total_groups == 2 and stats.total_users == 2 and stats.matching_users == 1
    per_group = {g.telegram_group_id: g for g in repo.group_stats(CRITERIA)}
    assert per_group[G1].users == 2 and per_group[G1].matches == 1
    assert per_group[G2].users == 0
    assert len(repo.detections_per_day(14)) == 14


def test_left_members_after_complete_sync(db):
    ingest = IngestService(db)
    ingest.ingest_members(G1, [(user(1, "a"), None, None), (user(2, "b"), None, None)])
    assert ingest.mark_missing_left(G1, {1}) == 1
    rows = {r.user_id: r for r in LeadQueryRepository(db).query(LeadFilter(), CRITERIA)}
    assert rows[2].status == "Left"


def test_delete_group_cascades(db):
    ingest = IngestService(db)
    ingest.ingest_messages([msg(G1, 1, user(1, "a"), "python")])
    GroupRepository(db).delete(G1)
    assert LeadQueryRepository(db).query(LeadFilter(), CRITERIA) == []
