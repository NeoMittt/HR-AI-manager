from aiogram.fsm.state import State, StatesGroup


class CandidateStates(StatesGroup):
    # Ждём резюме от кандидата
    waiting_resume = State()

    # Идёт собеседование (диалог с AI)
    interviewing = State()

    # Сеанс заблокирован (off-topic)
    blocked = State()

    # Собеседование завершено
    completed = State()
