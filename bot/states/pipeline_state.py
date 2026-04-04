import json
import time
import os
from bot.states.base import BaseState


class PipelineState(BaseState):
    """
    Мульти-фазный сценарий: цепочка фаз, каждая со своим типом действия.
    Поддерживает loop (зацикливание всего пайплайна).

    Типы фаз:
      spam_click    - спамит кликами по найденной кнопке (duration_sec или click_count)
      click_once    - находит шаблон и кликает один раз
      sequence_click - находит все вхождения, кликает слева направо
      send_keys     - находит шаблоны-стрелки на экране, маппит на клавиши, жмет по порядку
      wait_for      - ждет появления шаблона (пауза)
    """

    def __init__(self, scenario_file: str):
        self.scenario_file = scenario_file
        self.scenario_name = scenario_file.split('\\')[-1].split('/')[-1].split('.')[0]
        self._cooldown_until = 0

        self._last_mtime = 0
        self._current_phase = 0
        self._phase_start_time = 0
        self._phase_click_count = 0
        self._pipeline_active = False

        self._load_config()

    def _load_config(self):
        if not os.path.exists(self.scenario_file):
            return

        self._last_mtime = os.path.getmtime(self.scenario_file)

        with open(self.scenario_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            self.priority = data.get("priority", 100)
            self.loop = data.get("loop", True)
            self.default_confidence = data.get("confidence", 0.8)
            self.phases = data.get("phases", [])
            self.trigger_templates = data.get("trigger_templates", [])
            self.loop_exit_templates = data.get("loop_exit_templates", [])

    @property
    def name(self) -> str:
        phase_name = ""
        if self._pipeline_active and self._current_phase < len(self.phases):
            phase_name = f" -> {self.phases[self._current_phase].get('name', f'Phase {self._current_phase}')}"
        return f"Pipeline: {self.scenario_name}{phase_name}"

    def execute(self, engine) -> bool:
        # Hot Reload
        if os.path.exists(self.scenario_file):
            current_mtime = os.path.getmtime(self.scenario_file)
            if current_mtime > self._last_mtime:
                engine.log(f"[*] {self.scenario_name}.json Hot Reload. Pipeline сброшен.")
                self._load_config()
                self._pipeline_active = False
                self._current_phase = 0
                self._phase_start_time = 0
                self._phase_click_count = 0
                self._cooldown_until = 0
                engine.is_locked = False

        if time.time() < self._cooldown_until:
            if self._pipeline_active:
                engine.locked_since = time.time()
                return True
            return False

        if not self.phases:
            return False

        # Активация пайплайна по триггеру (или сразу если триггеров нет)
        if not self._pipeline_active:
            # Если движок залочен другим стейтом — не вмешиваемся
            if engine.is_locked:
                return False

            if not self.trigger_templates:
                self._pipeline_active = True
                self._current_phase = 0
                self._phase_start_time = time.time()
                self._phase_click_count = 0
                engine.is_locked = True
                engine.locked_since = time.time()
                engine.log(f"[+] {self.name}: Pipeline запущен (без триггера).")
            else:
                img = engine.get_current_frame()
                if img is None:
                    return False
                for trigger in self.trigger_templates:
                    if engine.vision.find_template(img, trigger, threshold=self.default_confidence):
                        self._pipeline_active = True
                        self._current_phase = 0
                        self._phase_start_time = time.time()
                        self._phase_click_count = 0
                        engine.is_locked = True
                        engine.locked_since = time.time()
                        engine.log(f"[+] {self.name}: Триггер найден! Pipeline запущен.")
                        return True
                return False

        # Все фазы пройдены
        if self._current_phase >= len(self.phases):
            if self.loop:
                # Проверяем exit_templates — условие выхода из loop
                if self.loop_exit_templates:
                    img = self._fresh_frame(engine)
                    if img is not None:
                        for exit_t in self.loop_exit_templates:
                            if engine.vision.find_template(img, exit_t, threshold=self.default_confidence):
                                engine.log(f"[+] {self.name}: Loop exit '{exit_t}'. Завершаем.")
                                self._pipeline_active = False
                                engine.is_locked = False
                                return False

                engine.log(f"[+] {self.name}: Цикл завершен. Перезапуск...")
                self._current_phase = 0
                self._phase_start_time = time.time()
                self._phase_click_count = 0
                self._cooldown_until = 0
                return True
            else:
                self._pipeline_active = False
                engine.is_locked = False
                engine.log(f"[+] {self.name}: Pipeline завершен.")
                return False

        phase = self.phases[self._current_phase]
        action = phase.get("action", "click_once")

        if action == "spam_click":
            return self._phase_spam_click(engine, phase)
        elif action == "click_once":
            return self._phase_click_once(engine, phase)
        elif action == "sequence_click":
            return self._phase_sequence_click(engine, phase)
        elif action == "send_keys":
            return self._phase_send_keys(engine, phase)
        elif action == "wait_for":
            return self._phase_wait_for(engine, phase)
        elif action == "wait_disappear":
            return self._phase_wait_disappear(engine, phase)
        elif action == "memory_match":
            return self._phase_memory_match(engine, phase)
        else:
            engine.log(f"[-] {self.name}: Неизвестный action: {action}. Пропускаем фазу.")
            self._advance_phase(engine)
            return True

    def _advance_phase(self, engine):
        """Переход к следующей фазе."""
        old_name = self.phases[self._current_phase].get("name", f"Phase {self._current_phase}")
        self._current_phase += 1
        self._phase_start_time = time.time()
        self._phase_click_count = 0

        if self._current_phase < len(self.phases):
            new_name = self.phases[self._current_phase].get("name", f"Phase {self._current_phase}")
            engine.log(f"[+] {self.scenario_name}: [{old_name}] done -> [{new_name}]")
        engine.locked_since = time.time()

    def _fresh_frame(self, engine):
        """Берёт последний кадр из буфера capture thread и подготавливает для CV."""
        with engine._raw_frame_lock:
            frame = engine._latest_raw_frame
        if frame is None:
            return None
        engine.current_frame = frame
        engine.vision.prepare_frame(engine.current_frame)
        return engine.current_frame

    def _get_current_delay(self, phase, batch_start_time):
        """
        Вычисляет текущую задержку клика с учётом ramp_up.

        ramp_up: [0.3, 0.6, 1.0]  - коэффициенты от delay_ms
        ramp_period_sec: 3.0      - время одного цикла нарастания (потом сброс)

        Без ramp_up возвращает базовый delay.
        """
        delay_ms = phase.get("delay_ms", 50)
        ramp_up = phase.get("ramp_up", None)

        if not ramp_up:
            return delay_ms / 1000.0

        ramp_period = phase.get("ramp_period_sec", 3.0)
        elapsed_in_period = (time.time() - batch_start_time) % ramp_period
        progress = elapsed_in_period / ramp_period

        # Определяем текущий уровень по прогрессу внутри периода
        idx = min(int(progress * len(ramp_up)), len(ramp_up) - 1)
        coeff = ramp_up[idx]

        return (delay_ms * coeff) / 1000.0

    # --- spam_click: спамит кнопку N секунд или N кликов ---
    def _phase_spam_click(self, engine, phase):
        """
        JSON доп. поля:
          ramp_up: [0.3, 0.6, 1.0]  - нарастающий клик, коэффициенты от delay_ms
          ramp_period_sec: 3.0       - период цикла нарастания в секундах
        При ramp_up=[0.3, 0.6, 1.0] и delay_ms=100, ramp_period_sec=3:
          0-1с: delay=30ms, 1-2с: delay=60ms, 2-3с: delay=100ms, потом сброс
        """
        duration = phase.get("duration_sec", 0)
        max_clicks = phase.get("click_count", 0)
        base_delay = phase.get("delay_ms", 50) / 1000.0
        recheck_every = phase.get("recheck_every", 100)
        templates = phase.get("templates", [])
        confidence = phase.get("confidence", self.default_confidence)

        skip_exit = phase.get("skip_exit_sec", 3.0)
        elapsed = time.time() - self._phase_start_time

        if duration > 0 and elapsed >= duration:
            engine.log(f"[+] {self.name}: {duration}сек истекли ({self._phase_click_count} кликов).")
            self._advance_phase(engine)
            return True

        if max_clicks > 0 and self._phase_click_count >= max_clicks:
            engine.log(f"[+] {self.name}: {max_clicks} кликов выполнено.")
            self._advance_phase(engine)
            return True

        img = self._fresh_frame(engine)
        if img is None:
            return True

        if elapsed > skip_exit:
            for exit_t in phase.get("exit_templates", []):
                if engine.vision.find_template(img, exit_t, threshold=confidence):
                    engine.log(f"[+] {self.name}: Exit найден. Переход.")
                    self._advance_phase(engine)
                    return True

        coord = None
        for tmpl in templates:
            coord = engine.vision.find_template(img, tmpl, threshold=confidence)
            if coord:
                break

        engine.locked_since = time.time()

        if coord:
            batch = min(recheck_every, max_clicks - self._phase_click_count if max_clicks > 0 else recheck_every)
            if duration > 0:
                remaining = duration - (time.time() - self._phase_start_time)
                max_by_time = max(1, int(remaining / base_delay)) if base_delay > 0 else batch
                batch = min(batch, max_by_time)

            batch_start = time.time()
            for i in range(batch):
                engine.capture.tap(coord[0], coord[1], variance=5)
                self._phase_click_count += 1
                engine.locked_since = time.time()

                delay = self._get_current_delay(phase, batch_start)
                time.sleep(delay)

                if duration > 0 and (time.time() - self._phase_start_time) >= duration:
                    break
                if max_clicks > 0 and self._phase_click_count >= max_clicks:
                    break

            if self._phase_click_count % 500 < recheck_every:
                engine.log(f"[*] {self.name}: {self._phase_click_count} кликов...")

        return True

    # --- click_once: находит шаблон и кликает один раз ---
    def _phase_click_once(self, engine, phase):
        """
        JSON доп. поля:
          skip_if_near: "templates/menu/clock.png"  - пропустить фазу если этот шаблон рядом
          skip_near_radius: 150                     - радиус "рядом" в пикселях
        """
        templates = phase.get("templates", [])
        confidence = phase.get("confidence", self.default_confidence)
        post_delay = phase.get("post_delay_sec", 1.0)
        offset_x = phase.get("offset_x", 0)
        offset_y = phase.get("offset_y", 0)
        timeout = phase.get("timeout_sec", 30)
        skip_if_near = phase.get("skip_if_near", None)
        skip_near_radius = phase.get("skip_near_radius", 150)

        elapsed = time.time() - self._phase_start_time

        if elapsed > timeout:
            engine.log(f"[-] {self.name}: Timeout {timeout}сек. Пропускаем фазу.")
            self._advance_phase(engine)
            return True

        img = self._fresh_frame(engine)
        if img is None:
            return True

        # Проверяем блокер (часики) — если правее и ниже кнопки, пропускаем
        if skip_if_near:
            all_blockers = engine.vision.find_all_templates(img, skip_if_near, threshold=confidence)
            if all_blockers:
                for tmpl in templates:
                    coord = engine.vision.find_template(img, tmpl, threshold=confidence)
                    if coord:
                        cx, cy = int(coord[0]), int(coord[1])
                        for bx, by in all_blockers:
                            bx, by = int(bx), int(by)
                            # Блокер должен быть правее кнопки и в пределах radius по вертикали
                            is_right = 0 < (bx - cx) < skip_near_radius
                            is_near_y = abs(by - cy) < skip_near_radius
                            if is_right and is_near_y:
                                engine.log(f"[*] {self.name}: Часики правее кнопки ({bx}>{cx}). Пропускаем pipeline.")
                                self._pipeline_active = False
                                engine.is_locked = False
                                self._cooldown_until = time.time() + 10.0
                                return False

        for tmpl in templates:
            coord = engine.vision.find_template(img, tmpl, threshold=confidence)
            if coord:
                engine.log(f"[!] {self.name}: Найден {tmpl}. Кликаем.")
                engine.click(coord[0] + offset_x, coord[1] + offset_y, variance=3)
                self._cooldown_until = time.time() + post_delay
                self._advance_phase(engine)
                return True

        return True

    # --- sequence_click: находит все, кликает слева направо ---
    def _phase_sequence_click(self, engine, phase):
        templates = phase.get("templates", [])
        confidence = phase.get("confidence", self.default_confidence)
        click_delay = phase.get("click_delay", 0.3)
        post_delay = phase.get("post_delay_sec", 1.0)
        timeout = phase.get("timeout_sec", 30)

        if (time.time() - self._phase_start_time) > timeout:
            engine.log(f"[-] {self.name}: Timeout. Пропускаем.")
            self._advance_phase(engine)
            return True

        img = self._fresh_frame(engine)
        if img is None:
            return True

        for tmpl in templates:
            hits = engine.vision.find_all_templates(img, tmpl, threshold=confidence)
            if hits:
                engine.log(f"[!] {self.name}: {len(hits)} элементов. Кликаем слева направо.")
                for coord in hits:
                    engine.click(coord[0], coord[1], variance=2)
                    time.sleep(click_delay)
                self._cooldown_until = time.time() + post_delay
                self._advance_phase(engine)
                return True

        return True

    # --- send_keys: находит шаблоны-стрелки, маппит на клавиши, жмет ---
    def _phase_send_keys(self, engine, phase):
        """
        Ищет шаблоны на экране, для каждого найденного отправляет
        соответствующую клавишу. Повторяет пока не появится exit_template.

        JSON:
          key_map: [
            {"template": "templates/arrows/arrow_left.png", "key": "left"},
            {"template": "templates/arrows/arrow_right.png", "key": "right"},
            {"template": "templates/arrows/arrow_up.png", "key": "up"},
            {"template": "templates/arrows/arrow_down.png", "key": "down"}
          ]
          exit_templates: ["templates/craft/crafted.png"]
          scan_delay_sec: 1.0   - пауза между сканированиями
          key_delay_sec: 0.15   - пауза между нажатиями клавиш
          timeout_sec: 120
        """
        key_map = phase.get("key_map", [])
        exit_templates = phase.get("exit_templates", [])
        scan_delay = phase.get("scan_delay_sec", 1.0)
        key_delay = phase.get("key_delay_sec", 0.15)
        timeout = phase.get("timeout_sec", 120)
        skip_exit = phase.get("skip_exit_sec", 3.0)
        confidence = phase.get("confidence", self.default_confidence)

        elapsed = time.time() - self._phase_start_time

        if elapsed > timeout:
            engine.log(f"[-] {self.name}: Timeout {timeout}сек.")
            self._advance_phase(engine)
            return True

        img = self._fresh_frame(engine)
        if img is None:
            return True

        # Не проверяем exit первые skip_exit_sec секунд (экран ещё не обновился)
        if elapsed > skip_exit:
            for exit_t in exit_templates:
                if engine.vision.find_template(img, exit_t, threshold=confidence):
                    engine.log(f"[+] {self.name}: Exit '{exit_t}' найден. Стрелки завершены.")
                    self._advance_phase(engine)
                    return True

        # Ищем все шаблоны из key_map, собираем (x, key) пары
        found_keys = []
        for entry in key_map:
            tmpl = entry.get("template")
            key = entry.get("key")
            if not tmpl or not key:
                continue

            hits = engine.vision.find_all_templates(img, tmpl, threshold=confidence)
            for coord in hits:
                found_keys.append((coord[0], coord[1], key))

        if found_keys:
            # Сортируем слева направо
            found_keys.sort(key=lambda item: item[0])

            keys_str = " ".join([k[2] for k in found_keys])
            engine.log(f"[!] {self.name}: Найдено {len(found_keys)} стрелок: [{keys_str}]")

            for x, y, key in found_keys:
                engine.capture.send_key(key)
                time.sleep(key_delay)

        self._cooldown_until = time.time() + scan_delay
        return True

    # --- wait_for: ждет появления шаблона ---
    def _phase_wait_for(self, engine, phase):
        templates = phase.get("templates", [])
        confidence = phase.get("confidence", self.default_confidence)
        timeout = phase.get("timeout_sec", 60)
        post_delay = phase.get("post_delay_sec", 0.5)

        if (time.time() - self._phase_start_time) > timeout:
            engine.log(f"[-] {self.name}: Timeout ожидания.")
            self._advance_phase(engine)
            return True

        img = self._fresh_frame(engine)
        if img is None:
            return True

        for tmpl in templates:
            if engine.vision.find_template(img, tmpl, threshold=confidence):
                engine.log(f"[+] {self.name}: Дождались {tmpl}!")
                self._cooldown_until = time.time() + post_delay
                self._advance_phase(engine)
                return True

        return True

    # --- wait_disappear: ждет пока шаблон ИСЧЕЗНЕТ с экрана ---
    def _phase_wait_disappear(self, engine, phase):
        """
        Ждёт пока шаблон пропадёт с экрана. Полезно для таймеров.

        JSON:
          templates: ["templates/menu/clock.png"]
          near_template: "templates/kovka/logo.png"  - искать часики только рядом с этой кнопкой
          near_radius: 150                           - радиус поиска в пикселях
          confidence: 0.8
          scan_delay_sec: 2.0
          skip_exit_sec: 5.0
          timeout_sec: 600
          post_delay_sec: 1.0
        """
        templates = phase.get("templates", [])
        near_template = phase.get("near_template", None)
        near_radius = phase.get("near_radius", 150)
        confidence = phase.get("confidence", self.default_confidence)
        scan_delay = phase.get("scan_delay_sec", 2.0)
        skip_exit = phase.get("skip_exit_sec", 5.0)
        timeout = phase.get("timeout_sec", 600)
        post_delay = phase.get("post_delay_sec", 1.0)

        elapsed = time.time() - self._phase_start_time

        if elapsed > timeout:
            engine.log(f"[-] {self.name}: Timeout {timeout}сек.")
            self._advance_phase(engine)
            return True

        img = self._fresh_frame(engine)
        if img is None:
            return True

        engine.locked_since = time.time()

        if elapsed < skip_exit:
            self._cooldown_until = time.time() + scan_delay
            return True

        # Если указан near_template — ищем часики только рядом с ним
        found = False
        if near_template:
            anchor = engine.vision.find_template(img, near_template, threshold=confidence)
            if anchor:
                ax, ay = int(anchor[0]), int(anchor[1])
                for tmpl in templates:
                    hits = engine.vision.find_all_templates(img, tmpl, threshold=confidence)
                    for hx, hy in hits:
                        if abs(int(hx) - ax) < near_radius and abs(int(hy) - ay) < near_radius:
                            found = True
                            break
                    if found:
                        break
        else:
            for tmpl in templates:
                if engine.vision.find_template(img, tmpl, threshold=confidence):
                    found = True
                    break

        if not found:
            engine.log(f"[+] {self.name}: Шаблон исчез! Таймер завершён.")
            self._cooldown_until = time.time() + post_delay
            self._advance_phase(engine)
            return True

        self._cooldown_until = time.time() + scan_delay
        return True

    # --- memory_match: игра "найди 3 одинаковых" ---
    def _phase_memory_match(self, engine, phase):
        """
        Игра мемори 3x3: запоминает сетку, кликает, проверяет что открылось.

        JSON:
          closed_template: "templates/fight/slot_closed.png"
          exit_templates: ["templates/fight/fight_win.png"]
          cell_open_delay: 1.5
          cell_close_delay: 2.0
          similarity: 0.7
          timeout_sec: 120
        """
        closed_tmpl = phase.get("closed_template", "")
        exit_templates = phase.get("exit_templates", [])
        cell_open_delay = phase.get("cell_open_delay", 1.5)
        cell_close_delay = phase.get("cell_close_delay", 2.0)
        similarity_threshold = phase.get("similarity", 0.7)
        confidence = phase.get("confidence", self.default_confidence)
        timeout = phase.get("timeout_sec", 120)

        elapsed = time.time() - self._phase_start_time

        if elapsed > timeout:
            engine.log(f"[-] {self.name}: Timeout.")
            self._mem = None
            self._advance_phase(engine)
            return True

        # Инициализация
        if not hasattr(self, '_mem') or self._mem is None:
            self._mem = {
                "all_positions": [],
                "known": {},
                "crops": {},
                "next_group": 0,
                "round": 0,
            }

        mem = self._mem
        img = self._fresh_frame(engine)
        if img is None:
            return True

        engine.locked_since = time.time()

        # Проверяем победу
        for exit_t in exit_templates:
            if engine.vision.find_template(img, exit_t, threshold=confidence):
                engine.log(f"[+] {self.name}: Победа!")
                self._mem = None
                self._advance_phase(engine)
                return True

        # Размер клетки
        if closed_tmpl in engine.vision._template_cache:
            cell_h, cell_w = engine.vision._template_cache[closed_tmpl]["original_shape"]
        else:
            cell_w, cell_h = 60, 60

        # Шаг 1: запомнить все 9 позиций (один раз)
        if not mem["all_positions"]:
            cells = engine.vision.find_all_templates(img, closed_tmpl, threshold=confidence)
            if len(cells) < 9 and elapsed < 5.0:
                engine.log(f"[*] {self.name}: Ждём сетку ({len(cells)}/9)...")
                self._cooldown_until = time.time() + 1.0
                return True

            mem["all_positions"] = [(int(cx), int(cy)) for cx, cy in cells]
            engine.log(f"[*] {self.name}: Сетка: {len(mem['all_positions'])} клеток запомнено.")
            return True

        # Определяем какие клетки сейчас закрыты
        closed_now = engine.vision.find_all_templates(img, closed_tmpl, threshold=confidence)
        closed_set = set()
        for cx, cy in closed_now:
            for px, py in mem["all_positions"]:
                if abs(int(cx) - px) < cell_w // 2 and abs(int(cy) - py) < cell_h // 2:
                    closed_set.add((px, py))
                    break

        # Ищем тройку среди известных закрытых
        group_to_closed = {}
        for pos, gid in mem["known"].items():
            if pos in closed_set:
                if gid not in group_to_closed:
                    group_to_closed[gid] = []
                group_to_closed[gid].append(pos)

        for gid, positions in group_to_closed.items():
            if len(positions) >= 3:
                trio = positions[:3]
                engine.log(f"[!] {self.name}: Тройка group {gid}! Кликаем {trio}")
                for tx, ty in trio:
                    engine.capture.tap(tx, ty, variance=3)
                    time.sleep(cell_open_delay)

                # Ждём и проверяем: клетки исчезли (успех) или закрылись (ошибка)?
                time.sleep(cell_close_delay)
                with engine._raw_frame_lock:
                    check_frame = engine._latest_raw_frame
                if check_frame is not None:
                    check_frame = check_frame.copy()
                    engine.vision.prepare_frame(check_frame)
                    still_there = engine.vision.find_all_templates(check_frame, closed_tmpl, threshold=confidence)
                    still_positions = set()
                    for sx, sy in still_there:
                        for tx, ty in trio:
                            if abs(int(sx) - tx) < cell_w // 2 and abs(int(sy) - ty) < cell_h // 2:
                                still_positions.add((tx, ty))

                    if len(still_positions) == 0:
                        # Успех — клетки исчезли
                        engine.log(f"[+] {self.name}: Тройка засчитана!")
                        for pos in trio:
                            mem["known"].pop(pos, None)
                            mem["crops"].pop(pos, None)
                            if pos in mem["all_positions"]:
                                mem["all_positions"].remove(pos)
                    else:
                        # Ошибка — клетки вернулись, группировка была неверной
                        engine.log(f"[-] {self.name}: Тройка НЕ засчитана! Сброс группы {gid}.")
                        # Удаляем все записи этой группы — они неверные
                        bad_positions = [p for p, g in mem["known"].items() if g == gid]
                        for pos in bad_positions:
                            mem["known"].pop(pos, None)
                            mem["crops"].pop(pos, None)

                self._cooldown_until = time.time() + 0.5
                return True

        # Разведка: выбираем 3 неизвестные закрытые клетки
        unknown = [pos for pos in closed_set if pos not in mem["known"]]
        if not unknown:
            # Все известны но тройки нет — ошибки накопились, сброс
            engine.log(f"[*] {self.name}: Все известны, тройки нет. Сброс памяти.")
            mem["known"] = {}
            mem["crops"] = {}
            mem["next_group"] = 0
            unknown = list(closed_set)

        cells_to_open = unknown[:3]
        mem["round"] += 1
        engine.log(f"[*] {self.name}: Раунд {mem['round']} - кликаем {cells_to_open}")

        for cx, cy in cells_to_open:
            engine.capture.tap(cx, cy, variance=3)
            time.sleep(cell_open_delay + 0.5)

            # Берём кадр из буфера capture thread (не PrintWindow напрямую!)
            with engine._raw_frame_lock:
                frame = engine._latest_raw_frame
            if frame is None:
                continue
            frame = frame.copy()

            # Кропаем
            crop = engine.vision.crop_cell(frame, cx, cy, cell_w, cell_h)
            if crop is None:
                continue

            # Проверяем что кроп не чёрный (клетка реально открылась)
            mean_brightness = float(crop.mean())
            if mean_brightness < 30:
                engine.log(f"    ({cx},{cy}) = слишком тёмный (brightness={mean_brightness:.0f}), пропуск")
                continue


            # Сравниваем с известными
            matched_group = None
            best_sim = 0.0
            best_pos = None
            for (px, py), saved_crop in mem["crops"].items():
                sim = engine.vision.compare_cells(crop, saved_crop)
                if sim > best_sim:
                    best_sim = sim
                    best_pos = (px, py)
                if sim >= similarity_threshold:
                    matched_group = mem["known"].get((px, py))
                    break

            if matched_group is not None:
                mem["known"][(cx, cy)] = matched_group
                mem["crops"][(cx, cy)] = crop
                engine.log(f"    ({cx},{cy}) = group {matched_group} (sim={best_sim:.2f} vs {best_pos})")
            else:
                gid = mem["next_group"]
                mem["next_group"] += 1
                mem["known"][(cx, cy)] = gid
                mem["crops"][(cx, cy)] = crop
                engine.log(f"    ({cx},{cy}) = group {gid} NEW (best_sim={best_sim:.2f} vs {best_pos})")

        self._cooldown_until = time.time() + cell_close_delay

        groups = {}
        for gid in mem["known"].values():
            groups[gid] = groups.get(gid, 0) + 1
        engine.log(f"[*] {self.name}: Известно {len(mem['known'])}/{len(mem['all_positions'])}, группы: {groups}")

        return True

    def reset(self, engine=None):
        self._pipeline_active = False
        self._current_phase = 0
        self._phase_click_count = 0
        self._mem = None
        if engine and engine.is_locked:
            engine.is_locked = False
            engine.log(f"[*] {self.name}: Pipeline сброшен.")
