"""Тексты сообщений бота (HTML). Всё, что пришло от пользователя, проходит через esc()."""

from html import escape

from app.config import DELIVERY, PAYMENT, PAYMENT_STATUS, PAYMENT_TTL_HOURS
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


def days(info: dict) -> str:
    lo, hi = info.get("days_min"), info.get("days_max")
    if lo and hi and lo != hi:
        return f"{lo}–{hi} дн."
    return f"{lo or hi} дн." if lo or hi else ""


def payment_line(o: Order) -> str:
    line = PAYMENT.get(o.payment, esc(o.payment))
    if o.payment_status:
        line += f" — {PAYMENT_STATUS.get(o.payment_status, esc(o.payment_status))}"
    return line


def delivery_block(o: Order, admin: bool = False) -> str:
    """Контакты, доставка и оплата. admin — ещё коды ПВЗ/города и индекс: по ним проверяют отправление."""
    info = o.delivery_info
    service = DELIVERY.get(o.delivery, esc(o.delivery))
    if o.delivery_cost:
        service += f", {money(o.delivery_cost)}"
    if term := days(info):
        service += f", срок {term}"
    label = "Адрес ПВЗ" if o.delivery == "cdek" else "Адрес"
    lines = [
        f"<b>📱 Телефон:</b> +7 {esc(o.phone)}",
        f"<b>✈️ Telegram:</b> {esc(o.telegram)}",
        f"<b>📧 Email:</b> {esc(o.email)}",
        f"<b>🚚 Доставка:</b> {service}",
        f"<b>📍 {label}:</b> {esc(o.address)}",
    ]
    if admin and info.get("cdek_point"):
        lines.append(f"<b>🏷 ПВЗ:</b> <code>{esc(info['cdek_point'])}</code>, "
                     f"код города <code>{esc(info.get('cdek_city_code'))}</code>")
    if admin and (post := info.get("post_address")):
        lines.append(f"<b>🏷 Индекс:</b> <code>{esc(post.get('index'))}</code>")
    lines.append(f"<b>💳 Оплата:</b> {payment_line(o)}")
    return "\n".join(lines)


def status_line(o: Order) -> str:
    line = f"<b>Статус:</b> {esc(o.status)}"
    if o.track:
        line += f"\n<b>Трек-номер:</b> <code>{esc(o.track)}</code>"
    return line


def order_for_admin(o: Order, title: str = "📦 Заказ") -> str:
    user = f"@{esc(o.username)}" if o.username else "нет ника"
    tech = ""
    if o.qr_index:
        tech += f"\n<b>⚙️ QR-код:</b> №{o.qr_index}"
    if o.payment_id:
        tech += f"\n<b>⚙️ Платёж ЮKassa:</b> <code>{esc(o.payment_id)}</code>"
    if o.shipment_id:
        tech += f"\n<b>⚙️ Отправление:</b> <code>{esc(o.shipment_id)}</code>"
    return (
        f"<b>{title} #{o.id}</b> · {o.created_msk:%d.%m.%Y %H:%M}\n\n"
        f"<b>👤 Клиент:</b> {full_name(o)}\n"
        f"<b>🆔 Telegram:</b> <a href=\"tg://user?id={o.user_id}\">{o.user_id}</a>, {user}\n"
        f"{delivery_block(o, admin=True)}{tech}\n"
        f"<b>💬 Комментарий:</b> {esc(o.comment or 'нет')}\n\n"
        f"<b>🛒 Состав:</b>\n{items_block(o)}\n\n"
        f"{totals(o)}\n\n"
        f"{status_line(o)}"
    )


def totals(o: Order) -> str:
    if not o.delivery_cost:
        return f"<b>💵 Итого: {money(o.total)}</b>"
    return (f"Товары: {money(o.goods_total)}\nДоставка: {money(o.delivery_cost)}\n"
            f"<b>💵 Итого: {money(o.total)}</b>")


def order_for_user(o: Order) -> str:
    return (
        f"<b>Заказ #{o.id}</b> от {o.created_msk:%d.%m.%Y}\n"
        f"{status_line(o)}\n\n"
        f"{delivery_block(o)}\n\n"
        f"<b>🛒 Состав:</b>\n{items_block(o)}\n"
        f"{totals(o)}"
    )


def order_created(o: Order) -> str:
    return (f"✅ <b>Заказ #{o.id} оформлен и ожидает оплаты</b>\n\n{order_for_user(o)}\n\n"
            f"Оплатите его по кнопке «Оплатить» в течение {PAYMENT_TTL_HOURS} часов — иначе заказ отменится "
            "автоматически.\nВсе заказы: /my_orders")


def order_paid(o: Order) -> str:
    return (f"💳 <b>Оплата заказа #{o.id} получена</b> — {money(o.total)}.\n\n"
            "Соберём и отправим заказ, трек-номер пришлём сюда.\nВсе заказы: /my_orders")


def order_cancelled_unpaid(o: Order) -> str:
    return (f"❌ <b>Заказ #{o.id} отменён</b>: оплата не поступила.\n\n"
            "Если заказ ещё нужен — оформите его заново в приложении.")


def order_deleted(o: Order, comment: str | None = None) -> str:
    text = f"🗑 <b>Заказ #{o.id} удалён</b>."
    if comment:
        text += f"\n\n<b>Комментарий менеджера:</b>\n{esc(comment)}"
    return text + "\n\nВсе заказы: /my_orders"


def delete_prompt(o: Order) -> str:
    """Подтверждение удаления: что ещё надо сделать руками, кроме самого удаления."""
    text = f"Удалить заказ #{o.id} без возможности восстановления? Покупателю придёт сообщение, что заказ удалён."
    if o.payment_status == "paid":
        text += "\n\n⚠️ Заказ оплачен: удаление не вернёт деньги. Сначала нажмите «💸 Вернуть деньги»."
    if o.shipment_id:
        text += f"\n\n⚠️ {shipment_reminder(o)}"
    return text


def shipment_reminder(o: Order) -> str:
    return (f"По заказу создано отправление ({DELIVERY.get(o.delivery, esc(o.delivery))}: "
            f"<code>{esc(o.shipment_id)}</code>) — удалите его в личном кабинете службы вручную.")


def refunded(o: Order) -> str:
    return (f"↩️ <b>Заказ #{o.id} отменён, деньги возвращены</b> — {money(o.total)}.\n\n"
            "Срок зачисления на карту зависит от банка, обычно несколько дней.")


def order_button(o: Order) -> str:
    return f"#{o.id} · {o.surname} · {money(o.total)} · {o.status}"[:64]


def status_changed(o: Order) -> str:
    return f"🔔 <b>Заказ #{o.id} обновлён</b>\n\n{status_line(o)}"


def start() -> str:
    return "Привет! Вот наше приложение:"


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
