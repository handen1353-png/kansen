from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NORMALIZED = ROOT / "normalized"
OUTPUT = ROOT / "questions.json"


EXAMS = [
    ("2011", "第19回", "17331072346040_第19回(2011)認定試験　問題[1].txt", "doc"),
    ("2012", "第20回", "17331072984157_第20回認定看護師認定審査試験.txt", "doc"),
    ("2013", "第21回", "17331074863145_第21回認定審査　問題[1].txt", "doc"),
    ("2015", "第23回", "17331081987732_第23回認定看護師認定審査 (1).txt", "pdf"),
    ("2017", "第25回", "17331082649568_第25回認定看護師認定審査 (1).txt", "pdf"),
    ("2018", "第26回", "17331082951046_第26回認定看護師認定審査 (1).txt", "image"),
    ("2019", "第27回", "第27回（2019年）感染管理認定看護師認定審査問題.txt", "pdf"),
    ("2020", "第28回", "第28回(2020年) 感染管理認定看護師審査 問題用紙.txt", "pdf"),
    ("2022", "令和4年", "令和4年認定過去問.txt", "image"),
]


def clean(text: str) -> str:
    text = text.replace("\x00", " ").replace("\ufeff", " ")
    text = re.sub(r"===== PAGE \d+ =====", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def num(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value).lower().replace("l", "1").replace("i", "1")
    digits = re.sub(r"\D", "", normalized)
    if not digits:
        return None
    result = int(digits)
    return result if 1 <= result <= 40 else None


MARKER = re.compile(r"(?:★\s*)?(?:【|I)\s*[問間]\s*([0-9０-９lI]{1,2})\s*(?:】|1|z)?")


def split_sections(text: str) -> dict[int, str]:
    text = clean(text)
    matches = list(MARKER.finditer(text))
    sections: dict[int, str] = {}
    for index, marker in enumerate(matches):
        question_number = num(marker.group(1))
        if not question_number or question_number in sections:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[marker.end() : end].strip()
        if len(section) >= 8:
            sections[question_number] = section
    return sections


CHOICE = re.compile(r"(?<![0-9０-９])([1-4１-４])\s*[\.．、_]")


def split_choices(section: str) -> tuple[str, list[str]]:
    markers = list(CHOICE.finditer(section))
    starts = []
    seen = set()
    for marker in markers:
        value = int(unicodedata.normalize("NFKC", marker.group(1)))
        if value not in seen:
            starts.append((value, marker.start(), marker.end()))
            seen.add(value)
        if len(starts) == 4:
            break
    if [value for value, _, _ in starts] != [1, 2, 3, 4]:
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        if len(lines) >= 5:
            chunks = []
            current = ""
            for line in lines:
                current = (current + " " + line).strip()
                if current.endswith(("。", "．", ".")):
                    chunks.append(current)
                    current = ""
            if current:
                chunks.append(current)
            if len(chunks) >= 5:
                candidate_choices = chunks[-4:]
                candidate_question = " ".join(chunks[:-4]).strip()
                if candidate_question and all(len(item) >= 4 for item in candidate_choices):
                    return candidate_question[:2400], [item[:1600] for item in candidate_choices]
        return section[:900].replace("\n", " "), [
            "原文から選択肢を自動分割できませんでした。",
            "管理画面で原文を確認し、選択肢を登録してください。",
            "解答未登録です。",
            "要確認",
        ]
    question = section[: starts[0][1]].strip()
    choices = []
    for index, (_, _, content_start) in enumerate(starts):
        content_end = starts[index + 1][1] if index + 1 < len(starts) else len(section)
        choices.append(section[content_start:content_end].strip()[:1600])
    return question[:2400] or "問題文を管理画面で確認してください。", choices


def read_answers() -> dict[str, dict[int, int | None]]:
    cells = json.loads(next(NORMALIZED.glob("*.json")).read_text(encoding="utf-8"))
    grid = {(cell["row"], cell["col"]): cell["value"] for cell in cells if cell["sheet"] == "Sheet1"}
    result: dict[str, dict[int, int | None]] = {}
    for exam, start_col in [("第23回", 5), ("第25回", 15)]:
        answers = {}
        for row in range(2, 22):
            for question_col, answer_col in [(start_col, start_col + 1), (start_col + 2, start_col + 3)]:
                question = grid.get((row, question_col))
                answer = grid.get((row, answer_col))
                if isinstance(question, (int, float)):
                    answers[int(question)] = int(answer) if isinstance(answer, (int, float)) and answer in (1, 2, 3, 4) else None
        result[exam] = answers
    return result


def build() -> list[dict]:
    answers = read_answers()
    result = []
    app_id = 1
    summary = []
    for year, exam, filename, kind in EXAMS:
        text = (NORMALIZED / filename).read_text(encoding="utf-8", errors="replace")
        sections = split_sections(text) if kind != "image" else {}
        parsed = 0
        for question_number in range(1, 41):
            section = sections.get(question_number, "")
            if section:
                question, choices = split_choices(section)
                parsed += 1
            else:
                question = f"{exam} 問{question_number}：画像PDFまたは抽出困難な箇所のため、問題文は未抽出です。管理画面から登録してください。"
                choices = [
                    "選択肢1（未抽出）",
                    "選択肢2（未抽出）",
                    "選択肢3（未抽出）",
                    "選択肢4（未抽出）",
                ]
            correct = answers.get(exam, {}).get(question_number)
            note = "原資料から自動抽出しました。"
            if "未抽出" in question or "自動分割できません" in choices[0]:
                note += " 問題文または選択肢を管理画面で確認・修正してください。"
            if correct is None:
                note += " 正解は未登録です。"
            else:
                note += " 正解番号は解答表から登録しました。"
            result.append(
                {
                    "id": app_id,
                    "year": year,
                    "category": f"{exam}過去問",
                    "question": question,
                    "choices": choices,
                    "correct": correct,
                    "explanation": note,
                    "isAiGenerated": False,
                }
            )
            app_id += 1
        summary.append({"exam": exam, "year": year, "sectionsFound": len(sections), "records": 40})
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "question_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    questions = build()
    print(f"questions={len(questions)}")
    for row in json.loads((ROOT / "question_summary.json").read_text(encoding="utf-8")):
        print(f"{row['exam']}\tsections={row['sectionsFound']}\trecords={row['records']}")
