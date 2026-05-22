"""
bookers/holland2stay.py — Holland2Stay 自动下单实现
====================================================

Thin wrapper：直接转发给 ``booker.try_book``。零行为变更——所有 H2S 现有
路径（dry_run / cancel_enabled / payment_method / prewarmed 复用 / blocked /
race_lost 等 phase）原样保留。

将来如果要把 H2S checkout 实现从 ``booker.py`` 重构进这里，可以独立做——
当前阶段只关心架构清洁，业务逻辑不动。
"""
from __future__ import annotations

import logging

from booker import try_book

from .base import AbstractBooker, BookingRequest, BookingResult


logger = logging.getLogger(__name__)


class HollandStayBooker(AbstractBooker):
    """
    Holland2Stay GraphQL checkout booker。

    ``listing.source`` 必须为 ``"holland2stay"``——分发器会保证这点，
    所以这里不做防御性校验。
    """

    source = "holland2stay"

    def book(self, request: BookingRequest) -> BookingResult:
        # H2S 专属的 auto_book 配置字段。request.dry_run 是权威值——
        # 调用方（book_with_fallback）已经合并了 user.auto_book.dry_run，
        # 这里不再额外 or 一次，避免"打开 dry_run 后无法关掉"的怪行为。
        ab = request.user.auto_book
        return try_book(
            request.listing,
            ab.email,
            ab.password,
            dry_run=request.dry_run,
            cancel_enabled=ab.cancel_enabled,
            payment_method=ab.payment_method,
            prewarmed=request.prewarmed,
        )
