import glob
import time
from .base import BaseState

class LoadingState(BaseState):
    """ Отрабатывает попапы, крестики закрытия и прочий мусор """

    def __init__(self):
        self._cooldown_until = 0

    @property
    def name(self) -> str:
        return "Loading / Popups"

    def execute(self, engine) -> bool:
        if engine.is_locked:
            return False

        if time.time() < self._cooldown_until:
            return False

        img = engine.get_current_frame()
        if img is None:
            return False

        priority_folders = [
            "templates/close/*.png",
        ]

        for folder_mask in priority_folders:
            templates = glob.glob(folder_mask)
            for btn_path in templates:
                coord = engine.vision.find_template(img, btn_path, threshold=0.8)
                if coord:
                    engine.log(f"[!] {self.name}: Найден {btn_path}! Нажимаем...")
                    engine.click(coord[0], coord[1], variance=3)
                    self._cooldown_until = time.time() + 1.5
                    return True

        return False

    def reset(self, engine=None):
        self._cooldown_until = 0
