"""
403 / Cloudflare 屏蔽处理测试（适配新 CloakBrowser 路径）。

旧 curl_cffi _post_gql 已删除；现在浏览器内 fetch 检测 403 并通过
BlockedError 向上传播。monitor 级别的 circuit breaker + 通知节流不受影响。

异常从 ``scrapers.base`` 导入——顶层 ``scraper.py`` 那层 re-export 兼容垫片
已删除（生产代码零 import，只有本文件还在用它）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import monitor
from monitor import run_once
from scrapers.base import (
    BlockedError,
    ScrapeNetworkError,
    ScrapeTask,
    UpstreamMaintenanceError,
    is_maintenance_body,
)
from notifier import BaseNotifier
from users import UserConfig
from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
from storage import Storage


_PATCH_SCRAPE = "scrapers.holland2stay._scrape_city_pages"


def _make_fetcher(*responses):
    """构造带 fetch_gql 响应的 mock fetcher。"""
    fetcher = MagicMock()
    fetcher.fetch_gql.side_effect = list(responses) if len(responses) > 1 else responses
    return fetcher


# ─── BlockedError 传播测试 ──────────────────────────────────────

def _h2s_tasks(*pairs):
    from scrapers.base import ScrapeTask
    return [
        ScrapeTask(
            source="holland2stay",
            city_key=city_id,
            city_display=city_name,
            extra={"availability_ids": ["179"]},
        )
        for city_name, city_id in pairs
    ]


class TestBlockedErrorPropagation:
    """BlockedError 必须从 scraper 一路传到 dispatcher，不被中间层吞。"""

    def test_blocked_error_propagates_through_scrape_city_pages(self):
        from scrapers.holland2stay import _scrape_city_pages

        fetcher = _make_fetcher(BlockedError("test"))
        with pytest.raises(BlockedError):
            _scrape_city_pages(fetcher, "Eindhoven", ["29"], ["179"], {})

    def test_blocked_error_propagates_through_dispatch(self):
        from scrapers import dispatch_scrape_tasks

        with patch(_PATCH_SCRAPE, side_effect=BlockedError("test")), \
             patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            with pytest.raises(BlockedError):
                dispatch_scrape_tasks(_h2s_tasks(("Eindhoven", "29")))


# ─── ScrapeNetworkError 传播测试 ─────────────────────────────────

class TestScrapeNetworkErrorPropagation:
    def test_first_page_network_error_raises_scrape_network_error(self):
        from scrapers.holland2stay import _scrape_city_pages

        fetcher = _make_fetcher(ScrapeNetworkError("network fail"))
        with pytest.raises(ScrapeNetworkError):
            _scrape_city_pages(fetcher, "Eindhoven", ["29"], ["179"], {})

    def test_later_page_network_error_returns_previous_pages(self):
        from scrapers.holland2stay import _scrape_city_pages

        first_page = {
            "data": {
                "products": {
                    "items": [],
                    "page_info": {"current_page": 1, "total_pages": 2},
                }
            }
        }
        fetcher = _make_fetcher(first_page, TimeoutError("timeout"))
        result, complete = _scrape_city_pages(fetcher, "Eindhoven", ["29"], ["179"], {})

        assert result == []
        assert complete is False

    def test_dispatch_raises_when_all_cities_fail_on_first_page(self):
        from scrapers import dispatch_scrape_tasks

        with patch(_PATCH_SCRAPE, side_effect=ScrapeNetworkError("page 1 failed")), \
             patch("scrapers.holland2stay.BrowserFetcher", return_value=MagicMock()):
            with pytest.raises(ScrapeNetworkError) as excinfo:
                dispatch_scrape_tasks(
                    _h2s_tasks(("Eindhoven", "29"), ("Amsterdam", "24"))
                )

        assert "全部 2 个任务网络失败" in str(excinfo.value)


# ─── monitor 的 BlockedError 处理（不变：circuit breaker + 通知节流）───

class TestMonitorBlockedHandling:
    def setup_method(self):
        monitor._throttle_notify_block.reset()
        monitor.prewarm_cache.clear()

    def teardown_method(self):
        monitor._throttle_notify_block.reset()
        monitor.prewarm_cache.clear()

    def _run(self, tmp_path, scrape_fn, user, notifications_capture=None):
        cfg = Config(
            check_interval=300,
            cities=[CityFilter(name="E", id=29)],
            availability_filters=[AvailabilityFilter(label="A", id=179)],
            db_path=tmp_path / "test.db", log_level="WARNING",
        )

        class CapturingNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t):
                if notifications_capture is not None:
                    notifications_capture.append(t)
                return True
            async def close(self): pass

        notifs = [(user, CapturingNotifier())]
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        async def go():
            with patch("mcore.scrape_round.dispatch_scrape_tasks", side_effect=scrape_fn):
                await run_once(cfg, storage, notifs, dry_run=False)

        try:
            asyncio.run(go())
        finally:
            storage.close()

    def test_run_once_opens_h2s_circuit_on_blocked_error(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("cf block"))

        self._run(tmp_path, scrape, user)

        assert monitor._source_circuits.fail_streak("holland2stay") == 1
        assert monitor._source_circuits.remaining("holland2stay") > 0

    def test_block_is_not_pushed_to_users(self, tmp_path):
        """抓取被屏蔽是运维问题，用户既判断不了也处置不了。

        每小时被这类消息打扰几次，足够让人把整个通知渠道静音，连真正的房源
        通知一起埋掉。改由 _notify_admin_only 只发 admin（web 面板 + admin push）。
        """
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(
            BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")
        )
        notifs_received: list[str] = []

        self._run(tmp_path, scrape, user, notifs_received)

        assert notifs_received == [], f"屏蔽通知发到用户渠道了: {notifs_received}"

    def _run_capturing_admin(self, tmp_path, scrape_fn, user, admin_msgs):
        """屏蔽通知只发 admin，所以捕获点也挪到 _notify_admin_only。"""
        async def fake_admin(storage, web_notifier, msg, *, kind):
            # 只收熔断这一类。同一条路上还有「长时间被 block」告警，它本来就是
            # admin-only、另有节流，混在一起数会把这条用例变成两个机制的和。
            if kind == "h2s_circuit":
                admin_msgs.append(msg)

        with patch("mcore.round_failures.notify_admin_only", side_effect=fake_admin):
            self._run(tmp_path, scrape_fn, user)

    def test_admin_notification_throttled_30min(self, tmp_path):
        """节流机制本身没变，只是收件人从所有用户改成了 admin。"""
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        admin_msgs: list[str] = []

        for _ in range(3):
            self._run_capturing_admin(tmp_path, scrape, user, admin_msgs)
            monitor._source_circuits.expire("holland2stay")

        assert len(admin_msgs) == 1, (
            f"30 分钟内多次屏蔽应该只发 1 条，实际 {len(admin_msgs)}"
        )

    def test_admin_notification_unthrottled_after_interval(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        scrape = lambda *a, **k: (_ for _ in ()).throw(BlockedError("test"))
        admin_msgs: list[str] = []

        self._run_capturing_admin(tmp_path, scrape, user, admin_msgs)
        assert len(admin_msgs) == 1

        # 把节流窗口拨到过去（原来是减 monotonic 时间戳，现在是拨 deadline）
        monitor._throttle_notify_block.expire()
        monitor._source_circuits.expire("holland2stay")

        self._run_capturing_admin(tmp_path, scrape, user, admin_msgs)
        assert len(admin_msgs) == 2, "超过 30 分钟后应该重新通知"

    def test_long_h2s_block_notifies_admin_to_check_server(self, tmp_path):
        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        cfg = Config(
            check_interval=300,
            cities=[CityFilter(name="E", id=29)],
            availability_filters=[AvailabilityFilter(label="A", id=179)],
            db_path=tmp_path / "test.db", log_level="WARNING",
        )
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        class CapturingNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t): return True
            async def close(self): pass

        class CapturingAdmin:
            def __init__(self):
                self.errors: list[str] = []
            async def send_error(self, msg):
                self.errors.append(msg)
                return True

        admin = CapturingAdmin()
        scrape = lambda *a, **k: (_ for _ in ()).throw(
            BlockedError("Cloudflare WAF 屏蔽（HTTP 403）")
        )

        async def go():
            with patch("mcore.scrape_round.dispatch_scrape_tasks", side_effect=scrape), \
                 patch("mcore.push.dispatch_admin", new=AsyncMock(return_value=1)):
                for _ in range(3):
                    await run_once(
                        cfg, storage,
                        [(user, CapturingNotifier())],
                        web_notifier=admin, dry_run=False,
                    )
                    monitor._source_circuits.expire("holland2stay")

        try:
            asyncio.run(go())
        finally:
            storage.close()

        long_block_msgs = [
            msg for msg in admin.errors
            if "H2S 长时间被 block" in msg
        ]
        assert len(long_block_msgs) == 1
        assert "需要检查服务器" in long_block_msgs[0]


class TestOperationNotAllowedHasItsOwnHandling:
    """``OperationNotAllowedError`` 在抓取侧必须有自己的分支。

    问题
    ----
    monitor **根本没导入过这个类**。``_ROUND_FAILURE_PRIORITY`` 里没有，
    ``run_once`` 和 ``main_loop`` 的 except 链里也没有。于是全源失败时它一路
    落到 ``run_once`` 的兜底::

        except Exception → "抓取阶段未分类错误…这是一条未被归类的内部异常，
                            请查看服务器日志排查"

    **它是全系统最可诉诸行动的一条故障**（去照抄站点那条 operation），却被报成
    「未分类的内部异常」，把排查引向服务器日志而不是 docs/H2S.md §5.1。

    同一个类在 booker 里有专属 phase ``operation_rejected``，还配了三行注释解释
    为什么不能当 blocked 处理——两边待遇差得离谱。

    优先级为什么排在 BlockedError 前面
    ----------------------------------
    两者都是 HTTP 403，但把后者当前者的代价是实打实的：2026-08-19 一次自动预订
    连续两次「重建 CF 会话」各跑一轮完整挑战，75 秒、约 3 MB 代理流量，结束时
    仍是同一个 403，随后误判触发 1 小时登录链路抑制。同一轮里两种 403 都出现时，
    「这条 operation 没登记」是更具体、也更可诉诸行动的那个诊断。
    """

    def test_monitor_knows_the_class(self):
        import monitor
        from scrapers.base import OperationNotAllowedError

        assert monitor.OperationNotAllowedError is OperationNotAllowedError

    def test_it_is_in_the_round_failure_priority(self):
        import monitor
        from scrapers.base import OperationNotAllowedError

        assert OperationNotAllowedError in monitor._ROUND_FAILURE_PRIORITY, (
            "不在优先级表里 → _pick_round_failure 按列表顺序碰运气，"
            "然后落进 run_once 的「未分类内部异常」兜底"
        )

    def test_it_outranks_blocked(self):
        import monitor
        from scrapers.base import BlockedError, OperationNotAllowedError

        pri = monitor._ROUND_FAILURE_PRIORITY
        assert pri.index(OperationNotAllowedError) < pri.index(BlockedError), (
            "被 BlockedError 抢先 → 走换 IP / 熔断 / 登录抑制，"
            "而这三件事对「operation 没登记」一件都不管用"
        )

    def test_pick_round_failure_prefers_it_over_blocked(self):
        import monitor
        from scrapers.base import BlockedError, OperationNotAllowedError

        op = OperationNotAllowedError("GetCategories 被拒")
        picked = monitor._pick_round_failure([
            ("ourdomain:X", BlockedError("403")),
            ("holland2stay:Eindhoven", op),
        ])
        assert picked is op

    def test_maintenance_and_proxy_still_outrank_it(self):
        """反向守卫：这两个仍排在前面。

        代理挂了根本拿不到站点的真实响应，同轮里的 403 多半只是次生现象；
        平台维护则是站点级状态，与我们发什么 operation 无关。
        """
        import monitor
        from scrapers.base import (
            OperationNotAllowedError, ProxyError, UpstreamMaintenanceError,
        )

        pri = monitor._ROUND_FAILURE_PRIORITY
        assert pri.index(ProxyError) < pri.index(OperationNotAllowedError)
        assert pri.index(UpstreamMaintenanceError) < pri.index(OperationNotAllowedError)

    def test_main_loop_does_not_rotate_ip_or_suppress_login(self):
        """守卫：它不继承 BlockedError，所以任何 `except BlockedError` 都接不住。

        这是整个类存在的理由——换 IP / 熔断 / 1 小时登录抑制对它一件都不管用。
        """
        from scrapers.base import BlockedError, OperationNotAllowedError

        assert not issubclass(OperationNotAllowedError, BlockedError)
        assert not issubclass(OperationNotAllowedError, ScrapeNetworkError)

    def test_admin_message_is_actionable_not_unclassified(self):
        """告警文案必须指向真正的修法，而不是「查服务器日志」。"""
        import inspect

        from mcore import round_failures

        src = inspect.getsource(round_failures.handle_scrape_failure)
        marker = "isinstance(exc, OperationNotAllowedError)"
        assert marker in src, (
            "失败处置没有专属分支，只能落进「未分类内部异常」兜底"
        )
        # 分支正文里要出现「照抄」这个动作词——它是唯一的修法
        i = src.index(marker)
        body = src[i:i + 1500]
        assert "照抄" in body, f"告警没说清要做什么: {body[:300]}"


# ── 平台维护态（原 test_scraper_maintenance.py） ────────────────────
#
# 旧 curl_cffi ``_post_gql`` 的 403 streak / maintenance probe 逻辑已退役，
# ``probe_h2s_maintenance`` 也随之删除（浏览器导航本来就经过主站，顺手判掉即可）。
#
# 现在的判定路径：``browser_fetcher._h2s_maintenance_check`` 作为 H2S_PROFILE 的
# ``maintenance_check`` 钩子，在 CF 挑战解开**之后**拿页面标题/正文调
# ``is_maintenance_body``。dispatcher 优先级 + monitor admin 通知逻辑不变。

_MAINT_HTML = """<!DOCTYPE html>
<html><body>
<h1>We'll be back soon</h1>
<p>We are currently performing scheduled maintenance to update our systems.
We anticipate being back online by 11:30(CET).</p>
</body></html>"""


class TestIsMaintenanceBody:
    def test_we_will_be_back_soon(self):
        assert is_maintenance_body("We'll be back soon")
        assert is_maintenance_body("We will be back soon")
        assert is_maintenance_body("WE'LL BE BACK SOON")

    def test_scheduled_maintenance(self):
        assert is_maintenance_body("Currently performing scheduled maintenance")

    def test_full_h2s_maintenance_page(self):
        assert is_maintenance_body(_MAINT_HTML)

    def test_empty_returns_false(self):
        assert is_maintenance_body("") is False
        assert is_maintenance_body(None) is False  # type: ignore[arg-type]

    def test_normal_html_returns_false(self):
        normal = "<html><body><h1>Holland2Stay</h1><p>Find your home</p></body></html>"
        assert is_maintenance_body(normal) is False

    def test_graphql_json_returns_false(self):
        assert is_maintenance_body('{"data":{"products":{"items":[]}}}') is False


class TestDispatcherMaintenancePriority:
    def test_maintenance_wins_over_blocked(self):
        from contextlib import contextmanager
        from scrapers import dispatch_scrape_tasks

        tasks = [
            ScrapeTask(source="holland2stay", city_key="29", city_display="Eindhoven"),
            ScrapeTask(source="ourdomain", city_key="amsterdam", city_display="Amsterdam"),
        ]

        def fake_scrape(self, task):
            if task.source == "holland2stay":
                raise UpstreamMaintenanceError("h2s maintenance")
            raise BlockedError("ourdomain blocked")

        @contextmanager
        def _noop_batch(self):
            yield

        with patch("scrapers.holland2stay.HollandStayScraper.scrape", fake_scrape), \
             patch("scrapers.holland2stay.HollandStayScraper.batch_session", _noop_batch), \
             patch("scrapers.ourdomain.OurDomainScraper.scrape", fake_scrape):
            with pytest.raises(UpstreamMaintenanceError):
                dispatch_scrape_tasks(tasks)


class TestMonitorMaintenanceAdminNotify:
    def setup_method(self):
        import monitor
        monitor._throttle_notify_maintenance.reset()
        monitor.prewarm_cache.clear()

    def teardown_method(self):
        import monitor
        monitor._throttle_notify_maintenance.reset()
        monitor.prewarm_cache.clear()

    def _run(self, tmp_path, *, user_notify_capture, admin_notify_capture):
        import asyncio
        from monitor import run_once
        from notifier import BaseNotifier
        from users import UserConfig
        from config import AutoBookConfig, Config, CityFilter, AvailabilityFilter
        from storage import Storage

        cfg = Config(
            check_interval=300,
            cities=[CityFilter(name="E", id=29)],
            availability_filters=[AvailabilityFilter(label="A", id=179)],
            db_path=tmp_path / "test.db", log_level="WARNING",
        )

        class CapUserNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t):
                user_notify_capture.append(t)
                return True
            async def close(self): pass

        class CapAdminNotifier(BaseNotifier):
            has_channels = True
            async def _send(self, t): return True
            async def send_error(self, message):
                admin_notify_capture.append(message)
                return True
            async def close(self): pass

        user = UserConfig(
            name="A", id="aaaa", enabled=True, notifications_enabled=True,
            notification_channels=[],
            auto_book=AutoBookConfig(enabled=False),
        )
        notifs = [(user, CapUserNotifier())]
        admin = CapAdminNotifier()
        storage = Storage(tmp_path / "test.db", timezone_str="UTC")

        async def go():
            with patch(
                "mcore.scrape_round.dispatch_scrape_tasks",
                side_effect=UpstreamMaintenanceError("test maintenance"),
            ):
                await run_once(cfg, storage, notifs, web_notifier=admin, dry_run=False)

        try:
            asyncio.run(go())
        finally:
            storage.close()

    def test_first_maintenance_notifies_admin_only(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(
                tmp_path,
                user_notify_capture=user_msgs,
                admin_notify_capture=admin_msgs,
            )
        assert user_msgs == [], f"用户通道不应该收到维护通知，实际收到 {user_msgs}"
        assert len(admin_msgs) == 1, f"admin 应该收到 1 条，实际 {len(admin_msgs)}"
        assert "维护" in admin_msgs[0] or "maintenance" in admin_msgs[0].lower()

    def test_maintenance_notify_throttled_1h(self, tmp_path):
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        for _ in range(3):
            with pytest.raises(UpstreamMaintenanceError):
                self._run(
                    tmp_path,
                    user_notify_capture=user_msgs,
                    admin_notify_capture=admin_msgs,
                )
        assert len(admin_msgs) == 1, (
            f"1 小时内多次维护应该只发 1 条，实际 {len(admin_msgs)}"
        )

    def test_maintenance_notify_unthrottled_after_interval(self, tmp_path):
        import monitor
        user_msgs: list[str] = []
        admin_msgs: list[str] = []

        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 1

        monitor._throttle_notify_maintenance.expire()

        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        assert len(admin_msgs) == 2

    def test_maintenance_writes_meta(self, tmp_path):
        from storage import Storage
        user_msgs: list[str] = []
        admin_msgs: list[str] = []
        with pytest.raises(UpstreamMaintenanceError):
            self._run(tmp_path, user_notify_capture=user_msgs, admin_notify_capture=admin_msgs)
        st = Storage(tmp_path / "test.db", timezone_str="UTC")
        try:
            assert st.get_meta("upstream_maintenance_seen_at", default="") != ""
            assert st.get_meta("upstream_maintenance_last_at", default="") != ""
        finally:
            st.close()


class TestMaintenanceSurvivesTheScrapeLoop:
    """维护异常必须原样穿过 ``_scrape_city_pages``，不能被改判成网络错误。

    2026-08-04 / 08-15 生产实测共 20 次误判，日志长这样——一句话里同时写着
    「网络错误」和「平台维护中」::

        [ERROR] holland2stay:Eindhoven 抓取网络失败，已隔离该任务:
                [Eindhoven] 第 1 页网络错误: H2S 平台维护中（页面标题: H2S-Maintenance）

    成因是重抛白名单漏了 ``UpstreamMaintenanceError``，于是它落进下面那条
    ``except Exception`` 被包成 ``ScrapeNetworkError``。

    讽刺的是 ``browser_fetcher.fetch()`` 里专门为这件事写过一段注释——
    「这是维护，不是屏蔽。压成 BlockedError 会让 monitor 走熔断 + admin 告警，
    而不是安静的维护冷却」——它小心翼翼把异常原样抛出来，下一层就给压掉了。

    代价：维护期走的是网络失败路径（5 分钟冷却 + 连续失败计数 + ERROR 日志），
    而不是设计好的维护路径（15 分钟安静冷却 + INFO + 不发用户告警 + dashboard
    banner）。

    ``XiorScraper._post_ajax`` 的同一个位置一直是对的
    （``except (BlockedError, UpstreamMaintenanceError): raise``）。
    """

    @staticmethod
    def _run(exc):
        import scrapers.holland2stay as h2s

        class _Fetcher:
            def fetch_gql(self, *a, **k):
                raise exc

        return h2s._scrape_city_pages(
            _Fetcher(), "Eindhoven", city_ids=["29"],
            availability_ids=["179"], attr_labels={},
        )

    def test_maintenance_is_not_reclassified_as_network_error(self):
        from scrapers.base import ScrapeNetworkError

        with pytest.raises(UpstreamMaintenanceError):
            self._run(UpstreamMaintenanceError("H2S 平台维护中（页面标题: H2S-Maintenance）"))

        # 反向确认：它没有变成 ScrapeNetworkError
        try:
            self._run(UpstreamMaintenanceError("维护"))
        except ScrapeNetworkError as e:                     # pragma: no cover
            pytest.fail(f"维护被改判成网络错误了: {e}")
        except UpstreamMaintenanceError:
            pass

    def test_the_message_does_not_say_network(self):
        """回归到文案层：日志里不该再出现「网络错误: …平台维护中」这种自相矛盾。"""
        try:
            self._run(UpstreamMaintenanceError("H2S 平台维护中"))
        except Exception as e:
            assert "网络错误" not in str(e), (
                f"维护异常的消息里还写着「网络错误」: {e}"
            )

    @pytest.mark.parametrize("exc_name", [
        "RateLimitError", "BlockedError", "OperationNotAllowedError",
        "ScrapeNetworkError", "UpstreamMaintenanceError",
    ])
    def test_every_taxonomy_error_passes_through_unchanged(self, exc_name):
        """整个 taxonomy 都必须原样穿过——漏掉任何一个都是同一类 bug。

        判据很简单：这些类各自代表一种**上层要据以做不同决策**的根因
        （换 IP / 等冷却 / 照抄 operation / 安静等维护 / 查代理）。在这里把
        任何一个压成别的类，上层那套决策就全废了。
        """
        import scrapers.base as base

        cls = getattr(base, exc_name)
        with pytest.raises(cls):
            self._run(cls("boom"))

    def test_unexpected_errors_still_become_network_errors(self):
        """反向守卫：taxonomy 之外的异常（我们自己的 bug）仍然归网络类。

        不是说这个归类有多准，而是上层的隔离与重试语义依赖它，别顺手改掉。
        """
        from scrapers.base import ScrapeNetworkError

        with pytest.raises(ScrapeNetworkError):
            self._run(TypeError("解析炸了"))
