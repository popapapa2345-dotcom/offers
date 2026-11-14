"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ЦЕНА [ссылка] [eng|kit|arb]' -> Бот отправляет оффер.
Плюс: система входа (ENTRY_PRICE TON), заявки админу, доступ только у одобренных.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
try:
    from aiogram.exceptions import TelegramRetryAfter
except ImportError:
    try:
        from aiogram.exceptions import TelegramFloodWait as TelegramRetryAfter
    except ImportError:
        class TelegramRetryAfter(Exception):
            def __init__(self, message): self.message = message
            @property
            def retry_after(self): return 60

from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
TTL_HOURS = int(os.getenv("OFFER_TTL_HOURS", "5"))

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
TON_WALLET = os.getenv("TON_WALLET", "UQ_ТВОЙ_АДРЕС_TON")
ENTRY_PRICE = os.getenv("ENTRY_PRICE", "5.5")
USERS_FILE = os.getenv("USERS_FILE", "allowed_users.json")

# ===== Кому отправлять подарок (НФТ) при нажатии "Передать" =====
GIFT_RECIPIENT = "mindly_offer" 

router = Router()
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

# Маркеры языка в конце команды
LANG_TOKENS = {
    "eng": "en",   # английский
    "kit": "zh",   # китайский
    "arb": "ar",   # арабский
}


def detect_lang(payload: str) -> str:
    for token in payload.lower().split():
        if token in LANG_TOKENS:
            return LANG_TOKENS[token]
    return "ru"


# ===== Технический cooldown (защита от FLOOD_WAIT) =====
SEND_COOLDOWN_SECONDS = 1.0

_last_send_time: float = 0.0
_send_lock = asyncio.Lock()


# ===== Хранилище доступа и заявок =====
def load_allowed_users() -> set[int]:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except Exception:
        return set()


def save_allowed_users() -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(allowed_users), f)
    except Exception:
        logging.exception("Не удалось сохранить список пользователей")


allowed_users: set[int] = load_allowed_users()
entry_pending: set[int] = set()


def user_info_line(user) -> str:
    uname = f"@{user.username}" if getattr(user, "username", None) else "нет"
    return (
        f"ID: <code>{user.id}</code>\n"
        f"Имя: {html.escape(user.full_name)}\n"
        f"Username: {uname}"
    )


def admin_decision_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Добавить", callback_data=f"admin:allow:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin:deny:{user_id}"),
    ]])


def is_peer_flood(e: Exception) -> bool:
    return "PEER_FLOOD" in str(e).upper()


async def send_cooldown() -> None:
    global _last_send_time
    async with _send_lock:
        now = time.monotonic()
        elapsed = now - _last_send_time
        if elapsed < SEND_COOLDOWN_SECONDS:
            await asyncio.sleep(SEND_COOLDOWN_SECONDS - elapsed)
        _last_send_time = time.monotonic()


@dataclass
class Offer:
    id: str
    connection_id: str
    chat_id: int
    message_id: int
    buyer_id: int
    buyer_name: str
    price: str
    currency: str
    lang: str = "ru"
    buyer_username: str = ""
    gift_link: str = "подарок"  # Ссылка на подарок


offers: dict[str, Offer] = {}


# ===== Отправка БЕЗ ретраев на PEER_FLOOD =====
async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
    link_preview_options: LinkPreviewOptions
) -> Message:
    await send_cooldown()
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup, link_preview_options=link_preview_options
        )
    except TelegramRetryAfter as e:
        if is_peer_flood(e):
            raise
        wait_time = getattr(e, "retry_after", 30)
        logging.info(f"FLOOD_WAIT {wait_time}s — один ретрай.")
        await asyncio.sleep(wait_time)
        return await bot.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup, link_preview_options=link_preview_options
        )


