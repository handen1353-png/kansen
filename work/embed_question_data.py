from pathlib import Path

root = Path(__file__).resolve().parent.parent
html_path = root / "index.html"
json_path = root / "work" / "questions.json"

html = html_path.read_text(encoding="utf-8")
questions = json_path.read_text(encoding="utf-8")
start = html.index("      var sampleQuestions = [")
end = html.index("\n\n      function safeGet", start)
replacement = "      var sampleQuestions = " + questions.replace("\n", "\n      ") + ";"
html_path.write_text(html[:start] + replacement + html[end:], encoding="utf-8")
