import cv2
import numpy as np
import os
import time

class Vision:
    def __init__(self):
        self._template_cache = {}
        self._prepared_gray = None
        self._overlay_entries = []
        self._overlay_duration = 3.0
        self._text_scale = 0.8

    def load_image_from_memory(self, image_bytes):
        """ Конвертим байты скрина в формат OpenCV (numpy array) """
        image_np = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(image_np, cv2.IMREAD_COLOR)
        return img

    def prepare_frame(self, bgr_image):
        """
        Один раз конвертим BGR->Gray для текущего кадра.
        Grayscale матчинг устойчивее к артефактам сжатия.
        """
        if bgr_image is not None and len(bgr_image.shape) == 3:
            self._prepared_gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        else:
            self._prepared_gray = bgr_image

    def _preload_template(self, template_path):
        if not os.path.exists(template_path):
            return False

        template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
        if template is None:
            return False

        entry = {"original_shape": template.shape[:2]}

        for scale in np.arange(0.5, 1.5, 0.05):
            width = int(template.shape[1] * scale)
            height = int(template.shape[0] * scale)
            if width < 4 or height < 4:
                continue
            entry[round(scale, 2)] = cv2.resize(template, (width, height))

        self._template_cache[template_path] = entry
        return True

    def invalidate_cache(self, template_path=None):
        """ Сбрасываем кэш шаблонов (для Hot Reload) """
        if template_path:
            self._template_cache.pop(template_path, None)
        else:
            self._template_cache.clear()

    def clear_overlays(self):
        self._overlay_entries.clear()

    def draw_overlays(self, main_image):
        """ Рисует все сохраненные оверлеи на изображении, удаляя просроченные. """
        now = time.time()
        new_entries = []
        for entry in self._overlay_entries:
            if now - entry["timestamp"] <= self._overlay_duration:
                if entry["type"] == "rectangle":
                    cv2.rectangle(main_image, entry["top_left"], entry["bottom_right"],
                                  entry["color"], entry["thickness"])
                    cv2.putText(main_image, entry["text"], entry["text_pos"],
                                cv2.FONT_HERSHEY_SIMPLEX, entry["text_scale"],
                                entry["color"], entry["text_thickness"])
                new_entries.append(entry)
        self._overlay_entries = new_entries
        return main_image

    def find_template(self, main_image, template_path, threshold=0.8):
        """
        Ищем темплейт на Grayscale кадре.
        Early exit: если нашли совпадение выше threshold - сразу возвращаем.
        Сохраняет debug-прямоугольники в _overlay_entries.
        """
        if template_path not in self._template_cache:
            if not self._preload_template(template_path):
                return None

        cache = self._template_cache[template_path]
        orig_shape = cache["original_shape"]
        gray = self._prepared_gray
        if gray is None:
            return None

        best_val = -1
        best_loc = None
        best_scale = 1.0

        scales = sorted(
            [s for s in cache.keys() if isinstance(s, float)],
            key=lambda s: abs(s - 1.0)
        )

        for scale in scales:
            resized = cache[scale]
            if resized.shape[1] > gray.shape[1] or resized.shape[0] > gray.shape[0]:
                continue

            result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val > best_val:
                best_val = max_val
                best_loc = max_loc
                best_scale = scale

            if best_val >= threshold:
                break

        if best_val > 0.4:
            h = int(orig_shape[0] * best_scale)
            w = int(orig_shape[1] * best_scale)
            top_left = best_loc
            bottom_right = (top_left[0] + w, top_left[1] + h)

            color = (0, 255, 0) if best_val >= threshold else (0, 0, 255)
            filename = os.path.basename(template_path).split('.')[0]
            text = f"{filename}: {best_val:.2f}"

            self._overlay_entries.append({
                "type": "rectangle",
                "top_left": top_left,
                "bottom_right": bottom_right,
                "color": color,
                "thickness": 2,
                "text": text,
                "text_pos": (top_left[0], top_left[1] - 5),
                "text_scale": self._text_scale,
                "text_thickness": 2,
                "timestamp": time.time()
            })

            if best_val >= threshold:
                center_x = top_left[0] + w // 2
                center_y = top_left[1] + h // 2
                return (center_x, center_y)

        return None

    def find_all_templates(self, main_image, template_path, threshold=0.8):
        """
        Ищет ВСЕ вхождения темплейта на кадре. Возвращает список координат центров.
        Полезно для серийных кликов (последовательности кнопок слева направо).
        """
        if template_path not in self._template_cache:
            if not self._preload_template(template_path):
                return []

        cache = self._template_cache[template_path]
        orig_shape = cache["original_shape"]
        gray = self._prepared_gray
        if gray is None:
            return []

        best_scale = 1.0
        best_val = -1

        scales = sorted(
            [s for s in cache.keys() if isinstance(s, float)],
            key=lambda s: abs(s - 1.0)
        )

        # Находим лучший масштаб по одному совпадению
        for scale in scales:
            resized = cache[scale]
            if resized.shape[1] > gray.shape[1] or resized.shape[0] > gray.shape[0]:
                continue
            result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            if max_val > best_val:
                best_val = max_val
                best_scale = scale
            if best_val >= threshold:
                break

        if best_val < threshold:
            return []

        # Ищем все вхождения на лучшем масштабе
        resized = cache[best_scale]
        result = cv2.matchTemplate(gray, resized, cv2.TM_CCOEFF_NORMED)
        locations = np.where(result >= threshold)

        h = int(orig_shape[0] * best_scale)
        w = int(orig_shape[1] * best_scale)

        hits = []
        for pt in zip(*locations[::-1]):
            center_x = pt[0] + w // 2
            center_y = pt[1] + h // 2
            hits.append((center_x, center_y))

        # Убираем дубликаты (NMS - если центры слишком близко)
        if not hits:
            return []

        filtered = [hits[0]]
        for h_point in hits[1:]:
            too_close = False
            for existing in filtered:
                if abs(h_point[0] - existing[0]) < w // 2 and abs(h_point[1] - existing[1]) < h // 2:
                    too_close = True
                    break
            if not too_close:
                filtered.append(h_point)

        # Сортируем слева направо
        filtered.sort(key=lambda p: p[0])

        # Оверлеи для каждого найденного вхождения
        filename = os.path.basename(template_path).split('.')[0]
        for idx, (cx, cy) in enumerate(filtered):
            top_left = (cx - w // 2, cy - h // 2)
            bottom_right = (cx + w // 2, cy + h // 2)
            self._overlay_entries.append({
                "type": "rectangle",
                "top_left": top_left,
                "bottom_right": bottom_right,
                "color": (255, 200, 0),
                "thickness": 2,
                "text": f"{filename} #{idx + 1}",
                "text_pos": (top_left[0], top_left[1] - 5),
                "text_scale": self._text_scale,
                "text_thickness": 2,
                "timestamp": time.time()
            })

        return filtered

    def crop_cell(self, frame, cell_x, cell_y, cell_w, cell_h):
        """Вырезает внутреннюю часть клетки (без рамки), возвращает grayscale crop."""
        # Обрезаем 20% с каждой стороны чтобы убрать рамку
        margin_x = int(cell_w * 0.2)
        margin_y = int(cell_h * 0.2)
        rx = max(0, cell_x - cell_w // 2 + margin_x)
        ry = max(0, cell_y - cell_h // 2 + margin_y)
        rx2 = min(frame.shape[1], cell_x + cell_w // 2 - margin_x)
        ry2 = min(frame.shape[0], cell_y + cell_h // 2 - margin_y)

        cell_region = frame[ry:ry2, rx:rx2]
        if cell_region.size == 0:
            return None

        gray = cv2.cvtColor(cell_region, cv2.COLOR_BGR2GRAY) if len(cell_region.shape) == 3 else cell_region
        resized = cv2.resize(gray, (64, 64))
        return resized

    def compare_cells(self, crop1, crop2):
        """Сравнивает два кропа клеток. Возвращает 0.0-1.0 (1.0 = одинаковые)."""
        if crop1 is None or crop2 is None:
            return 0.0
        result = cv2.matchTemplate(crop1, crop2, cv2.TM_CCOEFF_NORMED)
        return float(result[0][0])

