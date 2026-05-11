from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_settings

GREETING_NEW = (
    "👋 Я слежу за свободными ЭЗС в Беларуси "
    "(сети Маланка, Evika, Battery-fly).\n"
    "Подпишись на нужные локации — пришлю пуш, как только станция освободится."
)

GREETING_RETURNING = "С возвращением! Чем помочь?"


def about_text() -> str:
    s = get_settings()
    return (
        "<b>О боте</b>\n\n"
        "Charger Watcher — неофициальный сервис, который следит за статусом "
        "зарядных станций в Беларуси (Маланка, Evika, Battery-fly) и уведомляет, "
        "когда выбранная точка освобождается.\n\n"
        f"<b>Бесплатный тариф</b>: {s.free_tier_max_subscriptions} подписка, "
        f"задержка уведомления {s.free_tier_notify_delay_sec // 60} мин.\n"
        f"<b>Платный тариф</b>: до {s.paid_tier_max_subscriptions} подписок, "
        f"мгновенные уведомления — {s.paid_tier_price_stars} ⭐ / "
        f"{s.paid_tier_duration_days} дней.\n\n"
        "Данные берутся с публичных эндпоинтов операторов. Сервис не аффилирован "
        "с операторами ЭЗС.\n\n"
        "<b>Связь с автором</b>: @AsmodeusGL · tg id <code>630675506</code>\n\n"
        "<i>Управление — через кнопки выше.</i>"
    )


def onboarding_kb() -> InlineKeyboardMarkup:
    s = get_settings()
    upgrade_label = f"💎 Расширить лимиты — {s.paid_tier_price_stars} ⭐"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти зарядку рядом", callback_data="onboard:nearby")],
            [InlineKeyboardButton(text="📍 Поиск по адресу", callback_data="onboard:find")],
            [InlineKeyboardButton(text="📋 Мои подписки", callback_data="onboard:list")],
            [InlineKeyboardButton(text=upgrade_label, callback_data="onboard:upgrade")],
            [InlineKeyboardButton(text="ℹ️ О боте", callback_data="onboard:about")],
        ]
    )
