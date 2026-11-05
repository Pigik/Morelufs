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
        self.answers = []
        self.deleted = False

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self

    async def delete(self):
        self.deleted = True


class FakeCall:
    def __init__(self, message):
        self.from_user = message.from_user
        self.message = message
        self.answered = False

    async def answer(self):
        self.answered = True


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_first_start_asks_to_continue_then_sends_regular_start(settings):
    async def scenario():
        db = await Database.open(":memory:")
        bot = FakeBot()

        first = FakeMessage(11)
        await user.start(first, bot, db, settings)
        text, kwargs = first.answers[0]
        assert text == texts.start_legal(settings.webapp_url)
        assert "публичную оферту" in text and "политику конфиденциальности" in text
        assert buttons(kwargs["reply_markup"]) == ["Продолжить"]

        # повторный /start до «Продолжить»: старое сообщение удалено, пришло такое же новое
        again = FakeMessage(12)
        await user.start(again, bot, db, settings)
        assert bot.deleted == [(1, 11)]
        assert again.answers[0][0] == texts.start_legal(settings.webapp_url)
        assert buttons(again.answers[0][1]["reply_markup"]) == ["Продолжить"]

        # «Продолжить»: сообщение с документами удалено, отправлен обычный старт
        legal = FakeMessage(12)
        call = FakeCall(legal)
        await user.start_continue(call, bot, db, settings)
        assert call.answered and legal.deleted
        assert await db.start_confirmed(1) is True
        assert bot.sent[-1] == (1, texts.start())
        assert bot.sent[-1][1] == "Привет! Вот наше приложение:"
        assert buttons(bot.markups[-1][1]) == ["Открыть приложение"]

        # после согласия /start сразу отвечает обычным сообщением
        after = FakeMessage(13)
        await user.start(after, bot, db, settings)
        assert after.answers[0][0] == texts.start()
        assert buttons(after.answers[0][1]["reply_markup"]) == ["Открыть приложение"]
        assert bot.deleted == [(1, 11)]  # старое сообщение больше не трогаем

        await db.close()
    run(scenario())
