import re
from datetime import datetime, timedelta
from typing import List, Dict, Any

def parse_time_hhmm(t_str: str):
    t_str = t_str.lower().strip()
    # Simple hh:mm or h:mm
    m = re.match(r"^(\d{1,2}):(\d{2})$", t_str)
    if m:
        return datetime.strptime(f"{int(m.group(1)):02d}:{m.group(2)}", "%H:%M").time()
    # Just hour
    m = re.match(r"^(\d{1,2})$", t_str)
    if m:
        h = int(m.group(1))
        if h < 24:
            return datetime.strptime(f"{h:02d}:00", "%H:%M").time()
    # AM/PM
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", t_str)
    if m:
        h = int(m.group(1))
        mm = m.group(2) or "00"
        ampm = m.group(3)
        if ampm == "pm" and h < 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return datetime.strptime(f"{h:02d}:{mm}", "%H:%M").time()
    return None

def parse_slot_text(text: str) -> List[Dict[str, Any]]:
    slots: List[Dict[str, Any]] = []
    parts = re.split(r"[;\n,]+", text or "")
    for part in parts:
        raw = part.strip()
        if not raw: continue
        print(f"Parsing: '{raw}'")
        # Regex jo "Monday time chemistry 9 am" jaise natural sentences samajhta hai
        m = re.search(
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*(?:time|at)?\s*"
            r"([a-zA-Z\s]+)\s+" 
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)" 
            r"(?:\s*[-to]+\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?", 
            raw, flags=re.I
        )
        if m:
            print(f"Matched: groups={m.groups()}")
            subject, start_str, end_str = m.group(1).strip(), m.group(2).strip(), m.group(3)
            start = parse_time_hhmm(start_str)
            if end_str:
                end = parse_time_hhmm(end_str.strip())
            elif start:
                end = (datetime.combine(datetime.today(), start) + timedelta(minutes=90)).time()
            else: continue
            if start and end:
                slots.append({"subject": subject.title(), "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")})
        else:
            print("No match")
    return slots

test_input = "Physics 9 am , chemistry 10:30 am , mathematics 12 pm"
results = parse_slot_text(test_input)
print(f"Results: {results}")
