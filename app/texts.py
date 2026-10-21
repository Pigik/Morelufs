"""Тексты сообщений бота (HTML). Всё, что пришло от пользователя, проходит через esc()."""

from html import escape

from app.config import DELIVERY, PAYMENT
from app.db import Order

TG_LIMIT = 4096


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def money(amount: int) -> str:
    return f"{amount:,} руб.".replace(",", " ")


def full_name(o: Order) -> str:
    return esc(f"{o.surname} {o.name} {o.patronymic}")


def items_block(o: Order) -> str:
    return "\n".join(
        f"{n}. {esc(i.title)} (разм. {esc(i.size)}) — {i.qty} шт. × {money(i.price)}"
        for n, i in enumerate(o.items, 1)
    )


def delivery_block(o: Order) -> str:
    return (
        f"<b>📱 Телефон:</b> +7 {esc(o.phone)}\n"
        f"<b>✈️ Telegram:</b> {esc(o.telegram)}\n"
        f"<b>📧 Email:</b> {esc(o.email)}\n"
        f"<b>🚚 Доставка:</b> {DELIVERY.get(o.delivery, esc(o.delivery))}\n"
        f"<b>📍 Адрес ПВЗ:</b> {esc(o.address)}\n"
        f"<b>💳 Оплата:</b> {PAYMENT.get(o.payment, esc(o.payment))}"
    )


def status_line(o: Order) -> str:
    line = f"<b>Статус:</b> {esc(o.status)}"
    if o.track:
        line += f"\n<b>Трек-номер:</b> <code>{esc(o.track)}</code>"
    return line


def order_for_admin(o: Order, title: str = "📦 Заказ") -> str:
    user = f"@{esc(o.username)}" if o.username else "нет ника"
    qr = f"\n<b>⚙️ QR-код:</b> №{o.qr_index}" if o.qr_index else ""
    return (
        f"<b>{title} #{o.id}</b> · {o.created_msk:%d.%m.%Y %H:%M}\n\n"
        f"<b>👤 Клиент:</b> {full_name(o)}\n"
        f"<b>🆔 Telegram:</b> <a href=\"tg://user?id={o.user_id}\">{o.user_id}</a>, {user}\n"
        f"{delivery_block(o)}{qr}\n"
        f"<b>💬 Комментарий:</b> {esc(o.comment or 'нет')}\n\n"
        f"<b>🛒 Состав:</b>\n{items_block(o)}\n\n"
        f"<b>💵 Итого:</b> <b>{money(o.total)}</b>\n\n"
        f"{status_line(o)}"
    )


def order_for_user(o: Order) -> str:
    return (
        f"<b>Заказ #{o.id}</b> от {o.created_msk:%d.%m.%Y}\n"
        f"{status_line(o)}\n\n"
        f"{delivery_block(o)}\n\n"
        f"<b>🛒 Состав:</b>\n{items_block(o)}\n"
        f"<b>💵 Итого: {money(o.total)}</b>"
    )


def order_created(o: Order) -> str:
    tail = ("Оплатите заказ по QR-коду из приложения — статус обновится после проверки оплаты."
            if o.payment == "qr" else "Менеджер свяжется с вами в ближайшее время.")
    return f"✅ <b>Заказ #{o.id} оформлен!</b>\n\n{order_for_user(o)}\n\n{tail}\nВсе заказы: /my_orders"


def order_button(o: Order) -> str:
    return f"#{o.id} · {o.surname} · {money(o.total)} · {o.status}"[:64]


def status_changed(o: Order) -> str:
    return f"🔔 <b>Заказ #{o.id} обновлён</b>\n\n{status_line(o)}"


def new_user(user_id: int, full: str, username: str | None) -> str:
    nick = f"@{esc(username)}" if username else "без ника"
    return f"Новый пользователь: <a href=\"tg://user?id={user_id}\">{esc(full)}</a>, {nick}, <code>{user_id}</code>"


def chunks(blocks: list[str], sep: str = "\n\n") -> list[str]:
    """Склеивает блоки в сообщения не длиннее лимита Telegram."""
    out, cur = [], ""
    for block in blocks:
        if cur and len(cur) + len(sep) + len(block) > TG_LIMIT:
            out.append(cur)
            cur = ""
        cur = f"{cur}{sep}{block}" if cur else block
    return out + [cur] if cur else out
