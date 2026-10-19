"""
app.forms.user_form.build_user_from_form 测试。

这是 Web 面板「新增/编辑用户」表单 → UserConfig 的绑定层。
核心契约（误改一行就出生产事故）：

1. **密码保留语义**：编辑模式下，密码字段空提交必须保留旧值，
   绝不能清空（用户不会每次编辑都重输密码）。
2. **数值字段范围校验**：超界值静默丢弃，不向上抛 → fail-closed 进 None。
3. **支付方式白名单**：不在 {ideal/visa/mastercard} 内的值 → 默认 ideal，
   阻止攻击者注入未授权的支付方式代码。
4. **列表字段双格式**：checkbox 多选 → list；单值字符串保持单元素。
"""
from __future__ import annotations

import pytest
from werkzeug.datastructures import ImmutableMultiDict

from app.forms.user_form import (
    build_user_from_form,
    build_user_from_form_self,
    VALID_PAYMENT_METHODS,
    DEFAULT_PAYMENT_METHOD,
)
from users import UserConfig, set_app_password, verify_app_password


def _form(*pairs):
    """构造 ImmutableMultiDict，方便测试多值场景（同名 key 多次出现）。"""
    return ImmutableMultiDict(pairs)


# ─── 基础字段绑定 ─────────────────────────────────────────────────


class TestBasicBinding:
    def test_minimal_form_creates_user(self):
        u = build_user_from_form(_form(("name", "Alice")))
        assert isinstance(u, UserConfig)
        assert u.name == "Alice"
        assert u.enabled is False  # checkbox 没勾 → False
        assert len(u.id) == 8  # uuid hex 8 字符前缀

    def test_id_auto_generated_when_not_supplied(self):
        u1 = build_user_from_form(_form(("name", "A")))
        u2 = build_user_from_form(_form(("name", "B")))
        # 两次生成的 id 应该不同
        assert u1.id != u2.id

    def test_id_preserved_in_edit_mode(self):
        u = build_user_from_form(_form(("name", "X")), user_id="abcd1234")
        assert u.id == "abcd1234"

    def test_default_name_when_empty(self):
        u = build_user_from_form(_form(("name", "")))
        assert u.name == "未命名用户"

    def test_enabled_checkbox(self):
        u_on  = build_user_from_form(_form(("name", "x"), ("enabled", "true")))
        u_off = build_user_from_form(_form(("name", "x"), ("enabled", "false")))
        u_no  = build_user_from_form(_form(("name", "x")))
        assert u_on.enabled is True
        assert u_off.enabled is False
        assert u_no.enabled is False

    def test_notifications_enabled_defaults_true(self):
        """NOTIFICATIONS_ENABLED 缺省值 = true（向后兼容旧表单）。"""
        u = build_user_from_form(_form(("name", "x")))
        assert u.notifications_enabled is True

    def test_notification_channels_split_and_normalized(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("NOTIFICATION_CHANNELS", "iMessage, Telegram,EMAIL"),
        ))
        # 大小写归一 + 空白剥离
        assert u.notification_channels == ["imessage", "telegram", "email"]


