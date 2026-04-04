import time
import glob
import json
import cv2
import threading
import os
from bot.window_capture import WindowCapture
from bot.vision import Vision
from bot.states.loading_state import LoadingState
from bot.states.scenario_state import ScenarioState
from bot.states.pipeline_state import PipelineState


class BotEngine:
    def __init__(self, window_title="Discord", show_debug=True, target_fps=10.0):
        self.capture = WindowCapture(window_title=window_title)
        self.vision = Vision()
        self.current_frame = None
        self.show_debug = show_debug
        self.target_fps = target_fps

        self.is_locked = False
        self.locked_since = 0

        self._stop_event = threading.Event()
        self._latest_raw_frame = None
        self._raw_frame_lock = threading.Lock()

        self.cv_process_every_n = 3

        self.states = []

        scenario_files = glob.glob("configs/scenarios/*.json")
        for sc_file in scenario_files:
            if "state.json" in sc_file:
                continue

            with open(sc_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("mode") == "pipeline":
                self.states.append(PipelineState(sc_file))
                self.log(f"[*] Pipeline загружен: {sc_file}")
            else:
                self.states.append(ScenarioState(sc_file))
                self.log(f"[*] Сценарий загружен: {sc_file}")

        self.states.sort(key=lambda s: getattr(s, 'priority', 100))
        self.states.append(LoadingState())

    def log(self, message):
        print(message)

    def get_current_frame(self):
        return self.current_frame

    def click(self, x, y, variance=5):
        self.capture.tap(x, y, variance)

    def _get_frame(self):
        """Получает кадр из окна Discord через PrintWindow."""
        return self.capture.get_screencap()

    def _capture_loop(self):
        """
        Capture Thread: только захватывает кадры и складывает в буфер.
        Не блокируется кликами и CV - работает на максимальной скорости.
        """
        while not self._stop_event.is_set():
            try:
                frame = self._get_frame()
                if frame is not None:
                    with self._raw_frame_lock:
                        self._latest_raw_frame = frame
                else:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.1)

    def _worker_loop(self):
        """
        Worker Thread: берёт последний кадр из буфера, прогоняет CV + стейты.
        Не занимается захватом и отображением.
        """
        cv_frame_counter = 0

        while not self._stop_event.is_set():
            try:
                with self._raw_frame_lock:
                    frame = self._latest_raw_frame

                if frame is None:
                    time.sleep(0.05)
                    continue

                self.current_frame = frame

                cv_frame_counter += 1
                if cv_frame_counter % self.cv_process_every_n != 0:
                    time.sleep(1.0 / self.target_fps)
                    continue

                self.vision.prepare_frame(self.current_frame)

                if self.is_locked and (time.time() - self.locked_since > 60):
                    self.log("[-] Лок висит слишком долго (60 сек). Принудительный сброс!")
                    self.is_locked = False
                    self.locked_since = 0
                    for state in self.states:
                        if hasattr(state, 'reset'):
                            state.reset(self)

                action_taken = False
                for state in self.states:
                    if state.execute(self):
                        action_taken = True
                        break

                if not action_taken:
                    self.log("[*] Ни один стейт не нашел знакомых кнопок.")

                time.sleep(1.0 / self.target_fps)

            except Exception as e:
                self.log(f"[-] Ошибка в Worker Thread: {e}")
                time.sleep(2)

    def start(self):
        self.log("[*] Запускаем Last Meadow Online Bot Engine")

        if not self.capture.connect():
            self.log("[-] Не удалось найти окно Discord.")
            return

        self.log("[*] Окно Discord найдено. Запускаем цикл...")

        capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        capture_thread.start()

        worker = threading.Thread(target=self._worker_loop, daemon=True)
        worker.start()

        fps_counter = 0
        fps_display = 0
        fps_timer = time.time()

        try:
            while True:
                if self.show_debug:
                    with self._raw_frame_lock:
                        raw = self._latest_raw_frame

                    if raw is not None:
                        display = self.vision.draw_overlays(raw.copy())

                        h, w = display.shape[:2]
                        resized = cv2.resize(display, (w // 2, h // 2))

                        fps_counter += 1
                        now = time.time()
                        if now - fps_timer >= 1.0:
                            fps_display = fps_counter
                            fps_counter = 0
                            fps_timer = now

                        cv2.putText(resized, f"FPS: {fps_display}", (10, 30),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

                        cv2.imshow("Last Meadow Online - Bot Vision", resized)

                    key = cv2.waitKey(30) & 0xFF
                    if key == 27:
                        break
                else:
                    time.sleep(1.0 / self.target_fps)
        except KeyboardInterrupt:
            self.log("\n[+] Бот остановлен.")
        finally:
            self._stop_event.set()
            cv2.destroyAllWindows()
