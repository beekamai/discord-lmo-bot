from abc import ABC, abstractmethod

class BaseState(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def execute(self, engine) -> bool:
        """
        Отрабатывает логику конкретного стейта.
        Возвращает True, если бот выполнил действие (кликнул).
        Возвращает False, если для этого стейта кнопок на экране не найдено.
        """
        pass