async def safe_edit_message_text(
    bot: Bot, text: str, chat_id: int, message_id: int,
    business_connection_id: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None
) -> None:
    if link_preview_options is None:
        link_preview_options = LinkPreviewOptions(is_disabled=True)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=link_preview_options
            )
            return
        except TelegramRetryAfter as e:
            if is_peer_flood(e):
                raise
            wait_time = getattr(e, "retry_after", 60)
            if attempt == max_retries - 1:
                raise
            logging.info(f"Edit FLOOD_WAIT ({wait_time}s).")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logging.debug(f"Edit failed: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1)


def parse_price(payload: str) -> str | None:
    parts = payload.split()
    for part in parts:
        try:
            raw = part.replace(",", ".").lower()
            if raw.replace(".", "").isdigit():
                price_val = float(raw)
                if 0 < price_val < 10000000:
                    formatted = f"{int(price_val) if int(price_val) == price_val else price_val:f}"
                    return formatted.rstrip('0').rstrip('.')
        except ValueError:
            pass
    return None


# ===== Многоязычные тексты офферов =====
def offer_text(offer: Offer) -> str:
    price = offer.price
    cur = offer.currency
    ttl = TTL_HOURS
    gift = offer.gift_link  # Сюда попадет ссылка или слово "подарок"
    
    # Неразрывный пробел (Unicode) для времени, чтобы "м." не съезжала
    nbsp = "\u00A0"

    if offer.lang == "en":
        return (
            f"User <b>{html.escape(offer.buyer_name)}</b> offers you\n"
            f"<b>{price} {cur}</b> for {gift}\n\n"
            f"The offer is valid for another <b>{ttl}h{nbsp}59m.</b>"
        )
    if offer.lang == "zh":
        return (
            f"用户 <b>{html.escape(offer.buyer_name)}</b> 向您出价\n"
            f"<b>{price} {cur}</b> 购买{gift}\n\n"
            f"报价有效期还剩 <b>{ttl}小时59分钟。</b>"
        )
    if offer.lang == "ar":
        return (
            f"المستخدم <b>{html.escape(offer.buyer_name)}</b> يعرض عليك\n"
            f"<b>{price} {cur}</b> مقابل {gift}\n\n"
            f"العرض صالح لمدة <b>{ttl}{nbsp}س{nbsp}و{nbsp}59{nbsp}د أخرى.</b>"
        )

    # Русский (по умолчанию)
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает\n"
        f"Вам <b>{price} {cur}</b> за {gift}\n\n"
        f"Предложение действует ещё <b>{ttl}{nbsp}ч.{nbsp}59{nbsp}м.</b>"
    )


def decline_text(offer: Offer) -> str:
    return {
        "en": "❌ Offer declined",
        "zh": "❌ 报价已被拒绝",
        "ar": "❌ تم رفض العرض",
    }.get(offer.lang, "❌ Предложение отклонено")


def sold_text(offer: Offer) -> str:
    cur = offer.currency
    if offer.lang == "en":
        return (
            f"{GIFT_EMOJI_HTML} The gift has been sold!\n\n"
            f"• Transfer the gift to the buyer to credit {cur}."
        )
    if offer.lang == "zh":
        return (
            f"{GIFT_EMOJI_HTML} 礼物已成功售出！\n\n"
            f"• 请将礼物转给买家以入账 {cur}。"
        )
    if offer.lang == "ar":
        return (
            f"{GIFT_EMOJI_HTML} تم بيع الهدية بنجاح!\n\n"
            f"• قم بتحويل الهدية للمشتري ليتم إضافة {cur}."
        )
    return (
        f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n"
        f"• Передайте подарок покупателю для зачисления {cur}."
    )


def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    transfer_label = {
        "en": "Transfer",
        "zh": "转赠",
        "ar": "إرسال",
    }.get(offer.lang, "Передать")
    
    # Отправляем на GIFT_RECIPIENT (mindly_offer)
    recipient = GIFT_RECIPIENT if GIFT_RECIPIENT else offer.buyer_username
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=transfer_label, url=f"tg://send_gift?to={recipient}")],
    ])


def proposal_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    accept_label, decline_label = {
        "en": ("✓ Accept", "✕ Decline"),
        "zh": ("✓ 接受", "✕ 拒绝"),
        "ar": ("✓ قبول", "✕ رفض"),
    }.get(offer.lang, ("✓ Принять", "✕ Отклонить"))

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=decline_label, callback_data=f"decline:{offer.id}"),
        InlineKeyboardButton(text=accept_label, callback_data=f"accept:{offer.id}"),
    ]])


# ===== /start =====
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if user.id == ADMIN_ID or user.id in allowed_users:
        await message.answer(
            "Привет! 👋\n\nУ вас уже есть доступ к офферам ✅\n"
            "Пишите в чат с бизнесом команду: <code>купить ЦЕНА</code>"
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start"),
    ]])
    await message.answer(
        f"Привет! Это бот по офферам 🎁\n\n"
        f"Вход стоит <b>{ENTRY_PRICE} TON</b>.\n\n"
        f"После оплаты вы сможете отправлять офферы командой <code>купить ЦЕНА</code>.",
        reply_markup=kb,
    )


