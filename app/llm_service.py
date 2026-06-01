"""
Table extraction pipeline:
  1. Qwen VL sees image → outputs structured JSON directly (best layout accuracy)
  2. If JSON broken → DeepSeek repairs (fixes syntax, preserves content)
  3. Fallback: OCR spatial data → simple table
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from base64 import b64encode
from io import BytesIO
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

import httpx
from PIL import Image

from .config import get_settings
from .models import CellData, TableData

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# Qwen VL: output compact format — 3x smaller than JSON, no truncation
VISION_PROMPT = """仔细观察这张表格图片，用紧凑格式输出所有单元格。

⚠️ 特别注意：表格最左侧的竖排文字（竖向排列的文字）也必须识别并输出。
⚠️ 表格底部的备注、签名栏等内容不能遗漏。

格式（每个单元格一行，从上到下、从左到右）：
标题: 表格标题
行0 列0 | 文字:单位工程名称 | 跨行:1 | 跨列:1
行0 列1 | 文字:某高标准农田项目 | 跨行:1 | 跨列:3
...

规则：
1. 竖排文字请转换为横排文字输出（如竖排"验收结论"输出为"验收结论"）
2. 每个单元格必须标注跨行和跨列，默认为1
3. 被合并占用的空位也要输出，文字留空
4. 保留 □ ☑ 等特殊符号，精确还原数字和单位
5. 逐格输出，不要跳格，不要遗漏任何区域"""

# DeepSeek: compact format → JSON
STRUCTURING_PROMPT = """将紧凑格式的表格数据转换为标准JSON。

输入格式：每行一个单元格，格式为 "行N 列M | 文字:xxx | 跨行:N | 跨列:N"

输出格式：
{
  "title": "...",
  "rows": [[{"text": "...", "rowspan": 1, "colspan": 1}, ...], ...]
}

