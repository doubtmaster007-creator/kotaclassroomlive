from PIL import Image, ImageDraw, ImageFont
from textwrap import wrap


OUT = r"C:\Users\MAYANK\OneDrive\Desktop\New folder (4)\my_personal_mentor_flowchart.jpg"

W, H = 2600, 3400
BG = (250, 248, 242)
BOX = (255, 255, 255)
TEXT = (35, 35, 35)
LINE = (73, 97, 239)
ACCENT = (20, 20, 20)
YES = (34, 139, 34)
NO = (178, 34, 34)

img = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

font_title = ImageFont.load_default()
font = ImageFont.load_default()
font_small = ImageFont.load_default()


def box(x, y, w, h, text, fill=BOX, outline=ACCENT):
    draw.rounded_rectangle((x, y, x + w, y + h), radius=18, fill=fill, outline=outline, width=3)
    lines = []
    for para in text.split("\n"):
        lines.extend(wrap(para, width=max(14, w // 14)) or [""])
    line_h = 15
    total_h = len(lines) * line_h
    ty = y + (h - total_h) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x + (w - tw) // 2, ty), line, fill=TEXT, font=font)
        ty += line_h


def arrow(x1, y1, x2, y2, label=None, label_color=TEXT):
    draw.line((x1, y1, x2, y2), fill=LINE, width=4)
    ah = 12
    if abs(y2 - y1) >= abs(x2 - x1):
        if y2 >= y1:
            draw.polygon([(x2, y2), (x2 - ah, y2 - ah), (x2 + ah, y2 - ah)], fill=LINE)
        else:
            draw.polygon([(x2, y2), (x2 - ah, y2 + ah), (x2 + ah, y2 + ah)], fill=LINE)
    else:
        if x2 >= x1:
            draw.polygon([(x2, y2), (x2 - ah, y2 - ah), (x2 - ah, y2 + ah)], fill=LINE)
        else:
            draw.polygon([(x2, y2), (x2 + ah, y2 - ah), (x2 + ah, y2 + ah)], fill=LINE)
    if label:
        mx = (x1 + x2) // 2
        my = (y1 + y2) // 2
        draw.rectangle((mx - 34, my - 10, mx + 34, my + 10), fill=BG)
        bbox = draw.textbbox((0, 0), label, font=font_small)
        tw = bbox[2] - bbox[0]
        draw.text((mx - tw // 2, my - 6), label, fill=label_color, font=font_small)


draw.text((860, 35), "MY PERSONAL MENTOR FLOWCHART", fill=TEXT, font=font_title)

# Main vertical spine
box(980, 90, 640, 80, "User taps 'My Personal Mentor'")
box(980, 220, 640, 80, "text == 'My Personal Mentor'")
box(980, 350, 640, 90, "mentorship(update, context)")
box(980, 500, 640, 90, "student exists?")

arrow(1300, 170, 1300, 220)
arrow(1300, 300, 1300, 350)
arrow(1300, 440, 1300, 500)

# Right registration branch
box(1820, 460, 560, 90, "existing profile available?")
arrow(1620, 545, 1820, 505, "No student")
box(1820, 620, 560, 95, "mentor_confirm_existing")
arrow(2100, 550, 2100, 620, "Yes", YES)
box(1820, 790, 560, 120, "Use existing details?\nYes -> upsert_student_by_telegram(...)\nNo -> mentor_phone")
arrow(2100, 715, 2100, 790)
box(1820, 980, 560, 90, "mentor_waiting_approval")
arrow(2100, 910, 2100, 980)
box(1820, 1140, 560, 100, "faculty /accept_student\nstep = ready_for_new_doubt")
arrow(2100, 1070, 2100, 1140)
arrow(1820, 1190, 1620, 1300)

box(1820, 1320, 560, 90, "Click 'My Personal Mentor' again")
arrow(2100, 1240, 2100, 1320)

# Approved branch
box(980, 650, 640, 90, "student.is_approved?")
arrow(1300, 590, 1300, 650)
box(980, 810, 640, 90, "step = mentor_tab_selection")
arrow(1300, 740, 1300, 810, "Yes", YES)
box(980, 960, 640, 95, "Tab selected:\nBacklogs or Daily Planner")
arrow(1300, 900, 1300, 960)

# Not approved state
box(300, 650, 460, 90, "registration processing /\nwaiting approval")
arrow(980, 695, 760, 695, "No", NO)

# Backlogs left branch
box(120, 1120, 560, 90, "mentor_backlog_ready")
arrow(980, 1010, 680, 1165, "Backlogs")
box(120, 1270, 560, 100, "Yes -> mentor_backlog_share")
arrow(400, 1210, 400, 1270)
box(120, 1430, 560, 90, "mentor_backlog_hours")
arrow(400, 1370, 400, 1430)
box(120, 1580, 560, 90, "mentor_backlog_target")
arrow(400, 1520, 400, 1580)
box(120, 1730, 560, 90, "mentor_backlog_completion")
arrow(400, 1670, 400, 1730)
box(120, 1880, 560, 95, "create_backlog(...)")
arrow(400, 1820, 400, 1880)
box(120, 2040, 560, 130, "mentor_backlog_options\nAdd Next Backlogs -> mentor_backlog_ready\nGenerate AI Plan (Daily) -> mentor_planner_date\nSwitch to Daily Planner -> mentor_planner_date")
arrow(400, 1975, 400, 2040)

# Daily planner center/right branch
box(980, 1120, 640, 90, "mentor_planner_date")
arrow(1300, 1055, 1300, 1120, "Daily Planner")
box(980, 1270, 640, 90, "mentor_planner_timetable")
arrow(1300, 1210, 1300, 1270)
box(980, 1420, 640, 110, "compute_free_slots(...)\nupsert_weekly_timetable_row(...)")
arrow(1300, 1360, 1300, 1420)
box(980, 1590, 640, 115, "mentor_planner_menu\nGenerate My Daily Plan\nShow My Self-Study Planner\nSwitch to Backlogs")
arrow(1300, 1530, 1300, 1590)

box(980, 1770, 640, 90, "start_sequential_hw_flow(...)")
arrow(1300, 1705, 1300, 1770)
box(980, 1920, 640, 90, "mentor_sequential_hw")
arrow(1300, 1860, 1300, 1920)
box(980, 2070, 640, 90, "generate_ai_task_planner(...)")
arrow(1300, 2010, 1300, 2070)
box(980, 2220, 640, 100, "needs_overload_check?\nYes -> mentor_overload_confirm")
arrow(1300, 2160, 1300, 2220)
box(980, 2380, 640, 95, "create_task(...) plan saved")
arrow(1300, 2320, 1300, 2380)
box(980, 2530, 640, 90, "mentor_ready")
arrow(1300, 2475, 1300, 2530)
box(980, 2680, 640, 105, "My Mentorship menu\nShow Mentorship Progress\nStart Mentorship Flow\nHW Input\nActive Mentorship Schedule for Day")
arrow(1300, 2620, 1300, 2680)

# Bottom options
box(280, 2860, 520, 95, "Show Mentorship Progress")
box(900, 2860, 800, 120, "Start Mentorship Flow\nif timetable exists -> HW Input\nelse -> mentor_timetable_date")
box(1820, 2860, 500, 110, "HW Input\nif timetable exists and classes done\n-> start_sequential_hw_flow(...)")
box(980, 3050, 640, 90, "Active Mentorship Schedule for Day")

arrow(1240, 2785, 560, 2860)
arrow(1300, 2785, 1300, 2860)
arrow(1360, 2785, 2070, 2860)
arrow(1300, 2785, 1300, 3050)

# Linking backlog options back into planner
arrow(680, 2105, 980, 1165)

img.save(OUT, "JPEG", quality=95)
print(OUT)
