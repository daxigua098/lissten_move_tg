"""资源库服务：按 tg_id 归一、状态机、人工锁定与筛选（F-R01 / F-R08 / F-R10）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.resource_probe import ProbeMetrics
from app.db.models import (
    PROBE_FAILED,
    RESOURCE_ADOPTED,
    RESOURCE_CANDIDATE,
    RESOURCE_PROBED,
    ResourceProbeLog,
    TgResource,
)
from app.db.session import session_scope
from app.services import resource_service
from app.services.resource_service import ResourceRef


def metrics(**overrides) -> ProbeMetrics:
    """构造一组指标，测试只覆盖关心的字段。"""
    base = {
        "sample_size": 100,
        "posts_per_day": 12.0,
        "human_ratio": 0.8,
        "unique_senders": 30,
        "link_density": 5.0,
        "activity_score": 62.5,
        "lead_potential": 40.0,
        "language": "zh",
        "country": "CN",
        "categories": ("求职招聘",),
    }
    base.update(overrides)
    return ProbeMetrics(**base)


async def _upsert(ref: ResourceRef, **kwargs) -> TgResource:
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(session, ref, **kwargs)
        assert outcome.resource is not None
        return outcome.resource


class TestUpsert:
    async def test_creates_candidate(self, db) -> None:
        resource = await _upsert(
            ResourceRef(tg_id=1001, title="第一个群", username="first_group"),
            discovered_by="keyword",
            discovered_from="搜索词：求职",
        )

        assert resource.status == RESOURCE_CANDIDATE
        assert resource.tg_id == 1001
        assert resource.discovered_by == "keyword"
        assert resource.discovered_from == "搜索词：求职"
        assert resource.first_seen_at is not None

    async def test_same_tg_id_does_not_create_second_row(self, db) -> None:
        first = await _upsert(ResourceRef(tg_id=2002, title="群"))
        second = await _upsert(
            ResourceRef(tg_id=2002, title="群", username="group_2002"),
            discovered_by="link",
        )

        assert first.id == second.id
        assert second.username == "group_2002"
        async with session_scope() as session:
            rows = list(await session.scalars(select(TgResource)))
        assert len(rows) == 1

    async def test_invite_only_record_gets_tg_id_later(self, db) -> None:
        """私密群先按邀请链接入库，加入后回填数字 ID，不产生第二条记录。"""
        staged = await _upsert(
            ResourceRef(
                title="私密群",
                invite_link="https://t.me/+AbCdEf123456",
                chat_type="group",
            ),
            discovered_by="link",
        )
        assert staged.tg_id is None

        merged = await _upsert(
            ResourceRef(title="私密群", tg_id=3003, invite_link="https://t.me/+AbCdEf123456"),
        )

        assert merged.id == staged.id
        assert merged.tg_id == 3003
        async with session_scope() as session:
            assert len(list(await session.scalars(select(TgResource)))) == 1

    async def test_title_change_is_recorded(self, db) -> None:
        await _upsert(ResourceRef(tg_id=4004, title="老名字"))
        renamed = await _upsert(ResourceRef(tg_id=4004, title="新名字"))

        history = resource_service.load_title_history(renamed)
        assert history
        assert history[-1]["title"] == "老名字"
        assert renamed.title == "新名字"

    async def test_blacklisted_resource_is_not_collected(self, db) -> None:
        resource = await _upsert(ResourceRef(tg_id=5005, title="拉黑群", username="bad_group"))
        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            await resource_service.set_blacklisted(session, found, True, reason="广告太多")

        async with session_scope() as session:
            outcome = await resource_service.upsert_resource(
                session,
                ResourceRef(tg_id=5005, title="拉黑群", username="bad_group"),
                discovered_by="keyword",
            )
        assert outcome.resource is None
        assert outcome.skipped == "blacklist"


class TestMetricsApply:
    async def test_probe_moves_candidate_to_probed_and_logs(self, db) -> None:
        resource = await _upsert(ResourceRef(tg_id=6006, title="求职群"))

        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            updated = await resource_service.apply_metrics(
                session,
                found,
                metrics(),
                member_count=8800,
                about="每天发布招聘信息",
                requests_used=3,
            )

        assert updated.status == RESOURCE_PROBED
        assert updated.activity_score == 62.5
        assert updated.member_count == 8800
        assert updated.last_probed_at is not None
        async with session_scope() as session:
            logs = list(
                await session.scalars(
                    select(ResourceProbeLog).where(ResourceProbeLog.resource_id == resource.id)
                )
            )
        assert len(logs) == 1
        assert logs[0].requests_used == 3

    async def test_manual_fields_survive_refresh(self, db) -> None:
        """人工改过的行业标签，刷新后必须保持不变（F-R10）。"""
        resource = await _upsert(ResourceRef(tg_id=7007, title="群"))

        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            await resource_service.apply_metrics(session, found, metrics())
        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            manual = await resource_service.update_manual_fields(
                session,
                found,
                categories=["我自己标的教育培训"],
                note="重点跟",
            )
        assert manual.categories == '["我自己标的教育培训"]'
        assert "categories" in resource_service.load_locked(manual)

        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            refreshed = await resource_service.apply_metrics(session, found, metrics())

        assert refreshed.categories == '["我自己标的教育培训"]'
        # 没锁的字段照常更新
        assert refreshed.language == "zh"
        assert refreshed.note == "重点跟"

    async def test_failed_probe_keeps_previous_metrics(self, db) -> None:
        resource = await _upsert(ResourceRef(tg_id=8008, title="群"))
        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            await resource_service.apply_metrics(session, found, metrics())
        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            failed = await resource_service.apply_metrics(
                session,
                found,
                ProbeMetrics(),
                result=PROBE_FAILED,
                error="对话不存在",
            )

        assert failed.status == RESOURCE_PROBED
        assert failed.activity_score == 62.5  # 上次的指标没被擦掉
        assert failed.probe_error == "对话不存在"


class TestRefreshPolicy:
    async def test_layered_refresh_intervals(self, db) -> None:
        candidate = await _upsert(ResourceRef(tg_id=9009, title="候选"))
        assert resource_service.refresh_days(candidate, db) == 7

        async with session_scope() as session:
            found = await resource_service.require_resource(session, candidate.id)
            adopted = await resource_service.mark_adopted(session, found, adopted_by="admin")
        assert adopted.status == RESOURCE_ADOPTED
        assert resource_service.refresh_days(adopted, db) == 1
        assert adopted.adopted_review_at is not None

    async def test_low_value_uses_monthly_interval(self, db) -> None:
        resource = await _upsert(ResourceRef(tg_id=1010, title="僵尸群"))
        async with session_scope() as session:
            found = await resource_service.require_resource(session, resource.id)
            stale = await resource_service.apply_metrics(
                session,
                found,
                metrics(activity_score=3.0, last_active_at=datetime(2025, 1, 1, tzinfo=UTC)),
            )

        assert resource_service.is_low_value(stale, db.resource.activity_threshold) is True
        assert resource_service.refresh_days(stale, db) == 30

    async def test_freshness_buckets(self, db) -> None:
        resource = await _upsert(ResourceRef(tg_id=1011, title="群"))
        assert resource_service.data_freshness(resource) == "new"

        now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        resource.last_probed_at = now - timedelta(hours=2)
        assert resource_service.data_freshness(resource, now=now) == "fresh"
        resource.last_probed_at = now - timedelta(days=3)
        assert resource_service.data_freshness(resource, now=now) == "warm"
        resource.last_probed_at = now - timedelta(days=30)
        assert resource_service.data_freshness(resource, now=now) == "stale"


class TestQueryAndExport:
    async def _seed(self) -> list[int]:
        rows = [
            ("求职大群", 20001, "zh", ["求职招聘"], 70.0, 9000, True),
            ("房产交流", 20002, "zh", ["房产"], 20.0, 800, False),
            ("english chat", 20003, "en", ["游戏"], 55.0, 3000, False),
        ]
        ids: list[int] = []
        for title, tg_id, language, categories, score, members, index_group in rows:
            resource = await _upsert(ResourceRef(tg_id=tg_id, title=title))
            async with session_scope() as session:
                found = await resource_service.require_resource(session, resource.id)
                updated = await resource_service.apply_metrics(
                    session,
                    found,
                    metrics(
                        activity_score=score,
                        language=language,
                        categories=tuple(categories),
                        is_index_group=index_group,
                    ),
                    member_count=members,
                )
                ids.append(updated.id)
        return ids

    async def test_filters_by_language_and_activity(self, db) -> None:
        await self._seed()
        async with session_scope() as session:
            rows, total = await resource_service.list_resources(
                session,
                languages=["zh"],
                min_activity=30,
            )

        assert total == 1
        assert rows[0].title == "求职大群"

    async def test_filters_by_category_and_member_range(self, db) -> None:
        await self._seed()
        async with session_scope() as session:
            rows, total = await resource_service.list_resources(
                session,
                categories=["房产"],
                member_min=500,
                member_max=1000,
            )

        assert total == 1
        assert rows[0].tg_id == 20002

    async def test_sort_by_members(self, db) -> None:
        await self._seed()
        async with session_scope() as session:
            rows, _total = await resource_service.list_resources(session, sort="members")

        assert [row.member_count for row in rows] == [9000, 3000, 800]

    async def test_index_group_filter(self, db) -> None:
        await self._seed()
        async with session_scope() as session:
            rows, total = await resource_service.list_resources(session, is_index_group=True)

        assert total == 1
        assert rows[0].is_index_group is True

    async def test_facets_and_stats(self, db) -> None:
        await self._seed()
        async with session_scope() as session:
            facets = await resource_service.facet_options(session)
            overview = await resource_service.stats(session)

        assert facets["languages"] == ["en", "zh"]
        assert "房产" in facets["categories"]
        assert overview["total"] == 3
        assert overview["probed"] == 3

    async def test_csv_export_has_bom_and_headers(self, db) -> None:
        await self._seed()
        async with session_scope() as session:
            rows, _total = await resource_service.list_resources(session, limit=1)
            text = resource_service.resources_to_csv(rows)

        assert text.startswith("\ufeff")
        header = text.splitlines()[0]
        assert "名称" in header
        assert "真人活跃度" in header
        assert len(text.splitlines()) == 2

    async def test_probe_history_is_time_ordered(self, db) -> None:
        resource = await _upsert(ResourceRef(tg_id=30001, title="群"))
        base = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        for offset in (2, 0, 1):
            async with session_scope() as session:
                found = await resource_service.require_resource(session, resource.id)
                await resource_service.apply_metrics(
                    session,
                    found,
                    metrics(activity_score=float(offset + 1)),
                    probed_at=base + timedelta(days=offset),
                )

        async with session_scope() as session:
            logs = await resource_service.probe_history(session, resource.id)

        assert [item.activity_score for item in logs] == [1.0, 2.0, 3.0]
