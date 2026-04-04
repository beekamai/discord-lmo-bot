import time
import random
import ctypes
import ctypes.wintypes
import numpy as np
import cv2
import pygetwindow as gw

user32 = ctypes.windll.user32

# DPI Awareness — без этого координаты поедут на мониторах с масштабированием 125%/150%
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

# Win32 константы
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MK_LBUTTON = 0x0001

# Маппинг имён клавиш -> виртуальные коды (VK_*)
VK_MAP = {
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "enter": 0x0D,
    "space": 0x20,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
}


def _make_lparam(x, y):
    """Пакует x, y в LPARAM для WM_LBUTTON*."""
    return (y << 16) | (x & 0xFFFF)


class WindowCapture:
    """
    Захват окна Discord и взаимодействие через Win32 PostMessage.
    Клики и клавиши отправляются напрямую в HWND окна,
    без движения реального курсора и без перехвата клавиатуры.
    """

    def __init__(self, window_title="Discord"):
        self.window_title = window_title
        self._window = None
        self._hwnd = None
        self._input_hwnd = None
        self._last_region = None
        self._gdi_cache = None

    def connect(self):
        """
        Находит окно Discord по exe-процессу (Discord.exe).
        Не триггерится на IDE/браузер с 'Discord' в заголовке.
        """
        self._hwnd = None
        self._window = None

        target_exe = "discord.exe"
        candidates = []

        def _enum_callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            # Получаем PID окна
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            # Получаем имя exe по PID
            try:
                import psutil
                proc = psutil.Process(pid.value)
                exe_name = proc.name().lower()
            except Exception:
                return True

            if exe_name != target_exe:
                return True

            # Получаем заголовок
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            # Получаем размеры
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            candidates.append((hwnd, title, w, h))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)

        if not candidates:
            print("[-] Процесс Discord.exe не найден или нет видимых окон!")
            return False

        # Берём окно с самым длинным заголовком (главное окно, не вспомогательные)
        candidates.sort(key=lambda c: len(c[1]), reverse=True)
        self._hwnd, title, w, h = candidates[0]

        # Если окно свёрнуто, разворачиваем
        SW_RESTORE = 9
        if user32.IsIconic(self._hwnd):
            print("[*] Discord свёрнут. Разворачиваем...")
            user32.ShowWindow(self._hwnd, SW_RESTORE)
            time.sleep(0.5)
            # Перечитываем размер после разворота
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

        # Ищем дочерний Chrome_RenderWidgetHostHWND для кликов
        self._input_hwnd = self._hwnd
        self._find_render_child()

        print(f"[+] Discord найден: '{title}' ({w}x{h}) HWND={self._hwnd} Input={self._input_hwnd}")
        return True

    def _find_render_child(self):
        """Ищет дочерний Chrome_RenderWidgetHostHWND — туда идут клики в Electron."""
        def _child_cb(child_hwnd, _):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child_hwnd, cls, 256)
            if cls.value == "Chrome_RenderWidgetHostHWND":
                self._input_hwnd = child_hwnd
                return False
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        user32.EnumChildWindows(self._hwnd, WNDENUMPROC(_child_cb), 0)

    def get_window_rect(self):
        """Возвращает (left, top, width, height) окна Discord."""
        if self._window:
            try:
                return (self._window.left, self._window.top,
                        self._window.width, self._window.height)
            except Exception:
                pass

        if self._hwnd:
            rect = ctypes.wintypes.RECT()
            if user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
                return (rect.left, rect.top,
                        rect.right - rect.left, rect.bottom - rect.top)
        return None

    def _init_gdi_cache(self, width, height):
        """Создаёт и кеширует GDI объекты для повторного использования."""
        gdi32 = ctypes.windll.gdi32

        # Удаляем старый кеш если размер изменился
        if self._gdi_cache is not None:
            old = self._gdi_cache
            gdi32.DeleteObject(old["bitmap"])
            gdi32.DeleteDC(old["cdc"])
            user32.ReleaseDC(self._hwnd, old["wdc"])

        wdc = user32.GetDC(self._hwnd)
        cdc = gdi32.CreateCompatibleDC(wdc)
        bitmap = gdi32.CreateCompatibleBitmap(wdc, width, height)
        gdi32.SelectObject(cdc, bitmap)

        # BITMAPINFOHEADER (40 bytes) - создаём один раз
        bmi = ctypes.create_string_buffer(40)
        import struct
        struct.pack_into('<iiiHH', bmi, 0, 40, width, -height, 1, 32)

        self._gdi_cache = {
            "wdc": wdc,
            "cdc": cdc,
            "bitmap": bitmap,
            "bmi": bmi,
            "width": width,
            "height": height,
            "buf_size": width * height * 4,
        }

    def get_screencap(self):
        """
        Делает скриншот содержимого окна Discord через PrintWindow.
        Работает даже если окно перекрыто другим.
        GDI объекты кешируются для скорости.
        """
        if not self._hwnd:
            return None

        gdi32 = ctypes.windll.gdi32

        rect = ctypes.wintypes.RECT()
        user32.GetClientRect(self._hwnd, ctypes.byref(rect))
        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width <= 0 or height <= 0:
            return None

        # Пересоздаём GDI кеш только если размер окна изменился
        if (self._gdi_cache is None
                or self._gdi_cache["width"] != width
                or self._gdi_cache["height"] != height):
            self._init_gdi_cache(width, height)

        c = self._gdi_cache

        # PW_CLIENTONLY=1 | PW_RENDERFULLCONTENT=2 = 3
        result = user32.PrintWindow(self._hwnd, c["cdc"], 3)

        if not result:
            gdi32.BitBlt(c["cdc"], 0, 0, width, height, c["wdc"], 0, 0, 0x00CC0020)

        img_buf = ctypes.create_string_buffer(c["buf_size"])
        gdi32.GetDIBits(c["cdc"], c["bitmap"], 0, height, img_buf, c["bmi"], 0)

        img = np.frombuffer(img_buf, dtype=np.uint8).reshape(height, width, 4)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

    def tap(self, x, y, variance=5):
        """
        Кликает по координатам ВНУТРИ окна через PostMessage.
        x, y - координаты относительно клиентской области окна.
        """
        if not self._hwnd:
            return

        rx = int(x) + random.randint(-variance, variance)
        ry = int(y) + random.randint(-variance, variance)
        lparam = _make_lparam(rx, ry)

        target = self._input_hwnd or self._hwnd
        user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        time.sleep(0.01)
        user32.PostMessageW(target, WM_LBUTTONUP, 0, lparam)

    def send_key(self, key, presses=1, interval=0.05):
        """
        Отправляет клавишу в окно через PostMessage.
        Не перехватывает реальную клавиатуру.
        key - имя клавиши: 'left', 'right', 'up', 'down', 'enter', 'space', и т.д.
        """
        if not self._hwnd:
            print("[-] HWND не установлен!")
            return

        vk = VK_MAP.get(key.lower())
        if vk is None:
            print(f"[-] Неизвестная клавиша: {key}")
            return

        target = self._input_hwnd or self._hwnd
        for _ in range(presses):
            user32.PostMessageW(target, WM_KEYDOWN, vk, 0)
            time.sleep(0.01)
            user32.PostMessageW(target, WM_KEYUP, vk, 0)
            time.sleep(interval)

    def is_window_alive(self):
        """Проверяет, что окно Discord ещё существует."""
        if not self._hwnd:
            return False
        return user32.IsWindow(self._hwnd)

    def focus_window(self):
        """Выводит окно Discord на передний план."""
        if not self._window:
            return False
        try:
            self._window.activate()
            time.sleep(0.1)
            return True
        except Exception:
            return False
