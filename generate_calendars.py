"""Generate per-radiologist .ics files for the current month's rota in README.md.

Parses DR AM/PM and On-Call entries under the latest "### MONTH YYYY" section
(currently JUNE 2026), writes one calendar per radiologist into ./calendars,
and rewrites the trailing "## Calendars" block of README.md with download links.
"""

from __future__ import annotations

import calendar
import hashlib
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
OUT_DIR = ROOT / "calendars"

MONTH_HEADER = "### JUNE 2026"
YEAR = 2026
MONTH = 6

# Canonicalise the many spellings a single doctor appears under (DR slots use
# "Dr Surname", on-call entries use "Dr X Surname", SpRs sometimes go by full name).
NAME_MAP: dict[str, str] = {
    # Consultants
    "Dr Thomas": "Dr D Thomas", "Dr D Thomas": "Dr D Thomas",
    "Dr Jain": "Dr S Jain", "Dr S Jain": "Dr S Jain",
    "Dr Azam": "Dr H Azam", "Dr H Azam": "Dr H Azam",
    "Dr Yahya": "Dr S Yahya", "Dr S Yahya": "Dr S Yahya",
    "Dr Chaudhary": "Dr G Chaudhary", "Dr G Chaudhary": "Dr G Chaudhary",
    "Dr Saha": "Dr S Saha", "Dr S Saha": "Dr S Saha",
    "Dr Kurian": "Dr R Kurian", "Dr R Kurian": "Dr R Kurian",
    "Dr Rafati": "Dr F Rafati", "Dr F Rafati": "Dr F Rafati",
    "Dr Au Yong": "Dr T Au Yong", "Dr T Au Yong": "Dr T Au Yong",
    "Dr Li": "Dr P Li", "Dr P Li": "Dr P Li",
    "Dr Khatoon": "Dr M Khatoon", "Dr M Khatoon": "Dr M Khatoon",
    "Dr Mahmood": "Dr A Mahmood", "Dr A Mahmood": "Dr A Mahmood",
    "Dr Bapusamy": "Dr A Bapusamy", "Dr A Bapusamy": "Dr A Bapusamy",
    "Dr Vydianath": "Dr S Vydianath", "Dr S Vydianath": "Dr S Vydianath",
    "Dr Khan": "Dr I Khan", "Dr I Khan": "Dr I Khan",
    "Dr Agamy": "Dr A Agamy", "Dr A Agamy": "Dr A Agamy",
    "Dr Jawad": "Dr R Jawad", "Dr R Jawad": "Dr R Jawad",
    "Dr Pang": "Dr W Pang", "Dr W Pang": "Dr W Pang",
    "Dr Cole": "Dr K Cole", "Dr K Cole": "Dr K Cole",
    "Dr Syed": "Dr F Syed", "Dr F Syed": "Dr F Syed",
    "Dr Gupta": "Dr A Gupta", "Dr A Gupta": "Dr A Gupta",
    "Dr Qaiyum": "Dr M Qaiyum", "Dr M Qaiyum": "Dr M Qaiyum",
    "Dr Rosewarne": "Dr Rosewarne",
    "Dr Collins": "Dr Collins",
    "Dr Blakeman": "Dr Blakeman",
    # SpRs / Registrars
    "Syed Zaidi": "Syed Zaidi", "Dr S Zaidi": "Syed Zaidi",
    "Vladimir Popa-Nimigean": "Vladimir Popa-Nimigean",
    "Dr V Popa-Nimigean": "Vladimir Popa-Nimigean",
    "Ali Gulamhussein": "Ali Gulamhussein",
    "Dr A Gulamhussein": "Ali Gulamhussein",
    "George Macfarlane": "George Macfarlane",
    "Dr G Macfarlane": "George Macfarlane",
    "Gaurav Bhalla": "Gaurav Bhalla", "Dr G Bhalla": "Gaurav Bhalla",
    "Rajat Bhardwaj": "Rajat Bhardwaj", "Dr R Bhardwaj": "Rajat Bhardwaj",
    "Nihal Chanian": "Nihal Chanian", "Dr N Chanian": "Nihal Chanian",
    "Sobia Khan": "Sobia Khan", "Dr S Khan": "Sobia Khan",
    "Dominic Catlow": "Dominic Catlow", "Dr D Catlow": "Dominic Catlow",
    "Yuuki Na": "Yuuki Na", "Dr Y Na": "Yuuki Na",
    "Tanmay Jadhav": "Tanmay Jadhav", "Dr T Jadhav": "Tanmay Jadhav",
    "Amandeep Pahal": "Amandeep Pahal", "Dr A Pahal": "Amandeep Pahal",
}

SKIP_NAMES = {"ST1", "TBC", "TBA", "—", "-"}

# Stable timestamp so re-runs don't churn UIDs/DTSTAMP.
DTSTAMP = "20260529T000000Z"


