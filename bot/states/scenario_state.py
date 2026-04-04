import json
import time
import os
from bot.states.base import BaseState

class ScenarioState(BaseState):
    """
    Отрабатывает заранее заготовленные сценарии из JSON конфигов.
    Поддерживает три режима:
    1. 'sequential' - строгая последовательность шагов.
    2. 'whitelist' - ищет и кликает любую разрешенную кнопку, пока не найдет точку выхода.
    3. 'sequence_click' - находит все вхождения шаблона и кликает слева направо.
    4. 'spam_click' - находит кнопку и спамит по ней N раз с заданной задержкой.
    """

    def __init__(self, scenario_file: str):
        self.scenario_file = scenario_file
        self.scenario_name = scenario_file.split('\\')[-1].split('/')[-1].split('.')[0]
        self.is_active = False
        self._cooldown_until = 0

        self.db_path = "configs/scenarios/state.json"
        self._load_db()

        self._last_mtime = 0
        self._load_config()

    def _load_config(self):
        if not os.path.exists(self.scenario_file):
            return

        self._last_mtime = os.path.getmtime(self.scenario_file)

        with open(self.scenario_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.mode = data.get("mode", "sequential")
            self.run_once = data.get("run_once", False)
            self.priority = data.get("priority", 100)

            self.steps = data.get("steps", [])
            self.start_step = data.get("start_step", 0)

            if not hasattr(self, 'current_step_index'):
                self.current_step_index = self.start_step
                self.actual_repeats = 0
            elif hasattr(self, '_prev_start_step') and self._prev_start_step != self.start_step:
                self.current_step_index = self.start_step
                self.actual_repeats = 0

            self._prev_start_step = self.start_step
            self.trigger_templates = data.get("trigger_templates", [])
            self.exit_templates = data.get("exit_templates", [])
            self.allowed_clicks = data.get("allowed_clicks", [])
            self.post_delay_sec = data.get("post_delay_sec", 1.5)
            self.default_confidence = data.get("confidence", 0.8)
            self.spam_clicks = data.get("spam_clicks", 100)
            self.spam_delay = data.get("spam_delay_ms", 50) / 1000.0
            self.spam_recheck_every = data.get("spam_recheck_every", 0)

    def _load_db(self):
        if not os.path.exists(self.db_path):
            self.db = {"completed_scenarios": []}
            self._save_db()
        else:
            try:
                with open(self.db_path, 'r', encoding='utf-8') as f:
                    self.db = json.load(f)
            except Exception:
                self.db = {"completed_scenarios": []}

    def _save_db(self):
        with open(self.db_path, 'w', encoding='utf-8') as f:
            json.dump(self.db, f, indent=4)

    def _mark_completed(self):
        if self.run_once:
            if self.scenario_name not in self.db.get("completed_scenarios", []):
                self.db.setdefault("completed_scenarios", []).append(self.scenario_name)
                self._save_db()

    @property
    def name(self) -> str:
        return f"Scenario: {self.scenario_name} [{self.mode}]"

    def execute(self, engine) -> bool:
        # Hot Reload
        if os.path.exists(self.scenario_file):
            current_mtime = os.path.getmtime(self.scenario_file)
            if current_mtime > self._last_mtime:
                engine.log(f"[*] Файл {self.scenario_name}.json изменился! Hot Reload.")
                self._load_config()

        if self.run_once and self.scenario_name in self.db.get("completed_scenarios", []):
            return False

        # Если движок залочен другим стейтом - не вмешиваемся
        if engine.is_locked and not self.is_active:
            return False

        if time.time() < self._cooldown_until:
            return True

        if self.mode == "sequential":
            return self._execute_sequential(engine)
        elif self.mode == "whitelist":
            return self._execute_whitelist(engine)
        elif self.mode == "sequence_click":
            return self._execute_sequence_click(engine)
        elif self.mode == "spam_click":
            return self._execute_spam_click(engine)
        return False

    def _execute_sequential(self, engine) -> bool:
        if self.current_step_index >= len(self.steps):
            if engine.is_locked:
                engine.is_locked = False
                engine.log(f"[+] {self.name}: Сценарий завершен. Лок снят.")
            self._mark_completed()
            return False

        img = engine.get_current_frame()
        if img is None:
            return False

        current_step = self.steps[self.current_step_index]
        templates_to_find = current_step.get("templates", [])
        repeats_needed = current_step.get("repeats", 1)

        for template_path in templates_to_find:
            confidence = current_step.get("confidence", self.default_confidence)
            coord = engine.vision.find_template(img, template_path, threshold=confidence)
            if not coord:
                continue

            if not engine.is_locked:
                engine.is_locked = True

            engine.locked_since = time.time()
            engine.log(f"[!] {self.name}: Шаг {self.current_step_index + 1}/{len(self.steps)} "
                       f"(Повтор {self.actual_repeats + 1}/{repeats_needed}) - Найден {template_path}")

            offset_x = current_step.get("offset_x", 0)
            offset_y = current_step.get("offset_y", 0)

            engine.click(coord[0] + offset_x, coord[1] + offset_y, variance=3)

            delay = current_step.get("post_delay_sec", 1.5)
            self._cooldown_until = time.time() + delay

            self.actual_repeats += 1
            if self.actual_repeats >= repeats_needed:
                self.current_step_index += 1
                self.actual_repeats = 0

            return True

        return False

    def _execute_whitelist(self, engine) -> bool:
        img = engine.get_current_frame()
        if img is None:
            return False

        if not self.is_active:
            for trigger in self.trigger_templates:
                if engine.vision.find_template(img, trigger, threshold=self.default_confidence):
                    self.is_active = True
                    engine.is_locked = True
                    engine.locked_since = time.time()
                    engine.log(f"[+] {self.name}: Триггер {trigger} найден. Сценарий активирован!")
                    return True
            return False

        if self.is_active:
            for exit_templ in self.exit_templates:
                if engine.vision.find_template(img, exit_templ, threshold=self.default_confidence):
                    self.is_active = False
                    if engine.is_locked:
                        engine.is_locked = False
                    engine.log(f"[+] {self.name}: Точка выхода {exit_templ}. Сценарий завершен.")
                    self._mark_completed()
                    return False

            for allowed in self.allowed_clicks:
                if isinstance(allowed, dict):
                    template_path = allowed.get("template")
                    step_name = allowed.get("step_name", "Unknown Step")
                    confidence = allowed.get("confidence", self.default_confidence)
                else:
                    template_path = allowed
                    step_name = template_path.split("/")[-1]
                    confidence = self.default_confidence

                coord = engine.vision.find_template(img, template_path, threshold=confidence)
                if coord:
                    engine.log(f"[!] {self.name}: [{step_name}] найдена. Нажимаем...")
                    engine.click(coord[0], coord[1], variance=3)
                    self._cooldown_until = time.time() + self.post_delay_sec
                    engine.locked_since = time.time()
                    return True

            return False

    def _execute_sequence_click(self, engine) -> bool:
        """
        Режим для серийных кликов: находит все вхождения шаблона,
        сортирует слева направо и кликает по очереди с задержкой.
        Полезно для последовательностей кнопок в ряд.
        """
        img = engine.get_current_frame()
        if img is None:
            return False

        if not self.is_active:
            for trigger in self.trigger_templates:
                if engine.vision.find_template(img, trigger, threshold=self.default_confidence):
                    self.is_active = True
                    engine.is_locked = True
                    engine.locked_since = time.time()
                    engine.log(f"[+] {self.name}: Триггер серии найден!")
                    return True
            return False

        for exit_templ in self.exit_templates:
            if engine.vision.find_template(img, exit_templ, threshold=self.default_confidence):
                self.is_active = False
                engine.is_locked = False
                engine.log(f"[+] {self.name}: Серия завершена.")
                self._mark_completed()
                return False

        for click_entry in self.allowed_clicks:
            if isinstance(click_entry, dict):
                template_path = click_entry.get("template")
                confidence = click_entry.get("confidence", self.default_confidence)
                click_delay = click_entry.get("click_delay", 0.3)
            else:
                template_path = click_entry
                confidence = self.default_confidence
                click_delay = 0.3

            hits = engine.vision.find_all_templates(img, template_path, threshold=confidence)
            if hits:
                engine.log(f"[!] {self.name}: Найдено {len(hits)} элементов серии. Кликаем слева направо...")
                for coord in hits:
                    engine.click(coord[0], coord[1], variance=2)
                    time.sleep(click_delay)

                self._cooldown_until = time.time() + self.post_delay_sec
                engine.locked_since = time.time()
                return True

        return False

    def _execute_spam_click(self, engine) -> bool:
        """
        Режим spam_click: находит кнопку и спамит по ней N раз.
        JSON-поля:
          spam_clicks      - сколько раз кликнуть (default 100)
          spam_delay_ms    - задержка между кликами в мс (default 50)
          spam_recheck_every - пересканировать позицию каждые N кликов (0 = не пересканировать)
        """
        img = engine.get_current_frame()
        if img is None:
            return False

        if not self.is_active:
            for trigger in self.trigger_templates:
                coord = engine.vision.find_template(img, trigger, threshold=self.default_confidence)
                if coord:
                    self.is_active = True
                    engine.is_locked = True
                    engine.locked_since = time.time()
                    engine.log(f"[+] {self.name}: Триггер найден! Начинаем спам {self.spam_clicks} кликов...")

                    self._do_spam(engine, coord)

                    self.is_active = False
                    engine.is_locked = False
                    self._mark_completed()
                    engine.log(f"[+] {self.name}: Спам завершен.")
                    return True
            return False

        return False

    def _do_spam(self, engine, initial_coord):
        """Выполняет серию быстрых кликов по найденной позиции."""
        x, y = initial_coord

        for i in range(self.spam_clicks):
            if self.spam_recheck_every > 0 and i > 0 and i % self.spam_recheck_every == 0:
                frame = engine._get_frame()
                if frame is not None:
                    engine.current_frame = frame.copy()
                    engine.vision.prepare_frame(engine.current_frame)

                    for exit_templ in self.exit_templates:
                        if engine.vision.find_template(engine.current_frame, exit_templ, threshold=self.default_confidence):
                            engine.log(f"[+] {self.name}: Exit template найден на клике {i}. Стоп.")
                            return

                    new_coord = None
                    for trigger in self.trigger_templates:
                        new_coord = engine.vision.find_template(engine.current_frame, trigger, threshold=self.default_confidence)
                        if new_coord:
                            break

                    if new_coord:
                        x, y = new_coord
                        engine.log(f"[*] {self.name}: Recheck #{i} - позиция обновлена: ({x}, {y})")
                    else:
                        engine.log(f"[*] {self.name}: Recheck #{i} - кнопка пропала. Стоп.")
                        return

            engine.capture.tap(x, y, variance=5)
            time.sleep(self.spam_delay)

            if (i + 1) % 500 == 0:
                engine.log(f"[*] {self.name}: {i + 1}/{self.spam_clicks} кликов...")

    def reset(self, engine=None):
        if engine and engine.is_locked:
            engine.is_locked = False
            engine.log(f"[*] {self.name}: Сценарий сброшен, лок снят.")