class TestSharedEmailValidation:
    def test_shared_email_rejects_multiple_recipients(self):
        with pytest.raises(ValueError, match="收件邮箱格式错误"):
            build_user_from_form(_form(
                ("name", "x"),
                ("EMAIL_MODE", "shared"),
                ("EMAIL_TO", "a@example.com,b@example.com"),
            ))

    def test_shared_email_rejects_malformed_address(self):
        with pytest.raises(ValueError, match="收件邮箱格式错误"):
            build_user_from_form(_form(
                ("name", "x"),
                ("EMAIL_MODE", "shared"),
                ("EMAIL_TO", "not-an-email"),
            ))

    def test_custom_email_keeps_legacy_multiple_recipients(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("EMAIL_MODE", "custom"),
            ("EMAIL_TO", "a@example.com,b@example.com"),
        ))
        assert u.email_mode == "custom"
        assert u.email_to == "a@example.com,b@example.com"

    def test_self_edit_forces_shared_and_drops_custom_smtp(self):
        existing = UserConfig(
            name="X",
            id="aaaaaaaa",
            enabled=True,
            app_login_enabled=True,
            email_mode="custom",
            email_smtp_host="smtp.old.example",
            email_username="old-user",
            email_password="OLD",
            email_from="old@example.com",
        )
        u = build_user_from_form_self(_form(
            ("enabled", "true"),
            ("EMAIL_MODE", "custom"),
            ("EMAIL_TO", "owner@example.com"),
            ("EMAIL_SMTP_HOST", "127.0.0.1"),
            ("EMAIL_USERNAME", "attacker"),
            ("EMAIL_PASSWORD", "SECRET"),
            ("EMAIL_FROM", "attacker@example.com"),
        ), existing=existing)
        assert u.email_mode == "shared"
        assert u.email_to == "owner@example.com"
        assert u.email_smtp_host == ""
        assert u.email_username == ""
        assert u.email_password == ""
        assert u.email_from == ""


# ─── 数值字段范围校验（fail-closed） ──────────────────────────────


class TestNumericRangeValidation:
    """超界值静默丢弃（return None），不抛异常，不污染数据。"""

    def test_max_rent_within_range(self):
        u = build_user_from_form(_form(("name", "x"), ("MAX_RENT", "1500")))
        assert u.listing_filter.max_rent == 1500.0

    def test_max_rent_above_50000_dropped(self):
        u = build_user_from_form(_form(("name", "x"), ("MAX_RENT", "99999999")))
        assert u.listing_filter.max_rent is None

    def test_max_rent_below_0_01_dropped(self):
        u = build_user_from_form(_form(("name", "x"), ("MAX_RENT", "0")))
        assert u.listing_filter.max_rent is None

    def test_max_rent_non_numeric_dropped(self):
        u = build_user_from_form(_form(("name", "x"), ("MAX_RENT", "abc")))
        assert u.listing_filter.max_rent is None

    def test_min_floor_within_range(self):
        u = build_user_from_form(_form(("name", "x"), ("MIN_FLOOR", "5")))
        assert u.listing_filter.min_floor == 5

    def test_min_floor_above_200_dropped(self):
        u = build_user_from_form(_form(("name", "x"), ("MIN_FLOOR", "9999")))
        assert u.listing_filter.min_floor is None

    def test_min_floor_negative_dropped(self):
        u = build_user_from_form(_form(("name", "x"), ("MIN_FLOOR", "-1")))
        assert u.listing_filter.min_floor is None

    def test_email_smtp_port_valid(self):
        u = build_user_from_form(_form(("name", "x"), ("EMAIL_SMTP_PORT", "465")))
        assert u.email_smtp_port == 465

    def test_email_smtp_port_out_of_range_falls_back_to_587(self):
        # 端口号超范围 → None → `or 587` 兜底
        u = build_user_from_form(_form(("name", "x"), ("EMAIL_SMTP_PORT", "99999")))
        assert u.email_smtp_port == 587


# ─── 密码保留语义（生产事故防线） ──────────────────────────────────


