from app import texts
from app.bot import user
from app.db import Database
from tests.conftest import FakeBot, run


class FakeUser:
    id = 1
    username = "ivan"
    full_name = "Иван"


class FakeMessage:
    def __init__(self, message_id=1):
        self.from_user = FakeUser()
        self.message_id = message_id
        self.reply_to_message = None
        self.answers = []
        self.edits = []
        self.deleted = False

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)
        return self

    async def delete(self):
        self.deleted = True


class FakeCall:
    def __init__(self, message):
        self.from_user = FakeUser()
        self.message = message
        self.answers = []

    async def answer(self, text="", **kwargs):
        self.answers.append((text, kwargs))


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_start_always_sends_same_message(settings):
    async def scenario():
        db = await Database.open(":memory:")
        bot = FakeBot()

        first = FakeMessage(11)
        await user.start(first, bot, db, settings)
        text, kwargs = first.answers[0]
        assert text == texts.start()
        assert text == "Привет! Вот наше приложение:"
        assert buttons(kwargs["reply_markup"]) == ["Открыть приложение"]
        assert {chat_id for chat_id, _ in bot.sent} == {100, 200}  # админам — уведомление о новом

        # повторный /start — то же самое сообщение, без документов и «Продолжить»
        again = FakeMessage(12)
        await user.start(again, bot, db, settings)
        assert again.answers[0][0] == texts.start()
        assert buttons(again.answers[0][1]["reply_markup"]) == ["Открыть приложение"]
        assert bot.deleted == []
        assert len(bot.sent) == 2  # нового пользователя уже нет — админам ничего не шлём

        await db.close()
    run(scenario())


def test_mailing_asks_confirmation(settings):
    from aiogram.filters import CommandObject

    from app.bot import admin
    from app.bot.keyboards import MailingConfirm

    async def scenario():
        db = await Database.open(":memory:")
        await db.upsert_user(1, "ivan", "Иван")
        await db.upsert_user(2, None, "Пётр")
        bot = FakeBot()
        command = CommandObject(command="mailing", args="Новость магазина")

        prompt = FakeMessage(21)
        await admin.mailing(prompt, command, db)
        text, kwargs = prompt.answers[-1]
        assert text.startswith("Начать рассылку?") and "Получателей: 2" in text
        assert buttons(kwargs["reply_markup"]) == ["Да", "Нет"]
        assert bot.sent == []  # до подтверждения никому не пишем

        call = FakeCall(FakeMessage(22))
        await admin.mailing_confirm(call, MailingConfirm(action="yes"), bot, db)
        assert sorted(cid for cid, _ in bot.sent) == [1, 2]
        assert all(t == "Новость магазина" for _, t in bot.sent)
        assert "Рассылка завершена" in prompt.answers[-1][0]

        # «Нет» отменяет рассылку и очищает ожидание
        await admin.mailing(FakeMessage(23), command, db)
        sent_before = len(bot.sent)
        cancel = FakeCall(FakeMessage(24))
        await admin.mailing_confirm(cancel, MailingConfirm(action="no"), bot, db)
        assert len(bot.sent) == sent_before
        assert cancel.message.edits == ["Рассылка отменена."]
        assert await db.active_user_ids()  # база не тронута

        await db.close()
    run(scenario())