def canon(raw: str) -> str | None:
    """Normalise a name fragment. Returns None for unknown / skipped entries."""
    s = raw.strip().rstrip(".,;")
    # Strip trailing markers like "(L)", "(A)", "(MS)" etc.
    s = re.sub(r"\s*\([A-Za-z]+\)\s*$", "", s).strip()
    if not s or s in SKIP_NAMES:
        return None
    if s in NAME_MAP:
        return NAME_MAP[s]
    # Unknown name — keep as-is so we don't silently drop people.
    return s


def parse_people(segment: str) -> list[str]:
    """Pull names out of a slash-separated DR slot like 'Dr X / SpR Y / Dr Z'."""
    out = []
    for part in segment.split("/"):
        c = canon(part)
        if c:
            out.append(c)
    return out


_ON_CALL_RE = re.compile(
    r"1st On Call:\s*(?P<first>[^;]+?)\s*;\s*2nd On Call:\s*(?P<second>.+)",
    re.IGNORECASE,
)


def parse_on_call_people(line: str) -> tuple[list[str], list[str]]:
    """Return (first_on_call_names, second_on_call_names) from one On-Call line."""
    m = _ON_CALL_RE.search(line)
    if not m:
        return [], []

    def split_second(s: str) -> list[str]:
        # Drop any inline weekend sub-window like "**09.00am - 2.00pm**".
        s = re.sub(r"\*\*[^*]+\*\*", " ", s)
        # Split on "/" (A vs B groups) — every named consultant in the segment is on.
        names = []
        for chunk in re.split(r"[/]", s):
            chunk = chunk.strip()
            if not chunk:
                continue
            c = canon(chunk)
            if c:
                names.append(c)
        return names

    firsts = [c for c in (canon(m.group("first")),) if c]
    seconds = split_second(m.group("second"))
    return firsts, seconds


def extract_month_block(text: str) -> str:
    start = text.index(MONTH_HEADER)
    rest = text[start:]
    # Cut at the horizontal rule that ends the rota.
    end = rest.find("\n---")
    return rest if end == -1 else rest[:end]


DAY_HEADER_RE = re.compile(r"^####\s+([A-Za-z]+)\s+(\d{1,2})\s*$", re.MULTILINE)


def iter_days(block: str):
    matches = list(DAY_HEADER_RE.finditer(block))
    for i, m in enumerate(matches):
        day_of_month = int(m.group(2))
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
        yield date(YEAR, MONTH, day_of_month), block[body_start:body_end]