# ===== Кнопка "Купить вход" -> адрес кошелька =====
@router.callback_query(F.data == "entry:start")
async def entry_start(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user.id == ADMIN_ID or user.id in allowed_users:
        await callback.answer("У вас уже есть доступ ✅")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="entry:paid")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="entry:back")],
    ])
    await callback.message.edit_text(
        f"💳 Оплата входа <b>{ENTRY_PRICE} TON</b>\n\n"
        f"Адрес кошелька (сеть TON):\n<code>{html.escape(TON_WALLET)}</code>\n\n"
        f"Переведите <b>{ENTRY_PRICE} TON</b> на этот адрес, затем нажмите «Я оплатил» "
        f"и пришлите скриншот перевода.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "entry:back")
async def entry_back(callback: CallbackQuery, bot: Bot) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start"),
    ]])
    await callback.message.edit_text(
        f"Привет! Это бот по офферам 🎁\n\nВход стоит <b>{ENTRY_PRICE} TON</b>.",
        reply_markup=kb,
    )
    await callback.answer()


# ===== "Я оплатил" -> заявка админу + ожидание скрина =====
@router.callback_query(F.data == "entry:paid")
async def entry_paid(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user.id == ADMIN_ID or user.id in allowed_users:
        await callback.answer("У вас уже есть доступ ✅")
        return
    entry_pending.add(user.id)
    await callback.message.edit_text(
        "📸 Пришлите <b>скриншот перевода</b> одним сообщением (фото)."
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Заявка на вход ({ENTRY_PRICE} TON)</b>\n\n{user_info_line(user)}\n\n"
            f"Нажал «Я оплатил», ожидает скриншот.",
            reply_markup=admin_decision_kb(user.id),
        )
    except Exception:
        logging.exception("Не удалось отправить заявку админу")
    await callback.answer("Заявка отправлена ✅")


# ===== Приём скриншота -> пересылка админу =====
@router.message((F.photo | F.document), F.chat.type == "private")
async def on_screenshot(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or (user.id != ADMIN_ID and user.id not in entry_pending):
        return
    entry_pending.discard(user.id)
    try:
        await message.copy_to(
            ADMIN_ID,
            caption=f"📸 <b>Скриншот оплаты (вход {ENTRY_PRICE} TON)</b>\n\n{user_info_line(user)}",
            reply_markup=admin_decision_kb(user.id),
        )
        await message.answer("Скриншот отправлен на проверку ✅ Ожидайте.")
    except Exception:
        logging.exception("Не удалось переслать скриншот админу")


# ===== /ledgerteam =====
@router.message(Command("ledgerteam"), F.chat.type == "private")
async def cmd_ledgerteam(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if user.id == ADMIN_ID or user.id in allowed_users:
        await message.answer("У вас уже есть доступ ✅")
        return
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚀 <b>Заявка /ledgerteam</b>\n\n{user_info_line(user)}",
            reply_markup=admin_decision_kb(user.id),
        )
        await message.answer("Заявка отправлена ✅ Ожидайте решения админа.")
    except Exception:
        logging.exception("Не удалось отправить заявку админу")


# ===== Админ принимает/отклоняет =====
@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только для админа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        user_id = int(raw_id)
    except Exception:
        await callback.answer("Некорректные данные.")
        return

    if action == "allow":
        allowed_users.add(user_id)
        save_allowed_users()
        entry_pending.discard(user_id)
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Вам выдан доступ к офферам!</b>\n\n"
                "Теперь пишите в чат с бизнесом команду: <code>купить ЦЕНА</code>",
            )
        except Exception:
            logging.exception(f"Не удалось уведомить {user_id}")
        if callback.message:
            await callback.message.edit_text(
                callback.message.html_text + "\n\n— ✅ <b>Добавлен админом</b>"
            )
        await callback.answer("Пользователь добавлен.")
    else:
        entry_pending.discard(user_id)
        try:
            await bot.send_message(user_id, "❌ Ваша заявка отклонена. Свяжитесь с админом.")
        except Exception:
            pass
        if callback.message:
            await callback.message.edit_text(
                callback.message.html_text + "\n\n— ❌ <b>Отклонено админом</b>"
            )
        await callback.answer("Заявка отклонена.")


# ===== Админ-команды =====
@router.message(Command("grant"), F.chat.type == "private")
async def cmd_grant(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
    except Exception:
        await message.answer("Формат: <code>/grant ID</code>")
        return
    allowed_users.add(user_id)
    save_allowed_users()
    await message.answer(f"✅ Пользователь <code>{user_id}</code> получил доступ.")


@router.message(Command("revoke"), F.chat.type == "private")
async def cmd_revoke(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
    except Exception:
        await message.answer("Формат: <code>/revoke ID</code>")
        return
    allowed_users.discard(user_id)
    save_allowed_users()
    await message.answer(f"🚫 Доступ у <code>{user_id}</code> удалён.")


@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    if not allowed_users:
        await message.answer("Список пуст.")
        return
    text = "👥 <b>Список с доступом:</b>\n" + "\n".join(f"• <code>{u}</code>" for u in sorted(allowed_users))
    await message.answer(text)


# ===== Основная логика офферов =====
@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return

    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match:
        return
    payload = match.group(1).strip()

    lang = detect_lang(payload)

    price = parse_price(payload)
    if price is None:
        await message.answer("Укажите цену! Пример: купить 100 (или купить 100 eng)", quote=False)
        return

    # Ищем ссылку в команде (начинается с http)
    gift_link = "подарок"
    for word in payload.split():
        if word.startswith(("http://", "https://", "t.me/")):
            gift_link = word
            break

    # Валюта (gram, TON, Stars)
    payload_lower = payload.lower()
    if "gram" in payload_lower or "грам" in payload_lower:
        currency = "gram"
    elif "ton" in payload_lower or "тон" in payload_lower or "тн" in payload_lower or "toncoin" in payload_lower:
        currency = "TON"
    else:
        currency = "Stars"

    try:
        # Покупатель — тот, кто пишет команду
        buyer = message.from_user
        if not buyer:
            return
            
        buyer_id = buyer.id
        buyer_username = buyer.username if buyer.username else str(buyer.id)
        buyer_name = (
            f"@{buyer.username}" if getattr(buyer, "username", None)
            else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        )

        # Проверка доступа
        if buyer_id != ADMIN_ID and buyer_id not in allowed_users:
            await message.answer(
                "⛔ У вас нет доступа к офферам.\n"
                "Получите доступ: зайдите в личку с ботом и нажмите /start.",
                quote=False,
            )
            return

        offer_id = secrets.token_urlsafe(8)
        temp_offer = Offer(
            id=offer_id,
            connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            buyer_id=buyer_id,
            buyer_name=buyer_name,
            price=price,
            currency=currency,
            lang=lang,
            buyer_username=buyer_username,
            gift_link=gift_link,
        )

        text = offer_text(temp_offer)
        keyboard = proposal_keyboard(temp_offer)

        msg = await safe_send_message_text(
            bot, text, message.chat.id, message.business_connection_id, keyboard,
            LinkPreviewOptions(is_disabled=True)
        )
        temp_offer.message_id = msg.message_id

        offers[offer_id] = temp_offer

        await bot.delete_business_messages(
            message.business_connection_id, message_ids=[message.message_id]
        )
        logging.info(f"Оффер {offer_id}: создан (цена {price} {currency}, язык {lang}).")

    except Exception as e:
        if is_peer_flood(e):
            logging.error(f"PEER_FLOOD: {e!r}")
            try:
                await message.answer(
                    "⚠️ Telegram временно ограничил отправку сообщений.\n"
                    "Это антиспам-мера на стороне Telegram, не бота.\n"
                    "Попробуйте позже — обычно через 15–30 минут.",
                    quote=False,
                )
            except Exception:
                pass
        else:
            logging.exception(f"Error: {e}")


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    if action == "decline":
        await safe_edit_message_text(
            bot, decline_text(offer), offer.chat_id, offer.message_id,
            offer.connection_id, None
        )
        offers.pop(offer_id, None)
        await callback.answer("Отклонено.")
    else:
        text = sold_text(offer)
        await safe_edit_message_text(
            bot, text, offer.chat_id, offer.message_id,
            offer.connection_id, sold_keyboard(offer)
        )
        offers.pop(offer_id, None)
        await callback.answer("Принято.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"), timeout=15)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())