"""
bookers/holland2stay.py — Holland2Stay 自动下单实现
====================================================

Thin wrapper：直接转发给 ``bookers.h2s_checkout.try_book``（H2S 结账流程本身
在那个文件里）。零行为变更——所有 H2S 现有路径（dry_run / payment_method /
prewarmed 复用 / blocked / race_lost 等 phase）原样保留。
"""
from __future__ import annotations

import logging

from .base import AbstractBooker, BookingRequest, BookingResult


def try_book(*args, **kwargs) -> BookingResult:
    """转发给 ``bookers.h2s_checkout.try_book``。

    调用时才 import：h2s_checkout 会 import 浏览器层，而 bookers/__init__ 加载本
    文件时它可能还没初始化完（先 import h2s_checkout → bookers/__init__ → 本文件）。
    保留这个模块级名字，测试照旧 patch ``bookers.holland2stay.try_book``。
    """
    from bookers.h2s_checkout import try_book as _try_book

    return _try_book(*args, **kwargs)


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
            payment_method=ab.payment_method,
            prewarmed=request.prewarmed,
        )