class TestPasswordPreservation:
    """编辑模式下，密码字段为空必须保留旧值，不能清空。"""

    def test_new_user_empty_password_stored_as_empty(self):
        """新建模式：空密码 → 存为空字符串（用户尚未填）。"""
        u = build_user_from_form(_form(("name", "x")))
        assert u.email_password == ""
        assert u.twilio_token == ""

    def test_edit_blank_password_preserves_existing_email_pw(self):
        existing = UserConfig(name="X", id="aaaaaaaa", email_password="OLD_PW")
        u = build_user_from_form(
            _form(("name", "x")),
            user_id="aaaaaaaa", existing=existing,
        )
        assert u.email_password == "OLD_PW"

    def test_edit_blank_password_preserves_existing_twilio(self):
        existing = UserConfig(name="X", id="aaaaaaaa", twilio_token="OLD_TWILIO")
        u = build_user_from_form(
            _form(("name", "x")),
            user_id="aaaaaaaa", existing=existing,
        )
        assert u.twilio_token == "OLD_TWILIO"

    def test_edit_new_password_overrides_existing(self):
        existing = UserConfig(name="X", id="aaaaaaaa", email_password="OLD_PW")
        u = build_user_from_form(
            _form(("name", "x"), ("EMAIL_PASSWORD", "NEW_PW")),
            user_id="aaaaaaaa", existing=existing,
        )
        assert u.email_password == "NEW_PW"

    def test_edit_whitespace_only_password_treated_as_empty(self):
        """全空白密码 = "用户没填" → 保留旧值，不存储空白字符串。"""
        existing = UserConfig(name="X", id="aaaaaaaa", email_password="OLD_PW")
        u = build_user_from_form(
            _form(("name", "x"), ("EMAIL_PASSWORD", "   ")),
            user_id="aaaaaaaa", existing=existing,
        )
        assert u.email_password == "OLD_PW"

    def test_auto_book_password_preservation(self):
        """auto_book.password 同样适用密码保留语义。"""
        from config import AutoBookConfig
        existing = UserConfig(
            name="X", id="aaaaaaaa",
            auto_book=AutoBookConfig(enabled=True, email="x@y.com", password="H2S_OLD"),
        )
        u = build_user_from_form(
            _form(("name", "x")),
            user_id="aaaaaaaa", existing=existing,
        )
        assert u.auto_book.password == "H2S_OLD"


# ─── 支付方式白名单 ─────────────────────────────────────────────


class TestPaymentMethodWhitelist:
    """payment_method 字段必须走白名单，攻击者不能注入任意字符串。"""

    def test_valid_methods_accepted(self):
        for method in VALID_PAYMENT_METHODS:
            u = build_user_from_form(_form(
                ("name", "x"), ("AUTO_BOOK_PAYMENT_METHOD", method),
            ))
            assert u.auto_book.payment_method == method

    def test_invalid_method_falls_back_to_default(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("AUTO_BOOK_PAYMENT_METHOD", "evil_method_paypal_attacker"),
        ))
        assert u.auto_book.payment_method == DEFAULT_PAYMENT_METHOD

    def test_empty_falls_back_to_default(self):
        u = build_user_from_form(_form(("name", "x")))
        assert u.auto_book.payment_method == DEFAULT_PAYMENT_METHOD

    def test_sql_injection_payload_blocked(self):
        """安全：尝试 SQL 注入 payload 也必须落到默认值。"""
        u = build_user_from_form(_form(
            ("name", "x"),
            ("AUTO_BOOK_PAYMENT_METHOD", "'; DROP TABLE listings; --"),
        ))
        assert u.auto_book.payment_method == DEFAULT_PAYMENT_METHOD


# ─── 列表字段（checkbox + 兼容逗号串） ──────────────────────────


class TestListFields:
    def test_checkbox_multi_value(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("ALLOWED_TYPES", "Studio"),
            ("ALLOWED_TYPES", "1"),
            ("ALLOWED_TYPES", "2"),
        ))
        assert u.listing_filter.allowed_types == ["Studio", "1", "2"]

    def test_single_value_kept_as_single_element(self):
        """单值（即使含逗号）保持 1 个元素 —— 这是当前的实际行为。"""
        u = build_user_from_form(_form(
            ("name", "x"),
            ("ALLOWED_TYPES", "Studio,1,2"),
        ))
        # form.getlist 返回 ['Studio,1,2']，非空 → 直接返回
        assert u.listing_filter.allowed_types == ["Studio,1,2"]

    def test_empty_list_when_absent(self):
        u = build_user_from_form(_form(("name", "x")))
        assert u.listing_filter.allowed_types == []

    def test_whitespace_only_values_filtered_out(self):
        """空字符串/全空白的元素被剔除。"""
        u = build_user_from_form(_form(
            ("name", "x"),
            ("ALLOWED_TYPES", "Studio"),
            ("ALLOWED_TYPES", "   "),
            ("ALLOWED_TYPES", ""),
            ("ALLOWED_TYPES", "Loft"),
        ))
        assert u.listing_filter.allowed_types == ["Studio", "Loft"]


