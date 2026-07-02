"""Generate per-radiologist .ics files for the current month's rota in README.md.

Parses DR AM/PM and On-Call entries under the latest "### MONTH YYYY" section
(currently JULY 2026), writes one calendar per radiologist into ./calendars,
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

MONTH_HEADER = "### JULY 2026"
YEAR = 2026
MONTH = 7

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
    "Dr Syed": "Dr F Syed", "Dr F Syed": "Dr F Syed",
    "Dr Gupta": "Dr A Gupta", "Dr A Gupta": "Dr A Gupta",
    "Dr Qaiyum": "Dr M Qaiyum", "Dr M Qaiyum": "Dr M Qaiyum",
    "Dr Rosewarne": "Dr Rosewarne", "Dr D Rosewarne": "Dr Rosewarne",
    "Dr Collins": "Dr Collins",
    "Dr Blakeman": "Dr Blakeman",
    # Peripheral CTA consultants (appear surname-only in the CTA slot)
    "Dr Nikkar": "Dr Nikkar",
    "Dr Sirakaya": "Dr Sirakaya",
    "Dr Dyer": "Dr Dyer",
    "Dr Sarang": "Dr Sarang",
    "Dr Rangarajan": "Dr Rangarajan",
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
    # Khyle Cole is a registrar (SpR), not a consultant — the DR rota spells him
    # "Khyle Cole", the on-call rota "Dr K Cole"; both map to the one SpR name.
    "Khyle Cole": "Khyle Cole", "Dr K Cole": "Khyle Cole", "Dr Cole": "Khyle Cole",
    "Mimi Li": "Mimi Li", "Dr M Li": "Mimi Li",
    "Powel Sokal": "Powel Sokal", "Dr P Sokal": "Powel Sokal",
    "Mehrab Durrani": "Mehrab Durrani", "Dr M Durrani": "Mehrab Durrani",
}

SKIP_NAMES = {"ST1", "TBC", "TBA", "Locum", "—", "-"}

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


def parse_people_labeled(segment: str) -> list[tuple[str, str | None]]:
    """Parse a DR slot, tagging duty consultants by position.

    DR AM/PM slots read 'Dr X / SpR Y / Dr Z': the first listed radiologist is
    the duty consultant DR1, the last is the duty consultant DR2, and anyone in
    between (typically the registrar/SpR) carries no DR label.
    """
    parts = segment.split("/")
    n = len(parts)
    out: list[tuple[str, str | None]] = []
    for i, part in enumerate(parts):
        c = canon(part)
        if not c:
            continue
        if i == 0:
            label = "DR1"
        elif i == n - 1 and n >= 2:
            label = "DR2"
        else:
            label = None
        out.append((c, label))
    return out


_DR_LABEL_DESC = {
    "DR1": " (Duty Consultant DR1)",
    "DR2": " (Duty Consultant DR2)",
}


def dr_slot_summary(slot: str, base_desc: str, label: str | None) -> tuple[str, str]:
    """Build the (summary, description) for a DR AM/PM event given its DR1/DR2 label."""
    if label:
        return f"{slot} ({label})", base_desc + _DR_LABEL_DESC[label]
    return slot, base_desc


def parse_cta_people(segment: str) -> list[str]:
    """Parse a Peripheral CTA slot, e.g. 'Dr Dyer (JD)' or 'Dr Rangarajan / Dr Sarang (BR / ZS)'."""
    # Drop a trailing initials parenthetical, which may itself contain a slash.
    segment = re.sub(r"\s*\([^)]*\)\s*$", "", segment).strip()
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


def parse_on_call_people(
    line: str,
) -> tuple[list[tuple[str, str | None]], list[tuple[str, str | None]]]:
    """Return (first_on_call, second_on_call) as lists of (name, group).

    `group` is "Rad A"/"Rad B" for the 2nd on-call consultants who carry an
    (A)/(B) marker (the A and B split that runs at weekends), else None.
    """
    m = _ON_CALL_RE.search(line)
    if not m:
        return [], []

    def split_second(s: str) -> list[tuple[str, str | None]]:
        # Drop any inline weekend sub-window like "**09.00am - 2.00pm**".
        s = re.sub(r"\*\*[^*]+\*\*", " ", s)
        # Split on "/" (A vs B groups) — every named consultant in the segment is on.
        people: list[tuple[str, str | None]] = []
        for chunk in re.split(r"[/]", s):
            chunk = chunk.strip()
            if not chunk:
                continue
            gm = re.search(r"\(([AB])\)", chunk)
            group = f"Rad {gm.group(1)}" if gm else None
            c = canon(chunk)
            if c:
                people.append((c, group))
        return people

    firsts = [(c, None) for c in (canon(m.group("first")),) if c]
    seconds = split_second(m.group("second"))
    return firsts, seconds


# On-call windows as they appear in the rota. (start_time, end_time, ends_next_day).
ONCALL_PATTERNS = [
    (r"\*\*On-Call\s+5\.00pm\s*[–-]\s*9\.00pm\*\*\s+(.*)", time(17, 0), time(21, 0), False),
    (r"\*\*On-Call\s+9\.00pm\s*[–-]\s*9\.15am\*\*\s+(.*)", time(21, 0), time(9, 15), True),
    (r"\*\*On-Call\s+9\.00pm\s*[–-]\s*9\.00am\*\*\s+(.*)", time(21, 0), time(9, 0), True),
    (r"\*\*On-Call\s+9\.00am\s*[–-]\s*9\.00pm\*\*\s+(.*)", time(9, 0), time(21, 0), False),
]


def parse_oncall_window(d: date, line: str):
    """Return (start, end, firsts, seconds) for one on-call line, or None."""
    for pat, t_start, t_end, next_day in ONCALL_PATTERNS:
        m = re.match(pat, line)
        if m:
            start = datetime.combine(d, t_start)
            end_day = d + timedelta(days=1) if next_day else d
            end = datetime.combine(end_day, t_end)
            firsts, seconds = parse_on_call_people(m.group(1))
            return start, end, firsts, seconds
    return None


def merge_runs(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Coalesce contiguous/overlapping (start, end) intervals into maximal blocks."""
    merged: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def emit_oncall(events, windows):
    """Emit on-call events, merging each person's adjacent windows into one block.

    The 2nd on-call (consultant) covers every window of the day with the same
    person, so this yields a single continuous block. The 1st on-call (registrar)
    changes at the 21:00 handover, so those stay as separate events.
    """
    for role_idx, ordinal in ((2, "1st"), (3, "2nd")):
        # Key on (name, group) so a Rad A and Rad B consultant don't merge together.
        per_person: dict[tuple[str, str | None], list[tuple[datetime, datetime]]] = defaultdict(list)
        for start, end, *roles in windows:
            for name, group in roles[role_idx - 2]:
                per_person[(name, group)].append((start, end))
        for (name, group), ivals in per_person.items():
            tag = f" ({group})" if group else ""
            for s, e in merge_runs(ivals):
                desc = f"{ordinal} on-call{(' ' + group) if group else ''}, {s:%H:%M} – {e:%H:%M}"
                events[name].append((s, e, f"On-Call {ordinal}{tag}", desc))


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
        oncall_windows = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue

            # DR AM slot
            m = re.match(r"\*\*DR AM\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(9, 0))
                end = datetime.combine(d, time(13, 0))
                for name, label in parse_people_labeled(m.group(1)):
                    summary, desc = dr_slot_summary("DR AM", "Diagnostic radiology — morning", label)
                    events[name].append((start, end, summary, desc))
                continue

            m = re.match(r"\*\*DR PM\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(13, 0))
                end = datetime.combine(d, time(17, 0))
                for name, label in parse_people_labeled(m.group(1)):
                    summary, desc = dr_slot_summary("DR PM", "Diagnostic radiology — afternoon", label)
                    events[name].append((start, end, summary, desc))
                continue

            # Peripheral CTA slot (no rota time given — treated as a full working day).
            m = re.match(r"\*\*Peripheral CTA\*\*\s+(.*)", line)
            if m:
                start = datetime.combine(d, time(9, 0))
                end = datetime.combine(d, time(17, 0))
                for name in parse_cta_people(m.group(1)):
                    events[name].append(
                        (start, end, "Peripheral CTA", "Peripheral CT angiography duty")
                    )
                continue

            # On-Call windows — collected per day, then merged per person below.
            w = parse_oncall_window(d, line)
            if w:
                oncall_windows.append(w)
                continue

        emit_oncall(events, oncall_windows)

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
            lines.append(f"- [{name}.ics](calendars/{fname.replace(' ', '%20')})")
        lines.append("")
    if sprs:
        lines.append("**SpRs / Registrars**")
        lines.append("")
        for name, fname in sprs:
            lines.append(f"- [{name}.ics](calendars/{fname.replace(' ', '%20')})")
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
