"""
Утилита для захвата скриншота окна Discord.
Запусти, открой нужный экран игры, скрин сохранится в templates/full_screen.png.
Потом вырезай нужные кнопки в Paint/Photoshop.
"""
import time
import os
import sys
import cv2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.window_capture import WindowCapture


def main():
    print("[*] Захват скриншота Discord для шаблонов")

    capture = WindowCapture(window_title="Discord")

    if not capture.connect():
        return

    print("[*] Ждем 3 секунды. Открой нужный экран в игре...")
    time.sleep(3)

    img = capture.get_screencap()
    if img is not None:
        if not os.path.exists("templates"):
            os.makedirs("templates")

        filepath = os.path.join("templates", "full_screen.png")
        cv2.imwrite(filepath, img)
        print(f"[+] Скриншот сохранен: {filepath}")
        print("[!] Открой файл в Paint, вырежи нужные кнопки и сохрани в templates/")
    else:
        print("[-] Ошибка захвата экрана")


if __name__ == "__main__":
    main()