# ─── auto_book 嵌套绑定 ────────────────────────────────────────


class TestAutoBookConfig:
    def test_default_disabled(self):
        u = build_user_from_form(_form(("name", "x")))
        assert u.auto_book.enabled is False
        assert u.auto_book.dry_run is True  # 默认 dry_run

    def test_enabled_explicitly(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("AUTO_BOOK_ENABLED", "true"),
            ("AUTO_BOOK_DRY_RUN", "false"),
            ("AUTO_BOOK_EMAIL", "h2s@example.com"),
            ("AUTO_BOOK_PASSWORD", "h2sPW"),
        ))
        assert u.auto_book.enabled is True
        assert u.auto_book.dry_run is False
        assert u.auto_book.email == "h2s@example.com"
        assert u.auto_book.password == "h2sPW"

    def test_auto_book_listing_filter_independent_from_user_filter(self):
        """auto_book 内嵌的 listing_filter 与用户级 filter 字段独立。"""
        u = build_user_from_form(_form(
            ("name", "x"),
            ("MAX_RENT", "1000"),               # user-level
            ("AUTO_BOOK_MAX_RENT", "800"),       # auto_book-level（更严格）
        ))
        assert u.listing_filter.max_rent == 1000.0
        assert u.auto_book.listing_filter.max_rent == 800.0


class TestEnergySanitization:
    def test_notif_energy_bogus_value_sanitized(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("ALLOWED_ENERGY", "banana"),
        ))
        assert u.listing_filter.allowed_energy == ""

    def test_auto_book_energy_bogus_value_sanitized(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("AUTO_BOOK_ENABLED", "true"),
            ("AUTO_BOOK_ALLOWED_ENERGY", "banana"),
        ))
        assert u.auto_book.listing_filter.allowed_energy == ""

    def test_notif_energy_valid_value_preserved(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("ALLOWED_ENERGY", "A"),
        ))
        assert u.listing_filter.allowed_energy == "A"

    def test_auto_book_energy_valid_value_preserved(self):
        u = build_user_from_form(_form(
            ("name", "x"),
            ("AUTO_BOOK_ENABLED", "true"),
            ("AUTO_BOOK_ALLOWED_ENERGY", "B"),
        ))
        assert u.auto_book.listing_filter.allowed_energy == "B"

    def test_notif_energy_empty_allowed(self):
        u = build_user_from_form(_form(("name", "x")))
        assert u.listing_filter.allowed_energy == ""


# ── app_login / app_password 三种语义（原 test_user_form_app_password.py） ──
#
#   - 留空且未勾 clear → 保留旧 hash
#   - 填新密码         → bcrypt 重新哈希
#   - 勾 clear         → hash=""
#
# 同时验证 app_login_enabled 复选框是否被正确读取。

def _app_form(**fields) -> ImmutableMultiDict:
    """便利：把 kwargs 转成 form 多字段映射。"""
    return ImmutableMultiDict([(k, v) for k, v in fields.items()])


def _existing_with_app_password() -> UserConfig:
    u = UserConfig(name="kong", id="kong0001")
    u.enabled = True
    u.app_login_enabled = True
    set_app_password(u, "old_pw_xyz")
    return u


