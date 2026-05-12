from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from config import get_settings

# Метки reply-клавиатуры. Хэндлеры матчат по точному равенству text — поэтому
# любая правка надписи должна синхронно поменяться в main.py.
BTN_NEARBY = "📍 Рядом"
BTN_FIND = "🔎 По адресу"
BTN_LIST = "📋 Подписки"
BTN_TIER = "💎 Тариф"
BTN_SETTINGS = "⚙️ Настройки"
BTN_REFERRAL = "🎁 Пригласить друга"

GREETING_NEW = (
    "👋 Я слежу за свободными ЭЗС в Беларуси "
    "(сети Маланка, Evika, Battery-fly).\n"
    "Подпишись на нужные локации — пришлю пуш, как только станция освободится.\n\n"
    "Меню под полем ввода — основные действия. /about — про бота."
)

GREETING_RETURNING = "С возвращением! Меню под полем ввода."


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
        "<b>Связь с автором</b>: @AsmodeusGL · tg id <code>630675506</code>"
    )


def main_reply_kb() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода — основная навигация.

    `📍 Рядом` сразу шлёт геолокацию (request_location=True), остальные
    кнопки — обычный текст, который ловится F.text-хэндлерами.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_NEARBY, request_location=True),
                KeyboardButton(text=BTN_FIND),
            ],
            [
                KeyboardButton(text=BTN_LIST),
                KeyboardButton(text=BTN_TIER),
            ],
            [
                KeyboardButton(text=BTN_SETTINGS),
                KeyboardButton(text=BTN_REFERRAL),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def about_kb() -> InlineKeyboardMarkup:
    s = get_settings()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💎 Расширить лимиты — {s.paid_tier_price_stars} ⭐",
                    callback_data="onboard:upgrade",
                )
            ]
        ]
    )
