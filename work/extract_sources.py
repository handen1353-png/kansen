from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
SOURCE_DIR = ROOT / "sources"
OUT_DIR = ROOT / "normalized"
OUT_DIR.mkdir(exist_ok=True)


def repair_pdf_text(text: str) -> str:
    """Some PDFs expose CP932 bytes as Latin-1 characters."""
    bad = sum(1 for ch in text if 0x80 <= ord(ch) <= 0x9F)
    if bad < max(5, len(text) // 100):
        return text
    try:
        return text.encode("latin1").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        data = text.encode("latin1", errors="replace")
        return data.decode("cp932", errors="replace")


def extract_pdfs() -> None:
    for path in sorted(SOURCE_DIR.glob("*.pdf")):
        reader = PdfReader(str(path))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = repair_pdf_text(page.extract_text() or "")
            except Exception as exc:  # keep processing the remaining pages
                text = f"[[EXTRACT_ERROR {type(exc).__name__}: {exc}]]"
            pages.append(f"\n\n===== PAGE {index} =====\n{text}")
        output = OUT_DIR / f"{path.stem}.txt"
        output.write_text("".join(pages), encoding="utf-8")
        print(f"PDF\t{path.name}\tpages={len(reader.pages)}\tchars={output.stat().st_size}")


def printable_unicode_runs(data: bytes, minimum: int = 1) -> list[str]:
    text = data.decode("utf-16le", errors="ignore")
    runs = re.findall(
        rf"[\u3000-\u30ff\u3400-\u9fffＡ-Ｚａ-ｚ０-９"
        rf"\uFF01-\uFF60A-Za-z0-9 .,，。、：:；;（）()【】「」『』"
        rf"％%・／/＋+\-＝=～〜&_'\"!?<>℃\r\n\t]{{{minimum},}}",
        text,
    )
    return [re.sub(r"\s+", " ", item).strip() for item in runs if item.strip()]


def extract_docs() -> None:
    for path in sorted(SOURCE_DIR.glob("*.doc")):
        text = path.read_bytes().decode("utf-16le", errors="ignore")
        positions = [position for position in (text.find("問題1"), text.find("【問")) if position >= 0]
        start = min(positions) if positions else 0
        text = text[start:]
        text = text.replace("\r", "\n")
        text = "".join(ch if ch == "\n" or ord(ch) >= 32 else " " for ch in text)
        useful = [line.strip() for line in text.splitlines()]
        output = OUT_DIR / f"{path.stem}.txt"
        output.write_text("\n".join(useful), encoding="utf-8")
        print(f"DOC\t{path.name}\truns={len(useful)}\tchars={output.stat().st_size}")


class CompoundFile:
    FREE = 0xFFFFFFFF
    END = 0xFFFFFFFE

    def __init__(self, data: bytes):
        self.data = data
        if data[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
            raise ValueError("Not an OLE compound file")
        self.sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        self.mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        self.num_fat_sectors = struct.unpack_from("<I", data, 44)[0]
        self.first_dir_sector = struct.unpack_from("<I", data, 48)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 56)[0]
        self.first_minifat_sector = struct.unpack_from("<I", data, 60)[0]
        self.num_minifat_sectors = struct.unpack_from("<I", data, 64)[0]
        self.first_difat_sector = struct.unpack_from("<I", data, 68)[0]
        self.num_difat_sectors = struct.unpack_from("<I", data, 72)[0]
        difat = list(struct.unpack_from("<109I", data, 76))
        next_sector = self.first_difat_sector
        per_difat = self.sector_size // 4 - 1
        for _ in range(self.num_difat_sectors):
            sector = self.sector(next_sector)
            difat.extend(struct.unpack_from(f"<{per_difat}I", sector, 0))
            next_sector = struct.unpack_from("<I", sector, self.sector_size - 4)[0]
        fat_sector_ids = [item for item in difat if item not in (self.FREE, self.END)][: self.num_fat_sectors]
        self.fat = []
        for sector_id in fat_sector_ids:
            self.fat.extend(struct.unpack(f"<{self.sector_size // 4}I", self.sector(sector_id)))
        directory_data = self.read_chain(self.first_dir_sector)
        self.entries = []
        for offset in range(0, len(directory_data), 128):
            entry = directory_data[offset : offset + 128]
            if len(entry) < 128:
                continue
            name_length = struct.unpack_from("<H", entry, 64)[0]
            name = entry[: max(0, name_length - 2)].decode("utf-16le", errors="ignore")
            self.entries.append(
                {
                    "name": name,
                    "type": entry[66],
                    "start": struct.unpack_from("<I", entry, 116)[0],
                    "size": struct.unpack_from("<Q", entry, 120)[0],
                }
            )
        root = next((item for item in self.entries if item["type"] == 5), None)
        self.ministream = self.read_chain(root["start"])[: root["size"]] if root else b""
        self.minifat = []
        if self.num_minifat_sectors and self.first_minifat_sector not in (self.FREE, self.END):
            raw = self.read_chain(self.first_minifat_sector)
            self.minifat = list(struct.unpack(f"<{len(raw) // 4}I", raw))

    def sector(self, sector_id: int) -> bytes:
        start = (sector_id + 1) * self.sector_size
        return self.data[start : start + self.sector_size]

    def read_chain(self, start: int, mini: bool = False) -> bytes:
        if start in (self.FREE, self.END):
            return b""
        table = self.minifat if mini else self.fat
        result = bytearray()
        current = start
        seen = set()
        while current not in (self.FREE, self.END) and current not in seen and current < len(table):
            seen.add(current)
            if mini:
                begin = current * self.mini_sector_size
                result.extend(self.ministream[begin : begin + self.mini_sector_size])
            else:
                result.extend(self.sector(current))
            current = table[current]
        return bytes(result)

    def stream(self, name: str) -> bytes:
        entry = next((item for item in self.entries if item["name"].lower() == name.lower()), None)
        if not entry:
            raise KeyError(name)
        mini = entry["size"] < self.mini_cutoff and entry["type"] == 2
        return self.read_chain(entry["start"], mini=mini)[: entry["size"]]


def parse_xls(path: Path) -> list[dict]:
    ole = CompoundFile(path.read_bytes())
    workbook = ole.stream("Workbook")
    records = []
    offset = 0
    while offset + 4 <= len(workbook):
        record_id, size = struct.unpack_from("<HH", workbook, offset)
        payload = workbook[offset + 4 : offset + 4 + size]
        records.append((offset, record_id, payload))
        offset += 4 + size
    sheet_starts = []
    for _, record_id, payload in records:
        if record_id == 0x0085 and len(payload) >= 8:
            start = struct.unpack_from("<I", payload, 0)[0]
            name_size = payload[6]
            flags = payload[7]
            raw = payload[8 : 8 + name_size * (2 if flags & 0x01 else 1)]
            name = raw.decode("utf-16le" if flags & 0x01 else "cp1252", errors="replace")
            sheet_starts.append((start, name))
    sheet_starts.sort()

    def sheet_for(record_offset: int) -> str:
        name = ""
        for start, candidate in sheet_starts:
            if start > record_offset:
                break
            name = candidate
        return name

    strings = []
    for _, record_id, payload in records:
        if record_id != 0x00FC:
            continue
        if len(payload) < 8:
            continue
        _, unique = struct.unpack_from("<II", payload, 0)
        pos = 8
        while len(strings) < unique and pos + 3 <= len(payload):
            chars = struct.unpack_from("<H", payload, pos)[0]
            flags = payload[pos + 2]
            pos += 3
            rich = struct.unpack_from("<H", payload, pos)[0] if flags & 0x08 else 0
            pos += 2 if flags & 0x08 else 0
            phonetic = struct.unpack_from("<I", payload, pos)[0] if flags & 0x04 else 0
            pos += 4 if flags & 0x04 else 0
            wide = bool(flags & 0x01)
            width = 2 if wide else 1
            raw = payload[pos : pos + chars * width]
            pos += chars * width
            strings.append(raw.decode("utf-16le" if wide else "cp1252", errors="replace"))
            pos += rich * 4 + phonetic
    cells = []
    def decode_rk(raw: int):
        if raw & 0x02:
            value = raw >> 2
        else:
            value = struct.unpack("<d", struct.pack("<II", 0, raw & 0xFFFFFFFC))[0]
        return value / 100 if raw & 0x01 else value

    for record_offset, record_id, payload in records:
        sheet = sheet_for(record_offset)
        if not sheet:
            continue
        if record_id == 0x00FD and len(payload) >= 10:
            row, col, _, index = struct.unpack_from("<HHHI", payload, 0)
            value = strings[index] if index < len(strings) else f"[[SST {index}]]"
            cells.append({"sheet": sheet, "row": row, "col": col, "value": value})
        elif record_id == 0x0203 and len(payload) >= 14:
            row, col, _ = struct.unpack_from("<HHH", payload, 0)
            value = struct.unpack_from("<d", payload, 6)[0]
            cells.append({"sheet": sheet, "row": row, "col": col, "value": value})
        elif record_id == 0x027E and len(payload) >= 10:
            row, col, _, raw = struct.unpack_from("<HHHI", payload, 0)
            value = decode_rk(raw)
            cells.append({"sheet": sheet, "row": row, "col": col, "value": value})
        elif record_id == 0x00BD and len(payload) >= 6:
            row, first_col = struct.unpack_from("<HH", payload, 0)
            last_col = struct.unpack_from("<H", payload, len(payload) - 2)[0]
            for index, col in enumerate(range(first_col, last_col + 1)):
                chunk = 4 + index * 6
                if chunk + 6 > len(payload) - 2:
                    break
                raw = struct.unpack_from("<I", payload, chunk + 2)[0]
                cells.append({"sheet": sheet, "row": row, "col": col, "value": decode_rk(raw)})
        elif record_id == 0x0006 and len(payload) >= 14:
            row, col, _ = struct.unpack_from("<HHH", payload, 0)
            result = payload[6:14]
            if result[6:8] == b"\xff\xff":
                value = {0: "", 1: bool(result[0]), 2: f"[[ERROR {result[0]}]]", 3: ""}.get(result[0], "")
            else:
                value = struct.unpack("<d", result)[0]
            cells.append({"sheet": sheet, "row": row, "col": col, "value": value})
    return cells


def extract_xls() -> None:
    for path in sorted(SOURCE_DIR.glob("*.xls")):
        cells = parse_xls(path)
        output = OUT_DIR / f"{path.stem}.json"
        output.write_text(json.dumps(cells, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"XLS\t{path.name}\tcells={len(cells)}")


if __name__ == "__main__":
    extract_pdfs()
    extract_docs()
    extract_xls()