class TestAppPasswordSemantics:
    def test_new_user_with_password(self):
        form = _app_form(name="kong", enabled="true", app_login_enabled="true", app_password="brand_new_pw")
        u = build_user_from_form(form)
        assert u.app_login_enabled is True
        assert u.app_password_hash != ""
        assert verify_app_password(u, "brand_new_pw") is True

    def test_new_user_without_password(self):
        form = _app_form(name="x", app_login_enabled="true")
        u = build_user_from_form(form)
        assert u.app_login_enabled is True
        assert u.app_password_hash == ""

    def test_empty_password_keeps_existing(self):
        old = _existing_with_app_password()
        form = _app_form(name="kong", enabled="true", app_login_enabled="true")  # app_password 不传
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash == old.app_password_hash
        assert verify_app_password(u, "old_pw_xyz") is True

    def test_new_password_overrides_existing(self):
        old = _existing_with_app_password()
        form = _app_form(name="kong", enabled="true", app_login_enabled="true", app_password="new_pw_456")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash != old.app_password_hash
        assert verify_app_password(u, "new_pw_456") is True
        assert verify_app_password(u, "old_pw_xyz") is False

    def test_clear_password_wipes_hash(self):
        old = _existing_with_app_password()
        form = _app_form(name="kong",
                         app_login_enabled="true",
                         app_password_clear="true")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash == ""
        # 即使开 app_login_enabled，verify 也会 fail-closed
        assert verify_app_password(u, "old_pw_xyz") is False

    def test_clear_takes_precedence_over_new_password(self):
        """同时填新密码 + 勾 clear → 以 clear 为准（防误操作）。"""
        old = _existing_with_app_password()
        form = _app_form(name="kong", app_login_enabled="true",
                         app_password="should_be_ignored",
                         app_password_clear="true")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_password_hash == ""

    def test_login_disabled_default(self):
        """不勾选 enabled checkbox → app_login_enabled=False。"""
        form = _app_form(name="kong", app_password="anything")
        u = build_user_from_form(form)
        assert u.app_login_enabled is False

    def test_login_explicitly_disabled(self):
        old = _existing_with_app_password()
        # checkbox 没勾 → 不出现在 form 中（HTML 行为）→ enabled=False
        form = _app_form(name="kong")
        u = build_user_from_form(form, user_id="kong0001", existing=old)
        assert u.app_login_enabled is False
        # 密码 hash 仍保留（admin 只是临时关）
        assert u.app_password_hash == old.app_password_hash


class TestRevokeOnPasswordChange:
    """密码修改后 token 应被撤销——这是 app/routes/users.py:user_edit 的责任。"""

    def test_edit_user_with_new_password_revokes_tokens(
        self, test_app, tmp_path, monkeypatch, isolated_data_dir
    ):
        # 隔离 DB 到 tmp_path 避免污染
        monkeypatch.setattr("app.db.DB_PATH", tmp_path / "edit.db")
        from app import api_auth
        api_auth.invalidate_token_cache()
        from app.db import storage
        from users import save_users
        # 准备：写一个用户 + 签一枚 token
        u = _existing_with_app_password()
        save_users([u])
        st = storage()
        try:
            _, plaintext = st.create_app_token(
                role="user", user_id=u.id, device_name="dev")
        finally:
            st.close()
        # 准备 admin client
        c = test_app.test_client()
        with c.session_transaction() as s:
            s["authenticated"] = True
            s["role"] = "admin"
            s["csrf_token"] = "test_csrf"
        # POST 修改密码（带所有必需字段）
        r = c.post(f"/users/{u.id}", data={
            "csrf_token": "test_csrf",
            "name": "kong",
            "enabled": "true",
            "NOTIFICATIONS_ENABLED": "true",
            "app_login_enabled": "true",
            "app_password": "totally_new_password",
        }, follow_redirects=False)
        assert r.status_code == 302  # redirect
        # 旧 token 必须已撤销
        st = storage()
        try:
            rows = st.list_app_tokens(user_id=u.id, include_revoked=True)
            assert len(rows) == 1
            assert rows[0]["revoked"] == 1
        finally:
            st.close()
        api_auth.invalidate_token_cache()