规则：
1. 根据"行N 列M"将单元格放入正确位置
2. 根据"跨行""跨列"设置 rowspan/colspan
3. 被合并占位的空位也要保留
4. 每行 colspan 总和必须相等
5. 只输出纯 JSON"""

# DeepSeek: fix broken JSON
REPAIR_PROMPT = """修复以下有语法错误的JSON，保留所有文字内容不变，只修复语法。只输出修复后的纯JSON。"""

# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

MOCK_TABLE = {
    "title": "水利水电工程单元工程施工质量评定表",
    "rows": [
        [
            {"text": "单位工程名称", "rowspan": 1, "colspan": 1},
            {"text": "某高标准农田灌溉项目", "rowspan": 1, "colspan": 3},
        ],
        [
            {"text": "检查项目", "rowspan": 1, "colspan": 2},
            {"text": "质量标准", "rowspan": 1, "colspan": 1},
            {"text": "检查记录", "rowspan": 1, "colspan": 1},
        ],
        [
            {"text": "基础面处理", "rowspan": 1, "colspan": 1},
            {"text": "无淤泥、无积水", "rowspan": 1, "colspan": 1},
            {"text": "符合设计要求", "rowspan": 1, "colspan": 1},
            {"text": "☑ 合格  □ 不合格", "rowspan": 1, "colspan": 1},
        ],
        [
            {"text": "综合评定", "rowspan": 1, "colspan": 3},
            {"text": "合格", "rowspan": 1, "colspan": 1},
        ],
    ],
}

# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_OCR_READER = None


def _get_ocr():
    global _OCR_READER
    if _OCR_READER is None:
        try:
            import easyocr
            _OCR_READER = easyocr.Reader(get_settings().ocr_languages, gpu=True)
            logger.info("EasyOCR ready")
        except Exception as e:
            logger.warning("EasyOCR unavailable: %s", e)
            _OCR_READER = False
    return _OCR_READER if _OCR_READER is not False else None


def _run_ocr(image_bytes: bytes) -> list[dict]:
    """Run OCR + vertical text detection → list of {text, x, y, w, h}.

    Runs EasyOCR twice: normal orientation + 90° rotated for vertical text.
    """
    reader = _get_ocr()
    if not reader:
        return []
    try:
        import numpy as np
        img = Image.open(BytesIO(image_bytes))
        arr = np.array(img)

        # Pass 1: normal orientation
        results = reader.readtext(arr)

        # Pass 2: rotate 90° CW to detect vertical text, then rotate coordinates back
        h, w = arr.shape[:2]
        arr_rot = np.rot90(arr, k=-1)  # 90° clockwise
        results_rot = reader.readtext(arr_rot)

        # Convert rotated coordinates back to original
        for bbox, text, _ in results_rot:
            # bbox in rotated image: (x', y') → original: x = h - y', y = x'
            x_orig = h - int(bbox[1][1])  # y_max in rotated → x in original
            y_orig = int(bbox[0][0])       # x_min in rotated → y in original
            # Estimate width/height
            rw = int(bbox[2][1] - bbox[0][1])
            rh = int(bbox[2][0] - bbox[0][0])
            # Add as synthetic bbox
            results.append(([[x_orig, y_orig], [x_orig + rh, y_orig],
                            [x_orig + rh, y_orig + rw], [x_orig, y_orig + rw]], text, 0.5))
    except Exception:
        return []

    items = []
    seen = set()
    for bbox, text, _ in results:
        x, y = int(bbox[0][0]), int(bbox[0][1])
        bw = int(bbox[2][0] - bbox[0][0])
        bh = int(bbox[2][1] - bbox[0][1])
        # Deduplicate by text + approximate position
        key = (text.strip(), x // 20, y // 20)
        if key in seen or not text.strip():
            continue
        seen.add(key)
        items.append({"text": text.strip(), "x": x, "y": y, "w": bw, "h": bh})

    items.sort(key=lambda it: (it["y"], it["x"]))
    logger.info("OCR: %d texts (incl. vertical)", len(items))
    return items


def _row_threshold(img_h: int) -> int:
    """Dynamic row-grouping threshold based on image height.
    Returns px value: ~1/50 of image height, clamped to [15, 80].
    Falls back to 20px when image dimensions are unknown.
    """
    if img_h <= 0:
        return 20
    return max(15, min(80, img_h / 50))


def _ocr_to_text(items: list[dict], img_h: int = 0) -> str:
    """Format OCR items as spatial text for DeepSeek."""
    if not items:
        return ""
    rows = []
    thresh = _row_threshold(img_h)
    current, threshold = [], thresh
    for item in items:
        if not current or abs(item["y"] - current[0]["y"]) < threshold:
            current.append(item)
        else:
            rows.append(current)
            current = [item]
    if current:
        rows.append(current)

    lines = ["OCR 空间坐标（像素）："]
    for i, row in enumerate(rows):
        cells = [f'{c["text"]}(x={c["x"]},y={c["y"]},w={c["w"]},h={c["h"]})' for c in row]
        lines.append(f"行{i}: " + " | ".join(cells))
    logger.info("OCR: %d rows", len(rows))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON parsing & repair
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return b64encode(data).decode("utf-8")


def _detect_dpi(img: Image.Image) -> int:
    """Detect image DPI from metadata. Falls back to 200 if unavailable."""
    try:
        info = img.info
        if 'dpi' in info and info['dpi']:
            dpi = info['dpi'][0] if isinstance(info['dpi'], (tuple, list)) else info['dpi']
            return int(dpi)
    except Exception:
        pass
    return 200


def _preprocess(data: bytes) -> tuple[bytes, int, int, int]:
    """Preprocess image for VL model.
    Returns (image_bytes, width, height, dpi).
    Only downscales truly oversized images (> max_width/height).
    """
    s = get_settings()
    try:
        img = Image.open(BytesIO(data))
        w, h = img.size
        dpi = _detect_dpi(img)

        # Log low-DPI images — text may be hard to read
        if dpi < s.image_min_dpi:
            logger.warning(
                "Low DPI image: %ddpi (%dx%dpx). "
                "Text may be unclear for VL model. Recommend >=%ddpi.",
                dpi, w, h, s.image_min_dpi,
            )

        # Only downscale if oversized
        if w > s.image_max_width or h > s.image_max_height:
            r = min(s.image_max_width / w, s.image_max_height / h)
            new_w, new_h = int(w * r), int(h * r)
            logger.info(
                "Downscaling: %dx%d → %dx%d (%.0f%%, DPI was %d)",
                w, h, new_w, new_h, r * 100, dpi,
            )
            img = img.resize((new_w, new_h), Image.LANCZOS)
            w, h = new_w, new_h

        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), w, h, dpi
    except Exception:
        # Bare fallback — return raw data with unknown dimensions
        logger.warning("Image preprocessing failed, using raw bytes")
        return data, 0, 0, 200


def _extract_json(text: str) -> dict[str, Any]:
    """Multi-strategy JSON extraction."""
    t = text.strip()
    for s in [t]:
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            pass
    # Repair unclosed braces
    if i != -1:
        sub = t[i:j + 1]
        need = sub.count("{") - sub.count("}")
        if need > 0:
            try:
                return json.loads(sub + "}" * need)
            except json.JSONDecodeError:
                pass
        need_b = sub.count("[") - sub.count("]")
        if need_b > 0:
            need_c = sub.count("{") - sub.count("}")
            repaired = sub + "]" * need_b + "}" * need_c
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Failed to extract JSON. Preview: {t[:300]}...")


def _parse(raw: dict[str, Any]) -> TableData:
    rows = [[CellData(**c) for c in row] for row in raw.get("rows", [])]
    return TableData(title=raw.get("title", ""), rows=rows)


def _parse_compact(text: str) -> dict[str, Any] | None:
    """Parse Qwen's compact format directly in Python.

    Format:
      标题: xxx
      行0 列0 | 文字:xxx | 跨行:1 | 跨列:1 | 竖排:否
      行0 列1 | 文字:xxx | 跨行:1 | 跨列:3 | 竖排:否
    """
    lines = text.strip().split("\n")
    title = ""
    cells: dict[tuple[int, int], dict[str, Any]] = {}
    max_row, max_col = 0, 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("标题:") or line.startswith("标题："):
            title = line.split(":", 1)[-1].split("：", 1)[-1].strip()
            continue

        # Parse: 行N 列M | 文字:xxx | 跨行:N | 跨列:N
        m = re.match(r"行(\d+)\s+列(\d+)\s*\|\s*文字:(.*?)\s*\|\s*跨行:(\d+)\s*\|\s*跨列:(\d+)", line)
        if not m:
            continue

        r, c = int(m.group(1)), int(m.group(2))
        txt = m.group(3).strip()
        rs, cs = int(m.group(4)), int(m.group(5))

        cells[(r, c)] = {"text": txt, "rowspan": rs, "colspan": cs}
        max_row = max(max_row, r)
        max_col = max(max_col, c + cs - 1)

    if not cells:
        return None

    # Build grid
    rows: list[list[dict[str, Any]]] = []
    occupied: set[tuple[int, int]] = set()

    for r in range(max_row + 1):
        row: list[dict[str, Any]] = []
        c = 0
        while c <= max_col:
            if (r, c) in occupied:
                c += 1
                continue
            cell = cells.get((r, c), {"text": "", "rowspan": 1, "colspan": 1})
            row.append(cell)
            # Mark occupied cells for rowspan/colspan
            for ri in range(r, r + cell["rowspan"]):
                for ci in range(c, c + cell["colspan"]):
                    if ri != r or ci != c:
                        occupied.add((ri, ci))
            c += cell["colspan"]
        if row:
            rows.append(row)

    logger.info("Compact parser: %d rows from %d cells", len(rows), len(cells))
    return {"title": title, "rows": rows}


def _merge_ocr_text(table: dict[str, Any], ocr_items: list[dict], img_h: int = 0) -> dict[str, Any]:
    """Fill empty cell texts using OCR data.

    For each empty cell in the table, look for OCR text in the corresponding
    position and fill it in. This combines Qwen's layout accuracy with
    EasyOCR's text extraction reliability.
    """
    if not ocr_items:
        return table

    # Build OCR grid: group by rows, then columns within each row
    items = sorted(ocr_items, key=lambda it: (it["y"], it["x"]))
    ocr_rows: list[list[str]] = []
    cur_row: list[dict] = []
    thresh = _row_threshold(img_h)

    for it in items:
        if not cur_row or abs(it["y"] - cur_row[0]["y"]) < thresh:
            cur_row.append(it)
        else:
            # Sort current row by x, extract texts
            cur_row.sort(key=lambda it: it["x"])
            ocr_rows.append([c["text"] for c in cur_row])
            cur_row = [it]
    if cur_row:
        cur_row.sort(key=lambda it: it["x"])
        ocr_rows.append([c["text"] for c in cur_row])

    if not ocr_rows:
        return table

    # Fill empty cells
    rows = table.get("rows", [])
    filled = 0
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            if not cell.get("text", "").strip():
                # Try to get text from OCR at same position
                if r_idx < len(ocr_rows) and c_idx < len(ocr_rows[r_idx]):
                    ocr_text = ocr_rows[r_idx][c_idx].strip()
                    if ocr_text:
                        cell["text"] = ocr_text
                        filled += 1

    if filled:
        logger.info("OCR merge: filled %d empty cells", filled)
    return table


def _build_from_ocr(items: list[dict], img_h: int = 0) -> dict[str, Any] | None:
    """Build simple table from OCR items — last resort."""
    if not items:
        return None
    rows, cur, thresh = [], [], _row_threshold(img_h)
    for it in items:
        if not cur or abs(it["y"] - cur[0]["y"]) < thresh:
            cur.append(it)
        else:
            rows.append([{"text": c["text"], "rowspan": 1, "colspan": 1} for c in cur])
            cur = [it]
    if cur:
        rows.append([{"text": c["text"], "rowspan": 1, "colspan": 1} for c in cur])
    return {"title": "", "rows": rows} if rows else None


def _save_debug(text: str, tag: str = "qwen") -> None:
    try:
        d = Path(__file__).resolve().parent.parent / "debug_logs"
        d.mkdir(exist_ok=True)
        p = d / f"{tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        p.write_text(text, encoding="utf-8")
        logger.info("Debug saved: %s (%d chars)", p.name, len(text))
    except Exception:
        pass


def _cache_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class _Cache:
    def __init__(self, n: int, ttl: int):
        self.n, self.ttl = n, ttl
        self.d: dict[str, tuple[float, Any]] = {}
        self.o: list[str] = []

    def get(self, k: str) -> Any | None:
        if k not in self.d:
            return None
        ts, v = self.d[k]
        if time.monotonic() - ts > self.ttl:
            del self.d[k]; self.o.remove(k)
            return None
        self.o.remove(k); self.o.append(k)
        return v

    def set(self, k: str, v: Any) -> None:
        if k in self.d:
            self.o.remove(k)
        elif len(self.d) >= self.n:
            del self.d[self.o.pop(0)]
        self.d[k] = (time.monotonic(), v)
        self.o.append(k)


# ---------------------------------------------------------------------------
# LLM Service
# ---------------------------------------------------------------------------


class LLMService:
    """Qwen VL → JSON, DeepSeek repairs if broken, OCR as last resort."""

    def __init__(
        self,
        vision_key: str = "", vision_url: str = "", vision_model: str = "",
        reasoning_key: str = "", reasoning_url: str = "", reasoning_model: str = "",
        use_mock: bool = False,
    ) -> None:
        s = get_settings()
        self._vk = vision_key or s.vision_api_key
        self._vu = (vision_url or s.resolved_vision_url).rstrip("/")
        self._vm = vision_model or s.resolved_vision_model
        self._rk = reasoning_key or s.reasoning_api_key
        self._ru = (reasoning_url or s.resolved_reasoning_url).rstrip("/")
        self._rm = reasoning_model or s.resolved_reasoning_model
        self._mt = s.llm_max_tokens
        self._to = s.llm_timeout
        self._ocr = s.ocr_enabled
        self._mock = use_mock or not (self._vk and self._rk)
        self._cache = _Cache(s.cache_max_entries, s.cache_ttl_seconds) if s.cache_enabled else None
        self._last_raw = ""

        if self._mock:
            logger.info("MOCK mode")
        else:
            logger.info("Vision=%s | Repair=%s | OCR=%s", self._vm, self._rm, self._ocr)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def analyze(self, image_bytes: bytes, image_format: str = "PNG") -> TableData:
        if self._mock:
            return _parse(MOCK_TABLE)

        ck = _cache_key(image_bytes)
        if self._cache:
            v = self._cache.get(ck)
            if v:
                return _parse(v)

        img, img_w, img_h, img_dpi = _preprocess(image_bytes)
        self._img_h = img_h
        self._img_dpi = img_dpi

        # OCR
        ocr_items = []
        if self._ocr:
            try:
                ocr_items = await asyncio.to_thread(_run_ocr, img)
            except Exception:
                pass

        # Pipeline
        try:
            raw = await self._pipeline(img, image_format, ocr_items)
        except Exception:
            # Final fallback: OCR only
            raw = _build_from_ocr(ocr_items, img_h)
            if raw is None:
                raise RuntimeError("All strategies failed. Try a clearer image.")

        if self._cache:
            self._cache.set(ck, raw.copy())
        return _parse(raw)

    def get_mock_table(self) -> TableData:
        return _parse(MOCK_TABLE)

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def _pipeline(
        self, img: bytes, fmt: str, ocr_items: list[dict],
    ) -> dict[str, Any]:
        """1. Qwen VL → compact text  2. DeepSeek → JSON"""
        b64 = _b64(img)

        # Step 1: Qwen VL outputs compact format
        logger.info("Step 1: Qwen VL → compact format")
        raw = await self._call_vision(b64, fmt)
        self._last_raw = raw
        _save_debug(raw, "qwen_compact")

        # Try Python parser first (fast, no API call)
        result = _parse_compact(raw)
        if result is not None:
            logger.info("Step 1 OK (Python): %d rows", len(result.get("rows", [])))
            result = _merge_ocr_text(result, ocr_items, self._img_h)
            return result

        # Step 2: DeepSeek structures compact → JSON
        logger.info("Step 2: DeepSeek structuring")
        ocr_text = _ocr_to_text(ocr_items, self._img_h) if ocr_items else ""
        context = f"紧凑格式数据：\n\n{raw}"
        if ocr_text:
            context = f"{ocr_text}\n\n---\n{context}"

        result = await self._call_structuring(context)
        if result is not None:
            result = _merge_ocr_text(result, ocr_items, self._img_h)
            return result

        # Step 3: OCR fallback
        logger.warning("Step 3: OCR fallback")
        result = _build_from_ocr(ocr_items, self._img_h)
        if result is not None:
            return result

        raise RuntimeError("All strategies exhausted")

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def _call_vision(self, b64_img: str, fmt: str) -> str:
        """Qwen VL: image → JSON text."""
        url = f"{self._vu}/chat/completions"
        async with httpx.AsyncClient(timeout=self._to) as cli:
            r = await cli.post(url, headers={
                "Authorization": f"Bearer {self._vk}",
                "Content-Type": "application/json",
            }, json={
                "model": self._vm,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/{fmt.lower()};base64,{b64_img}"}},
                    {"type": "text", "text": VISION_PROMPT},
                ]}],
                "max_tokens": max(self._mt, 8192),
                "temperature": 0.1,
            })
            r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _call_structuring(self, context: str) -> dict[str, Any] | None:
        """DeepSeek: compact format → JSON."""
        url = f"{self._ru}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._to) as cli:
                r = await cli.post(url, headers={
                    "Authorization": f"Bearer {self._rk}",
                    "Content-Type": "application/json",
                }, json={
                    "model": self._rm,
                    "messages": [
                        {"role": "system", "content": STRUCTURING_PROMPT},
                        {"role": "user", "content": context},
                    ],
                    "max_tokens": self._mt,
                    "temperature": 0.0,
                })
                r.raise_for_status()
        except Exception as e:
            logger.error("Structuring API failed: %s", e)
            return None

        content = r.json()["choices"][0]["message"]["content"]
        _save_debug(content, "deepseek_json")
        try:
            return _extract_json(content)
        except ValueError as e:
            logger.warning("Structuring JSON broken: %s", e)
            return None

    async def _call_repair(self, context: str) -> dict[str, Any] | None:
        """DeepSeek: fix broken JSON."""
        url = f"{self._ru}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._to) as cli:
                r = await cli.post(url, headers={
                    "Authorization": f"Bearer {self._rk}",
                    "Content-Type": "application/json",
                }, json={
                    "model": self._rm,
                    "messages": [
                        {"role": "system", "content": REPAIR_PROMPT},
                        {"role": "user", "content": context},
                    ],
                    "max_tokens": self._mt,
                    "temperature": 0.0,
                })
                r.raise_for_status()
        except Exception as e:
            logger.error("Repair API failed: %s", e)
            return None

        content = r.json()["choices"][0]["message"]["content"]
        _save_debug(content, "deepseek_repaired")
        try:
            return _extract_json(content)
        except ValueError as e:
            logger.warning("Repair JSON still broken: %s", e)
            return None


def create_llm_service(use_mock: bool = False, **kw) -> LLMService:
    return LLMService(use_mock=use_mock, **kw)
