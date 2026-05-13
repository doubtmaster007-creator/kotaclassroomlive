from datetime import datetime

def parse_time_hhmm(value):
    value = value.strip().lower().replace(".", ":")
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%H", "%I %p", "%I%p"):
        try:
            return datetime.strptime(value, fmt).time()
        except:
            continue
    return None

print(f"8:50 -> {parse_time_hhmm('8:50')}")
print(f"10:25 -> {parse_time_hhmm('10:25')}")
print(f"12:00 -> {parse_time_hhmm('12:00')}")