def collect_events():
    """Return {canonical_name: [(start_dt, end_dt, summary, description), ...]}."""
    text = README.read_text()
    block = extract_month_block(text)

    events: dict[str, list[tuple[datetime, datetime, str, str]]] = defaultdict(list)

    for d, body in iter_days(block):
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue

            # DR AM slot
            m = re.match(r"\*\*DR AM\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(9, 0))
                end = datetime.combine(d, time(13, 0))
                for name in parse_people(m.group(1)):
                    events[name].append((start, end, "DR AM", "Diagnostic radiology — morning"))
                continue

            m = re.match(r"\*\*DR PM\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(13, 0))
                end = datetime.combine(d, time(17, 0))
                for name in parse_people(m.group(1)):
                    events[name].append((start, end, "DR PM", "Diagnostic radiology — afternoon"))
                continue

            # On-Call windows
            m = re.match(r"\*\*On-Call\s+5\.00pm\s*[–-]\s*9\.00pm\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(17, 0))
                end = datetime.combine(d, time(21, 0))
                firsts, seconds = parse_on_call_people(m.group(1))
                for name in firsts:
                    events[name].append((start, end, "On-Call 1st (evening)", "1st on-call, 5pm – 9pm"))
                for name in seconds:
                    events[name].append((start, end, "On-Call 2nd (evening)", "2nd on-call, 5pm – 9pm"))
                continue

            m = re.match(r"\*\*On-Call\s+9\.00pm\s*[–-]\s*9\.15am\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(21, 0))
                end = datetime.combine(d + timedelta(days=1), time(9, 15))
                firsts, seconds = parse_on_call_people(m.group(1))
                for name in firsts:
                    events[name].append((start, end, "On-Call 1st (overnight)", "1st on-call, 9pm – 9.15am"))
                for name in seconds:
                    events[name].append((start, end, "On-Call 2nd (overnight)", "2nd on-call, 9pm – 9.15am"))
                continue

            m = re.match(r"\*\*On-Call\s+9\.00pm\s*[–-]\s*9\.00am\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(21, 0))
                end = datetime.combine(d + timedelta(days=1), time(9, 0))
                firsts, seconds = parse_on_call_people(m.group(1))
                for name in firsts:
                    events[name].append((start, end, "On-Call 1st (overnight)", "1st on-call, 9pm – 9am"))
                for name in seconds:
                    events[name].append((start, end, "On-Call 2nd (overnight)", "2nd on-call, 9pm – 9am"))
                continue

            m = re.match(r"\*\*On-Call\s+9\.00am\s*[–-]\s*9\.00pm\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(9, 0))
                end = datetime.combine(d, time(21, 0))
                firsts, seconds = parse_on_call_people(m.group(1))
                for name in firsts:
                    events[name].append((start, end, "On-Call 1st (day)", "1st on-call, 9am – 9pm"))
                for name in seconds:
                    events[name].append((start, end, "On-Call 2nd (day)", "2nd on-call, 9am – 9pm"))
                continue

    return events


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def vtimezone_block() -> str:
    # Minimal Europe/London VTIMEZONE covering 2026 (BST in June).
    return (
        "BEGIN:VTIMEZONE\r\n"
        "TZID:Europe/London\r\n"
        "BEGIN:STANDARD\r\n"
        "DTSTART:19711031T020000\r\n"
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10\r\n"
        "TZOFFSETFROM:+0100\r\n"
        "TZOFFSETTO:+0000\r\n"
        "TZNAME:GMT\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "DTSTART:19710328T010000\r\n"
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3\r\n"
        "TZOFFSETFROM:+0000\r\n"
        "TZOFFSETTO:+0100\r\n"
        "TZNAME:BST\r\n"
        "END:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
    )


def build_ics(name: str, events: list[tuple[datetime, datetime, str, str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//whiteboard//rota//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{name} — {calendar.month_name[MONTH]} {YEAR} rota",
        "X-WR-TIMEZONE:Europe/London",
    ]
    out = "\r\n".join(lines) + "\r\n" + vtimezone_block()
    for start, end, summary, description in sorted(events):
        # Stable UID derived from the event content.
        h = hashlib.sha1(f"{name}|{start.isoformat()}|{summary}".encode()).hexdigest()[:16]
        out += (
            "BEGIN:VEVENT\r\n"
            f"UID:{h}@whiteboard\r\n"
            f"DTSTAMP:{DTSTAMP}\r\n"
            f"DTSTART;TZID=Europe/London:{fmt_local(start)}\r\n"
            f"DTEND;TZID=Europe/London:{fmt_local(end)}\r\n"
            f"SUMMARY:{summary} — {name}\r\n"
            f"DESCRIPTION:{description}\r\n"
            "END:VEVENT\r\n"
        )
    out += "END:VCALENDAR\r\n"
    return out


def sort_key(name: str) -> tuple[int, str]:
    # Consultants ("Dr X Surname") first, then SpRs (full first+last), alphabetised by surname.
    if name.startswith("Dr "):
        surname = name.split()[-1]
        return (0, surname.lower())
    return (1, name.split()[-1].lower())


def write_calendars(events_by_name):
    OUT_DIR.mkdir(exist_ok=True)
    # Clear out any stale files from prior months/runs.
    for old in OUT_DIR.glob("*.ics"):
        old.unlink()

    written = []
    for name in sorted(events_by_name, key=sort_key):
        evs = events_by_name[name]
        if not evs:
            continue
        filename = name.replace("/", "-") + ".ics"
        (OUT_DIR / filename).write_text(build_ics(name, evs))
        written.append((name, filename))
    return written


CALENDARS_HEADER = "## Calendars"


def update_readme(written: list[tuple[str, str]]):
    text = README.read_text()
    # Drop any existing trailing "## Calendars" block before appending the fresh one.
    cut = text.rfind(f"\n{CALENDARS_HEADER}\n")
    if cut != -1:
        text = text[:cut].rstrip() + "\n"
    else:
        text = text.rstrip() + "\n"

    lines = [
        "",
        CALENDARS_HEADER,
        f"### {calendar.month_name[MONTH]} {YEAR} — per-radiologist DR & on-call shifts",
        "",
        "Right-click any name and choose *Save link as…* to download the `.ics` file, then import it into your calendar app.",
        "",
    ]
    consultants = [(n, f) for n, f in written if n.startswith("Dr ")]
    sprs = [(n, f) for n, f in written if not n.startswith("Dr ")]
    if consultants:
        lines.append("**Consultants**")
        lines.append("")
        for name, fname in consultants:
            lines.append(f"- [{name}](calendars/{fname.replace(' ', '%20')})")
        lines.append("")
    if sprs:
        lines.append("**SpRs / Registrars**")
        lines.append("")
        for name, fname in sprs:
            lines.append(f"- [{name}](calendars/{fname.replace(' ', '%20')})")
        lines.append("")

    README.write_text(text + "\n".join(lines))


def main():
    events_by_name = collect_events()
    if not events_by_name:
        print("No events parsed — check MONTH_HEADER / parsing.", file=sys.stderr)
        sys.exit(1)
    written = write_calendars(events_by_name)
    update_readme(written)
    print(f"Wrote {len(written)} calendars to {OUT_DIR}")


if __name__ == "__main__":
    main()
