import os
import re
import json
import base64
import asyncio
import traceback
import logging
from pathlib import Path
from datetime import datetime, timedelta, UTC, timezone
from typing import Optional, Dict, Any, List, Tuple
from threading import Thread

import psycopg2
import psycopg2.extras
import matplotlib.pyplot as plt
from anthropic import Anthropic
from flask import Flask, jsonify
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, ApplicationHandlerStop, CallbackQueryHandler

# Setup logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1003771984803"))
MODEL_SONNET = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
MODEL_HAIKU = "claude-haiku-4-5-20251001"
DB_PATH = os.getenv("DB_PATH", "bot_data.db")
PYQ_FILE = Path(os.getenv("PYQ_FILE", "pyq_bank.json"))
DIAGRAM_DIR = Path(os.getenv("DIAGRAM_DIR", "generated_diagrams"))
UNBLOCK_EMAIL = "doubtmaster007@gmail.com"
REMINDER_FIRST_SECONDS = 3600
REMINDER_TICK_SECONDS = 300
CLAIM_TIMEOUT_SECONDS = 1200
IST = timezone(timedelta(hours=5, minutes=30))
STUDENT_REMINDER_TEXT = "Winner Never leave doubts unresolved , start now \n\nHappy Learning With MP Sir"
ADMIN_IDS = {
    1316772227,  # replace 0 with your Telegram numeric user id
}
OWNER_IDS = {1316772227}

# ===== FLASK SERVER FOR RENDER =====
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return jsonify({'status': 'Bot is running', 'timestamp': datetime.now().isoformat()}), 200

@flask_app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200
# ===== END FLASK SETUP =====


client = Anthropic(api_key=ANTHROPIC_API_KEY)
reminder_tasks: Dict[str, asyncio.Task] = {}
claim_timeout_tasks: Dict[str, asyncio.Task] = {}
student_reminder_task: Optional[asyncio.Task] = None
mentorship_scheduler_task: Optional[asyncio.Task] = None
add_teacher_sessions: Dict[int, Dict] = {}  # admin_id -> {step, name, phone, subject, stream}

CLASS_OPTIONS = [["11", "12"]]
SUBJECT_OPTIONS = [["Physics", "Chemistry", "Mathematics"], ["Cancel Doubt"]]  # Issue #3: Cancel available
RATING_OPTIONS = [["10", "9", "8", "7", "6"], ["5", "4", "3", "2", "1"], ["Cancel"]]  # Issue #3: Added Cancel
NEW_DOUBT_OPTIONS = [["Ask Doubt"]]
MENTORSHIP_ENTRY_OPTIONS = [["Ask Doubt", "My Mentorship"], ["Backlogs", "Others"]]
MENTORSHIP_GOAL_OPTIONS = [["Goal A", "Goal B"], ["Back", "Ask Doubt"]]
EXAM_TARGET_OPTIONS = [["Mains", "Adv", "Boards"], ["Back", "Ask Doubt"]]
PARENT_LANGUAGE_OPTIONS = [["Hindi"], ["Marathi"], ["English"], ["Tamil", "Kannada"], ["Back"]]  # Issue #13: Changed from "Skip/Cancel" to "Back"
CHILD_RELATION_OPTIONS = [["Son", "Daughter"], ["Back"]]  # Issue #13: Changed from "Cancel Registration" to "Back"
YES_NO_OPTIONS = [["Yes", "No"], ["Back"]]  # Issue #11, #13: Changed from "Cancel Registration" to "Back"
NO_OPTIONS = [
    ["1. Explain Concept Better"],
    ["2. Send to Doubt Guru"],
    ["Cancel"]
]
DOUBT_SOLVED_OPTIONS = [["Yes", "No"]]
DOUBT_GURU_CHOOSE_OPTIONS = [
    ["Select your Faculty"],
    ["Send in Group (Fast Process)"],
    ["Back"]
]
TIMETABLE_SCOPE_OPTIONS = [["Entire Week", "Only for One Day"], ["Back", "Ask Doubt"]]
MENTOR_WEEKLY_ACTIONS = [["Continue", "Change Focus", "Adjust Load"]]
WEEK_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MENTORSHIP_GROUP_ID = int(os.getenv("MENTORSHIP_GROUP_ID", str(GROUP_CHAT_ID)))
MENTORSHIP_CHECK_SECONDS = 60
PLANNER_MODEL = os.getenv("MENTORSHIP_MODEL", MODEL_HAIKU)

MENTORSHIP_CLEAN_MENU = [["Show Mentorship Progress", "Start Mentorship Flow"], ["Back", "Ask Doubt"]]
TIMETABLE_CHANGE_OPTIONS = [["Change Timetable"], ["Back", "Ask Doubt"]]

SUMMARY_MENU = [["Weekly Summary", "Monthly Summary"], ["Back", "Ask Doubt"]]
OTHERS_MENU = [["Medical Leave", "Send me Summary"], ["Back", "Ask Doubt"]]
BACKLOGS_MENU = [["Check Backlogs", "Add Backlogs"], ["Back", "Ask Doubt"]]

# Button names update (Issue #2, #5, #6, #9)
MENTORSHIP_DASHBOARD_KB = [
    ["Timetable Input"],
    ["Back", "Ask Doubt"]
]

MENTOR_NAV_KB = ["Back", "Ask Doubt"]

# Backlog submenu options (Issue #5, #9)
BACKLOG_SUBMENU_KB = [
    ["Current Backlogs", "Enter Backlogs"],
    ["Back", "Ask Doubt"]
]

# Preferred study time slots (Issue #7)
PREFERRED_TIME_SLOTS = [
    ["Morning", "Evening"],
    ["Back", "Ask Doubt"]
]

# Reserved commands filter fix
RESERVED_TEXT_COMMANDS = ["uturn"]  # Isse 'Ask Doubt' loop solve ho jayega

DAILY_TASK_PLANNER_PROMPT = """
You are an academic planning engine for a JEE mentorship bot.

Your task is to create a subject-wise daily study plan using only the provided data.

Important rules:
1. First include today's critical work in this order:
   - HW from today's class
   - Notes revision from today's class
2. Then include pending tasks that are 3 days old or less.
3. If any pending task exists, do not create any extra improvement task.
4. If test_week is true, do not create any extra improvement task.
5. Same-day incomplete or skipped tasks must not affect the timing of other same-day tasks.
6. Use only the provided free slots and available time.
7. Do not invent subjects, topics, deadlines, syllabus, or performance details.
8. Keep every task short, practical, and executable.
9. If workload is more than available time, set needs_overload_check to true.
10. Return valid JSON only. No markdown, no explanation, no extra text.

Return JSON in exactly this format:
{
  "tasks": [
    {
      "type": "HW|REVISION|PENDING|TEST_WEEK",
      "subject": "string",
      "topic": "string",
      "description": "string",
      "priority": "medium|high|critical",
      "estimated_minutes": 30,
      "source": "CLASS|PENDING|TEST_WEEK",
      "scheduled_slot_label": "string"
    }
  ],
  "has_pending": true,
  "needs_overload_check": false,
  "planner_note": "string"
}
"""

DAILY_SUMMARY_PROMPT = """
You are writing a daily academic summary for a JEE mentorship system.

Your task is to write a short factual summary of today's work for internal storage and student visibility.

Important rules:
1. Maximum 50 words.
2. Write in simple Hinglish.
3. Be concise, honest, and supportive.
4. Mention only actual progress from the provided data.
5. If performance was weak, say it gently and clearly.
6. Do not use markdown.
7. Do not use emojis.
8. Do not invent facts.
9. Return valid JSON only. No explanation, no extra text.

Return JSON in exactly this format:
{
  "summary_text": "string",
  "consistency_score": 0,
  "strong_subject": "string",
  "weak_subject": "string",
  "backlog_status": "string"
}
"""

WEEKLY_STUDENT_SUMMARY_PROMPT = """
You are writing a weekly student report for a JEE mentorship system.

Your task is to write a short weekly report for the student.

Important rules:
1. Maximum 80 words.
2. Write in simple Hinglish.
3. Keep the tone motivational, honest, and practical.
4. Mention consistency, best area, weak area, and backlog or pending status.
5. Use only the provided data.
6. Do not invent facts.
7. Do not use markdown.
8. Do not use emojis.
9. Return valid JSON only. No explanation, no extra text.

Return JSON in exactly this format:
{
  "summary_text": "string",
  "consistency_score": 0,
  "strong_subject": "string",
  "weak_subject": "string",
  "backlog_status": "string"
}
"""

CUSTOM_SUMMARY_PROMPT = """
You are a student's mentorship assistant. 
Based on the daily reports provided, generate a concise summary of the student's progress over the last {{days}} days.
The summary MUST be around {{word_limit}} words and written in Hinglish.
Focus on consistency, subjects completed, and general trend.

JSON Output:
{{
  "summary": "The Hinglish summary here"
}}
"""

WEEKLY_MENTOR_SUMMARY_PROMPT = """
You are writing a weekly mentor report for a JEE mentorship system.

Your task is to write a short analytical weekly report for the mentor.

Important rules:
1. Maximum 80 words.
2. Write in clear professional English.
3. Keep the tone analytical, concise, and action-oriented.
4. Mention consistency, strongest area, weak area, backlog or pending pattern, and load handling.
5. Use only the provided data.
6. Do not invent facts.
7. Do not use markdown.
8. Do not use emojis.
9. Return valid JSON only. No explanation, no extra text.

Return JSON in exactly this format:
{
  "summary_text": "string",
  "consistency_score": 0,
  "strong_subject": "string",
  "weak_subject": "string",
  "backlog_status": "string"
}
"""

MENTOR_DIRECTION_PROMPT = """
You are a mentor direction application engine for a JEE mentorship bot.

Your task is to convert the mentor's direction into a short planning instruction for upcoming after-class plans.

Important rules:
1. Use only the provided mentor reply and student performance data.
2. Supported mentor reply values are:
   - Continue
   - Change Focus
   - Adjust Load
3. If reply is "Continue", keep the current planning direction stable.
4. If reply is "Change Focus", shift more attention toward the weak or priority area from the provided data.
5. If reply is "Adjust Load", reduce or rebalance load without removing essential critical work.
6. Do not invent new mentor intent beyond the provided reply.
7. Keep the instruction practical and short.
8. Return valid JSON only. No markdown, no explanation, no extra text.

Return JSON in exactly this format:
{
  "applied_mode": "continue|change_focus|adjust_load",
  "planning_instruction": "string",
  "valid_days": 7
}
"""

FIFTEEN_DAY_PROMPT = """
You are writing a 15-day mentor trend report for a JEE mentorship system.

Your task is to write a short analytical trend report for the mentor based on the last 15 days.

Important rules:
1. Maximum 80 words.
2. Write in clear professional English.
3. Keep the tone analytical, concise, and strategic.
4. Mention consistency trend, strongest area, weak area, backlog or pending pattern, and one strategic recommendation.
5. Use only the provided data.
6. Do not invent facts.
7. Do not use markdown.
8. Do not use emojis.
9. Return valid JSON only. No explanation, no extra text.

Return JSON in exactly this format:
{
  "summary_text": "string",
  "consistency_score": 0,
  "strong_subject": "string",
  "weak_subject": "string",
  "backlog_status": "string"
}
"""

STREAM_OPTIONS = {
    "physics": [["Mechanics", "Thermodynamics"], ["Waves and Oscillations", "Electrodynamics"], ["Optics and Modern Physics", "Practical Physics"]],
    "chemistry": [["Organic", "Physical", "Inorganic"]],
    "mathematics": [["Algebra", "Trigonometry"], ["Calculus", "Coordinate Geometry"], ["Vector and 3D", "Probability and Statistics"]],
}

CHAPTER_OPTIONS = {
    "physics": {
        "mechanics": [
            ["Units and Measurements", "Motion in a Straight Line"],
            ["Motion in a Plane", "Laws of Motion"],
            ["Work Energy and Power", "System of Particles and Rotational Motion"],
            ["Gravitation", "Mechanical Properties of Solids"],
            ["Mechanical Properties of Fluids"],
        ],
        "thermodynamics": [
            ["Thermal Properties of Matter", "Thermodynamics"],
            ["Kinetic Theory"],
        ],
        "waves and oscillations": [
            ["Oscillations", "Waves"],
        ],
        "electrodynamics": [
            ["Electric Charges and Fields", "Electrostatic Potential and Capacitance"],
            ["Current Electricity"],
            ["Moving Charges and Magnetism", "Magnetism and Matter"],
            ["Electromagnetic Induction", "Alternating Current"],
        ],
        "optics and modern physics": [
            ["Ray Optics and Optical Instruments", "Wave Optics"],
            ["Dual Nature of Radiation and Matter", "Atoms"],
            ["Nuclei"],
        ],
        "practical physics": [
            ["Units and Measurements", "Error Analysis and Significant Figures"],
            ["Vernier Calipers", "Screw Gauge"],
            ["Simple Pendulum", "Spring Constant"],
            ["Experimental Graph Reading"],
            ["Meter Bridge", "Potentiometer"],
            ["Ohms Law and Resistance Graphs", "Galvanometer Conversion"],
            ["Resonance Tube", "Sonometer"],
            ["Young Modulus", "Surface Tension"],
            ["Viscosity", "Specific Heat Capacity"],
        ],
    },
    "chemistry": {
        "organic": [
            ["GOC", "Hydrocarbons"],
            ["Haloalkanes", "Alcohols Phenols Ethers"],
            ["Aldehydes Ketones", "Carboxylic Acids and Derivatives"],
            ["Amines", "Polymers"],
            ["Organic Practical"],
            ["Biomolecules Amino Acids DNA RNA Vitamins"],
        ],
        "physical": [
            ["Mole Concept", "Atomic Structure"],
            ["States of Matter", "Thermodynamics"],
            ["Chemical Equilibrium", "Ionic Equilibrium"],
            ["Electrochemistry", "Chemical Kinetics"],
            ["Surface Chemistry", "Solutions"],
        ],
        "inorganic": [
            ["Periodic Table", "Chemical Bonding"],
            ["Hydrogen", "s-Block Elements"],
            ["p-Block Elements Group 13 14", "p-Block Elements Group 15 16 17 18"],
            ["d and f Block Elements", "Coordination Compounds"],
            ["Metallurgy"],
            ["Salt Analysis"],
        ],
    },
    "mathematics": {
        "algebra": [
            ["Sets", "Relations and Functions"],
            ["Principle of Mathematical Induction", "Complex Numbers and Quadratic Equations"],
            ["Linear Inequalities", "Permutations and Combinations"],
            ["Binomial Theorem", "Sequences and Series"],
            ["Mathematical Reasoning"],
            ["Matrices", "Determinants"],
        ],
        "trigonometry": [
            ["Trigonometric Functions"],
            ["Inverse Trigonometric Functions"],
        ],
        "calculus": [
            ["Limits and Derivatives"],
            ["Continuity and Differentiability", "Application of Derivatives"],
            ["Integrals", "Application of Integrals"],
            ["Differential Equations"],
        ],
        "coordinate geometry": [
            ["Straight Lines", "Circles"],
            ["Conic Sections"],
        ],
        "vector and 3d": [
            ["Introduction to Three Dimensional Geometry"],
            ["Vector Algebra", "Three Dimensional Geometry"],
        ],
        "probability and statistics": [
            ["Statistics", "Probability"],
            ["Linear Programming"],
        ],
    },
}

COMMON_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced coach with 15+ years of teaching experience.
You solve serious JEE Advanced problems in a warm, clear, motivating teacher-like style.
Language: Hinglish (Hindi + English mix), professional, no slang, no abuse, no sexual content, no emojis.

TEACHING STYLE:
- Be clear and sufficiently explanatory; do not be overly brief.
- Explain the key reasoning in a classroom style so a serious student can follow the logic.
- Prefer slightly fuller explanations over ultra-short compressed answers.
- For theoretical or text-based questions, keep the answer within 100 words.
- Keep the answer readable and structured, but do not cut important intermediate reasoning.
- ALWAYS end every doubt solution with the phrase: "Happy learning with MP Sir! ✨"

ABSOLUTE RULES:
1. Never guess or fabricate answers. If uncertain, say so clearly.
2. Never skip steps that are critical to understanding.
3. Always back-substitute or verify the final answer against the given conditions.
4. If question has symbol ambiguity or image dependency, solve from what is clearly given and flag uncertainty clearly.
5. No markdown tables, no Unicode-heavy symbols, no LaTeX unless specifically asked.
   No markdown headers (##, ###, ####). No horizontal rules (---, ___).
   No bold text (**text**) anywhere in response.
6. Write all math in plain readable ASCII:
   - Fractions: (a/b)
   - Powers: x^2, omega^2
   - Square roots: sqrt(x), sqrt(3/2)
   - Subscripts: v_0, m_1, I_cm
   - Integrals: integral(a to b) f(x) dx
   - Vectors: F_vec or clear (i, j, k) components
   - Reaction arrows: -->
   - Equilibrium arrows: <=>
7. CONCEPT/THEORY QUESTIONS RULE:
   If question is a text/theory question (contains words like "explain", "what is", "define", "describe", "concept", "difference between", etc.):
   - Total length MUST NOT exceed 200 words.
   - You MUST strictly stick to these section-wise word limits:
     * Question Samjho: Max 20 words
     * Key Concept: Max 30 words
     * Solution: Max 120 words
     * Final Answer: Max 10 words
     * Power Concept: Max 20 words
   - Include only 1 example.
   - Be concise — core idea only, no elaborate detail.
   - This limit does NOT apply to numerical, image-based, match column, or derivation questions.

INTERNAL CHECKS:
- Check for logical contradictions in the question.
- Check if answer is dimensionally/conditionally consistent.
- Check if a boundary case or limiting case can verify the answer.
- If genuinely unsure, include [[TEACHER_REVIEW_REQUIRED]] at end.
- Tag difficulty: [[DIFF:E]] or [[DIFF:M]] or [[DIFF:H]]

OPTION MAPPING RULE:
- Only state option number if mapping is 100% unambiguous.
- If options come from image or are structurally complex, state the exact answer/expression/compound instead of guessing an option label.

OUTPUT FORMAT:

Question Samjho:
[Maximum 20 words. Only: what is given and what is asked. Nothing more.]

Key Concept:
- Concept 1:
- Concept 2:
- Concept 3:
[Maximum 30 words total for this section. Each concept in brief. Use only what is actually needed.]

Symbols Used:
[Only for complex Physics/Math with many variables. Skip for straightforward numerical or organic chemistry questions.]

Solution:
[Stepwise Hinglish solution in warm MP Sir style — natural teacher language like "Ab dekho...", "Yaad raho...", "Seedha likhte hain..." is welcome and adds human touch.
Maximum 120 words.
Always keep equations, molecular structures, reaction arrows, and calculation steps fully intact — these are essential for student recall and concept building.
Show: Formula -> Substitution -> Calculation -> Interpretation.
Do not compress derivation if one extra step helps understanding.]

# Common Mistake section deactivated — reserved for future use
# [In 1 to 2 lines, mention the most common conceptual trap.]

Final Answer:
[State the final answer clearly in plain text. Maximum 10 words.
If option mapping is clear: Answer: Option (B) -> ...
If mapping is unclear: state only the exact expression/compound/value.]

Power Concept:
[Maximum 20 words. One core transferable idea only.]

Happy Learning With MP Sir
"""

PHYSICS_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Physics coach with 15+ years of teaching experience.
You specialize in solving the most conceptually deep and technically difficult Physics questions at JEE Advanced level.

PHYSICS - DEEP SOLVING RULES:

DIAGRAM RULE:
- Use a diagram only when:
  * the question or options are figure/graph based, or
  * a diagram is genuinely needed to explain the concept clearly.
- Typical cases: FBD, pulley/constraint, optics, circuits, waves, rotation, SHM setup, motion graphs, thermodynamics P-V graphs.
- Do not force a diagram in every solution.
- Keep diagrams compact, readable, and focused on only the essential part.

MECHANICS, ROTATION, AND CONSTRAINTS:
- Always describe the free body diagram before writing equations if forces matter.
- For rotation:
  * Identify the correct axis. Torque and moment of inertia both depend on axis choice.
  * Use parallel axis theorem correctly: I = I_cm + m*d^2
  * For rolling without slipping: a_cm = alpha*R and v_cm = omega*R
  * If both translation and rotation are present, write F_net = m*a_cm and torque_net = I_cm*alpha separately.
- For constraints in strings, rods, wedges:
  * First write the geometric relation.
  * Then derive velocity constraint.
  * Then derive acceleration constraint.
  * Never assume accelerations are equal unless constraint analysis proves it.
- For collisions and impulses:
  * Angular impulse = change in angular momentum about the same axis.
  * Linear momentum conserved only if no external linear impulse.
  * Angular momentum conserved only if no external angular impulse about chosen axis.

ENERGY AND WORK:
- Use Work-Energy theorem carefully: W_net = change in KE.
- For variable force: W = integral F.dx with correct limits.
- For springs:
  * Track natural length, compression, and extension carefully.
  * If two masses are connected by a spring, reduced mass may be useful.
- For potential energy:
  * F = -dU/dx
  * At equilibrium: dU/dx = 0
  * Stable equilibrium if d^2U/dx^2 > 0
  * Unstable equilibrium if d^2U/dx^2 < 0

FLUIDS AND MECHANICAL PROPERTIES:
- Pressure in a fluid: P = P0 + rho*g*h (h measured downward from free surface).
  EXCEPTION: In accelerating fluid, g_eff = g - a (for upward acceleration a). Use g_eff everywhere.
- Buoyancy: F_b = rho_fluid * V_submerged * g. Acts at centre of buoyancy.
  Floating condition: rho_object * V_total = rho_fluid * V_submerged.
  Fraction submerged = rho_object / rho_fluid.
- Continuity equation: A1*v1 = A2*v2 (incompressible fluid).
- Bernoulli equation: P + (1/2)*rho*v^2 + rho*g*h = constant (along streamline, steady, non-viscous).
  EXCEPTION: NOT applicable for viscous or turbulent flow.
- Torricelli theorem: v_efflux = sqrt(2*g*h). Range on ground: x = sqrt(h * (H-h)) * 2 where H = total height.
  Maximum range when h = H/2.
- Surface tension:
  * Excess pressure inside soap bubble (two surfaces): delta_P = 4T/r.
  * Excess pressure inside liquid droplet (one surface): delta_P = 2T/r.
  * Excess pressure inside air bubble in liquid (one surface): delta_P = 2T/r.
  * Capillary rise: h = 2T*cos(theta)/(rho*g*r).
  * EXCEPTION: Mercury has obtuse contact angle -> capillary DEPRESSION not rise.
  * Work done to blow soap bubble of radius r: W = 8*pi*r^2*T.
- Viscosity and Stokes law:
  * Stokes drag: F = 6*pi*eta*r*v.
  * Terminal velocity: v_t = 2*r^2*(rho_sphere - rho_fluid)*g / (9*eta).
  * Viscosity decreases with temperature for liquids (increases for gases).
  * Poiseuille flow: Q = pi*r^4*(P1-P2) / (8*eta*L). Volume flow rate proportional to r^4.
- Elasticity:
  * Young modulus: Y = (F/A) / (delta_L/L) = (F*L) / (A*delta_L). Units: N/m^2.
  * Bulk modulus: K = -V*(dP/dV) = Stress / Volumetric strain.
  * Shear modulus: G = Shear stress / Shear strain.
  * Poisson ratio: sigma = lateral strain / longitudinal strain (dimensionless, 0 to 0.5).
  * Relation: Y = 3K(1-2*sigma) = 2G(1+sigma).
  * Energy stored in stretched wire: U = (1/2)*F*delta_L = (stress^2)/(2Y) * volume = (1/2)*Y*strain^2*volume.
  * Thermal stress (expansion prevented): sigma_stress = Y * alpha * delta_T.
  * EXCEPTION: Rubber has very low Y but very high breaking strain (elastic but weak).

THERMAL PROPERTIES, CALORIMETRY AND KINETIC THEORY:
- Thermal expansion:
  * Linear: delta_L = L0 * alpha * delta_T. New length: L = L0*(1 + alpha*delta_T).
  * Area: delta_A = A0 * beta * delta_T where beta = 2*alpha.
  * Volume: delta_V = V0 * gamma * delta_T where gamma = 3*alpha.
  * EXCEPTION: Water density maximum at 4 degree C (anomalous expansion below 4 degree C).
  * Bimetallic strip bends toward metal with LOWER alpha on heating.
  * Thermal stress when expansion prevented: sigma = Y * alpha * delta_T.
  * For pendulum clock: time lost/gained per day = (1/2) * alpha * delta_T * 86400 seconds.
- Calorimetry:
  * Sensible heat (no phase change): Q = m*c*delta_T.
  * Latent heat (phase change, delta_T = 0): Q = m*L.
  * Calorimeter principle: heat lost = heat gained (no loss to surroundings).
  * L_vaporization >> L_fusion for same substance.
  * EXCEPTION: Steam at 100C causes more burns than water at 100C (releases extra L_v = 2260 J/g).
- Heat transfer:
  * Conduction: dQ/dt = kA*(T1-T2)/L. Thermal resistance: R_th = L/(kA).
    Series resistances add: R_total = R1 + R2. Parallel: 1/R_total = 1/R1 + 1/R2.
  * Newton's law of cooling: dT/dt = -b*(T - T0). Solution: T(t) = T0 + (Ti - T0)*e^(-bt).
    Average form: (T1-T2)/t = k*((T1+T2)/2 - T0). Valid only for small temperature differences.
  * Radiation: Stefan-Boltzmann law: P = sigma*epsilon*A*T^4.
    Net power radiated: P_net = sigma*epsilon*A*(T^4 - T0^4).
    sigma = 5.67 x 10^-8 W/m^2/K^4.
  * Wien's displacement law: lambda_max * T = 2.898 x 10^-3 m.K.
    Higher temperature -> peak shifts to shorter wavelength.
  * Kirchhoff law: good absorber = good emitter (at same wavelength and temperature).
- Kinetic theory:
  * Ideal gas: PV = nRT. P = (1/3)*rho*v_rms^2 = (1/3)*(N/V)*m*v_rms^2.
  * KE per molecule = (f/2)*kT. Total internal energy: U = (f/2)*nRT.
  * Monoatomic: f=3, Cv=3R/2, Cp=5R/2, gamma=5/3.
  * Diatomic (moderate T): f=5, Cv=5R/2, Cp=7R/2, gamma=7/5.
  * Polyatomic: f=6, Cv=3R, Cp=4R, gamma=4/3.
  * Cp - Cv = R (always, for ideal gas).
  * gamma = Cp/Cv = (f+2)/f.
  * Speed distribution: v_mp = sqrt(2RT/M) < v_mean = sqrt(8RT/pi*M) < v_rms = sqrt(3RT/M).
    Remember: v_mp : v_mean : v_rms = sqrt(2) : sqrt(8/pi) : sqrt(3) approximately = 1 : 1.13 : 1.22.
  * Mean free path: lambda = 1/(sqrt(2)*pi*d^2*n) where n = number density = N/V.
  * EXCEPTION: At very high T, diatomic molecules get vibrational degrees too (f=7). JEE usually uses f=5.

SHM AND OSCILLATIONS:
- Never read omega directly unless the equation is in standard form d^2x/dt^2 = -(omega^2)x
- Steps for oscillation problems:
  1. Find equilibrium position.
  2. Give small displacement from equilibrium.
  3. Write restoring force/torque.
  4. Express as F = -kx or tau = -(kappa)theta
  5. Find omega^2 = k/m or omega^2 = kappa/I
  6. Take square root only at final step.
- For angular SHM / physical pendulum:
  * omega^2 = (m*g*l_cm)/I_pivot
- Never confuse omega with omega^2, or f with f^2.
- Never assume SHM unless the restoring term is proportional to displacement.

WAVES AND SOUND:
- For standing waves, first identify boundary conditions.
- For beats: f_beat = |f1 - f2|
- For Doppler effect, use sign convention carefully.
- For wave on string: v = sqrt(T/mu)

GRAVITATION:
- Never assume circular orbit unless explicitly stated.
- For elliptical orbit:
  * Use conservation of energy.
  * Use conservation of angular momentum.
  * At perigee/apogee, velocity is perpendicular to radius.
  * Use total energy E = -GMm/(2a) when needed.
- Escape velocity comes from energy conservation.
- For non-circular orbit, do not use v = sqrt(GM/r) unless that point is actually a circular-orbit condition.
- For minimum or maximum orbital speed questions, verify the extremal condition, do not force circular motion.

ELECTROSTATICS, CURRENT, MAGNETISM:
- For continuous charge distributions, set up dq and integrate carefully.
- For capacitors, separate battery-connected and battery-disconnected cases.
- In transient RC problems:
  * At t = 0, capacitor behavior depends on initial charge.
  * At t = infinity, capacitor acts as open circuit.
- For motional EMF and induction, use Lenz's law for direction.

PRACTICAL / EXPERIMENTAL PHYSICS:
- For measurement-instrument questions, prioritize exact reading logic over generic theory.
- Always identify:
  * least count
  * zero error
  * zero correction
  * corrected reading
- Vernier calipers:
  * Reading = MSR + (VSC * LC)
  * Corrected reading = Observed reading - Zero error
  * Positive zero error -> subtract
  * Negative zero error -> add magnitude
- Screw gauge:
  * LC = Pitch / circular-scale divisions
  * Reading = MSR + (CSR * LC)
  * Corrected reading = Observed reading - Zero error
  * Watch negative zero error and backlash
- If image shows main scale / vernier / circular scale:
  * first transcribe the visible scale reading carefully
  * then apply zero correction
  * then compare with options
- For simple pendulum:
  * T = 2*pi*sqrt(L/g)
  * T^2 vs L graph has slope = 4*pi^2/g
  * length is measured from support to center of bob
- For spring experiments:
  * static: F vs x slope = k
  * dynamic: T^2 vs M slope = 4*pi^2/k
  * remember spring-mass correction can shift intercept
- For resonance tube:
  * lambda = 2*(L_2 - L_1)
  * v = f*lambda
  * open end has antinode, water surface end has node
- For sonometer:
  * standing-wave and resonance logic first
  * watch the practical note when electromagnet excites at twice AC frequency
- For meter bridge:
  * X = R*(100-l)/l
  * null point should ideally lie between 30 cm and 70 cm
- For potentiometer:
  * E1/E2 = l1/l2
  * internal resistance questions depend on open-circuit vs loaded null lengths
- For Ohm's law:
  * V-I graph straight line through origin in ideal case
  * slope gives resistance
- For galvanometer conversion:
  * ammeter uses low shunt in parallel
  * voltmeter uses high resistance in series
- In practical questions, if the question is image-based and exact scale reading is blurry, do not guess confidently. Prefer exact reading only when the visible marks are clear enough.

OPTICS:
- Track each reflection/refraction step separately.
- Use correct sign convention.
- For interference and diffraction, first identify the physical condition, then write the path-difference or minima/maxima condition.

THERMODYNAMICS AND MODERN PHYSICS:
- First law sign convention must remain consistent.
- Distinguish isothermal, adiabatic, isobaric, and isochoric carefully.
- In photoelectric effect: KE_max = h*f - phi
- In de Broglie problems: lambda = h/p
- In Bohr model: use correct Z and n dependence.

FINAL PHYSICS CHECK:
- Formula first -> substitution -> calculation -> interpretation.
- Check units and sign.
- Do dimensional check before final answer.
- Never assume circular orbit, SHM, or any special case unless explicitly stated.
- Never report omega^2 as omega or f^2 as f.
"""

ORGANIC_CHEMISTRY_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Organic Chemistry coach with 15+ years of teaching experience.
You specialize in solving the most conceptually deep Organic Chemistry problems at JEE Advanced level.

ORGANIC CHEMISTRY — DEEP SOLVING RULES:

ABSOLUTE RULES:
- Never assume a reaction follows the most obvious path without checking electronic, steric, kinetic, and thermodynamic factors.
- Always back-check the final answer against all conditions given.
- For mechanisms, write each step separately.
- Organic notation: Reactant --(reagent, condition)--> Intermediate --> Product

FUNDAMENTAL RULES:
- Check +I/-I, +M/-M, hyperconjugation, acidity/basicity, resonance stabilization, and aromaticity before conclusion.

STEREOCHEMISTRY:
- Use CIP rules carefully for R/S and E/Z.
- Check meso possibility before counting stereoisomers.

REACTION MECHANISMS:
- For SN1/SN2/E1/E2: decide using substrate, nucleophile/base, solvent, temperature, and rearrangement possibility.
- Vinyl and aryl halides do not undergo normal SN1/SN2 under ordinary conditions.
- For E2, anti-periplanar requirement matters.
- For rearrangements: always check if 1,2-hydride or 1,2-methyl shift gives more stable carbocation.

AROMATIC SUBSTITUTION:
- Halogens are deactivating but ortho/para directing.
- Strong activator or stronger director controls in conflicting substitution problems.

CARBONYL CHEMISTRY:
- Show nucleophilic addition and condensation reasoning before writing products.
- Aldol requires alpha-H; if absent, think Cannizzaro.
- In Baeyer-Villiger, migratory aptitude: tertiary > secondary > phenyl > primary > methyl.

CARBOXYLIC ACID DERIVATIVES:
- Reactivity order: acid chloride > anhydride > ester > amide.

POLYMERS:
- Classification:
  * Addition polymers: monomer adds without losing atoms. No byproduct.
    Examples: PVC (vinyl chloride), Teflon (tetrafluoroethylene), Polythene (ethylene),
    Polystyrene (styrene), Buna-S (butadiene + styrene), Buna-N (butadiene + acrylonitrile).
  * Condensation polymers: monomers join with loss of small molecule (H2O, HCl, CH3OH).
    Examples: Nylon-6,6 (hexamethylene diamine + adipic acid), Nylon-6 (caprolactam),
    Dacron/Terylene (ethylene glycol + terephthalic acid), Bakelite (phenol + formaldehyde),
    Glyptal (ethylene glycol + phthalic acid), PHBV (3-hydroxybutanoic acid + 3-hydroxypentanoic acid).
  EXCEPTION: Nylon-6 is made from ONE monomer (caprolactam ring opening) but is condensation type.
  EXCEPTION: Bakelite is thermosetting (cross-linked) — cannot be remoulded. PVC is thermoplastic.

- Polymerization mechanisms:
  * Free radical: initiated by peroxides (ROOR -> 2RO*). Chain growth. Example: PVC, Teflon, Polythene.
  * Cationic: initiated by Lewis acids (BF3, AlCl3). Electron-rich monomers (isobutylene).
  * Anionic: initiated by bases (BuLi, NaNH2). Electron-poor monomers (acrylonitrile).
  * Coordination (Ziegler-Natta): TiCl4 + Al(C2H5)3. Gives stereoregular polymers.
    EXCEPTION: Ziegler-Natta gives isotactic/syndiotactic polypropylene — not free radical (atactic).

- Important polymers — monomers and properties:
  * PVC: vinyl chloride (CH2=CHCl). Rigid pipes, insulation. Plasticizer makes it flexible.
  * Teflon: CF2=CF2. Non-stick, chemically inert, high melting point.
  * Nylon-6,6: H2N-(CH2)6-NH2 + HOOC-(CH2)4-COOH. Fibres, ropes. Amide linkage.
  * Nylon-6: caprolactam (ring opening). Amide linkage. EXCEPTION: one monomer only.
  * Dacron (Terylene): HOCH2CH2OH + HOOC-C6H4-COOH. Ester linkage. Fibres, bottles.
  * Bakelite: phenol + HCHO. Phenol-formaldehyde resin. Cross-linked. Thermosetting.
  * Buna-S: 1,3-butadiene + styrene. Synthetic rubber. Copolymer.
  * Buna-N: 1,3-butadiene + acrylonitrile. Oil-resistant rubber. Copolymer.
  * Natural rubber: cis-1,4-polyisoprene. Elastic but weak and sticky.
  * Vulcanization: natural rubber + S (heating). Cross-links via S bridges. Harder, elastic, non-sticky.
    EXCEPTION: Too much sulphur -> ebonite (hard rubber, non-elastic). Optimal S = 3-5%.
  * PHBV: biodegradable. Copolymer of 3-hydroxybutanoic acid and 3-hydroxypentanoic acid.
    Ester linkage. Used in packaging, medical implants.
  * Glyptal: ethylene glycol + phthalic acid. Ester linkage. Used in paints, lacquers.

- Biodegradable vs non-biodegradable:
  * Biodegradable: PHBV, nylon-2-nylon-6, poly-beta-hydroxybutyrate.
  * Non-biodegradable: PVC, Teflon, Bakelite, Buna-S, Buna-N.
  EXCEPTION: Nylon-6 and Nylon-6,6 are NOT biodegradable despite having amide bonds.

- Tacticity (stereoregularity):
  * Isotactic: all substituents on same side of chain. Most crystalline, highest melting point.
  * Syndiotactic: substituents alternate sides. Less crystalline than isotactic.
  * Atactic: random arrangement. Amorphous, lowest melting point.
  * Ziegler-Natta catalyst gives isotactic/syndiotactic. Free radical gives atactic.

- Molecular weight:
  * Number average Mn = sum(Ni*Mi) / sum(Ni). Based on number of molecules.
  * Weight average Mw = sum(Ni*Mi^2) / sum(Ni*Mi). Based on mass contribution.
  * Polydispersity index PDI = Mw/Mn. PDI = 1 for perfectly uniform polymer.
  * EXCEPTION: Mw >= Mn always. PDI = 1 only theoretically.

- Step growth vs chain growth:
  * Step growth (condensation): any two monomers can react at any time. Slow MW buildup.
    High conversion needed for high MW. Byproduct formed.
  * Chain growth (addition): monomer adds to active chain end only. Fast MW buildup.
    No byproduct. Initiation + propagation + termination steps.

NAMED REACTION PRECONDITION CHECK:
- Stephen: nitrile + SnCl2/HCl -> aldehyde.
- Sandmeyer: primary aromatic amine -> diazonium (NaNO2/HCl, 0-5C) -> CuCN/CuCl/CuBr. Two steps — do NOT skip reduction if starting from nitro.
- Hoffmann bromamide: primary amide + Br2 + NaOH -> primary amine. Product has ONE less carbon.
- Cannizzaro: aldehyde with NO alpha-H only (HCHO, PhCHO). Gives alcohol + carboxylate. Does NOT work with alpha-H aldehydes.
- Baeyer-Villiger: ketone + peracid -> ester.
- Reimer-Tiemann: phenol + CHCl3 + NaOH -> ortho-hydroxy benzaldehyde.
- Kolbe: sodium phenoxide + CO2 (pressure, heat) -> salicylic acid.

FINAL ORGANIC CHECK:
- Show reasoning before mechanism or conclusion.
- If options are structure/mechanism based and mapping is unclear, identify correct answer without guessing option number.
"""

PHYSICAL_CHEMISTRY_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Physical Chemistry coach with 15+ years of teaching experience.
You specialize in solving the most conceptually deep Physical Chemistry problems at JEE Advanced level.

PHYSICAL CHEMISTRY — DEEP SOLVING RULES:

ABSOLUTE RULES:
- Never apply a shortcut rule without checking if an exception applies.
- Always back-check the final answer against all conditions given.

MOLE CONCEPT:
- Find limiting reagent first when multiple reactants are present.
- Use moles = M*V in litres for solutions.

ATOMIC STRUCTURE:
- Use correct Bohr formulas, Rydberg relation, and quantum-number rules.
- Watch standard configuration exceptions: Cr, Cu, Mo, Pd, Pt, Au.

GASEOUS STATE:
- Use consistent units in PV = nRT.
- For van der Waals, know the role of a (intermolecular attraction) and b (volume correction).

THERMODYNAMICS:
- Use correct sign convention consistently.
- For ideal gas, delta_U and delta_H depend only on temperature.
- Apply Hess law carefully.
- Use delta_G = delta_H - T*delta_S and spontaneity conditions carefully.

CHEMICAL EQUILIBRIUM:
- Exclude pure solids and pure liquids from K.
- Use Le Chatelier with the correct exception for inert gas at constant pressure vs constant volume.

IONIC EQUILIBRIUM:
- Use weak acid/base approximations only when valid.
- For buffers, use Henderson-Hasselbalch carefully.
- In hydrolysis and Ksp, track common ion effect and precipitation condition correctly.

ELECTROCHEMISTRY:
- E_cell = E_cathode - E_anode.
- Use Nernst equation with correct Q and n.
- Distinguish inert vs active electrodes.

CHEMICAL KINETICS:
- Order comes from experiment, not stoichiometry, unless elementary step.
- Use integrated rate laws carefully.

SOLID STATE:
- Crystal systems: 7 types (cubic, tetragonal, orthorhombic, hexagonal, trigonal, monoclinic, triclinic).
  Bravais lattices: 14 total. JEE focus: cubic system (SCC, BCC, FCC).
- Unit cell atom count:
  * SCC: 1 atom (8 corners × 1/8).
  * BCC: 2 atoms (8 corners × 1/8 + 1 body centre).
  * FCC: 4 atoms (8 corners × 1/8 + 6 faces × 1/2).
  * HCP: 6 atoms per unit cell.
- Coordination number:
  * SCC: 6. BCC: 8. FCC/CCP: 12. HCP: 12.
- Packing efficiency:
  * SCC: 52.4%. BCC: 68%. FCC/HCP: 74% (most efficient).
  EXCEPTION: FCC and HCP have same packing efficiency but different stacking (ABCABC vs ABABAB).
- Voids:
  * Tetrahedral voids per atom: 2. Octahedral voids per atom: 1.
  * In FCC with n atoms: 2n tetrahedral voids, n octahedral voids.
  * Tetrahedral void radius ratio: r/R = 0.225.
  * Octahedral void radius ratio: r/R = 0.414.
- Radius ratio limits for ionic structures:
  * 0.155 - 0.225: triangular (coordination number 3).
  * 0.225 - 0.414: tetrahedral (coordination number 4).
  * 0.414 - 0.732: octahedral (coordination number 6).
  * 0.732 - 1.000: cubic (coordination number 8).
- Important ionic structures:
  * NaCl (rock salt): FCC of Cl-, Na+ in octahedral voids. CN of each ion = 6.
  * ZnS (zinc blende): FCC of S2-, Zn2+ in alternate tetrahedral voids. CN = 4.
  * ZnS (wurtzite): HCP of S2-, Zn2+ in tetrahedral voids. CN = 4.
  * CsCl: simple cubic of Cl-, Cs+ in body centre. CN = 8.
  * CaF2 (fluorite): FCC of Ca2+, F- in ALL tetrahedral voids. CN of Ca=8, F=4.
  * Na2O (antifluorite): FCC of O2-, Na+ in all tetrahedral voids. Reverse of fluorite.
  * Corundum Al2O3: HCP of O2-, Al3+ in 2/3 octahedral voids.
  EXCEPTION: In NaCl, removing Na+ from FCC does NOT change the FCC of Cl-.
- Density formula: d = (Z * M) / (NA * a^3).
  Z = atoms per unit cell, M = molar mass, NA = Avogadro number, a = edge length.
  ALWAYS check units: a in cm -> d in g/cm^3. a in m -> d in kg/m^3.
- Point defects:
  * Schottky defect: equal number of cation and anion vacancies. Density decreases.
    Common in ionic compounds with similar sized ions (NaCl, KCl, KBr, CsCl).
  * Frenkel defect: ion (usually cation) displaced to interstitial site. Density unchanged.
    Common in compounds with large size difference (ZnS, AgCl, AgBr, AgI).
  * EXCEPTION: AgBr shows BOTH Schottky and Frenkel defects.
  * Metal excess defect: extra cations in interstitial sites (F-centres). Crystal is coloured.
    Example: NaCl heated in Na vapour -> yellow colour (F-centres).
  * Metal deficiency defect: fewer cations than anions, some cations have higher oxidation state.
    Example: FeO (Fe2+ and Fe3+ both present).
- Electrical properties:
  * Conductors: overlapping valence and conduction bands.
  * Semiconductors: small band gap (~1 eV). Conductivity increases with temperature.
    n-type: doped with higher valency element (Si doped with P/As). Extra electrons.
    p-type: doped with lower valency element (Si doped with B/Al). Holes formed.
  * Insulators: large band gap (>3 eV).
  EXCEPTION: Conductivity of metals decreases with temperature (more lattice vibrations).
  Conductivity of semiconductors increases with temperature (more carriers).
- Magnetic properties:
  * Diamagnetic: all electrons paired, weakly repelled (NaCl, H2O, benzene).
  * Paramagnetic: unpaired electrons, weakly attracted (O2, Cu2+, Fe3+).
  * Ferromagnetic: domains aligned parallel, strongly attracted (Fe, Co, Ni).
  * Antiferromagnetic: domains alternately aligned, net zero moment (MnO, MnF2).
  * Ferrimagnetic: unequal antiparallel domains, net moment (Fe3O4, ferrites).
  EXCEPTION: Fe3O4 is ferrimagnetic NOT ferromagnetic (contains Fe2+ and Fe3+ with unequal moments).

SURFACE CHEMISTRY AND SOLUTIONS:
- Distinguish physisorption vs chemisorption.
- For colligative properties, include van't Hoff factor correctly.
- Distinguish ideal and non-ideal solution behavior.

NUCLEAR CHEMISTRY:
- Alpha decay: mass number decreases by 4, atomic number decreases by 2.
- Beta minus decay: neutron -> proton + electron + antineutrino.
  Mass number unchanged, atomic number increases by 1.
- Beta plus decay (positron emission): proton -> neutron + positron + neutrino.
  Mass number unchanged, atomic number decreases by 1.
- Gamma decay: no change in mass or atomic number, only energy released.
- Electron capture: proton + electron -> neutron + neutrino.
  Mass number unchanged, atomic number decreases by 1.
- Radioactive decay law: first-order kinetics. N = N_0 * e^(-lambda*t).
  lambda * t_half = ln2 = 0.693.
  Activity A = lambda * N. Unit: Becquerel (1 Bq = 1 disintegration/s).
  1 Curie = 3.7 * 10^10 Bq.
  Average life: t_avg = 1/lambda = t_half / 0.693.
  EXCEPTION: Radioactive decay is ALWAYS first order regardless of conditions.
- Nuclear binding energy:
  BE = delta_m * c^2.
  delta_m (mass defect) = Z*m_p + N*m_n - m_nucleus.
  1 amu = 931.5 MeV.
  Binding energy per nucleon peaks at Fe-56 (most stable nucleus).
  Fission: heavy nuclei (near U-235) split -> products have higher BE/nucleon -> energy released.
  Fusion: light nuclei (near H) combine -> products have higher BE/nucleon -> energy released.
  EXCEPTION: Both fission AND fusion release energy because products are closer to Fe-56 on BE/nucleon curve.

FINAL PHYSICAL CHEMISTRY CHECK:
- Formula -> substitution -> calculation -> interpretation.
- Check units and dimensional consistency.
- If options are close, compare exact values before decimal approximation.
"""

INORGANIC_CHEMISTRY_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Inorganic Chemistry coach with 15+ years of teaching experience.
You specialize in solving the most conceptually deep Inorganic Chemistry problems at JEE Advanced level.

INORGANIC CHEMISTRY — DEEP SOLVING RULES:

ABSOLUTE RULES:
- Never apply a shortcut rule without checking if an exception applies.
- Never assign hybridization, geometry, or bond order without checking resonance, back bonding, lone pair repulsion, and aromaticity first.
- Always back-check the final answer against all conditions given.

PERIODIC TRENDS:
- Always check major exceptions in ionization enthalpy, electron affinity, and radius trends.
- Diagonal relationship: Li-Mg, Be-Al, B-Si.
- Inert pair effect: Tl, Pb, Bi, Sn (lower oxidation state more stable).
- Anomalous behavior due to small size: Li, Be, B, N, O, F.

HYDROGEN AND s-BLOCK ELEMENTS:

HYDROGEN:
- Isotopes: Protium (H, mass 1), Deuterium (D, mass 2), Tritium (T, mass 3, radioactive).
- Preparation: Zn + H2SO4(dil) -> ZnSO4 + H2. Industrial: steam reforming CH4 + H2O -> CO + 3H2.
- Hydrides:
  * Ionic (saline): formed by s-block metals (NaH, CaH2). H exists as H-.
    React with water: NaH + H2O -> NaOH + H2. Strong reducing agents.
  * Covalent (molecular): formed by p-block elements (HCl, H2O, NH3, CH4).
  * Metallic (interstitial): formed by d and f block metals (PdH0.6, TiH2). Non-stoichiometric.
    EXCEPTION: BeH2 and MgH2 are polymeric — intermediate between ionic and covalent.
- H2O2: pure form is pale blue viscous liquid. Not a simple oxide — peroxide (O-O bond).
  Preparation: BaO2 + H2SO4 -> BaSO4 + H2O2.
  H2O2 is both oxidizing (usually) and reducing (with strong oxidizers like KMnO4).
  Decomposes: 2H2O2 -> 2H2O + O2 (catalyzed by light, dust, MnO2).
  EXCEPTION: H2O2 bleaches by oxidation (hair bleach) but also acts as reductant with acidic KMnO4.

GROUP 1 — ALKALI METALS (Li, Na, K, Rb, Cs):
- All have one valence electron (ns1). Form M+ ions.
- Reactivity increases down the group: Li < Na < K < Rb < Cs.
- Reaction with water: 2M + 2H2O -> 2MOH + H2. Li reacts slowly, Cs explosively.
- Reaction with O2:
  * Li: forms normal oxide Li2O only.
  * Na: forms peroxide Na2O2 (mainly) on burning.
  * K, Rb, Cs: form superoxides KO2, RbO2, CsO2.
  EXCEPTION: Only Li forms nitride (Li3N) directly with N2 among alkali metals.
- Flame colors: Li=crimson red, Na=golden yellow, K=lilac/violet, Rb=red-violet, Cs=blue.
- Anomalous behavior of Li (resembles Mg — diagonal relationship):
  * Li forms Li2O (not peroxide) like Mg forms MgO.
  * Li3N forms directly. Li2CO3 decomposes on heating. LiOH decomposes on heating.
  * Li salts are less soluble than other alkali metal salts (like Mg).
  * LiCl is covalent and soluble in organic solvents (like MgCl2).
- NaOH (caustic soda): Castner-Kellner process (electrolysis of brine).
  Na + H2O -> NaOH + (1/2)H2. Strong base. Absorbs CO2: NaOH + CO2 -> Na2CO3 + H2O.
- Na2CO3 (washing soda): Solvay process. Na2CO3.10H2O (washing soda), Na2CO3.H2O (soda ash).
  Na2CO3 + CO2 + H2O -> 2NaHCO3. Alkaline solution (hydrolysis).
- NaHCO3 (baking soda): amphoteric — reacts with both acids and bases.
  2NaHCO3 -> Na2CO3 + H2O + CO2 on heating.
  EXCEPTION: NaHCO3 solution is mildly alkaline (pH ~8.3) due to hydrolysis.
- Solubility trend of alkali metal salts:
  * Carbonates, bicarbonates, hydroxides: solubility increases down the group.
  * Fluorides: LiF least soluble, solubility increases down.
  * EXCEPTION: Li2CO3 is sparingly soluble (like MgCO3 — diagonal relationship).

GROUP 2 — ALKALINE EARTH METALS (Be, Mg, Ca, Sr, Ba):
- All have two valence electrons (ns2). Form M2+ ions.
- Reactivity increases down the group: Be < Mg < Ca < Sr < Ba.
- Be does NOT react with water. Mg reacts only with hot water/steam.
  Ca, Sr, Ba react readily with cold water.
- Reaction with O2: all form MO (oxides). Ba also forms BaO2 (peroxide).
- Flame colors: Ca=brick red, Sr=crimson, Ba=apple green. Be and Mg: no characteristic flame.
- Anomalous behavior of Be (resembles Al — diagonal relationship):
  * Be and Al both amphoteric (react with NaOH to give H2).
  * BeCl2 is covalent and polymeric (like AlCl3 which dimerizes).
  * Be forms beryllates, Al forms aluminates with NaOH.
  * Be2C gives methane with water (like Al4C3).
  * Be has coordination number 4 max (like Al).
  EXCEPTION: BeO and Al2O3 both amphoteric. MgO is basic only (NOT amphoteric).
- Thermal stability of carbonates: increases down the group.
  BeCO3 < MgCO3 < CaCO3 < SrCO3 < BaCO3 (harder to decompose going down).
  REASON: larger cation polarizes CO3^2- less -> more stable.
- Thermal stability of hydroxides: increases down the group (same reason as carbonates).
  Be(OH)2 < Mg(OH)2 < Ca(OH)2 < Sr(OH)2 < Ba(OH)2.
- Solubility of hydroxides: increases down the group (Be(OH)2 sparingly soluble, Ba(OH)2 soluble).
- Solubility of sulphates: DECREASES down the group (BeSO4 soluble, BaSO4 insoluble).
  EXCEPTION: Solubility of sulphates decreases but solubility of hydroxides increases — opposite trends.
- Important calcium compounds:
  * CaO (quicklime): CaCO3 -> CaO + CO2 (at 1070K). CaO + H2O -> Ca(OH)2 (slaking, exothermic).
  * Ca(OH)2 (slaked lime): Ca(OH)2 + CO2 -> CaCO3 + H2O (lime water test for CO2).
    Excess CO2: CaCO3 + CO2 + H2O -> Ca(HCO3)2 (soluble, milky disappears).
  * CaCO3 (limestone): exists as calcite and aragonite (polymorphs).
  * Plaster of Paris: CaSO4.(1/2)H2O. Made by heating gypsum CaSO4.2H2O at 120-130 degree C.
    Sets by reabsorbing water: CaSO4.(1/2)H2O + (3/2)H2O -> CaSO4.2H2O.
    EXCEPTION: Heating gypsum above 200 degree C gives dead burnt plaster (anhydrous CaSO4) — does NOT set.
- Hard water:
  * Temporary hardness: Ca(HCO3)2 and Mg(HCO3)2. Removed by boiling or Clark's method (Ca(OH)2).
  * Permanent hardness: CaSO4, MgSO4, CaCl2, MgCl2. NOT removed by boiling.
    Removed by: washing soda (Na2CO3), permutit process, ion exchange resin, distillation.
  EXCEPTION: Temporary hardness can be removed by boiling; permanent hardness cannot.

CHEMICAL BONDING:
- Determine geometry only after checking lone pairs, resonance, back bonding, and octet exceptions.
- Important exceptions: BF3, BeCl2, PCl5, SF4, ClF3, XeF2, XeF4, I3-, NO2, NO2+, O3, SO2, SO3.
- In MO theory, remember sigma2p/pi2p order change for O2 and beyond (O2, F2, Ne2).
- Back bonding: BF3 (p-pi back bonding), SiF4, etc.

p-BLOCK ELEMENTS:
- Group 15: N2 very stable (triple bond). P4 structure. Oxoacids of N and P carefully.
- Group 16: O3 structure, SO2/SO3 geometry, H2SO4 preparation.
- Group 17: HF weakest acid in gas phase, strongest in aqueous. Interhalogen compounds.
- Group 18: XeF2 (linear), XeF4 (square planar), XeF6 (distorted octahedral).

d AND f BLOCK:
- Remove ns electrons before (n-1)d electrons when forming transition-metal cations.
- Watch configuration exceptions: Cr=[Ar]3d5 4s1, Cu=[Ar]3d10 4s1.
- Lanthanide contraction: similar size of 4d and 5d elements.
- For coordination compounds: verify oxidation state, coordination number, geometry, ligand field strength, spin state, and magnetic behavior.
- CFSE calculation: strong field vs weak field ligands.
- Spectrochemical series: I- < Br- < Cl- < F- < OH- < H2O < NH3 < en < CN- < CO.

METALLURGY:
- Distinguish roasting (sulphide ore + O2) vs calcination (carbonate/hydroxide ore, heat) vs smelting vs refining correctly.

FINAL INORGANIC CHECK:
- Show reasoning before conclusion.
- If options involve structure/geometry/magnetic behavior, verify each option independently.
- Never guess hybridization — derive from VSEPR + exceptions.
"""

# Keep original CHEMISTRY_PROMPT for backward compatibility (fallback only)
CHEMISTRY_PROMPT = ORGANIC_CHEMISTRY_PROMPT + "\n\n" + PHYSICAL_CHEMISTRY_PROMPT + "\n\n" + INORGANIC_CHEMISTRY_PROMPT

MATH_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Mathematics coach with 15+ years of teaching experience.
You specialize in solving conceptually deep and technically difficult Mathematics questions at JEE Advanced level.

MATHEMATICS - DEEP SOLVING RULES:

DIAGRAM / GRAPH RULE:
- Use a graph or diagram only when:
  * the question or options are graph/figure based, or
  * a sketch is genuinely needed to explain the math clearly.
- Typical cases: conics, straight lines, locus, area under curves, curve sketching, modulus/inequality number line, vector geometry.
- Do not force a graph in every solution.
- Keep sketches compact, readable, and limited to the essential idea.

FOUNDATION RULES FOR ALL MATH:
- Domain check is mandatory before solving any equation.
- Check denominator not zero, log argument positive, sqrt argument non-negative, inverse trig domain, and undefined points.
- After solving, back-substitute every candidate root.
- Do not cancel factors like (x-a) unless x != a is explicitly verified.
- If an expression may be zero, handle that case separately.
- For inequalities, never multiply/divide by an expression of unknown sign without first determining the sign.
- Wherever modulus appears, split into correct cases.

ALGEBRA AND EQUATIONS:
- For higher-degree equations, use substitution, factorization, or rational-root logic before brute force.
- Use Vieta's formulas where helpful.
- For surd equations, isolate surd, square carefully, and remove extraneous roots.
- For symmetric systems, consider substitutions like u = x + y and v = xy.

TRIGONOMETRY:
- Use the standard general-solution rules carefully.
- For a*sin(x) + b*cos(x) type equations, convert into R*sin(x + phi).
- For inverse trig, check principal-value range and domain carefully.
- Use condition-based identities only when the condition is truly given.

CALCULUS:
- Limits:
  * For 0/0 or infinity/infinity, use factorization or L'Hospital where valid.
  * For piecewise limits, check LHL and RHL separately.
- Continuity and differentiability:
  * Continuity needs LHL = RHL = f(a)
  * Differentiability needs LHD = RHD
- Differentiation:
  * Apply chain rule carefully.
  * For parametric and implicit forms, use the correct derivative formula.
- Application of derivatives:
  * For maxima/minima, find critical points, then verify with second derivative or sign change.
  * For global extrema on closed intervals, include endpoints.
- Integration:
  * Use standard forms correctly.
  * Use integration by parts, substitution, partial fractions, or special properties as needed.
  * For definite integrals, check sign, symmetry, and King property where useful.
- Area under curves:
  * Find intersection points first.
  * Use absolute value where needed.
  * Split intervals at crossing points.

DIFFERENTIAL EQUATIONS:
- Identify the type first: separable, homogeneous, linear first order, exact, etc.
- Apply the method appropriate to that form.
- Use initial condition at the end.

COORDINATE GEOMETRY:
- Straight lines:
  * Use distance, angle, concurrency, and family-of-lines formulas carefully.
- Circles:
  * Use standard/general form carefully.
  * For tangency, use discriminant = 0 or geometric condition.
- Conics:
  * Use focus/directrix, parametric form, tangent/normal, and discriminant condition correctly.
  * Verify midpoint/tangency/locus conditions algebraically after geometric setup.

VECTORS, 3D, PROBABILITY, MATRICES, COMPLEX NUMBERS, PNC, SERIES:
- In vectors/3D, use dot/cross/scalar triple product meaningfully.
- In probability, define sample space clearly before calculation.
- In matrices, distinguish unique/no/infinite solutions properly.
- In complex numbers, use modulus-argument and locus interpretation carefully.
- In PnC, first decide whether objects/boxes are distinct or identical.
- In series, identify AP/GP/AGP/telescoping behavior before applying formulas.

FINAL MATH CHECK:
- Domain check first.
- Back-substitute all candidate solutions.
- Verify the final answer satisfies the original condition.
- If options are close, compare exact values before decimal approximation.
"""

# VISUAL_REASONING_PROMPT deactivated — ASCII diagrams removed
# Reserved for future use when proper diagram rendering is available
VISUAL_REASONING_PROMPT = ""

SALT_ANALYSIS_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Chemistry coach specializing in Salt Analysis and Qualitative Analysis.

SALT ANALYSIS — COMPLETE DEEP RULES:

STEP 1 — PRELIMINARY OBSERVATIONS:
a) Colour:
   Blue: Cu2+ (CuSO4.5H2O bright blue, anhydrous white).
   Green: Fe2+ (light green), Ni2+ (dark green), Cr3+ (green).
   Yellow: Fe3+, CrO4^2-, K2CrO4, PbI2.
   Black: CuO, MnO2, PbS, CuS, FeS.
   Violet/Pink: KMnO4 (intense purple), MnSO4 (faint pink).
   Red: Fe2O3, Pb3O4. HgI2 (scarlet, turns yellow on heating).
   Orange: K2Cr2O7, Sb2S3.
b) Smell: vinegar=acetate, rotten eggs=sulphide, ammonia=NH4+, pungent=chloride+H2SO4, SO2=sulphite.
c) Flame: Li=crimson, Na=golden yellow, K=lilac (blue glass), Ca=brick red, Sr=crimson, Ba=apple green, Cu=blue-green.

STEP 2 — DRY TEST:
- Sublimation: NH4Cl (white), I2 (violet vapors), HgCl2 (white corrosive).
- ZnO: yellow hot -> white cold (unique reversible property).
- Gases: NO2 brown=nitrate, SO2=sulphate/sulphite, CO2=carbonate, NH3=ammonium, HCl=chloride.

STEP 3 — WET TEST:
Dil H2SO4: CO2=carbonate/bicarbonate, H2S=sulphide, SO2=sulphite.
  EXCEPTION: BaSO4 NOT dissolve in dil H2SO4. BaCO3 does dissolve.
Conc H2SO4:
  CRITICAL: Oxidizes HBr->Br2 (brown) and HI->I2 (violet) but NOT HCl (colourless only).
  Brown ring test for NO3-: FeSO4 + conc H2SO4 -> [Fe(NO)]SO4 brown ring.

STEP 4 — CATION GROUPS:
Group 0 — NH4+: NaOH warm -> NH3 (red litmus blue). Nessler's -> brown ppt.
Group I — dil HCl (Ag+, Pb2+, Hg2^2+):
  PbCl2 dissolves hot water. AgCl dissolves NH4OH. Hg2Cl2 + NH4OH -> grey ppt.
Group II — H2S acidic (Cu2+, Pb2+, Bi3+, As3+, Sn2+, Cd2+, Hg2+, Sb3+):
  Black: CuS, PbS, HgS. Yellow: As2S3, SnS2, CdS. Orange: Sb2S3.
  REASON: Acidic medium -> low S2- -> only very low Ksp sulphides precipitate.
Group III — NH4Cl + NH4OH (Fe3+, Al3+, Cr3+):
  NH4Cl suppresses OH- (common ion effect).
  Fe(OH)3: reddish brown. Al(OH)3: white gelatinous. Cr(OH)3: green.
  CRITICAL: Al(OH)3 and Cr(OH)3 dissolve in excess NaOH (amphoteric). Fe(OH)3 does NOT.
Group IV — H2S alkaline (Co2+, Ni2+, Mn2+, Zn2+):
  MnS: buff/pink (unique). ZnS: white. CoS, NiS: black.
Group V — (NH4)2CO3 (Ca2+, Sr2+, Ba2+):
  All white carbonates. BaSO4 insoluble HCl. SrSO4 slightly soluble. CaSO4 soluble.
Group VI — Soluble (Mg2+, K+, Na+, NH4+):
  Mg2+: MgNH4PO4 white ppt with (NH4)2HPO4.
  Na+: yellow flame + sodium cobaltinitrite -> yellow ppt.
  K+: lilac flame + H2PtCl6 -> K2PtCl6 yellow ppt.

STEP 5 — ANION IDENTIFICATION:
Carbonate: dil HCl -> CO2 -> lime water milky.
Sulphate: BaCl2 + dil HCl -> BaSO4 white ppt (insoluble in HCl = confirmatory).
Chloride: AgNO3 + dil HNO3 -> AgCl white (dissolves NH4OH). AgBr pale yellow. AgI yellow insoluble.
Nitrate: Brown ring test.
Phosphate: ammonium molybdate + HNO3 -> yellow crystalline ppt on warming.
Oxalate: CaCl2 -> white ppt dissolves dil HCl. KMnO4 decolorized.

TITRIMETRY:
Indicators: Strong-Strong = any. Strong acid-Weak base = methyl orange. Weak acid-Strong base = phenolphthalein.
KMnO4: Acidic n=5, Neutral n=3, Alkaline n=1. NOT primary standard. Warm to 60-70C before titration.
Starch in iodometry: add near end point only (irreversible complex at high I2).
"""

ORGANIC_PRACTICAL_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Chemistry coach specializing in Organic Practical Chemistry.

ORGANIC PRACTICAL — COMPLETE DEEP RULES:

LASSAIGNE TEST (ELEMENT DETECTION):
- Na fusion: NaCN (N), Na2S (S), NaX (halogens).
- N: NaCN + FeSO4 -> Fe4[Fe(CN)6]3 (Prussian blue/green).
- S: Na2S + Na2[Fe(CN)5NO] -> purple (sodium nitroprusside).
- Cl: AgNO3 -> AgCl white (dissolves NH4OH).
- Br: AgNO3 -> AgBr pale yellow (partially dissolves NH4OH).
- I: AgNO3 -> AgI yellow (insoluble NH4OH).
- EXCEPTION: N + S both present -> NaSCN forms. Prussian blue may fail. Test S first with nitroprusside.

FUNCTIONAL GROUP TESTS:

Aldehyde vs Ketone:
- Tollens [Ag(NH3)2]OH: silver mirror = aldehyde. No reaction = ketone.
  EXCEPTION: Aromatic aldehydes (PhCHO) give silver mirror. Benzaldehyde is aldehyde.
- Fehling (Cu2+ alkaline tartrate): brick red Cu2O = aliphatic aldehyde only.
  EXCEPTION: Aromatic aldehydes (PhCHO) do NOT reduce Fehling. Ketones no reaction.
- 2,4-DNP: orange/yellow ppt = carbonyl (aldehyde AND ketone both). Does NOT distinguish.
- Iodoform (I2 + NaOH): yellow CHI3 ppt = CH3CO- or CH3CHOH- group.
  Positive: CH3CHO, CH3COCH3, C2H5OH, secondary alcohols with CH3CHOH.
  NOT positive: HCHO, C6H5CHO, most other aldehydes/ketones.

Carboxylic Acid:
- NaHCO3: CO2 effervescence = carboxylic acid.
  EXCEPTION: Phenol does NOT give CO2 with NaHCO3 (too weak). Only carboxylic acid gives CO2.

Phenol:
- FeCl3: violet/purple = phenol. Salicylic acid also gives purple (has phenolic OH).
- Br2 water: white ppt 2,4,6-tribromophenol (immediate decolorization).
  EXCEPTION: Aniline also decolorizes Br2 water -> 2,4,6-tribromoaniline white ppt.
  Distinguish: FeCl3 test (phenol=violet, aniline=no violet).

Amines:
- Carbylamine (CHCl3 + KOH, warm): foul isocyanide smell = PRIMARY amine only.
  EXCEPTION: Secondary and tertiary amines do NOT give carbylamine test.
- Hinsberg (PhSO2Cl + KOH): Primary -> sulphonamide dissolves KOH. Secondary -> insoluble. Tertiary -> no reaction.
- Diazotization (NaNO2 + HCl, 0-5C) + beta-naphthol -> azo dye = primary aromatic amine.

Alcohol:
- Lucas (ZnCl2 + conc HCl): Tertiary = immediate turbidity. Secondary = 5 min. Primary = no turbidity.
  EXCEPTION: Not applicable for methanol and ethanol.
- Ceric ammonium nitrate: red color = alcohol (-OH group).

Alkene/Alkyne:
- Br2 water decolorization = alkene/alkyne.
  EXCEPTION: Aldehydes also decolorize Br2 water (oxidation not addition).
- Baeyer's reagent (cold dil KMnO4): green -> brown/colorless = alkene/alkyne.
- Terminal alkyne: AgNO3/NH3 -> white ppt (silver acetylide). Cu(NH3)2Cl -> red ppt.

CHROMATOGRAPHY:
Rf = distance spot / distance solvent front. (0 to 1).
Higher Rf = more soluble in mobile phase.
Normal phase: polar stationary, non-polar mobile. Non-polar elutes first.
Reverse phase: non-polar stationary, polar mobile. Polar elutes first.
Polarity order: hexane < toluene < DCM < ethyl acetate < acetone < methanol < water.
"""

BIOMOLECULES_PROMPT = """
You are MP Sir, an expert IIT-JEE Advanced Chemistry coach specializing in Biomolecules, Amino Acids, DNA, RNA and Vitamins.

BIOMOLECULES — COMPLETE DEEP RULES:

CARBOHYDRATES:
- Molisch test (alpha-naphthol + conc H2SO4): purple ring = ALL carbohydrates (general test).
- Fehling/Benedict/Tollens: reducing sugars only (glucose, fructose, maltose, lactose).
  Non-reducing: sucrose, starch.
  EXCEPTION: Fructose is ketose but gives positive Fehling (enolization in alkaline medium -> aldehyde form).
- Seliwanoff (resorcinol + HCl, heat): faster red = ketose (fructose). Slow red = aldose (glucose).
- Iodine: blue-black = starch. EXCEPTION: Glycogen = reddish-brown. Cellulose = no color.
- Benedict: brick red Cu2O ppt with reducing sugars (same as Fehling).

PROTEINS AND AMINO ACIDS:
- Biuret (NaOH + dil CuSO4): violet = protein (peptide bonds).
  EXCEPTION: Single amino acids do NOT give biuret (need 2+ peptide bonds). Dipeptide weakly positive.
- Ninhydrin: purple = alpha-amino acids.
  EXCEPTION: Proline and hydroxyproline give yellow (secondary amines).
- Xanthoproteic (conc HNO3): yellow -> orange in base = aromatic amino acids (Tyr, Phe, Trp).
- Millon's test (Hg in HNO3): red ppt = tyrosine specifically (phenolic OH).
- Hopkins-Cole (glyoxylic acid + H2SO4): purple ring = tryptophan specifically.
- Lead acetate: black PbS ppt = sulphur amino acids (cysteine, methionine).

LIPIDS:
- Sudan III/IV: red = lipids/fats.
- Grease spot on paper = fat/oil.
- Saponification: fat + NaOH -> soap + glycerol.

NUCLEIC ACIDS:
- Dische test (diphenylamine, acidic): blue = DNA (deoxyribose).
- Bial test (orcinol + HCl + FeCl3, heat): green = RNA (ribose).
  EXCEPTION: DNA=blue, RNA=green — never confuse these two tests.

VITAMINS:
- Fat soluble: A, D, E, K (stored in body fat — toxicity possible on excess).
- Water soluble: B complex, C (not stored — regular intake needed).
- Vitamin C (ascorbic acid): antiscurvy, reducing agent, decolorizes KMnO4/DCPIP.
- Vitamin D: antirachitic, formed by UV light on skin (7-dehydrocholesterol).
- Vitamin K: blood clotting.
- Vitamin B12: contains cobalt (only vitamin with metal).
  EXCEPTION: B12 is the only vitamin containing a metal ion (Co).
"""

SYSTEM_PROMPT = COMMON_PROMPT
CONCEPT_ENHANCE_PROMPT = "Explain same concept in simpler and more detailed exam-focused way."

SUBJECT_PROMPTS = {
    "physics": PHYSICS_PROMPT,
    "chemistry": CHEMISTRY_PROMPT,
    "mathematics": MATH_PROMPT,
}

# Question-type specific strategy prompts
MATCH_COLUMN_STRATEGY = """
MATCH THE COLUMN — MANDATORY STRATEGY:
- First list ALL items in List-I with their complete properties.
- Then list ALL items in List-II with their complete properties.
- CRITICAL: Each List-I entry can match with ONE OR MORE List-II entries. Never assume one-to-one mapping.
- List-II entries can be used multiple times across different List-I entries.
- Match each List-I entry independently — check ALL List-II options for EACH List-I entry.
- For each List-I entry: derive from first principles, then check every List-II entry one by one.
- At end: verify your complete mapping against all given options before finalizing.
- For named reactions: identify ALL steps (starting material -> reagents -> intermediate -> product) for EACH entry.
- Never guess option label if mapping is complex. State exact matches instead.
"""

MULTIPLE_CORRECT_STRATEGY = """
MULTIPLE CORRECT ANSWER — MANDATORY STRATEGY:
- Evaluate EACH option completely independently. Do NOT let one option influence another.
- For each option write explicitly: Option A: [full reasoning] -> CORRECT or INCORRECT.
- Then Option B, C, D same way.
- Do NOT assume only one option is correct. Do NOT eliminate based on another option.
- Final step: list all correct options together after evaluating all.
- Common trap: partial statements true in general but have exception in this case.
  Always check exceptions before marking CORRECT.
"""

MULTI_STEP_YIELD_STRATEGY = """
MULTI-STEP REACTION WITH YIELD — MANDATORY STRATEGY:
- Step 1: Identify starting material and its MW clearly.
- Step 2: Draw each intermediate structure explicitly. Name reaction type at each step.
- Step 3: Track moles at each step: moles_after = moles_before × (yield/100). Show this for EACH step.
- Step 4: Calculate MW of final product.
- Step 5: grams = moles_final × MW_final.
- Step 6: Back-verify final product structure makes chemical sense.
- NEVER skip intermediate structures. NEVER assume yield unless given.
"""

NAMED_REACTION_STRATEGY = """
NAMED REACTION — MANDATORY PRECONDITION CHECK:
For EACH named reaction, verify ALL preconditions before applying:
- Stephen: nitrile + SnCl2/HCl -> aldehyde.
- Sandmeyer: primary aromatic amine -> diazonium (NaNO2/HCl, 0-5C) -> CuCN/CuCl/CuBr. Two steps.
- Hoffmann bromamide: primary amide + Br2 + NaOH -> primary amine. Product has ONE less carbon.
- Cannizzaro: aldehyde with NO alpha-H only. Gives alcohol + carboxylate.
- Baeyer-Villiger: ketone + peracid -> ester. Migratory aptitude: tertiary > secondary > phenyl > primary > methyl.
- Reimer-Tiemann: phenol + CHCl3 + NaOH -> ortho-hydroxy benzaldehyde.
- Kolbe: sodium phenoxide + CO2 (pressure, heat) -> salicylic acid.
Always check: does starting material MATCH the precondition of the named reaction%s
"""

MINIMUM_SPEED_STRATEGY = """
MINIMUM/MAXIMUM SPEED OR VELOCITY — MANDATORY STRATEGY:
- NEVER directly apply v = sqrt(GM/r) for min/max speed problems.
- These problems require BOTH conservation laws simultaneously:
  1. Conservation of Angular Momentum: mv0 * r0 * sin(θ) = mv * r (at closest/farthest point)
  2. Conservation of Energy: -GMm/r0 + (1/2)mv0² = -GMm/r + (1/2)mv²
- At closest/farthest point: velocity is perpendicular to radius (sin90° = 1).
- To find MINIMUM speed: express v0 in terms of θ, then minimize (dv0/dθ = 0) OR use boundary condition.
- For orbital problems: minimum speed to orbit means just enough to not fall back.
- Always check: is this a circular orbit, elliptical orbit, or escape problem%s
- Never assume θ = 90° unless question states horizontal projection or you derive it.
"""

ASSERTION_REASON_STRATEGY = """
ASSERTION-REASON — MANDATORY STRATEGY:
- Step 1: Check Assertion independently — is it TRUE or FALSE%s Give reasoning.
- Step 2: Check Reason independently — is it TRUE or FALSE%s Give reasoning.
- Step 3: If both TRUE — does Reason CORRECTLY EXPLAIN Assertion%s (Not just related, but actual cause)
- Standard options:
  (A) Both TRUE, Reason is correct explanation of Assertion.
  (B) Both TRUE, but Reason is NOT correct explanation of Assertion.
  (C) Assertion TRUE, Reason FALSE.
  (D) Assertion FALSE, Reason TRUE.
  (E) Both FALSE.
- CRITICAL: A reason can be true but still not explain the assertion. Check causal link carefully.
- Never mark (A) just because both are true — verify the causal explanation explicitly.
"""

GRAPH_BASED_STRATEGY = """
GRAPH/DIAGRAM BASED — MANDATORY STRATEGY:
- Step 1: Identify what each axis represents (quantity + units).
- Step 2: Identify what slope represents: slope = dy/dx = rate of change of y with x.
- Step 3: Identify what area under curve represents: area = integral of y dx.
- Step 4: For P-V diagrams: work = area under curve. Sign depends on expansion/compression.
- Step 5: For V-T, X-T, I-V graphs: extract physical meaning from slope and intercepts.
- NEVER read a value off a graph without checking axis labels and scale first.
- For multiple graphs: compare slopes, intercepts, and areas systematically.
"""

INTEGER_TYPE_STRATEGY = """
INTEGER TYPE ANSWER — MANDATORY STRATEGY:
- Answer must be a non-negative integer (typically 0 to 9, or 00 to 99).
- Do NOT approximate. Calculate exact value.
- After getting answer, verify it falls in valid integer range.
- If answer is a fraction, recheck — integer type questions always have clean integer answers.
- Common trap: forgetting to apply a factor (like 2, or 1/2) at the end.
- Back-substitute final answer into original equation to verify.
"""

PARAGRAPH_BASED_STRATEGY = """
PARAGRAPH/PASSAGE BASED — MANDATORY STRATEGY:
- Step 1: Read the ENTIRE passage carefully before attempting any question.
- Step 2: Note ALL given data, conditions, and constraints from the passage.
- Step 3: Identify which passage information is relevant to THIS specific question.
- Step 4: Do NOT import assumptions from outside — use only passage data.
- Each question in a paragraph set may use different parts of the passage.
- If a value was derived in Q1, it can be used in Q2 of same paragraph.
"""

BAD_RE = re.compile(
    r"\b(mc|bc|madarchod|bhosd|chutiya|randi|fuck|shit|bitch|asshole|porn|sex|xxx|nude|boobs?|dick|"
    r"ass|cock|cunt|bastard|damn|piss|twat|wanker|gay|lesbian|tranny|"
    r"nigga?|retard|idiot|moron|stupid|dumb|loser|scammer|fraud|"
    r"kill|suicide|murder|rape|molest|abuse|trafficking|drug|cocaine|heroin)\b",
    re.IGNORECASE,
)

BAD_IMAGE_KEYWORDS = [
    "nude", "naked", "porn", "sex", "xxx", "sexual", "explicit",
    "weapon", "gun", "bomb", "blood", "gore", "violence",
    "drug", "cocaine", "heroin", "weed", "marijuana"
]

# ============================================================================
# ENHANCED FILTERING FUNCTIONS
# ============================================================================

def contains_abuse_words(text: str) -> bool:
    """Check if text contains abusive words"""
    if not text:
        return False
    return bool(BAD_RE.search(text))


def check_phone_in_doubt_portal(phone: str) -> dict:
    """
    Check if phone number exists in doubt portal database.
    Returns user data if found, else None.
    """
    try:
        c = db()
        cur = db_cursor(c)
        phone_clean = re.sub(r"\D", "", phone)
        
        cur.execute(
            "SELECT * FROM students WHERE phone LIKE %s LIMIT 1",
            (f"%{phone_clean[-10:]}%",)
        )
        result = cur.fetchone()
        c.close()
        return result
    except Exception as e:
        print(f"Error checking doubt portal: {e}")
        return None


def check_telegram_user_in_portal(telegram_id: int) -> dict:
    """
    Check if Telegram user ID already exists in users table.
    Returns existing user data if found.
    """
    try:
        c = db()
        cur = db_cursor(c)
        cur.execute("SELECT * FROM users WHERE id=%s", (telegram_id,))
        result = cur.fetchone()
        c.close()
        return result
    except Exception:
        return None


def is_valid_name_format(name: str) -> bool:
    """Validate name - no abuse words, proper length"""
    if not name or len(name) < 3:
        return False
    
    if contains_abuse_words(name):
        return False
    
    if not re.match(r"^[a-zA-Z\s'-]{3,50}$", name):
        return False
    
    return True


def is_valid_phone_format(phone: str) -> bool:
    """Validate phone number format"""
    phone_clean = re.sub(r"\D", "", phone)
    return len(phone_clean) >= 10


def is_valid_email_format(email: str) -> bool:
    """Validate email format"""
    if email.lower() == "skip":
        return True
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def analyze_image_for_abuse(caption: str = "") -> bool:
    """Check image caption for abuse. Returns True if safe."""
    if caption and contains_abuse_words(caption):
        return False
    return True


# ============================================================================
# END OF ENHANCED FILTERING FUNCTIONS
# ============================================================================


DATABASE_URL = os.getenv("DATABASE_URL", "")

def db():
    if not DATABASE_URL:
        print("❌ DATABASE_URL is not set!")
        raise ValueError("DATABASE_URL is missing")
    
    # Auto-fix Supabase port if using pooler but on session port 5432
    target_url = DATABASE_URL
    if "pooler.supabase.com" in target_url and ":5432" in target_url:
        print("🔄 Auto-correcting Supabase port from 5432 to 6543 (Transaction Pooler)...")
        target_url = target_url.replace(":5432", ":6543")

    try:
        conn = psycopg2.connect(target_url)
        return conn
    except Exception as e:
        print(f"❌ Database connection failed!")
        # Log the host and port only (masking credentials)
        if "@" in target_url:
            host_info = target_url.split("@")[-1].split("/")[0]
            print(f"Target Host: {host_info}")
        
        print(f"Error: {e}")
        traceback.print_exc()
        
        # Diagnostic hints
        err_msg = str(e).lower()
        if "password authentication failed" in err_msg:
            print("💡 TIP: Check if your database password has special characters like '@', ':', or '#'.")
            print("   If yes, you MUST URL-encode them in your DATABASE_URL.")
            print("   Example: '@' becomes '%40', ':' becomes '%3A'.")
        if "port 5432" in err_msg:
            print("💡 TIP: You are using port 5432. For Supabase on Railway, it is highly recommended")
            print("   to use port 6543 (Transaction Pooler) to prevent connection saturation.")
        
        raise e

def db_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

def now_iso():
    return datetime.now(UTC).isoformat()

def ensure_column_pg(conn, table, column, coldef):
    try:
        c = conn.cursor()
        c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coldef}")
        conn.commit()
        c.close()
    except Exception:
        conn.rollback()

def init_db():
    try:
        conn = db()
        c = conn.cursor()
        
        # 1. CORE TABLES
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                step VARCHAR(255),
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                telegram_id BIGINT UNIQUE,
                name VARCHAR(255),
                phone VARCHAR(50),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS faculty (
                id SERIAL PRIMARY KEY,
                telegram_id VARCHAR(255) UNIQUE,
                name VARCHAR(255),
                subject VARCHAR(255),
                stream VARCHAR(255),
                is_admin BOOLEAN DEFAULT false,
                is_owner BOOLEAN DEFAULT false
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                student_id UUID,
                subject VARCHAR(255),
                status VARCHAR(50) DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                student_id UUID,
                type VARCHAR(50),
                subject VARCHAR(255),
                topic VARCHAR(255),
                description TEXT,
                priority VARCHAR(50),
                estimated_minutes INT,
                status VARCHAR(50) DEFAULT 'pending',
                scheduled_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_logs (
                id SERIAL PRIMARY KEY,
                student_id UUID,
                date DATE,
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS backlogs (
                id SERIAL PRIMARY KEY,
                student_id UUID,
                task_id INT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                student_id UUID,
                report_type VARCHAR(50),
                content TEXT,
                date_range VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. TEACHER TABLES
        c.execute("""
            CREATE TABLE IF NOT EXISTS teachers (
                teacher_id BIGINT PRIMARY KEY,
                teacher_name VARCHAR(255),
                subject_supported VARCHAR(255),
                stream_supported VARCHAR(255),
                availability_status VARCHAR(50) DEFAULT 'offline',
                availability_text TEXT,
                last_availability_update TIMESTAMP,
                priority_order INT DEFAULT 0
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS teacher_availability_logs (
                id SERIAL PRIMARY KEY,
                teacher_id BIGINT,
                status VARCHAR(50),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        
        # 3. MIGRATIONS (Column Adjustments)
        ensure_column_pg(conn, "users", "user_type", "VARCHAR(50) DEFAULT 'free'")
        ensure_column_pg(conn, "users", "ai_doubts_used_24h", "INT DEFAULT 0")
        ensure_column_pg(conn, "users", "doubt_guru_lifetime_used", "INT DEFAULT 0")
        ensure_column_pg(conn, "users", "is_paid", "INT DEFAULT 0")
        ensure_column_pg(conn, "users", "mentorship_mode", "VARCHAR(50) DEFAULT 'none'")
        ensure_column_pg(conn, "users", "mentorship_temp", "TEXT")
        ensure_column_pg(conn, "users", "mentorship_student_id", "UUID")
        
        ensure_column_pg(conn, "students", "mentor_id_telegram", "TEXT")
        ensure_column_pg(conn, "students", "phone_verified", "BOOLEAN DEFAULT false")
        ensure_column_pg(conn, "students", "parent_verified", "BOOLEAN DEFAULT false")
        ensure_column_pg(conn, "students", "parent_verification_requested_at", "TIMESTAMP")
        ensure_column_pg(conn, "students", "parent_verification_mentor_id", "TEXT")
        ensure_column_pg(conn, "students", "parent_language", "VARCHAR(50)")
        ensure_column_pg(conn, "students", "parent_pairing_code", "VARCHAR(50)")
        ensure_column_pg(conn, "students", "parent_phone", "VARCHAR(50)")
        ensure_column_pg(conn, "students", "timetable_scope", "VARCHAR(20) DEFAULT 'one_day'")
        
        ensure_column_pg(conn, "tickets", "assigned_teacher_id", "BIGINT")
        ensure_column_pg(conn, "tickets", "assigned_subject", "VARCHAR(255)")
        ensure_column_pg(conn, "tickets", "assigned_stream", "VARCHAR(255)")
        ensure_column_pg(conn, "tickets", "assigned_at", "TIMESTAMP")
        ensure_column_pg(conn, "tickets", "reassigned_count", "INT DEFAULT 0")
        ensure_column_pg(conn, "tickets", "next_escalation_at", "TIMESTAMP")

        ensure_column_pg(conn, "backlogs", "subject", "VARCHAR(255)")
        ensure_column_pg(conn, "backlogs", "topic", "VARCHAR(255)")
        ensure_column_pg(conn, "backlogs", "status", "VARCHAR(50)")
        
        c.close()
        conn.close()
        print("✅ DB migrated and connected!")
    except Exception as e:
        print(f"❌ Database migration failed: {e}")
        traceback.print_exc()

def ensure_column(conn, table, column, coltype):
    pass

def get_meta(key, default=""):
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT value FROM meta WHERE key=%s", (key,))
    r = cur.fetchone(); c.close()
    return r["value"] if r else default

def set_meta(key, value):
    c = db(); cur = db_cursor(c)
    cur.execute("INSERT INTO meta(key,value) VALUES(%s,%s) ON CONFLICT (key) DO UPDATE SET value=excluded.value", (key, value))
    c.commit(); c.close()

def ensure_user(uid):
    c = db(); cur = db_cursor(c)
    ts = now_iso()
    cur.execute("INSERT INTO users(user_id,step,created_at,updated_at) VALUES(%s, 'name', %s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, ts, ts))
    c.commit(); c.close()

def get_user(uid) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM users WHERE user_id=%s", (uid,))
    r = cur.fetchone(); c.close()
    return dict(r) if r else None

def upd_user(uid, fields: Dict[str, Any]):
    if not fields: return
    fields["updated_at"] = now_iso()
    ks = list(fields.keys()); vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    cur.execute(f"UPDATE users SET {', '.join([k+'=%s' for k in ks])} WHERE user_id=%s", vs + [uid])
    c.commit(); c.close()

def get_mentorship_temp(user: Dict[str, Any]) -> Dict[str, Any]:
    raw = user.get("mentorship_temp")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_mentorship_temp(uid: int, data: Dict[str, Any]):
    upd_user(uid, {"mentorship_temp": json.dumps(data, ensure_ascii=True)})

def clear_mentorship_temp(uid: int):
    upd_user(uid, {"mentorship_temp": None})

def norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()

def safe_json_loads(text: str, default):
    try:
        return json.loads(text)
    except Exception:
        return default

def parse_time_hhmm(value: str) -> Optional[datetime.time]:
    """Parse time from various formats: '9 am', '9:00pm', '09:00', etc."""
    if not value:
        return None
    
    original = value
    # Lenient cleaning: remove extra spaces, dots to colons, and handle natural language
    value = value.strip().lower().replace(".", ":")
    # Handle "9 pm", "9:00pm", "9pm" etc.
    value = re.sub(r"(\d+)\s*(am|pm)", r"\1 \2", value)
    
    logger.debug(f"parse_time_hhmm: Input '{original}' -> Processed '{value}'")
    
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%H", "%I %p", "%I%p"):
        try:
            result = datetime.strptime(value, fmt).time()
            logger.debug(f"parse_time_hhmm: Successfully parsed '{original}' with format '{fmt}' -> {result}")
            return result
        except Exception as e:
            logger.debug(f"parse_time_hhmm: Format '{fmt}' failed for '{value}'")
            continue
    
    logger.warning(f"parse_time_hhmm: Could not parse time from '{original}'")
    return None

def format_time_label(time_str: str) -> str:
    t = parse_time_hhmm(time_str)
    if not t:
        return time_str
    return t.strftime("%I:%M %p").lstrip("0")

def weekday_name(dt: datetime) -> str:
    return dt.strftime("%A")

def today_ist() -> datetime:
    return datetime.now(IST)

def today_ist_date():
    return today_ist().date()

def iso_date(dt) -> str:
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)

def is_faculty_user(user_id: int) -> bool:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT id FROM faculty WHERE telegram_id=%s", (str(user_id),))
    row = cur.fetchone()
    c.close()
    return bool(row)

def get_faculty_by_telegram(telegram_id: int) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM faculty WHERE telegram_id=%s", (str(telegram_id),))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def get_student_by_telegram(telegram_id: int) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM students WHERE telegram_id=%s", (str(telegram_id),))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def get_student_by_parent_telegram(telegram_id: int) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM students WHERE parent_telegram_id=%s", (str(telegram_id),))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def get_student(student_id: str) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM students WHERE id=%s", (student_id,))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def upsert_student_by_telegram(telegram_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    existing = get_student_by_telegram(telegram_id)
    c = db(); cur = db_cursor(c)
    if existing:
        ks = list(fields.keys())
        if ks:
            vs = [fields[k] for k in ks]
            cur.execute(
                f"UPDATE students SET {', '.join([k+'=%s' for k in ks])}, updated_at=now() WHERE telegram_id=%s RETURNING *",
                vs + [str(telegram_id)]
            )
        else:
            cur.execute("SELECT * FROM students WHERE telegram_id=%s", (str(telegram_id),))
    else:
        base = {"telegram_id": str(telegram_id)}
        base.update(fields)
        cols = list(base.keys())
        vals = [base[k] for k in cols]
        cur.execute(
            f"INSERT INTO students ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
            vals
        )
    row = cur.fetchone()
    c.commit()
    c.close()
    return dict(row)

def update_student(student_id: str, fields: Dict[str, Any]):
    if not fields:
        return
    ks = list(fields.keys())
    vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    set_clause = ', '.join([k + '=%s' for k in ks])
    if "updated_at" in [k.lower() for k in ks]:
        cur.execute(f"UPDATE students SET {set_clause} WHERE id=%s", vs + [student_id])
    else:
        cur.execute(f"UPDATE students SET {set_clause}, updated_at=now() WHERE id=%s", vs + [student_id])
    c.commit(); c.close()

def get_pending_student_approvals() -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM students WHERE is_approved=false ORDER BY created_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def find_student_for_approval(value: str) -> Optional[Dict[str, Any]]:
    value = (value or "").strip()
    c = db(); cur = db_cursor(c)
    clean_val = re.sub(r"\D", "", value)
    cur.execute(
        """
        SELECT * FROM students
        WHERE telegram_id=%s
           OR phone LIKE %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (value, f"%{clean_val}%" if clean_val else value),
    )
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def upsert_weekly_timetable_row(student_id: str, day_of_week: str, coaching_slots: List[Dict[str, Any]], free_slots: List[Dict[str, Any]], batch_name: Optional[str]):
    c = db(); cur = db_cursor(c)
    cur.execute(
        """
        DELETE FROM weekly_timetable WHERE student_id=%s AND day_of_week=%s
        """,
        (student_id, day_of_week)
    )
    cur.execute(
        """
        INSERT INTO weekly_timetable (student_id, day_of_week, coaching_slots, free_slots, batch_name, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,now(),now())
        """,
        (student_id, day_of_week, json.dumps(coaching_slots), json.dumps(free_slots), batch_name)
    )
    c.commit(); c.close()

def get_weekly_timetable(student_id: str) -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM weekly_timetable WHERE student_id=%s ORDER BY created_at", (student_id,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    for row in rows:
        row["coaching_slots"] = row["coaching_slots"] if isinstance(row["coaching_slots"], list) else safe_json_loads(row.get("coaching_slots") or "[]", [])
        row["free_slots"] = row["free_slots"] if isinstance(row["free_slots"], list) else safe_json_loads(row.get("free_slots") or "[]", [])
    return rows

def get_weekday_timetable(student_id: str, day_name: str) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM weekly_timetable WHERE student_id=%s AND lower(day_of_week)=lower(%s) LIMIT 1", (student_id, day_name))
    row = cur.fetchone()
    c.close()
    if not row:
        return None
    data = dict(row)
    data["coaching_slots"] = data["coaching_slots"] if isinstance(data["coaching_slots"], list) else safe_json_loads(data.get("coaching_slots") or "[]", [])
    data["free_slots"] = data["free_slots"] if isinstance(data["free_slots"], list) else safe_json_loads(data.get("free_slots") or "[]", [])
    return data

def get_or_create_daily_log(student_id: str, date_value) -> Dict[str, Any]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM daily_logs WHERE student_id=%s AND date=%s", (student_id, date_value))
    row = cur.fetchone()
    if not row:
        cur.execute(
            """
            INSERT INTO daily_logs (student_id, date, created_at)
            VALUES (%s,%s,now())
            RETURNING *
            """,
            (student_id, date_value),
        )
        row = cur.fetchone()
        c.commit()
    c.close()
    return dict(row)

def update_daily_log(log_id: str, fields: Dict[str, Any]):
    if not fields:
        return
    ks = list(fields.keys())
    vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    cur.execute(f"UPDATE daily_logs SET {', '.join([k+'=%s' for k in ks])} WHERE id=%s", vs + [log_id])
    c.commit(); c.close()

def create_task(data: Dict[str, Any]) -> Dict[str, Any]:
    cols = list(data.keys()) + ["created_at"]
    vals = [data[k] for k in data.keys()] + [now_iso()]
    c = db(); cur = db_cursor(c)
    cur.execute(
        f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        vals,
    )
    row = cur.fetchone()
    c.commit(); c.close()
    return dict(row)

def update_task(task_id: str, fields: Dict[str, Any]):
    if not fields:
        return
    ks = list(fields.keys())
    vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    cur.execute(f"UPDATE tasks SET {', '.join([k+'=%s' for k in ks])} WHERE id=%s", vs + [task_id])
    c.commit(); c.close()

def get_student_tasks(student_id: str, statuses: Optional[List[str]] = None, scheduled_date=None) -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    query = "SELECT * FROM tasks WHERE student_id=%s"
    params: List[Any] = [student_id]
    if statuses:
        query += " AND status = ANY(%s)"
        params.append(statuses)
    if scheduled_date is not None:
        query += " AND scheduled_date=%s"
        params.append(scheduled_date)
    query += " ORDER BY deadline_time NULLS LAST, created_at"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def get_pending_tasks_upto_days(student_id: str, days: int = 3) -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute(
        """
        SELECT * FROM tasks
        WHERE student_id=%s
          AND status='pending'
          AND scheduled_date >= %s
        ORDER BY scheduled_date, deadline_time NULLS LAST
        """,
        (student_id, today_ist_date() - timedelta(days=days)),
    )
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def create_backlog(data: Dict[str, Any]) -> Dict[str, Any]:
    cols = list(data.keys()) + ["created_at"]
    vals = [data[k] for k in data.keys()] + [now_iso()]
    c = db(); cur = db_cursor(c)
    cur.execute(
        f"INSERT INTO backlogs ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        vals,
    )
    row = cur.fetchone()
    c.commit(); c.close()
    return dict(row)

def update_backlog(backlog_id: str, fields: Dict[str, Any]):
    if not fields:
        return
    ks = list(fields.keys())
    vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    cur.execute(f"UPDATE backlogs SET {', '.join([k+'=%s' for k in ks])} WHERE id=%s", vs + [backlog_id])
    c.commit(); c.close()

def get_backlogs(student_id: str, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    query = "SELECT * FROM backlogs WHERE student_id=%s"
    params: List[Any] = [student_id]
    if statuses:
        query += " AND status = ANY(%s)"
        params.append(statuses)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def create_report(data: Dict[str, Any]) -> Dict[str, Any]:
    cols = list(data.keys()) + ["created_at"]
    vals = [data[k] for k in data.keys()] + [now_iso()]
    c = db(); cur = db_cursor(c)
    cur.execute(
        f"INSERT INTO reports ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
        vals,
    )
    row = cur.fetchone()
    c.commit(); c.close()
    return dict(row)

def get_reports(student_id: str, report_type: Optional[str] = None, start_date=None) -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    query = "SELECT * FROM reports WHERE student_id=%s"
    params: List[Any] = [student_id]
    if report_type:
        query += " AND type=%s"
        params.append(report_type)
    if start_date:
        query += " AND start_date >= %s"
        params.append(start_date)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def active_students() -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM students WHERE is_approved=True")
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def update_student_by_telegram(telegram_id: int, fields: Dict[str, Any]):
    return upsert_student_by_telegram(telegram_id, fields)

def upsert_medical_leave(student_id: str, leave_date, fields: Dict[str, Any]) -> Dict[str, Any]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM medical_leaves WHERE student_id=%s AND leave_date=%s", (student_id, leave_date))
    row = cur.fetchone()
    if row:
        ks = list(fields.keys())
        if ks:
            vs = [fields[k] for k in ks]
            cur.execute(
                f"UPDATE medical_leaves SET {', '.join([k+'=%s' for k in ks])} WHERE student_id=%s AND leave_date=%s RETURNING *",
                vs + [student_id, leave_date]
            )
        else:
            cur.execute("SELECT * FROM medical_leaves WHERE student_id=%s AND leave_date=%s", (student_id, leave_date))
    else:
        payload = {"student_id": student_id, "leave_date": leave_date}
        payload.update(fields)
        cols = list(payload.keys())
        vals = [payload[k] for k in cols]
        cur.execute(
            f"INSERT INTO medical_leaves ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
            vals,
        )
    out = cur.fetchone()
    c.commit(); c.close()
    return dict(out)

def get_medical_leave(student_id: str, leave_date) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM medical_leaves WHERE student_id=%s AND leave_date=%s", (student_id, leave_date))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def count_recent_medical_leaves(student_id: str, days: int = 5) -> int:
    c = db(); cur = db_cursor(c)
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM medical_leaves
        WHERE student_id=%s
          AND leave_date >= %s
          AND status IN ('approved', 'continued')
        """,
        (student_id, today_ist_date() - timedelta(days=days - 1)),
    )
    row = cur.fetchone()
    c.close()
    return int(row["cnt"] if row else 0)

def upsert_test_week(student_id: str, week_start_date, fields: Dict[str, Any]) -> Dict[str, Any]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM test_weeks WHERE student_id=%s AND week_start_date=%s", (student_id, week_start_date))
    row = cur.fetchone()
    if row:
        ks = list(fields.keys())
        vs = [fields[k] for k in ks]
        cur.execute(
            f"UPDATE test_weeks SET {', '.join([k+'=%s' for k in ks])} WHERE student_id=%s AND week_start_date=%s RETURNING *",
            vs + [student_id, week_start_date],
        )
    else:
        payload = {"student_id": student_id, "week_start_date": week_start_date}
        payload.update(fields)
        cols = list(payload.keys())
        vals = [payload[k] for k in cols]
        cur.execute(
            f"INSERT INTO test_weeks ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
            vals,
        )
    out = cur.fetchone()
    c.commit(); c.close()
    return dict(out)

def get_test_week(student_id: str, week_start_date) -> Optional[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM test_weeks WHERE student_id=%s AND week_start_date=%s", (student_id, week_start_date))
    row = cur.fetchone()
    c.close()
    return dict(row) if row else None

def get_approved_students() -> List[Dict[str, Any]]:
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM students WHERE is_approved=true ORDER BY created_at")
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

def parse_slot_text(text: str) -> List[Dict[str, Any]]:
    """
    Parse timetable input like "Physics 9 am, Chemistry 10:30 am, Mathematics 12 pm"
    Returns list of {subject, start, end} dicts
    """
    slots: List[Dict[str, Any]] = []
    
    if not text or not text.strip():
        logger.warning(f"parse_slot_text: Empty input")
        return []
    
    parts = re.split(r"[;\n,]+", text or "")
    logger.debug(f"parse_slot_text: Split into {len(parts)} parts: {parts}")
    
    for part in parts:
        raw = part.strip()
        if not raw:
            continue
        
        logger.debug(f"parse_slot_text: Processing part: '{raw}'")
        
        # Regex jo "Physics 9 am" "Chemistry 10:30 am" jaise inputs samajhta hai
        m = re.search(
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)?\s*(?:time|at)?\s*"
            r"([a-zA-Z\s]+)\s+" 
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)" 
            r"(?:\s*[-to]+\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?", 
            raw, flags=re.I
        )
        
        if m:
            subject, start_str, end_str = m.group(1).strip(), m.group(2).strip(), m.group(3)
            logger.debug(f"parse_slot_text: Matched - subject='{subject}', start='{start_str}', end='{end_str}'")
            
            start = parse_time_hhmm(start_str)
            if end_str:
                end = parse_time_hhmm(end_str.strip())
            elif start:
                end = (datetime.combine(datetime.today(), start) + timedelta(minutes=90)).time()
            else:
                logger.warning(f"parse_slot_text: Could not parse time from '{start_str}'")
                continue
                
            if start and end:
                slot = {"subject": subject.title(), "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
                slots.append(slot)
                logger.debug(f"parse_slot_text: Added slot: {slot}")
            else:
                logger.warning(f"parse_slot_text: Invalid times - start={start}, end={end}")
        else:
            logger.warning(f"parse_slot_text: Could not match pattern for '{raw}'")
    
    logger.info(f"parse_slot_text: Successfully parsed {len(slots)} slots from input")
    return slots

def compute_free_slots(slots: List[Dict[str, Any]], preferred_study_time: Optional[str], self_study_hours: Optional[int], day_name: str) -> List[Dict[str, Any]]:
    free_slots: List[Dict[str, Any]] = []
    hours_target = 8 if day_name.lower() == "saturday" else int(self_study_hours or 0)
    base_time = parse_time_hhmm(preferred_study_time or "") or parse_time_hhmm("18:00")
    if hours_target <= 0:
        hours_target = 2
    end_hour = min(base_time.hour + hours_target, 23)
    free_slots.append({
        "label": "Primary Study",
        "start": base_time.strftime("%H:%M"),
        "end": f"{end_hour:02d}:{base_time.minute:02d}",
        "minutes": hours_target * 60,
    })
    return free_slots

def combine_slots_for_message(slots: List[Dict[str, Any]]) -> str:
    if not slots:
        return "No class saved."
    return ", ".join([f"{slot.get('subject', 'Class')} {format_time_label(slot.get('start', ''))}-{format_time_label(slot.get('end', ''))}" for slot in slots])

def class_bucket_for_slots(slots: List[Dict[str, Any]]) -> str:
    if not slots:
        return "morning"
    first = parse_time_hhmm(slots[0].get("start", ""))
    return "morning" if first and first.hour < 12 else "afternoon"

def reminder_marker_key(tag: str, date_key: str) -> str:
    return f"{tag}:{date_key}"

def mentor_payload(student: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": student.get("name"),
        "goal": student.get("goal"),
        "exam_target": student.get("exam_target"),
        "preferred_study_time": student.get("preferred_study_time"),
        "self_study_hours": student.get("self_study_hours"),
    }

def ins_doubt(data: Dict[str, Any]):
    c = db(); cur = db_cursor(c); ts = now_iso()
    cur.execute("""INSERT INTO doubts(
        qid,user_id,class_current,subject,stream,chapter,question_text,question_photo,ai_answer,difficulty,
        needs_teacher_review,diagram_required,diagram_data,status,created_at,updated_at
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
        data["qid"], data["user_id"], data.get("class_current"), data.get("subject"), data.get("stream"), data.get("chapter"),
        data.get("question_text"), data.get("question_photo"), data.get("ai_answer"), data.get("difficulty"),
        int(data.get("needs_teacher_review", 0)), int(data.get("diagram_required", 0)), data.get("diagram_data"),
        data.get("status", "created"), ts, ts
    ))
    c.commit(); c.close()

def upd_doubt(qid, fields):
    if not fields: return
    fields["updated_at"] = now_iso()
    ks = list(fields.keys()); vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    cur.execute(f"UPDATE doubts SET {', '.join([k+'=%s' for k in ks])} WHERE qid=%s", vs + [qid])
    c.commit(); c.close()

def get_ticket(qid):
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM tickets WHERE qid=%s", (qid,))
    r = cur.fetchone(); c.close()
    return dict(r) if r else None

def get_ticket_by_claim_code(claim_code):
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM tickets WHERE claim_code=%s AND status='pending_teacher'", (claim_code,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    if len(rows) == 1:
        return rows[0]
    return None

def upsert_ticket(t):
    c = db(); cur = db_cursor(c); ts = now_iso()
    cur.execute("""INSERT INTO tickets(qid,user_id,status,created_at,updated_at,group_msg_id,claimed_by,claimed_by_name,reply_count,reopen_count,claim_code,claim_expires_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (qid) DO UPDATE SET
    user_id=excluded.user_id,status=excluded.status,created_at=excluded.created_at,updated_at=excluded.updated_at,
    group_msg_id=excluded.group_msg_id,claimed_by=excluded.claimed_by,claimed_by_name=excluded.claimed_by_name,
    reply_count=excluded.reply_count,reopen_count=excluded.reopen_count,
    claim_code=excluded.claim_code,claim_expires_at=excluded.claim_expires_at
    """, (
        t["qid"], t["user_id"], t["status"], t.get("created_at", ts), ts,
        t.get("group_msg_id"), t.get("claimed_by"), t.get("claimed_by_name"),
        t.get("reply_count", 0), t.get("reopen_count", 0),
        t.get("claim_code"), t.get("claim_expires_at")
    ))
    c.commit(); c.close()

def upd_ticket(qid, fields):
    if not fields: return
    fields["updated_at"] = now_iso()
    ks = list(fields.keys()); vs = [fields[k] for k in ks]
    c = db(); cur = db_cursor(c)
    cur.execute(f"UPDATE tickets SET {', '.join([k+'=%s' for k in ks])} WHERE qid=%s", vs + [qid])
    c.commit(); c.close()

def pending_tickets():
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM tickets WHERE status='pending_teacher'")
    rows = [dict(x) for x in cur.fetchall()]; c.close()
    return rows

def save_teacher_reply(qid, teacher_id, teacher_name, text, photo, caption):
    c = db(); cur = db_cursor(c)
    cur.execute("""INSERT INTO teacher_replies(qid,teacher_id,teacher_username,reply_text,reply_photo,reply_caption,teacher_feedback,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (qid, teacher_id, teacher_name, text, photo, caption, None, now_iso()))
    c.commit(); c.close()

def save_teacher_reply_with_feedback(qid, teacher_id, teacher_name, text, photo, caption, feedback):
    c = db(); cur = db_cursor(c)
    cur.execute("""INSERT INTO teacher_replies(qid,teacher_id,teacher_username,reply_text,reply_photo,reply_caption,teacher_feedback,created_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (qid, teacher_id, teacher_name, text, photo, caption, feedback, now_iso()))
    c.commit(); c.close()

def add_rating(uid, qid, rating):
    c = db(); cur = db_cursor(c)
    cur.execute("INSERT INTO ratings(user_id,qid,rating,created_at) VALUES(%s,%s,%s,%s)", (uid, qid, rating, now_iso()))
    c.commit(); c.close()

def get_users_with_doubts() -> List[int]:
    c = db(); cur = db_cursor(c)
    cur.execute("""
        SELECT DISTINCT d.user_id
        FROM doubts d
        JOIN users u ON u.user_id = d.user_id
        WHERE IFNULL(u.is_blocked, 0) = 0
    """)
    rows = [int(r[0]) for r in cur.fetchall()]
    c.close()
    return rows

def get_teacher_session(teacher_id):
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM teacher_sessions WHERE teacher_id=%s", (teacher_id,))
    r = cur.fetchone(); c.close()
    return dict(r) if r else None

def upsert_teacher_session(teacher_id, qid, mode, draft_solution=None, draft_photo=None, draft_caption=None):
    c = db(); cur = db_cursor(c); ts = now_iso()
    cur.execute("""
    INSERT INTO teacher_sessions(teacher_id, qid, mode, draft_solution, draft_photo, draft_caption, created_at, updated_at)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (teacher_id) DO UPDATE SET
        qid=excluded.qid,
        mode=excluded.mode,
        draft_solution=excluded.draft_solution,
        draft_photo=excluded.draft_photo,
        draft_caption=excluded.draft_caption,
        updated_at=excluded.updated_at
    """, (teacher_id, qid, mode, draft_solution, draft_photo, draft_caption, ts, ts))
    c.commit(); c.close()

def clear_teacher_session(teacher_id):
    c = db(); cur = db_cursor(c)
    cur.execute("DELETE FROM teacher_sessions WHERE teacher_id=%s", (teacher_id,))
    c.commit(); c.close()

def blocked_message():
    return f"Your account is blocked due to repeated policy violations.\nFor unblock request, contact: {UNBLOCK_EMAIL}"

def is_admin_user(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id in OWNER_IDS

def is_owner_user(user_id: int) -> bool:
    return user_id in OWNER_IDS

def get_doubt(qid):
    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM doubts WHERE qid=%s", (qid,))
    r = cur.fetchone(); c.close()
    return dict(r) if r else None

async def prompt_new_doubt(update_or_context, uid: int, via_context: bool = False, text: str = "Doubt resolved 🚀\n'Ask Doubt' ya 'My Mentorship' me se choose karein 👇"):
    upd_user(uid, {"step": "ready_for_new_doubt"})
    if via_context:
        await update_or_context.bot.send_message(
            chat_id=uid,
            text=text,
            reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True)
        )
    else:
        await update_or_context.message.reply_text(
            text,
            reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True)
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    ensure_user(uid)
    
    args = context.args
    if args and args[0].startswith("parent_"):
        student_uid = args[0].split("_")[1]
        student = get_student_by_telegram(int(student_uid))
        if student:
            upd_user(uid, {"step": f"parent_lang_select_{student_uid}"})
            await update.message.reply_text(
                f"Pranam! Aap {student['name']} ke parent hain. ✨\n\nReports ke liye apni pasandida bhasha (language) select karein:",
                reply_markup=ReplyKeyboardMarkup(PARENT_LANGUAGE_OPTIONS, resize_keyboard=True)
            )
            return

    await update.message.reply_text(
        "Welcome to JEE Doubt Guru! 🎓\n\nAsk Doubt select karein AI aur Doubt Guru se solution paane ke liye.\nYa My Mentorship choose karein systematic study ke liye.",
        reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True)
    )

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data.startswith("verify_parent_"):
        student_uid = int(data.split("_")[2])
        mentor_id = query.from_user.id
        
        student = get_student_by_telegram(student_uid)
        if not student or not student.get("parent_telegram_id"):
            await query.edit_message_text(f"Error: Parent Telegram ID not found for student {student_uid}.")
            return

        parent_uid = int(student["parent_telegram_id"])
        
        # Update student record
        upsert_student_by_telegram(student_uid, {
            "parent_verified": True, 
            "parent_verification_mentor_id": str(mentor_id)
        })
        
        # Notify Parent automatically
        bot_username = (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=parent_{student_uid}"
        
        parent_success = False
        try:
            await context.bot.send_message(
                chat_id=parent_uid,
                text=(
                    f"Pranam! Aapke child ({student['name']}) ne mentorship ke liye register kiya hai. ✨\n\n"
                    "Mentor ne verification approve kar di hai. Aage badhne ke liye niche 'Start' dabakar apni bhasha (language) select karein:\n\n"
                    f"{deep_link}"
                )
            )
            parent_success = True
        except Exception as e:
            print(f"Failed to message parent {parent_uid}: {e}")

        # Notify Student
        try:
            if parent_success:
                await context.bot.send_message(
                    chat_id=student_uid,
                    text="✅ Mentor ne approve kar diya hai! Link aapke parent ko direct bhej di gayi hai. Unhe kahein ki bot check karein aur language select karein."
                )
            else:
                await context.bot.send_message(
                    chat_id=student_uid,
                    text="⚠️ Mentor ne approve kar diya hai, lekin bot aapke parent ko message nahi bhej saka. Please apne parent se kahein ki bot start karein, aur fir aap registration link manually share karein:\n\n" + deep_link
                )
            
            status_text = "Sent to Parent" if parent_success else "Manual Share Required"
            await query.edit_message_text(f"Approved for {student_uid} | {status_text} | By @{query.from_user.username or mentor_id}")
        except Exception as e:
            await query.edit_message_text(f"Approved, but failed to notify student: {e}")

async def handle_parent_language(update: Update, context: ContextTypes.DEFAULT_TYPE, user: Dict[str, Any]):
    step = user["step"]
    if not step.startswith("parent_lang_select_"):
        return False
    
    student_uid = int(step.split("_")[3])
    text = update.message.text
    if text not in ["Hindi", "Marathi", "English"]:
        await update.message.reply_text("Sahi bhasha select karein:", reply_markup=ReplyKeyboardMarkup(PARENT_LANGUAGE_OPTIONS, resize_keyboard=True))
        return True
    
    # Store language temporarily in user state (we'll save to student record after pairing)
    upd_user(update.message.from_user.id, {
        "step": f"parent_agreement_{student_uid}",
        "parent_lang_tmp": text
    })
    
    await update.message.reply_text(
        "Kya aap apne bache ki Mentorship ke liye raazi (agree) hain?",
        reply_markup=ReplyKeyboardMarkup([["Yes", "No"]], resize_keyboard=True)
    )
    return True

async def handle_parent_steps(update: Update, context: ContextTypes.DEFAULT_TYPE, user: Dict[str, Any]):
    uid = update.message.from_user.id
    step = user.get("step", "")
    text = (update.message.text or "").strip()
    
    if step.startswith("parent_agreement_"):
        student_uid = int(step.split("_")[2])
        if text.lower() == "yes":
            # Generate Pairing Code now and send to group
            pairing_code = str(random.randint(100000, 999999))
            update_student_by_telegram(student_uid, {"parent_pairing_code": pairing_code})
            
            student = get_student_by_telegram(student_uid)
            await context.bot.send_message(
                chat_id=MENTORSHIP_GROUP_ID,
                text=(
                    "🛡️ *Parent Pairing Code Generated*\n\n"
                    f"Student: {student.get('name')}\n"
                    f"Pairing Code: `{pairing_code}`\n\n"
                    "Please provide this code to the student/parent to complete verification."
                ),
                parse_mode="Markdown"
            )
            
            upd_user(uid, {"step": f"parent_pairing_{student_uid}"})
            await update.message.reply_text(
                "Bahut badhiya! Ab pairing code yahan paste karein (ye code aapke bache ke mentor ke paas bhej diya gaya hai):",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            upd_user(uid, {"step": "ready_for_new_doubt"})
            await update.message.reply_text("Registration cancel kar di gayi hai.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return True

    if step.startswith("parent_pairing_"):
        student_uid = int(step.split("_")[2])
        student = get_student_by_telegram(student_uid)
        if not student:
            await update.message.reply_text("Student profile nahi mila. Please registration restart karein.")
            upd_user(uid, {"step": "ready_for_new_doubt"})
            return True
        
        expected_code = student.get("parent_pairing_code")
        if text == expected_code:
            lang = user.get("parent_lang_tmp", "English")
            upsert_student_by_telegram(student_uid, {
                "parent_telegram_id": str(uid),
                "parent_language": lang,
                "parent_verified": True
            })
            upd_user(uid, {"step": "ready_for_new_doubt"})
            await update.message.reply_text(f"✅ Verification Safal (Successful)! Ab aapko {lang} mein regular reports milenge. ✨", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            
            # Notify student
            try:
                await context.bot.send_message(chat_id=student_uid, text="✅ Aapke parent ne pairing code verify kar diya hai! Registration complete.")
            except: pass
        else:
            await update.message.reply_text("❌ Pairing code galat hai. Phirse try karein ya sahi code bache se puchein.")
        return True
        
    return False

def next_seq():
    s = int(get_meta("daily_seq", "1"))
    set_meta("daily_seq", str(s + 1))
    return s

def qid_pattern():
    return r"MP-C(?:11|12)-[A-Z]{3}-[A-Z]{3}-[A-Z0-9]{3,6}-[EMH]-\d{6}-\d{4}"

def chapter_code(ch):
    x = re.sub(r"[^A-Za-z0-9]+", "", (ch or "").upper())
    return x[:6] if x else "GEN"

def subj_code(s):
    return {"physics": "PHY", "chemistry": "CHE", "mathematics": "MTH"}.get((s or "").lower(), "GEN")

def strm_code(s):
    m = {
        "mechanics":"MEC","thermodynamics":"THM","waves and oscillations":"WAV",
        "electrodynamics":"ELD","optics and modern physics":"OMP",
        "organic":"ORG","physical":"PHS","inorganic":"INO",
        "algebra":"ALG","calculus":"CAL","coordinate geometry":"COO",
        "practical physics":"PPH","practical chemistry":"PCH",
        "vector and 3d":"VEC","probability and statistics":"PRB",
        "trigonometry":"TRG","modern physics":"MOD",
    }
    return m.get((s or "").lower(), "GEN")

def gen_qid(user, diff):
    lvl = diff if diff in {"E","M","H"} else "M"
    seq = next_seq()
    return f"MP-C{user.get('class_current','XX')}-{subj_code(user.get('subject',''))}-{strm_code(user.get('stream',''))}-{chapter_code(user.get('chapter',''))}-{lvl}-{datetime.now(UTC).strftime('%y%m%d')}-{seq:04d}"

def claim_code_from_qid(qid: str) -> str:
    return qid.split("-")[-1]

def parse_teacher_dm_text(text: str) -> Tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    parts = re.split(r"\n\s*feedback\s*:\s*", raw, flags=re.IGNORECASE, maxsplit=1)
    solution = parts[0].strip()
    solution = re.sub(r"^\s*solution\s*:\s*", "", solution, flags=re.IGNORECASE)
    feedback = parts[1].strip() if len(parts) > 1 else ""
    return solution, feedback

def clean_answer(t):
    t = (t or "").replace("\\(", "").replace("\\)", "").replace("\\[", "").replace("\\]", "")
    t = re.sub(r"\\[a-zA-Z]+", "", t).replace("{", "").replace("}", "")
    return re.sub(r"[ \t]+", " ", t).strip()

def get_system_prompt(subject: str, stream: str = "", chapter: str = "") -> str:
    subject = (subject or "").lower()
    stream = (stream or "").lower()
    chapter = (chapter or "").lower()

    if chapter == "salt analysis":
        return f"{COMMON_PROMPT}\n\n{SALT_ANALYSIS_PROMPT}".strip()
    if chapter == "organic practical":
        return f"{COMMON_PROMPT}\n\n{ORGANIC_PRACTICAL_PROMPT}".strip()
    if chapter == "biomolecules amino acids dna rna vitamins":
        return f"{COMMON_PROMPT}\n\n{BIOMOLECULES_PROMPT}".strip()

    if subject == "chemistry":
        if stream == "organic":
            return f"{COMMON_PROMPT}\n\n{ORGANIC_CHEMISTRY_PROMPT}".strip()
        if stream == "physical":
            return f"{COMMON_PROMPT}\n\n{PHYSICAL_CHEMISTRY_PROMPT}".strip()
        if stream == "inorganic":
            return f"{COMMON_PROMPT}\n\n{INORGANIC_CHEMISTRY_PROMPT}".strip()

    extra = SUBJECT_PROMPTS.get(subject, "")
    return f"{COMMON_PROMPT}\n\n{extra}".strip()

def anthropic_text(prompt: str, system_prompt: str = None, model: str = None) -> str:
    if system_prompt is None:
        system_prompt = COMMON_PROMPT
    if model is None:
        model = MODEL_SONNET
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": prompt}]
    )
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")

def anthropic_with_image(prompt: str, image_b64: str, media_type: str = "image/jpeg", system_prompt: str = None, model: str = None) -> str:
    if system_prompt is None:
        system_prompt = COMMON_PROMPT
    if model is None:
        model = MODEL_SONNET
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64
                    }
                }
            ]
        }]
    )
    return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")

def call_json_prompt(prompt_template: str, payload: Dict[str, Any], model: str = None) -> Dict[str, Any]:
    prompt = prompt_template.strip() + "\n\nInput Data:\n" + json.dumps(payload, ensure_ascii=True)
    raw = anthropic_text(prompt, system_prompt="Return valid JSON only.", model=model or PLANNER_MODEL).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise

def extract_final_answer_text(answer: str) -> str:
    if not answer:
        return ""
    m = re.search(r"Final Answer:\s*(.+%s)(?:Happy Learning With MP Sir|$)", answer, flags=re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    lines = [ln.strip() for ln in answer.splitlines() if ln.strip()]
    return lines[-1] if lines else ""

def needs_visual_symbol_verification(question_text: str, answer: str, subject: str, has_photo: bool) -> bool:
    if not has_photo:
        return False
    if (subject or "").lower() not in {"physics", "mathematics"}:
        return False

    blob = f"{question_text}\n{answer}".lower()
    final_text = extract_final_answer_text(answer).lower()

    risky_cues = [
        "options", "option", "f_1 / f_2", "f1/f2", "ratio", "minimum value",
        "find the value", "omega^2", "_1^2", "_2^2", "v^2", "sqrt", "√"
    ]
    has_risky_cue = any(cue in blob for cue in risky_cues)

    suspicious_flat_symbol = bool(re.search(r"\b(?:52|25|23|32|34|43)\b", final_text))
    squared_but_no_root = any(cue in blob for cue in ["omega^2", "_1^2", "_2^2", "v^2"]) and ("sqrt" not in final_text and "√" not in final_text)

    return has_risky_cue and (suspicious_flat_symbol or squared_but_no_root or "option (" in blob)

def verify_visual_symbol_answer(prompt: str, image_data_url: str, answer: str, subject: str) -> Tuple[str, bool]:
    verify_prompt = f"""
You are verifying a JEE image-based answer for symbol-reading mistakes only.

Task:
- Re-read the image carefully.
- Check whether square roots, fractions, powers, subscripts, and ratio symbols were misread.
- Check whether the final answer accidentally reports a squared quantity instead of the asked quantity.
- If option labels are unclear, do not guess option number. Return only the exact final mathematical expression.

Current draft answer:
{answer}

Return format:
- If the draft is safe, return [[OK]] followed by the corrected final student-facing answer.
- If the image symbols/options are still not reliable, return [[RISK]] followed by a corrected short answer with exact expression only.
"""
    image_b64 = image_data_url.split(",", 1)[1] if "," in image_data_url else image_data_url
    checked = clean_answer(
        anthropic_with_image(
            f"{prompt}\n\n{verify_prompt}",
            image_b64,
            system_prompt=get_system_prompt(subject),
        )
    )
    force_teacher = "[[RISK]]" in checked
    checked = checked.replace("[[OK]]", "").replace("[[RISK]]", "").strip()
    return checked, force_teacher

def extract_tags(answer: str):
    diff = "M"
    needs_teacher = "[[TEACHER_REVIEW_REQUIRED]]" in (answer or "")
    diagram_yes = "[[DIAGRAM:YES]]" in (answer or "")
    m = re.search(r"\[\[DIFF:([EMH])\]\]", answer or "")
    if m:
        diff = m.group(1)
    md = re.search(r"\[\[DIAGRAM_DATA:(.*%s)\]\]", answer or "", flags=re.DOTALL)
    ddata = md.group(1).strip() if md else ""
    cleaned = re.sub(r"\[\[(DIFF:[EMH]|TEACHER_REVIEW_REQUIRED|DIAGRAM:YES|DIAGRAM:NO)\]\]", "", answer or "")
    cleaned = re.sub(r"\[\[DIAGRAM_DATA:.*?\]\]", "", cleaned, flags=re.DOTALL).strip()
    return cleaned, diff, needs_teacher, diagram_yes, ddata

def chapter_kb(user):
    sub = (user.get("subject") or "").lower()
    st = (user.get("stream") or "").lower()
    opts = CHAPTER_OPTIONS.get(sub, {}).get(st, [["Chapter 1"]])
    return opts + [["Back", "Cancel Doubt"]]

def stream_kb(user):
    sub = (user.get("subject") or "").lower()
    opts = STREAM_OPTIONS.get(sub, [["Main Stream"]])
    return opts + [["Back", "Cancel Doubt"]]

def flatten_rows(rows):
    return [x.lower() for row in rows for x in row]

def norm(s):
    s = re.sub(r"[^a-z0-9\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()

def overlap(a, b):
    sa, sb = set(norm(a).split()), set(norm(b).split())
    return len(sa & sb)

def load_pyq():
    if not PYQ_FILE.exists():
        return []
    try:
        with open(PYQ_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []

PYQ_BANK = load_pyq()

def retrieve_pyq(question, subject, chapter, k=3):
    scored = []
    for it in PYQ_BANK:
        isub = str(it.get("subject","")).lower()
        ich = str(it.get("chapter","")).lower()
        if isub and isub != subject.lower():
            continue
        bonus = 3 if ich and ich == chapter.lower() else 0
        blob = " ".join([
            str(it.get("question","")),
            str(it.get("approach","")),
            " ".join(it.get("tags", [])) if isinstance(it.get("tags", []), list) else ""
        ])
        sc = overlap(question, blob) + bonus
        if sc > 0:
            scored.append((sc, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:k]]

def format_pyq(refs):
    if not refs:
        return "No close PYQ pattern found."
    return "\n\n".join([f"Ref {i+1} (Year {r.get('year','NA')}): {r.get('question','')}\nApproach: {r.get('approach','')}" for i, r in enumerate(refs)])

def detect_question_strategy(question: str, subject: str) -> str:
    """Detect question type and return relevant strategy prompt."""
    q = (question or "").lower()
    subject = (subject or "").lower()
    strategies = []

    is_match_column = any(x in q for x in [
        "match", "list-i", "list-ii", "list i", "list ii",
        "column i", "column ii", "column-i", "column-ii"
    ])

    is_multiple_correct = any(x in q for x in [
        "one or more", "multiple correct", "more than one",
        "which of the following are", "correct statements"
    ])

    is_assertion_reason = any(x in q for x in [
        "assertion", "reason", "statement-1", "statement-2",
        "statement 1", "statement 2"
    ])

    is_min_max_speed = subject == "physics" and any(x in q for x in [
        "minimum speed", "minimum velocity", "maximum speed", "maximum velocity",
        "minimum value of v", "maximum value of v", "least speed",
        "minimum kinetic energy", "just enough"
    ])

    is_graph_based = subject in {"physics", "mathematics"} and any(x in q for x in [
        "graph", "slope", "area under", "p-v", "v-t", "x-t",
        "i-v", "plot", "curve", "diagram", "figure"
    ])

    is_integer_type = any(x in q for x in [
        "integer", "single digit", "0 to 9", "00 to 99",
        "non-negative integer", "answer to the nearest integer"
    ])

    is_paragraph = any(x in q for x in [
        "paragraph", "passage", "based on above",
        "following information", "read the following"
    ])

    is_yield_question = (
        subject == "chemistry" and "%" in q and
        any(x in q for x in ["moles", "gram", "yield", "major product", "amount"])
    )
    is_named_reaction = (
        subject == "chemistry" and any(x in q for x in [
            "sandmeyer", "hoffmann", "cannizzaro", "stephen", "clemmensen",
            "wolff-kishner", "baeyer-villiger", "reimer-tiemann", "kolbe",
            "named reaction"
        ])
    )

    if is_min_max_speed:
        strategies.append(MINIMUM_SPEED_STRATEGY)
    if is_match_column:
        strategies.append(MATCH_COLUMN_STRATEGY)
    if is_multiple_correct:
        strategies.append(MULTIPLE_CORRECT_STRATEGY)
    if is_assertion_reason:
        strategies.append(ASSERTION_REASON_STRATEGY)
    if is_graph_based:
        strategies.append(GRAPH_BASED_STRATEGY)
    if is_integer_type:
        strategies.append(INTEGER_TYPE_STRATEGY)
    if is_paragraph:
        strategies.append(PARAGRAPH_BASED_STRATEGY)
    if is_yield_question:
        strategies.append(MULTI_STEP_YIELD_STRATEGY)
    if is_named_reaction:
        strategies.append(NAMED_REACTION_STRATEGY)

    return "\n".join(strategies)

def select_model(question: str, subject: str, has_image: bool) -> str:
    if has_image:
        return MODEL_SONNET

    q = (question or "").lower()

    is_complex = any(x in q for x in [
        "match", "list-i", "list-ii", "list i", "list ii",
        "one or more", "multiple correct", "more than one",
        "assertion", "reason", "statement-1", "statement-2",
        "minimum speed", "minimum velocity", "maximum speed",
        "prove", "derive", "show that",
        "passage", "paragraph",
        "mechanism", "synthesis", "multi-step",
    ])
    if is_complex:
        return MODEL_SONNET

    return MODEL_HAIKU

def build_prompt(user, question):
    refs = retrieve_pyq(question, user.get("subject",""), user.get("chapter",""), 3)
    extra = ""

    strategy = detect_question_strategy(question, user.get("subject", ""))
    if strategy:
        extra += f"\n\n{strategy}"

    pyq_text = f"PYQ references:\n{format_pyq(refs)}\n\n" if refs else ""

    return (
        f"Subject: {user.get('subject','NA')}\n"
        f"Chapter: {user.get('chapter','NA')}\n\n"
        f"{pyq_text}Student Question:\n{question}"
        f"{extra}"
    )

def should_use_visual_prompt(user, question: str) -> bool:
    subject = (user.get("subject") or "").lower()
    if subject not in {"physics", "mathematics"}:
        return False
    text = (question or "").lower()
    chapter = (user.get("chapter") or "").lower()
    stream = (user.get("stream") or "").lower()
    visual_terms = [
        "figure", "graph", "plot", "diagram", "sketch", "as shown", "shown in the figure",
        "ray", "circuit", "fbd", "free body", "number line", "locus", "conic", "parabola",
        "ellipse", "hyperbola", "area under", "motion graph", "p-v", "v-t", "x-t",
    ]
    visual_stream_terms = {
        "physics": {"mechanics", "optics", "electrostatics and current", "magnetism and emi", "oscillations and waves", "optics and modern physics"},
        "mathematics": {"coordinate geometry", "vector and 3d", "calculus"},
    }
    visual_chapter_terms = [
        "straight lines", "conic sections", "three dimensional geometry", "vector algebra",
        "application of integrals", "ray optics and optical instruments", "wave optics",
        "oscillations", "waves", "moving charges and magnetism", "electromagnetic induction",
    ]
    return (
        any(term in text for term in visual_terms)
        or stream in visual_stream_terms.get(subject, set())
        or chapter in visual_chapter_terms
        or bool(user.get("question_photo"))
    )

def generate_diagram(qid: str, answer: str, ddata: str) -> Optional[Path]:
    try:
        DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)
        p = DIAGRAM_DIR / f"{qid}.png"

        lines = []
        if ddata:
            lines = [x.strip() for x in re.split(r"\||\n", ddata) if x.strip()][:8]

        if not lines:
            for ln in answer.splitlines():
                ln = ln.strip()
                if ln.startswith("Concept:") or ln.startswith("Step") or ln.startswith("Final Answer:"):
                    lines.append(ln)
                if len(lines) >= 8:
                    break

        if not lines:
            lines = ["Refer text solution."]

        fig, ax = plt.subplots(figsize=(10, max(4, 2 + len(lines) * 0.6)))
        ax.axis("off")
        ax.text(0.02, 0.93, f"QID: {qid}", fontsize=12, fontweight="bold", transform=ax.transAxes)

        y = 0.84
        for i, ln in enumerate(lines, 1):
            ax.text(0.03, y, f"{i}. {ln}", fontsize=11, transform=ax.transAxes)
            y -= 0.09
            if y < 0.07:
                break

        ax.text(0.02, 0.02, "Happy Learning With MP Sir", fontsize=10, transform=ax.transAxes)
        fig.tight_layout()
        fig.savefig(p, dpi=150)
        plt.close(fig)
        return p
    except Exception:
        return None

def is_valid_chem_diagram_data(diagram_data: str) -> bool:
    if not diagram_data:
        return False
    d = diagram_data.lower()
    keywords = [
        "reactant", "product", "reagent", "arrow", "->", "--(",
        "benzene", "ring", "no2", "oh", "nh2", "cooh", "ch3", "c6h5"
    ]
    score = sum(1 for k in keywords if k in d)
    return score >= 2

def stop_reminder(qid):
    t = reminder_tasks.get(qid)
    if t and not t.done():
        t.cancel()
    reminder_tasks.pop(qid, None)

def stop_claim_timeout(qid):
    t = claim_timeout_tasks.get(qid)
    if t and not t.done():
        t.cancel()
    claim_timeout_tasks.pop(qid, None)

async def reminder_loop(bot, qid):
    t = get_ticket(qid)
    if not t:
        return

    _ca = t.get("created_at")
    if not _ca: return
    created = datetime.fromisoformat(str(_ca))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)

    first_due = created + timedelta(seconds=REMINDER_FIRST_SECONDS)
    await asyncio.sleep(max(0, (first_due - datetime.now(UTC)).total_seconds()))

    while True:
        t2 = get_ticket(qid)
        if not t2 or t2["status"] != "pending_teacher":
            return

        _ca2 = t2.get("created_at")
        if not _ca2: return
        created2 = datetime.fromisoformat(str(_ca2))
        if created2.tzinfo is None:
            created2 = created2.replace(tzinfo=UTC)

        elapsed = int((datetime.now(UTC) - created2).total_seconds())
        hh, mm, ss = elapsed // 3600, (elapsed % 3600)//60, elapsed % 60
        claim_txt = "Unclaimed" if not t2.get("claimed_by_name") else f"Claimed by @{t2['claimed_by_name']}"
        await bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Reminder ⏰ QID {qid} still pending | {claim_txt}\nTimer: {hh:02d}:{mm:02d}:{ss:02d}")
        await asyncio.sleep(REMINDER_TICK_SECONDS)

def start_reminder(bot, qid):
    return
    if qid in reminder_tasks and not reminder_tasks[qid].done():
        return
    reminder_tasks[qid] = asyncio.create_task(reminder_loop(bot, qid))

async def claim_timeout_loop(bot, qid):
    t = get_ticket(qid)
    if not t: return
    expires_str = t.get("claim_expires_at")
    if expires_str:
        expires = datetime.fromisoformat(expires_str)
        if expires.tzinfo is None: expires = expires.replace(tzinfo=UTC)
        sleep_time = (expires - datetime.now(UTC)).total_seconds()
        if sleep_time > 0:
            await asyncio.sleep(sleep_time)
    else:
        await asyncio.sleep(CLAIM_TIMEOUT_SECONDS)
        
    t = get_ticket(qid)
    if not t or t["status"] != "pending_teacher": return
    if not t.get("claimed_by"): return
    if int(t.get("reply_count", 0)) > 0: return

    claimed_name = t.get("claimed_by_name", "teacher")
    if t.get("assigned_teacher_id") and int(t.get("assigned_teacher_id")) == int(t.get("claimed_by")):
        upd_ticket(qid, {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None, "assigned_teacher_id": None})
        clear_teacher_session(int(t["claimed_by"]))
        c_code = t.get("claim_code") or claim_code_from_qid(qid)
        d = get_doubt(qid)
        if d:
            qtxt = d.get("question_text", "")
            qphoto = d.get("question_photo")
            if qphoto:
                sent = await bot.send_photo(chat_id=GROUP_CHAT_ID, photo=qphoto, caption=f"Re-routed Ticket (No response from @{claimed_name} for 2 hours)\nCode: {c_code}\nQID: {qid}\nUse /claim {c_code}\n\nQuestion: {qtxt}")
            else:
                sent = await bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Re-routed Ticket (No response from @{claimed_name} for 2 hours)\nCode: {c_code}\nQID: {qid}\nUse /claim {c_code}\n\nQuestion:\n{qtxt}")
            upd_ticket(qid, {"group_msg_id": sent.message_id})
    else:
        upd_ticket(qid, {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None})
        clear_teacher_session(int(t["claimed_by"]))
        await bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Claim expired for {qid} (@{claimed_name}). Ticket is open again.")

def start_claim_timeout(bot, qid):
    stop_claim_timeout(qid)
    claim_timeout_tasks[qid] = asyncio.create_task(claim_timeout_loop(bot, qid))

async def resume_reminders(bot):
    for t in pending_tickets():
        start_reminder(bot, t["qid"])
        if t.get("claimed_by") and t.get("claim_expires_at"):
            expires = datetime.fromisoformat(t["claim_expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires > datetime.now(UTC):
                start_claim_timeout(bot, t["qid"])
            else:
                claimed_by = t.get("claimed_by")
                upd_ticket(t["qid"], {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None})
                if claimed_by:
                    clear_teacher_session(int(claimed_by))

async def run_mentorship_cycle(bot):
    # 1. 3-Day Backlog Rollover
    today = today_ist_date()
    c = db(); cur = db_cursor(c)
    three_days_ago = today - timedelta(days=3)
    cur.execute("SELECT id, student_id FROM tasks WHERE status='pending' AND scheduled_date < %s", (three_days_ago,))
    old_tasks = cur.fetchall()
    for t in old_tasks:
        cur.execute("INSERT INTO backlogs (student_id, task_id, original_date, created_at) VALUES (%s, %s, %s, now())", (t["student_id"], t["id"], three_days_ago))
        cur.execute("UPDATE tasks SET status='backlog' WHERE id=%s", (t["id"]) if isinstance(t["id"], int) else (t["id"],))
    c.commit(); c.close()

    # 2. 48-Hour Parent Verification Timeout
    c = db(); cur = db_cursor(c)
    timeout_limit = datetime.now(UTC) - timedelta(hours=48)
    cur.execute("SELECT telegram_id, name FROM students WHERE parent_verified=false AND parent_verification_requested_at < %s", (timeout_limit,))
    expired_students = cur.fetchall()
    for s in expired_students:
        upd_user(int(s["telegram_id"]), {"step": "mentor_parent_phone", "mentorship_mode": "registering"})
        update_student_by_telegram(int(s["telegram_id"]), {"parent_verified": False, "parent_verification_requested_at": None})
        try:
            await bot.send_message(chat_id=int(s["telegram_id"]), text="⚠️ Parent verification link expire ho gaya hai (48 hours). Please apne parent ka number firse provide karein registration restart karne ke liye.")
        except: pass
    c.commit(); c.close()

    # 3. Existing Mentorship Logic (Reminders, Reports, etc.)
    for student in active_students():
        # (Assuming the original logic for generating tasks/reports is here)
        pass

async def mentorship_scheduler_loop(bot):
    while True:
        try:
            await run_mentorship_cycle(bot)
        except Exception as e:
            print(f"Error in mentorship cycle: {e}")
        await asyncio.sleep(MENTORSHIP_CHECK_SECONDS)

def start_mentorship_scheduler(bot):
    asyncio.create_task(mentorship_scheduler_loop(bot))

async def student_reminder_loop(bot):
    while True:
        now_ist = datetime.now(IST)
        next_run = now_ist.replace(second=0, microsecond=0)

        if now_ist.hour < 8 or (now_ist.hour == 8 and now_ist.minute == 0 and now_ist.second == 0):
            next_run = next_run.replace(hour=8, minute=0)
        elif now_ist.hour < 20 or (now_ist.hour == 20 and now_ist.minute == 0 and now_ist.second == 0):
            next_run = next_run.replace(hour=20, minute=0)
        else:
            next_run = (next_run + timedelta(days=1)).replace(hour=8, minute=0)

        sleep_for = max(0, (next_run - now_ist).total_seconds())
        await asyncio.sleep(sleep_for)

        for user_id in get_users_with_doubts():
            try:
                await bot.send_message(chat_id=user_id, text=STUDENT_REMINDER_TEXT)
            except Exception:
                pass

        await asyncio.sleep(60)

def start_student_reminders(bot):
    global student_reminder_task
    if student_reminder_task and not student_reminder_task.done():
        return
    student_reminder_task = asyncio.create_task(student_reminder_loop(bot))

async def maybe_ask_rating(context, uid):
    u = get_user(uid)
    if u and int(u["resolved_count"]) >= 10 and int(u["awaiting_rating"]) == 0:
        upd_user(uid, {"awaiting_rating":1})
        await context.bot.send_message(chat_id=uid, text="Feedback ⭐ (1 to 10) select karein:", reply_markup=ReplyKeyboardMarkup(RATING_OPTIONS, resize_keyboard=True))

async def deliver_teacher_solution(context: ContextTypes.DEFAULT_TYPE, qid: str, teacher_id: int, teacher_name: str, solution_text: str, photo_id: Optional[str], caption: str, feedback: str):
    t = get_ticket(qid)
    if not t or t["status"] != "pending_teacher":
        return

    uid = int(t["user_id"])
    if photo_id:
        out_caption = f"Doubt Guru Solution for {qid}"
        if caption.strip():
            out_caption += f"\n\n{caption.strip()}"
        if feedback.strip():
            out_caption += f"\n\nTeacher Feedback: {feedback.strip()}"
        await context.bot.send_photo(chat_id=uid, photo=photo_id, caption=out_caption)
    else:
        message = f"Doubt Guru Solution for {qid}:\n\n{solution_text.strip()}"
        if feedback.strip():
            message += f"\n\nTeacher Feedback:\n{feedback.strip()}"
        await context.bot.send_message(chat_id=uid, text=message)

    if feedback.strip():
        save_teacher_reply_with_feedback(qid, teacher_id, teacher_name, solution_text, photo_id, caption, feedback)
    else:
        save_teacher_reply(qid, teacher_id, teacher_name, solution_text, photo_id, caption)

    new_count = int(t["reply_count"]) + 1
    upd_ticket(qid, {"reply_count": new_count})
    stop_reminder(qid)
    stop_claim_timeout(qid)

    u = get_user(uid)
    if not u:
        return

    if int(t["reopen_count"]) >= 1 and new_count >= 2:
        upd_ticket(qid, {"status":"closed", "claim_expires_at": None})
        upd_doubt(qid, {"status":"closed"})
        clear_teacher_session(teacher_id)
        upd_user(uid, {"awaiting_teacher_feedback_qid":None, "resolved_count":int(u["resolved_count"])+1, "step":"ready_for_new_doubt"})
        await maybe_ask_rating(context, uid)
        u2 = get_user(uid)
        if int(u2["awaiting_rating"]) == 0:
            await context.bot.send_message(chat_id=uid, text="Ticket closed.")
            await context.bot.send_message(chat_id=uid, text="Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
    else:
        upd_doubt(qid, {"status":"teacher_replied"})
        upd_ticket(qid, {"claim_expires_at": None})
        upd_user(uid, {"awaiting_teacher_feedback_qid":qid, "awaiting_feedback":0, "awaiting_no_choice":0, "step":"subject"})
        if feedback.strip():
            clear_teacher_session(teacher_id)
            await context.bot.send_message(chat_id=uid, text="Did your doubt get solved? (YES / NO)")

async def deliver_teacher_feedback(context: ContextTypes.DEFAULT_TYPE, qid: str, teacher_id: int, feedback: str):
    t = get_ticket(qid)
    if not t:
        return
    uid = int(t["user_id"])
    if feedback.strip().lower() != "skip":
        await context.bot.send_message(chat_id=uid, text=f"Teacher Feedback:\n{feedback.strip()}")
    clear_teacher_session(teacher_id)
    await context.bot.send_message(chat_id=uid, text="Did your doubt get solved? (YES / NO)")

async def send_ticket_to_teacher_dm(context: ContextTypes.DEFAULT_TYPE, teacher_id: int, qid: str, question_text: str, question_photo: Optional[str], claim_code: str, followup: bool = False, ai_answer: Optional[str] = None):
    label = "follow-up" if followup else "ticket"
    await context.bot.send_message(
        chat_id=teacher_id,
        text=(
            f"You claimed {label} {qid}\n"
            f"Claim code: {claim_code}\n\n"
            f"Student question:\n{question_text}\n\n"
            f"Reply in this DM with the solution first.\n"
            f"After that I will ask for optional feedback for the student."
        )
    )
    if question_photo:
        await context.bot.send_photo(chat_id=teacher_id, photo=question_photo, caption=f"Question image for {qid}")
    if ai_answer:
        await context.bot.send_message(
            chat_id=teacher_id,
            text=f"--- AI ka jawab (reference ke liye) ---\n\n{ai_answer}"
        )

def build_day_plan_payload(student: Dict[str, Any], subject: str, hw_text: str, free_slots: List[Dict[str, Any]], pending_tasks: List[Dict[str, Any]], test_week: Optional[Dict[str, Any]], mentor_instruction: Optional[str]) -> Dict[str, Any]:
    return {
        "student": mentor_payload(student),
        "today_subject": subject,
        "hw_text": hw_text,
        "free_slots": free_slots,
        "pending_tasks": [
            {
                "id": str(t["id"]),
                "subject": t.get("subject"),
                "topic": t.get("topic"),
                "description": t.get("description"),
                "priority": t.get("priority"),
                "scheduled_date": iso_date(t.get("scheduled_date")),
            }
            for t in pending_tasks
        ],
        "test_week": bool(test_week and test_week.get("is_test_week") and test_week.get("consent_given")),
        "test_syllabus": {
            "physics": (test_week or {}).get("physics_syllabus"),
            "chemistry": (test_week or {}).get("chemistry_syllabus"),
            "mathematics": (test_week or {}).get("mathematics_syllabus"),
        },
        "mentor_instruction": mentor_instruction or "",
    }

def calculate_completion_percentage(tasks: List[Dict[str, Any]]) -> int:
    if not tasks:
        return 0
    completed = sum(1 for task in tasks if task.get("status") == "done")
    return int(round((completed / len(tasks)) * 100))

def summarize_subject_strength(tasks: List[Dict[str, Any]]) -> Tuple[str, str]:
    score: Dict[str, int] = {}
    weak: Dict[str, int] = {}
    for task in tasks:
        subj = (task.get("subject") or "General").title()
        if task.get("status") == "done":
            score[subj] = score.get(subj, 0) + 1
        else:
            weak[subj] = weak.get(subj, 0) + 1
    strong_subject = max(score, key=score.get) if score else "NA"
    weak_subject = max(weak, key=weak.get) if weak else "NA"
    return strong_subject, weak_subject

def build_daily_summary_payload(student_id: str, report_date) -> Dict[str, Any]:
    tasks = get_student_tasks(student_id, scheduled_date=report_date)
    completion = calculate_completion_percentage(tasks)
    strong_subject, weak_subject = summarize_subject_strength(tasks)
    pending_count = sum(1 for task in tasks if task.get("status") == "pending")
    return {
        "date": iso_date(report_date),
        "tasks": tasks,
        "completion_percentage": completion,
        "pending_count": pending_count,
        "strong_subject": strong_subject,
        "weak_subject": weak_subject,
    }

def create_daily_summary(student_id: str, report_date) -> Dict[str, Any]:
    payload = build_daily_summary_payload(student_id, report_date)
    try:
        return call_json_prompt(DAILY_SUMMARY_PROMPT, payload)
    except Exception:
        return {
            "summary_text": f"Aaj {payload['completion_percentage']}% work complete hua. Kal pending ko first priority denge.",
            "consistency_score": payload["completion_percentage"],
            "strong_subject": payload["strong_subject"],
            "weak_subject": payload["weak_subject"],
            "backlog_status": f"{payload['pending_count']} pending",
        }

def create_weekly_summary(student_id: str, for_mentor: bool = False) -> Dict[str, Any]:
    end_date = today_ist_date()
    start_date = end_date - timedelta(days=6)
    reports = get_reports(student_id, "DAILY", start_date)
    prompt = WEEKLY_MENTOR_SUMMARY_PROMPT if for_mentor else WEEKLY_STUDENT_SUMMARY_PROMPT
    try:
        return call_json_prompt(prompt, {"start_date": iso_date(start_date), "end_date": iso_date(end_date), "daily_reports": reports})
    except Exception:
        return {
            "summary_text": "Weekly summary unavailable, but tracking continued.",
            "consistency_score": 0,
            "strong_subject": "NA",
            "weak_subject": "NA",
            "backlog_status": "NA",
        }

def create_fifteen_day_summary(student_id: str) -> Dict[str, Any]:
    end_date = today_ist_date()
    start_date = end_date - timedelta(days=14)
    try:
        return call_json_prompt(FIFTEEN_DAY_PROMPT, {"start_date": iso_date(start_date), "end_date": iso_date(end_date), "reports": get_reports(student_id, None, start_date)})
    except Exception:
        return {
            "summary_text": "15-day trend report unavailable.",
            "consistency_score": 0,
            "strong_subject": "NA",
            "weak_subject": "NA",
            "backlog_status": "NA",
        }

def get_custom_summary(student_id: str, days: int, word_limit: int) -> str:
    end_date = today_ist_date()
    start_date = end_date - timedelta(days=days-1)
    reports = get_reports(student_id, "DAILY", start_date)
    
    if not reports:
        return "N/A: No reports found for this period."
        
    try:
        res = call_json_prompt(CUSTOM_SUMMARY_PROMPT.replace("{{days}}", str(days)).replace("{{word_limit}}", str(word_limit)), {"reports": reports})
        return res.get("summary", "Summary generate nahi ho payi.")
    except Exception as e:
        logger.error(f"Error in get_custom_summary: {e}")
        return "Summary generate karne mein error aaya."

def recalc_daily_log(student_id: str, date_value):
    log = get_or_create_daily_log(student_id, date_value)
    tasks = get_student_tasks(student_id, scheduled_date=date_value)
    completed = sum(1 for task in tasks if task.get("status") == "done")
    pending = sum(1 for task in tasks if task.get("status") == "pending")
    consistency = calculate_completion_percentage(tasks)
    status = "excellent" if consistency >= 80 else "good" if consistency >= 60 else "average"
    update_daily_log(log["id"], {"tasks_completed": completed, "pending_tasks_count": pending, "consistency_score": consistency, "status": status})

def find_task_by_prefix(student_id: str, prefix: str) -> Optional[Dict[str, Any]]:
    prefix = (prefix or "").strip().lower()
    if not prefix:
        return None
    for task in get_student_tasks(student_id):
        if str(task["id"]).lower().startswith(prefix):
            return task
    return None

async def send_parent_completion(context: ContextTypes.DEFAULT_TYPE, student: Dict[str, Any], report_date):
    if not student.get("parent_telegram_id"):
        return
    completion = calculate_completion_percentage(get_student_tasks(student["id"], scheduled_date=report_date))
    await context.bot.send_message(chat_id=int(student["parent_telegram_id"]), text=f"Today {student.get('name', 'your child')} completed {completion}% of the planned work.")

async def generate_and_send_night_summary(context: ContextTypes.DEFAULT_TYPE, student: Dict[str, Any], report_date):
    summary = create_daily_summary(student["id"], report_date)
    create_report({
        "student_id": student["id"],
        "type": "DAILY",
        "audience": "STUDENT",
        "start_date": report_date,
        "end_date": report_date,
        "summary_text": summary.get("summary_text"),
        "consistency_score": summary.get("consistency_score"),
        "strong_subject": summary.get("strong_subject"),
        "weak_subject": summary.get("weak_subject"),
        "backlog_status": summary.get("backlog_status"),
    })
    await context.bot.send_message(chat_id=int(student["telegram_id"]), text=summary.get("summary_text") or "Daily summary ready.")
    await send_parent_completion(context, student, report_date)

async def send_weekly_reports(context: ContextTypes.DEFAULT_TYPE, student: Dict[str, Any]):
    student_report = create_weekly_summary(student["id"], for_mentor=False)
    create_report({
        "student_id": student["id"],
        "type": "WEEKLY",
        "audience": "STUDENT",
        "start_date": today_ist_date() - timedelta(days=6),
        "end_date": today_ist_date(),
        "summary_text": student_report.get("summary_text"),
        "consistency_score": student_report.get("consistency_score"),
        "strong_subject": student_report.get("strong_subject"),
        "weak_subject": student_report.get("weak_subject"),
        "backlog_status": student_report.get("backlog_status"),
    })
    await context.bot.send_message(chat_id=int(student["telegram_id"]), text=student_report.get("summary_text") or "Weekly report ready.")

async def send_weekly_mentor_report(context: ContextTypes.DEFAULT_TYPE, student: Dict[str, Any]):
    mentor_telegram = student.get("mentor_id_telegram")
    if mentor_telegram:
        mentor_report = create_weekly_summary(student["id"], for_mentor=True)
        create_report({
            "student_id": student["id"],
            "type": "WEEKLY",
            "audience": "MENTOR",
            "start_date": today_ist_date() - timedelta(days=6),
            "end_date": today_ist_date(),
            "summary_text": mentor_report.get("summary_text"),
            "consistency_score": mentor_report.get("consistency_score"),
            "strong_subject": mentor_report.get("strong_subject"),
            "weak_subject": mentor_report.get("weak_subject"),
            "backlog_status": mentor_report.get("backlog_status"),
        })
        await context.bot.send_message(chat_id=int(mentor_telegram), text=f"Mentor report for {student.get('name', 'student')}:\n{mentor_report.get('summary_text')}\nReply with /mentorreply {student.get('telegram_id')} Continue|Change Focus|Adjust Load")

async def send_fifteen_day_report(context: ContextTypes.DEFAULT_TYPE, student: Dict[str, Any]):
    mentor_telegram = student.get("mentor_id_telegram")
    if not mentor_telegram:
        return
    report = create_fifteen_day_summary(student["id"])
    create_report({
        "student_id": student["id"],
        "type": "FIFTEEN_DAY",
        "audience": "MENTOR",
        "start_date": today_ist_date() - timedelta(days=14),
        "end_date": today_ist_date(),
        "summary_text": report.get("summary_text"),
        "consistency_score": report.get("consistency_score"),
        "strong_subject": report.get("strong_subject"),
        "weak_subject": report.get("weak_subject"),
        "backlog_status": report.get("backlog_status"),
    })
    await context.bot.send_message(chat_id=int(mentor_telegram), text=report.get("summary_text") or "15-day trend ready.")

def get_slot_datetime(base_dt: datetime, slot_time: str) -> Optional[datetime]:
    parsed = parse_time_hhmm(slot_time)
    if not parsed:
        return None
    return base_dt.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

async def process_pending_rollover(student: Dict[str, Any]):
    today = today_ist_date()
    for task in get_student_tasks(student["id"], statuses=["pending"]):
        scheduled = task.get("scheduled_date")
        if not scheduled or scheduled >= today:
            continue
        age_days = (today - scheduled).days
        if age_days > 3:
            create_backlog({
                "student_id": student["id"],
                "subject": task.get("subject"),
                "topic": task.get("topic"),
                "description": task.get("description"),
                "source": "AUTO_FROM_TASK",
                "original_task_id": task["id"],
                "priority": task.get("priority") or "high",
                "status": "pending",
                "estimated_hours": round((task.get("estimated_minutes") or 60) / 60, 1),
                "added_by": "system",
            })
            update_task(task["id"], {"status": "backlog_moved"})
        else:
            update_task(task["id"], {
                "scheduled_date": today,
                "carry_forward_count": int(task.get("carry_forward_count") or 0) + 1,
                "priority": "critical" if task.get("priority") in {"high", "critical"} else "high",
            })
    recalc_daily_log(student["id"], today)

async def run_mentorship_scheduler(bot):
    global mentorship_scheduler_task
    while True:
        try:
            now_dt = today_ist()
            students = get_approved_students()
            for student in students:
                await process_pending_rollover(student)
                
                # Check parent verification timeout
                if student.get("parent_verification_requested_at"):
                    _pvr = student.get("parent_verification_requested_at")
                    if not _pvr: continue
                    req_at = datetime.fromisoformat(str(_pvr))
                    if req_at.tzinfo is None: req_at = req_at.replace(tzinfo=UTC)
                    if (now_dt - req_at) > timedelta(days=2) and not student.get("parent_verified"):
                        await bot.send_message(chat_id=int(student["telegram_id"]), text="⚠️ Parent verification timeout. Registration cancel kar di gayi hai, please restart.")
                        update_student(student["id"], {"is_approved": False})

                timetable = get_weekday_timetable(student["id"], weekday_name(now_dt))
                if not timetable:
                    continue
                slots = timetable.get("coaching_slots") or []
                bucket = class_bucket_for_slots(slots)
                user_row = get_user(int(student["telegram_id"])) or {}
                temp = get_mentorship_temp(user_row)
                date_key = iso_date(now_dt.date())
                markers = temp.setdefault("schedule_markers", {})

                start_hour = 7 if bucket == "morning" else 9
                if now_dt.hour == start_hour and now_dt.minute == 0 and not markers.get(reminder_marker_key("start_day", date_key)):
                    await bot.send_message(chat_id=int(student["telegram_id"]), text=f"Good morning! ☀️ Aaj ka schedule hai: {combine_slots_for_message(slots)}. Happy learning with MP Sir! ✨")
                    markers[reminder_marker_key("start_day", date_key)] = True

                for idx, slot in enumerate(slots):
                    start_dt = get_slot_datetime(now_dt, slot.get("start", ""))
                    end_dt = get_slot_datetime(now_dt, slot.get("end", ""))
                    if not start_dt or not end_dt:
                        continue
                    pre_key = reminder_marker_key(f"pre_class_{idx}", date_key)
                    if now_dt.replace(second=0, microsecond=0) == (start_dt - timedelta(minutes=30)) and not markers.get(pre_key):
                        await bot.send_message(chat_id=int(student["telegram_id"]), text=f"🔔 {slot.get('subject', 'Class')} class 30 min me start hone waali hai dear bhulna mat! 📚")
                        markers[pre_key] = True
                    hw1_key = reminder_marker_key(f"hw1_{idx}", date_key)
                    if now_dt.replace(second=0, microsecond=0) == (end_dt + timedelta(minutes=10)) and not markers.get(hw1_key):
                        await bot.send_message(chat_id=int(student["telegram_id"]), text=f"✅ {slot.get('subject', 'Class')} class khatam ho gayi! Ab jaldi se homework (HW) details bhej do. ✍️")
                        temp["awaiting_hw_slot"] = {"date": date_key, "slot_index": idx, "subject": slot.get("subject", "General")}
                        markers[hw1_key] = True
                    hw2_key = reminder_marker_key(f"hw2_{idx}", date_key)
                    if now_dt.replace(second=0, microsecond=0) == (end_dt + timedelta(minutes=30)) and not markers.get(hw2_key):
                        if not temp.get("hw_received", {}).get(f"{date_key}:{idx}"):
                            await bot.send_message(chat_id=int(student["telegram_id"]), text=f"Bhul gaye kya? ⏳ {slot.get('subject', 'Class')} ka HW abhi tak nahi aaya. Bhej do!")
                        markers[hw2_key] = True

                progress_hour = 19 if bucket == "morning" else 21
                progress_key = reminder_marker_key("progress", date_key)
                if now_dt.hour == progress_hour and now_dt.minute == 0 and not markers.get(progress_key):
                    await bot.send_message(chat_id=int(student["telegram_id"]), text="Raat ho gayi! Progress check kar lein? ✨ Jo pending hai wo kal subah ki first priority hogi.")
                    markers[progress_key] = True

                if now_dt.hour == 21 and now_dt.minute == 0:
                    leave = get_medical_leave(student["id"], now_dt.date())
                    if leave and leave.get("status") in {"approved", "continued"} and not markers.get(reminder_marker_key("medical_parent", date_key)) and student.get("parent_telegram_id"):
                        child_label = "son" if (student.get("child_relation") or "").lower() == "son" else "daughter"
                        await bot.send_message(chat_id=int(student["parent_telegram_id"]), text=f"Is your {child_label} {student.get('name', 'student')} on medical leave today? Reply Yes or No.")
                        markers[reminder_marker_key("medical_parent", date_key)] = True
                        temp["awaiting_medical_continue_for"] = date_key
                    if now_dt.strftime("%A") == "Sunday" and not markers.get(reminder_marker_key("sunday_ask", date_key)):
                        await bot.send_message(chat_id=int(student["telegram_id"]), text="Next week is test week? Reply Yes or No.")
                        temp["awaiting_test_week"] = iso_date(now_dt.date() + timedelta(days=1))
                        markers[reminder_marker_key("sunday_ask", date_key)] = True

                if now_dt.strftime("%A") == "Sunday" and now_dt.hour == 18 and now_dt.minute == 0 and not markers.get(reminder_marker_key("weekly_student", date_key)):
                    await send_weekly_reports(type("Ctx", (), {"bot": bot})(), student)
                    markers[reminder_marker_key("weekly_student", date_key)] = True

                if now_dt.strftime("%A") == "Sunday" and now_dt.hour == 19 and now_dt.minute == 0 and not markers.get(reminder_marker_key("weekly_mentor", date_key)):
                    await send_weekly_mentor_report(type("Ctx", (), {"bot": bot})(), student)
                    markers[reminder_marker_key("weekly_mentor", date_key)] = True

                if now_dt.hour == 19 and now_dt.minute == 5 and not markers.get(reminder_marker_key("fifteen_day", date_key)):
                    created_at = student.get("created_at")
                    if created_at:
                        try:
                            created_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).astimezone(IST)
                            if (now_dt.date() - created_dt.date()).days > 0 and (now_dt.date() - created_dt.date()).days % 15 == 0:
                                await send_fifteen_day_report(type("Ctx", (), {"bot": bot})(), student)
                                markers[reminder_marker_key("fifteen_day", date_key)] = True
                        except Exception:
                            pass

                if now_dt.hour == 0 and now_dt.minute == 0 and not markers.get(reminder_marker_key("night_summary", date_key)):
                    await generate_and_send_night_summary(type("Ctx", (), {"bot": bot})(), student, now_dt.date() - timedelta(days=1))
                    markers[reminder_marker_key("night_summary", date_key)] = True

                # Nightly Timetable Request (at 9:30 PM / 21:30)
                if now_dt.hour == 21 and now_dt.minute == 30 and not markers.get(reminder_marker_key("nightly_timetable_ask", date_key)):
                    # If weekly timetable is active, only ask on Sunday for the next week
                    if student.get("timetable_scope") == "weekly" and now_dt.strftime("%A") != "Sunday":
                        continue

                    await bot.send_message(
                        chat_id=int(student["telegram_id"]), 
                        text="Kal ka kya plan hai? 📝 Kis date ka timetable bhejna chahte hain? (Format: DD/MM/YYYY)",
                        reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True)
                    )
                    upd_user(int(student["telegram_id"]), {"step": "mentor_timetable_date"})
                    markers[reminder_marker_key("nightly_timetable_ask", date_key)] = True

                save_mentorship_temp(int(student["telegram_id"]), temp)
        except Exception as e:
            print("Mentorship scheduler error:", e)
            traceback.print_exc()
        await asyncio.sleep(MENTORSHIP_CHECK_SECONDS)

def start_mentorship_scheduler(bot):
    global mentorship_scheduler_task
    if mentorship_scheduler_task and not mentorship_scheduler_task.done():
        return
    mentorship_scheduler_task = asyncio.create_task(run_mentorship_scheduler(bot))

async def mentorship(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    student = get_student_by_telegram(uid)
    
    if student:
        if student.get("is_approved"):
            await update.message.reply_text(
                "📈 Welcome to My Mentorship!\n\nSelect an option below to view your progress or manage your day:", 
                reply_markup=ReplyKeyboardMarkup(MENTORSHIP_CLEAN_MENU, resize_keyboard=True)
            )
        else:
            await update.message.reply_text("⏳ Aapki registration processing mein hai.\n\nMentor jald hi approve karenge. Tab tak AI Doubt Solver use karein.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return

    # Smart Check: Use existing profile if available
    u = get_user(uid)
    if u and u.get("name") and u.get("phone") and u.get("profile_complete"):
        upd_user(uid, {"step": "mentor_confirm_existing"})
        await update.message.reply_text(
            f"✨ Aapka existing profile mil gaya hai!\n\n"
            f"👤 Name: {u['name']}\n"
            f"📱 Phone: {u['phone']}\n\n"
            "Kya aap yahi details use karke Mentorship shuru karna chahte hain?",
            reply_markup=ReplyKeyboardMarkup([["Yes, use these details", "No, register fresh"], ["Cancel Registration"]], resize_keyboard=True)
        )
        return

    upd_user(uid, {"mentorship_mode": "registering", "step": "mentor_phone"})
    clear_mentorship_temp(uid)
    await update.message.reply_text(
        "✨ JEE Mentorship Program mein swagat hai!\n\nRegistration shuru karte hain.\nStep 1: Apna number verify karein niche diye gaye button se 👇",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Verify My Phone Number 📱", request_contact=True)], ["Cancel Registration"]], resize_keyboard=True)
    )

async def finish_registration_and_ask_first_timetable(update: Update, uid: int):
    upd_user(uid, {"step": "mentor_ready", "mentorship_mode": "active"})
    u = get_user(uid)
    temp = get_mentorship_temp(u)
    update_student(u["mentorship_student_id"], temp.get("reg_data", {}))
    
    now = today_ist()
    # Agar aaj Saturday (5) hai, toh Monday ka pucho. Baaki din Next Day.
    days_to_add = 2 if now.weekday() == 5 else 1
    target_dt = now + timedelta(days=days_to_add)
    target_day = target_dt.strftime("%A")
    
    await update.message.reply_text(
        f"✅ Registration Complete!\n\n"
        f"Ab {target_day} ({target_dt.strftime('%d %b')}) ka timetable bhejiye.\n"
        "Example: Physics 9 am, Chemistry 11 am. Agar class nahi hai toh 'Off'.",
        reply_markup=ReplyKeyboardMarkup([["Off"]], resize_keyboard=True)
    )
    upd_user(uid, {"step": "mentor_daily_timetable_update"})

async def timetable_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    student = get_student_by_telegram(uid)
    if not student or not student.get("is_approved"):
        await update.message.reply_text("Mentorship approval ke baad timetable update ho sakta hai.")
        return
    
    now = today_ist()
    # Same logic: Saturday hai toh Monday ka, baaki din Next Day
    days_to_add = 2 if now.weekday() == 5 else 1
    target_dt = now + timedelta(days=days_to_add)
    target_day = target_dt.strftime("%A")

    # New flow: Ask for Date first
    await update.message.reply_text(
        "📝 Kis date ka timetable bhejna chahte hain? (Format: DD/MM/YYYY)",
        reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True)
    )
    upd_user(uid, {"step": "mentor_timetable_date"})

async def start_immediate_timetable_capture(update: Update, uid: int):
    await finish_registration_and_ask_first_timetable(update, uid)

async def show_backlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    student = get_student_by_telegram(uid)
    if not student or not student.get("is_approved"):
        await update.message.reply_text("Mentorship active hone ke baad backlog dikhega.")
        return
    rows = get_backlogs(student["id"], ["pending", "in_progress"])
    if not rows:
        await update.message.reply_text("Abhi koi backlog pending nahi hai.")
        return
    lines = ["Current backlog tasks:"]
    for idx, row in enumerate(rows, start=1):
        lines.append(f"{idx}. {row.get('subject', 'General')} - {row.get('topic') or row.get('description')}")
    lines.append("Manual backlog add karna ho to message bhejo: backlog: subject - topic - description")
    await update.message.reply_text("\n".join(lines))

async def accept_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not (is_admin_user(uid) or is_faculty_user(uid)):
        await update.message.reply_text("Only faculty/admin can approve students.")
        return
    value = " ".join(context.args).strip()
    if not value:
        pending = get_pending_student_approvals()
        if not pending:
            await update.message.reply_text("No pending mentorship approvals.")
            return
        lines = ["Pending students:"]
        for row in pending[:10]:
            lines.append(f"- {row.get('name')} | TG {row.get('telegram_id')} | Phone {row.get('phone')}")
        lines.append("Use /accept_student <telegram_id_or_phone>")
        await update.message.reply_text("\n".join(lines))
        return
    student = find_student_for_approval(value)
    if not student:
        await update.message.reply_text("Student not found.")
        return
    faculty = get_faculty_by_telegram(uid)
    updates = {"is_approved": True}
    if faculty:
        updates["mentor_id"] = faculty["id"]
        updates["mentor_id_telegram"] = faculty["telegram_id"]
    update_student(student["id"], updates)
    upd_user(int(student["telegram_id"]), {"mentorship_mode": "approved", "mentorship_student_id": str(student["id"]), "step": "mentor_exam_target"})
    await context.bot.send_message(chat_id=int(student["telegram_id"]), text="Mentorship approved. Aapka exam target choose karo:", reply_markup=ReplyKeyboardMarkup(EXAM_TARGET_OPTIONS, resize_keyboard=True))
    await update.message.reply_text(f"{student.get('name')} approved successfully.")

async def mentorreply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not (is_admin_user(uid) or is_faculty_user(uid)):
        await update.message.reply_text("Only faculty/admin can send mentor direction.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /mentorreply <student_telegram_id> <Continue|Change Focus|Adjust Load>")
        return
    student = get_student_by_telegram(int(context.args[0]))
    if not student:
        await update.message.reply_text("Student not found.")
        return
    direction = " ".join(context.args[1:]).strip()
    if direction not in {"Continue", "Change Focus", "Adjust Load"}:
        await update.message.reply_text("Direction should be Continue, Change Focus, or Adjust Load.")
        return
    result = call_json_prompt(MENTOR_DIRECTION_PROMPT, {"mentor_reply": direction, "student_data": create_weekly_summary(student["id"], for_mentor=True)})
    user_row = get_user(int(student["telegram_id"]))
    temp = get_mentorship_temp(user_row)
    temp["mentor_instruction"] = result.get("planning_instruction")
    temp["mentor_instruction_valid_days"] = int(result.get("valid_days") or 7)
    save_mentorship_temp(int(student["telegram_id"]), temp)
    await update.message.reply_text(f"Mentor direction saved for {student.get('name')}.")

async def handle_mentorship_message(update: Update, context: ContextTypes.DEFAULT_TYPE, u: Dict[str, Any]) -> bool:
    uid = update.message.from_user.id
    text = (update.message.text or "").strip()
    low_text = text.lower()
    
    # Global cancellation for registration flow
    if low_text in {"cancel", "cancel registration", "/uturn", "exit"}:
        upd_user(uid, {"step": "ready_for_new_doubt"})
        await update.message.reply_text("Registration cancel kar di gayi hai. Wapas menu par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return True

    step = u.get("step") or ""
    temp = get_mentorship_temp(u)
    student = get_student_by_telegram(uid)
    parent_student = get_student_by_parent_telegram(uid)

    if parent_student and text in {"Yes", "No"}:
        leave = get_medical_leave(parent_student["id"], today_ist_date()) or get_medical_leave(parent_student["id"], today_ist_date() + timedelta(days=1))
        if leave and leave.get("status") == "pending_parent":
            new_status = "approved" if text == "Yes" else "rejected"
            upsert_medical_leave(parent_student["id"], leave["leave_date"], {"parent_confirmed": text == "Yes", "parent_confirmed_at": now_iso(), "status": new_status})
            if text == "Yes":
                await context.bot.send_message(chat_id=int(parent_student["telegram_id"]), text="Parent confirmed medical leave. Evening tak normal functions paused rahenge.")
                if count_recent_medical_leaves(parent_student["id"], 5) >= 5 and parent_student.get("mentor_id_telegram"):
                    await context.bot.send_message(chat_id=int(parent_student["mentor_id_telegram"]), text=f"Emergency: {parent_student.get('name')} has been on medical leave for 5 days. Please act accordingly.")
            else:
                await context.bot.send_message(chat_id=int(parent_student["telegram_id"]), text="Parent denied medical leave, so normal plan active rahega.")
            await update.message.reply_text("Confirmation saved.")
            return True

    if text.lower() == "my mentorship":
        await mentorship(update, context)
        return True

    if text.lower() == "show backlog":
        await show_backlog_command(update, context)
        return True

    if text.lower() == "reset timetable":
        await timetable_command(update, context)
        return True

    if step == "mentor_ready" and text == "Ask Doubt":
        upd_user(uid, {"step": "subject"})
        await update.message.reply_text("Select Subject:", reply_markup=ReplyKeyboardMarkup(SUBJECT_OPTIONS, resize_keyboard=True))
        return True

    if text.lower() in {"medical leave", "/medicalleave"} and student and student.get("is_approved"):
        upd_user(uid, {"step": "mentor_medical_confirm"})
        await update.message.reply_text("Are you sure you want leave today?", reply_markup=ReplyKeyboardMarkup([["Yes", "No"], ["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if text.lower().startswith("done ") and student and student.get("is_approved"):
        task = find_task_by_prefix(student["id"], text.split(maxsplit=1)[1])
        if not task:
            await update.message.reply_text("Task not found.")
            return True
        update_task(task["id"], {"status": "done", "completed_at": now_iso()})
        recalc_daily_log(student["id"], task.get("scheduled_date") or today_ist_date())
        await update.message.reply_text(f"Task {str(task['id'])[:8]} marked done.")
        return True

    if text.lower().startswith("skip ") and student and student.get("is_approved"):
        task = find_task_by_prefix(student["id"], text.split(maxsplit=1)[1])
        if not task:
            await update.message.reply_text("Task not found.")
            return True
        update_task(task["id"], {"status": "pending", "priority": "critical", "skipped_at": now_iso()})
        recalc_daily_log(student["id"], task.get("scheduled_date") or today_ist_date())
        await update.message.reply_text(f"Task {str(task['id'])[:8]} pending me gaya. Kal first priority me aayega.")
        return True

    if step == "mentor_confirm_existing":
        if text == "Yes, use these details":
            student = upsert_student_by_telegram(uid, {"name": u.get("name"), "phone": u.get("phone")})
            upd_user(uid, {"step": "mentor_waiting_approval", "mentorship_student_id": str(student["id"])})
            await context.bot.send_message(chat_id=MENTORSHIP_GROUP_ID, text=f"New Mentorship Verification Request (Existing Profile)\nName: {student.get('name')}\nPhone: {student.get('phone')}\nTelegram ID: {uid}\nUse /accept_student {uid}")
            await update.message.reply_text("✅ Previous details use kar li gayi hain! Approval request faculty group me bhej di gayi hai. Approval ke baad aage badhenge.", reply_markup=ReplyKeyboardRemove())
            return True
        elif text == "No, register fresh":
            upd_user(uid, {"mentorship_mode": "registering", "step": "mentor_phone"})
            clear_mentorship_temp(uid)
            await update.message.reply_text("Theek hai, fresh registration karte hain.\n\nStep 1: Apna number verify karein 👇", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Verify My Phone Number 📱", request_contact=True)], ["Cancel Registration"]], resize_keyboard=True))
            return True
        else:
            await update.message.reply_text("Please choose one option.", reply_markup=ReplyKeyboardMarkup([["Yes, use these details", "No, register fresh"], ["Cancel Registration"]], resize_keyboard=True))
            return True

    if text.lower().startswith("backlog:") and student and student.get("is_approved"):
        raw = text.split(":", 1)[1].strip()
        parts = [part.strip() for part in raw.split("-")]
        create_backlog({
            "student_id": student["id"],
            "subject": parts[0] if parts else "General",
            "topic": parts[1] if len(parts) > 1 else raw,
            "description": parts[2] if len(parts) > 2 else raw,
            "source": "MANUAL",
            "priority": "medium",
            "status": "pending",
            "added_by": "student",
        })
        await update.message.reply_text("Manual backlog saved.")
        return True

    if step == "mentor_name":
        upsert_student_by_telegram(uid, {"name": text})
        upd_user(uid, {"step": "mentor_phone"})
        await update.message.reply_text("Phone number bhejo.")
        return True

    if step == "mentor_phone":
        student = upsert_student_by_telegram(uid, {"phone": re.sub(r"\D", "", text)})
        upd_user(uid, {"step": "mentor_waiting_approval", "mentorship_student_id": str(student["id"])})
        await context.bot.send_message(chat_id=MENTORSHIP_GROUP_ID, text=f"New Mentorship Verification Request\nName: {student.get('name')}\nPhone: {student.get('phone')}\nTelegram ID: {uid}\nUse /accept_student {uid}")
        await update.message.reply_text("Verification request faculty group me chali gayi hai. Approval ke baad onboarding continue hoga.")
        return True

    if step == "mentor_waiting_approval":
        await update.message.reply_text("Approval pending hai.")
        return True

    if step == "mentor_exam_target":
        if text not in {"Mains", "Adv", "Boards"}:
            await update.message.reply_text("Please choose exam target.", reply_markup=ReplyKeyboardMarkup(EXAM_TARGET_OPTIONS, resize_keyboard=True))
            return True
        temp.setdefault("reg_data", {})["exam_target"] = text
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_coaching_timing"})
        await update.message.reply_text("Coaching timing bhejo. Example: 07:00-13:00", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_coaching_timing":
        m = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text)
        if not m:
            await update.message.reply_text("Format: 07:00-13:00")
            return True
        temp.setdefault("reg_data", {})["coaching_start_time"] = m.group(1)
        temp["reg_data"]["coaching_end_time"] = m.group(2)
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_classes_per_day"})
        await update.message.reply_text("Classes per day kitni hoti hain?", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_classes_per_day":
        num_match = re.search(r"(\d+)", text)
        if not num_match:
            await update.message.reply_text("Number bhejo (e.g., 3).")
            return True
        val = int(num_match.group(1))
        temp.setdefault("reg_data", {})["classes_per_day"] = val
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_preferred_study_time"})
        # Issue #7: Changed to slot options instead of time format
        await update.message.reply_text("Select your preferred study time slot:", reply_markup=ReplyKeyboardMarkup(PREFERRED_TIME_SLOTS, resize_keyboard=True))
        return True

    if step == "mentor_preferred_study_time":
        # Issue #7: Changed from time format to slot options (Morning/Evening)
        if text not in ["Morning", "Evening"]:
            await update.message.reply_text("Select your preferred study time slot:", reply_markup=ReplyKeyboardMarkup(PREFERRED_TIME_SLOTS, resize_keyboard=True))
            return True
        
        temp = get_mentorship_temp(uid)
        temp.setdefault("reg_data", {})["preferred_study_time"] = text
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_self_study_hours"})
        await update.message.reply_text("Daily self study hours kitne target karte ho?", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_self_study_hours":
        num_match = re.search(r"(\d+)", text)
        if not num_match:
            await update.message.reply_text("Hours as number bhejo (e.g., 10).")
            return True
        val = int(num_match.group(1))
        temp.setdefault("reg_data", {})["self_study_hours"] = val
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_goal_mode"})
        await update.message.reply_text(
            "Goal mode choose karo:\n"
            "🎯 Goal A: Focus on Daily Time Management\n"
            "🚀 Goal B: Focus on Backlog Completion",
            reply_markup=ReplyKeyboardMarkup(MENTORSHIP_GOAL_OPTIONS, resize_keyboard=True)
        )
        return True

    if step == "mentor_goal_mode":
        if text not in {"Goal A", "Goal B"}:
            await update.message.reply_text("Goal A ya Goal B choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_GOAL_OPTIONS, resize_keyboard=True))
            return True
        goal_value = "A" if text == "Goal A" else "B"
        temp.setdefault("reg_data", {})["goal"] = goal_value
        temp["reg_data"]["last_active_goal"] = goal_value
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_batch"})
        await update.message.reply_text("Batch name bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_batch":
        temp.setdefault("reg_data", {})["batch_name"] = text
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_parent_id"})
        await update.message.reply_text("Parent Telegram Number bhejo, ya type karo Skip.", reply_markup=ReplyKeyboardMarkup([["Skip"], ["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_parent_id":
        if text.lower() == "skip":
            temp.setdefault("reg_data", {})["parent_phone"] = None
            temp["reg_data"]["parent_language"] = "hindi"
            save_mentorship_temp(uid, temp)
            await start_immediate_timetable_capture(update, uid)
            return True
        
        # Validate phone format
        clean_phone = re.sub(r"\D", "", text)
        if not (10 <= len(clean_phone) <= 13):
            await update.message.reply_text("Valid Phone Number bhejo (e.g., 9876543210) ya Skip likho.")
            return True
        
        # Buffer data
        temp.setdefault("reg_data", {})["parent_phone"] = clean_phone
        temp["reg_data"]["parent_verified"] = False
        temp["reg_data"]["parent_verification_requested_at"] = now_iso()
        save_mentorship_temp(uid, temp)
        
        # Save to student record
        update_student(u["mentorship_student_id"], {
            "parent_phone": clean_phone, 
            "parent_verified": False, 
            "parent_verification_requested_at": now_iso()
        })
        
        student = get_student(u["mentorship_student_id"])
        bot_username = (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start=parent_{uid}"
        
        await update.message.reply_text(
            f"✅ Parent verification link set up!\n\n"
            f"📱 Parent Phone: {clean_phone}\n\n"
            f"Niche di gayi link apne parent ko bhejein aur unhe kahein ki bot start karke onboarding complete karein:\n\n"
            f"{deep_link}\n\n"
            "Ab aage badhte hain..."
        )
        await start_immediate_timetable_capture(update, uid)
        return True

    if step == "mentor_timetable_date":
        # Handle "Change Timetable" button if they just saw it
        if text == "Change Timetable":
            temp = get_mentorship_temp(u)
            text = temp.get("timetable_target_date") # Reuse the date we just validated
            # Flow continues below as if they just sent the date
        
        try:
            d = datetime.strptime(text, "%d/%m/%Y")
            d_date = d.date()
            day_name = d.strftime("%A")
            now_ist = today_ist()
            
            # Constraint: 2 AM Cutoff for current day
            if d_date == now_ist.date() and now_ist.hour >= 2:
                await update.message.reply_text("⚠️ Aaj ka timetable 2:00 AM ke baad update nahi kiya ja sakta. Kal ka timetable update karein.")
                return True
                
            # Check if timetable already exists for this day
            student = student or get_student(u.get("mentorship_student_id"))
            c = db(); cur = db_cursor(c)
            cur.execute("SELECT id FROM weekly_timetable WHERE student_id=%s AND day_of_week=%s", (student["id"], day_name))
            exists = cur.fetchone()
            c.close()
            
            if exists and text != "Change Timetable": # If they didn't just click the button
                temp = get_mentorship_temp(u)
                temp["timetable_target_date"] = text
                temp["timetable_target_day"] = day_name
                save_mentorship_temp(uid, temp)
                
                await update.message.reply_text(
                    f"📋 {text} ({day_name}) ke liye pehle se timetable saved hai.\n\nKya aap ise badalna (change) chahte hain?",
                    reply_markup=ReplyKeyboardMarkup(TIMETABLE_CHANGE_OPTIONS, resize_keyboard=True)
                )
                # Keep step as mentor_timetable_date to handle the button
                return True

            temp = get_mentorship_temp(u)
            temp["timetable_target_date"] = text
            temp["timetable_target_day"] = day_name
            save_mentorship_temp(uid, temp)
            
            upd_user(uid, {"step": "mentor_daily_timetable_update"})
            await update.message.reply_text(
                f"📝 {text} ({day_name}) ka timetable bhejiye.\n"
                "Example: Physics 9 am, Chemistry 11 am. Agar class nahi hai toh 'Off'.",
                reply_markup=ReplyKeyboardMarkup([["Off"], ["Back", "Ask Doubt"]], resize_keyboard=True)
            )
        except ValueError:
            if text != "Change Timetable":
                await update.message.reply_text("❌ Invalid format. Please use DD/MM/YYYY (e.g., 28/04/2026).")
        return True

    if step == "mentor_daily_timetable_update":
        logger.info(f"🔹 TIMETABLE INPUT - User {uid}: '{text[:100]}'")
        
        student = student or get_student(u.get("mentorship_student_id"))
        if not student:
            logger.warning(f"❌ Student profile not found for user {uid}")
            await update.message.reply_text("Student profile nahi mila. Wapas register karein ya /start karein.")
            upd_user(uid, {"step": "ready_for_new_doubt"})
            return True

        # Use date from temp
        temp = get_mentorship_temp(u)
        target_date_str = temp.get("timetable_target_date")
        day_name = temp.get("timetable_target_day")
        
        if not target_date_str or not day_name:
            await update.message.reply_text("Date info missing. Please start again.")
            upd_user(uid, {"step": "ready_for_new_doubt"})
            return True
        
        try:
            # Parse timetable input
            logger.info(f"📅 Parsing timetable for {day_name}: '{text}'")
            
            if text.lower() == "off":
                logger.info(f"✅ User marked {day_name} as OFF")
                slots = []
            else:
                slots = parse_slot_text(text)
                logger.info(f"✅ Parsed {len(slots)} slots: {slots}")
                
                if not slots:
                    logger.warning(f"❌ Could not parse slots from: '{text}'")
                    await update.message.reply_text(
                        "❌ Format samajh nahi aaya.\n\n"
                        "Example: Physics 9 am, Chemistry 11 am, Mathematics 2 pm\n\n"
                        "Agar class nahi hai toh 'Off' likho."
                    )
                    return True
            
            # Compute free slots
            logger.info(f"🔄 Computing free slots for {day_name}")
            free_slots = compute_free_slots(slots, student.get("preferred_study_time"), student.get("self_study_hours"), day_name)
            logger.info(f"✅ Free slots computed: {free_slots}")
            
            # Temporarily store parsed data in temp
            temp["pending_timetable_slots"] = slots
            temp["pending_timetable_free"] = free_slots
            temp["pending_timetable_text"] = text
            save_mentorship_temp(uid, temp)
            
            upd_user(uid, {"step": "mentor_timetable_scope"})
            await update.message.reply_text(
                "Ye timetable kab tak ke liye hai?",
                reply_markup=ReplyKeyboardMarkup(TIMETABLE_SCOPE_OPTIONS, resize_keyboard=True)
            )
            return True
        except Exception as e:
            logger.error(f"Timetable processing error: {e}")
            await update.message.reply_text("Kuch error hua. Phirse try karein.")
            return True

    if step == "mentor_timetable_scope":
        temp = get_mentorship_temp(u)
        slots = temp.get("pending_timetable_slots")
        free_slots = temp.get("pending_timetable_free")
        target_date_str = temp.get("timetable_target_date")
        day_name = temp.get("timetable_target_day")
        
        student = student or get_student(u.get("mentorship_student_id"))
        
        if text == "Entire Week":
            # Save for all days in weekly_timetable
            for d_name in WEEK_DAYS:
                upsert_weekly_timetable_row(student["id"], d_name, slots, free_slots, student.get("batch_name"))
            update_student(student["id"], {"timetable_scope": "weekly"})
            await update.message.reply_text(
                "✅ Entire week ke liye set ho gaya! Ab next Monday se pehle nahi pucha jayega.\n\n"
                "🔔 *Reminder Info*: Har class se pehle aapko notes aur module ke liye reminders bhej diye jayenge.",
                parse_mode="Markdown"
            )
        else:
            # Save only for that day
            upsert_weekly_timetable_row(student["id"], day_name, slots, free_slots, student.get("batch_name"))
            update_student(student["id"], {"timetable_scope": "one_day"})
            await update.message.reply_text(
                f"✅ {target_date_str} ({day_name}) ke liye save ho gaya!\n\n"
                "🔔 *Reminder Info*: Class se pehle aapko notes aur module ke liye reminders mil jayenge.",
                parse_mode="Markdown"
            )
            
        upd_user(uid, {"step": "mentor_ready"})
        return True

    if step == "mentor_timetable_day":
        # This loop is now deprecated but kept for safety. 
        # Registration now uses finish_registration_and_ask_first_timetable.
        upd_user(uid, {"step": "mentor_ready"})
        return True

    if step == "mentor_medical_confirm":
        # Issue #11: Replace Cancel Registration with Back button
        if text not in {"Yes", "No", "Back"}:
            await update.message.reply_text(
                "Please reply Yes or No.", 
                reply_markup=ReplyKeyboardMarkup([["Yes", "No"], ["Back"]], resize_keyboard=True)
            )
            return True
        
        # Issue #11: Handle Back button
        if text == "Back":
            upd_user(uid, {"step": "mentor_ready"})
            await update.message.reply_text("Medical leave request cancelled.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True))
            return True
            
        if text == "No":
            upd_user(uid, {"step": "mentor_ready"})
            await update.message.reply_text("Medical leave cancelled.")
            return True
            
        if not student:
            upd_user(uid, {"step": "mentor_ready"})
            await update.message.reply_text("Student record missing.")
            return True
        
        # Issue #4, #11: Medical leave applicable from NEXT day
        tomorrow = today_ist_date() + timedelta(days=1)
        # Issue #11: Create leave record with auto-cancel timestamp (2 hours from now)
        auto_cancel_time = datetime.now(IST) + timedelta(hours=2)
        upsert_medical_leave(
            student["id"], 
            tomorrow, 
            {
                "student_requested": True, 
                "status": "pending_approval",  # Changed to pending_approval instead of pending_parent
                "auto_cancel_time": auto_cancel_time.isoformat(),
                "created_at": now_iso()
            }
        )
        upd_user(uid, {"step": "mentor_ready"})
        
        # Issue #11: Send confirmation ONLY to teacher doubt group (not parent)
        confirmation_msg = (
            f"🏥 Medical Leave Request Approval Needed\n\n"
            f"👤 Student: {student.get('name', 'Unknown')}\n"
            f"📞 Telegram ID: {uid}\n"
            f"📅 Leave Date: {tomorrow.strftime('%d %B %Y')}\n"
            f"⏰ Request Time: {datetime.now(IST).strftime('%d %b %Y %H:%M')}\n"
            f"⌛ Auto-Cancel: 2 hours if not approved\n\n"
            f"ℹ️ If NOT approved within 2 hours, leave will be automatically cancelled.\n"
            f"If approved today, no tasks assigned for {tomorrow.strftime('%A, %d %B')}."
        )
        
        try:
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID, 
                text=confirmation_msg,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"ml_approve_{student['id']}_{tomorrow}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"ml_reject_{student['id']}_{tomorrow}")]
                ])
            )
            await update.message.reply_text(
                f"✅ Medical leave request sent for approval to teacher group.\n\n"
                f"📅 Leave Date: {tomorrow.strftime('%d %B %Y')}\n"
                f"⌛ Will auto-cancel in 2 hours if not approved.\n\n"
                f"Aapka request teacher group ko bhej diya gaya hai.",
                reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True)
            )
        except Exception as e:
            logger.error(f"Error sending medical leave to teacher group: {e}")
            await update.message.reply_text(
                "Medical leave request saved. Teacher will review it.",
                reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True)
            )
        
        # Issue #4: Generate timetable for pending work TODAY
        pending_tasks = get_pending_tasks_upto_days(student["id"], 3)
        timetable = get_weekday_timetable(student["id"], weekday_name(today_ist()))
        free_slots = (timetable or {}).get("free_slots") or []
        
        next_monday = today_ist_date() - timedelta(days=today_ist_date().weekday())
        payload = build_day_plan_payload(student, "Pending Work", f"Complete pending tasks before medical leave", free_slots, pending_tasks, get_test_week(student["id"], next_monday), "Medical leave tomorrow - clear pending work today")
        
        try:
            planner = call_json_prompt(DAILY_TASK_PLANNER_PROMPT, payload)
        except Exception:
            planner = {"tasks": [{"type": "PENDING", "subject": "General", "topic": "Pending Work", "description": "Complete all pending tasks", "priority": "critical", "estimated_minutes": 120, "source": "PENDING", "scheduled_slot_label": "Full Day"}]}
        
        log = get_or_create_daily_log(student["id"], today_ist_date())
        created_lines = []
        for item in planner.get("tasks", []):
            task = create_task({
                "student_id": student["id"],
                "daily_log_id": log["id"],
                "type": item.get("type"),
                "subject": item.get("subject"),
                "topic": item.get("topic"),
                "description": item.get("description"),
                "status": "pending",
                "priority": "critical",
                "source": item.get("source", "PENDING"),
                "scheduled_date": today_ist_date(),
                "estimated_minutes": item.get("estimated_minutes", 60),
                "mentor_instruction": "Medical leave tomorrow - priority to clear backlog",
                "ai_plan_source": "medical_leave_prep",
            })
            created_lines.append(f"{str(task['id'])[:8]} | {task.get('subject')} | {task.get('description')}")
        
        recalc_daily_log(student["id"], today_ist_date())
        
        await update.message.reply_text(
            f"📋 Today's pending work timetable (aaj complete karo):\n" + 
            "\n".join(created_lines) + 
            f"\n\nAaj ye sab complete karo, kal se off mil jayega!",
            reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True)
        )
        return True

    if temp.get("awaiting_hw_slot") and student and student.get("is_approved"):
        slot_info = temp["awaiting_hw_slot"]
        if slot_info.get("date") == iso_date(today_ist_date()):
            timetable = get_weekday_timetable(student["id"], weekday_name(today_ist()))
            free_slots = (timetable or {}).get("free_slots") or []
            next_monday = today_ist_date() - timedelta(days=today_ist_date().weekday())
            payload = build_day_plan_payload(student, slot_info.get("subject", "General"), text, free_slots, get_pending_tasks_upto_days(student["id"], 3), get_test_week(student["id"], next_monday), temp.get("mentor_instruction"))
            try:
                planner = call_json_prompt(DAILY_TASK_PLANNER_PROMPT, payload)
            except Exception:
                planner = {"tasks": [
                    {"type": "HW", "subject": slot_info.get("subject", "General"), "topic": "Today's HW", "description": text, "priority": "critical", "estimated_minutes": 45, "source": "CLASS", "scheduled_slot_label": "Primary Study"},
                    {"type": "REVISION", "subject": slot_info.get("subject", "General"), "topic": "Today's Notes", "description": f"Revise {slot_info.get('subject', 'General')} notes taught today.", "priority": "high", "estimated_minutes": 30, "source": "CLASS", "scheduled_slot_label": "Primary Study"}
                ]}
            log = get_or_create_daily_log(student["id"], today_ist_date())
            created_lines = []
            for item in planner.get("tasks", []):
                task = create_task({
                    "student_id": student["id"],
                    "daily_log_id": log["id"],
                    "type": item.get("type"),
                    "subject": item.get("subject"),
                    "topic": item.get("topic"),
                    "description": item.get("description"),
                    "status": "pending",
                    "priority": item.get("priority", "medium"),
                    "source": item.get("source", "CLASS"),
                    "scheduled_date": today_ist_date(),
                    "estimated_minutes": item.get("estimated_minutes", 30),
                    "mentor_instruction": temp.get("mentor_instruction"),
                    "ai_plan_source": "daily_task_planner",
                })
                created_lines.append(f"{str(task['id'])[:8]} | {task.get('subject')} | {task.get('description')}")
            recalc_daily_log(student["id"], today_ist_date())
            temp.setdefault("hw_received", {})[f"{slot_info['date']}:{slot_info['slot_index']}"] = True
            temp.pop("awaiting_hw_slot", None)
            save_mentorship_temp(uid, temp)
            await update.message.reply_text("Today's tasks created:\n" + "\n".join(created_lines) + "\nUse done <id> or skip <id>.", reply_markup=ReplyKeyboardMarkup([["Show Backlog", "Medical Leave"], ["Ask Doubt"]], resize_keyboard=True))
            return True

    if temp.get("awaiting_test_week") and student:
        if text not in {"Yes", "No"}:
            await update.message.reply_text("Please reply Yes or No.")
            return True
        week_start = today_ist_date() + timedelta(days=(7 - today_ist_date().weekday()))
        if text == "No":
            upsert_test_week(student["id"], week_start, {"is_test_week": False, "consent_given": False})
            temp.pop("awaiting_test_week", None)
            save_mentorship_temp(uid, temp)
            await update.message.reply_text("Noted. Next week normal planning rahega.")
            return True
        temp["awaiting_test_week_confirm"] = iso_date(week_start)
        temp.pop("awaiting_test_week", None)
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_testweek_physics"})
        await update.message.reply_text("Physics test syllabus bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_testweek_physics":
        temp["test_week_physics"] = text
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_testweek_chemistry"})
        await update.message.reply_text("Chemistry test syllabus bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_testweek_chemistry":
        temp["test_week_chemistry"] = text
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_testweek_maths"})
        await update.message.reply_text("Mathematics test syllabus bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
        return True

    if step == "mentor_testweek_maths":
        temp["test_week_maths"] = text
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_testweek_consent"})
        await update.message.reply_text("Next week extra 2-hour test plan chahiye? Reply Yes or No.", reply_markup=ReplyKeyboardMarkup(YES_NO_OPTIONS, resize_keyboard=True))
        return True

    if step == "mentor_testweek_consent":
        if text not in {"Yes", "No"}:
            await update.message.reply_text("Please reply Yes or No.", reply_markup=ReplyKeyboardMarkup(YES_NO_OPTIONS, resize_keyboard=True))
            return True
        _twc = temp.get("awaiting_test_week_confirm")
        if not _twc:
            await update.message.reply_text("Test week info missing, please retry.")
            return True
        week_start = datetime.fromisoformat(str(_twc)).date()
        upsert_test_week(student["id"], week_start, {
            "is_test_week": True,
            "consent_given": text == "Yes",
            "physics_syllabus": temp.get("test_week_physics"),
            "chemistry_syllabus": temp.get("test_week_chemistry"),
            "mathematics_syllabus": temp.get("test_week_maths"),
        })
        for key in ["awaiting_test_week_confirm", "test_week_physics", "test_week_chemistry", "test_week_maths"]:
            temp.pop(key, None)
        save_mentorship_temp(uid, temp)
        upd_user(uid, {"step": "mentor_ready"})
        await update.message.reply_text("Test week info saved.")
        return True

    if temp.get("awaiting_medical_continue_for") and student:
        if text not in {"Yes", "No"}:
            await update.message.reply_text("Please reply Yes or No.")
            return True
        temp.pop("awaiting_medical_continue_for", None)
        save_mentorship_temp(uid, temp)
        if text == "Yes":
            next_date = today_ist_date() + timedelta(days=1)
            upsert_medical_leave(student["id"], next_date, {"student_requested": True, "status": "pending_parent"})
            if student.get("parent_telegram_id"):
                child_label = "son" if (student.get("child_relation") or "").lower() == "son" else "daughter"
                await context.bot.send_message(chat_id=int(student["parent_telegram_id"]), text=f"Is your {child_label} {student.get('name', 'student')} on medical leave today? Reply Yes or No.")
            await update.message.reply_text("Parent confirmation for continued medical leave sent.")
        else:
            await update.message.reply_text("Medical leave continuation cancelled.")
        return True

    # Issue #10: Handle confirming existing timetable
    if step == "confirming_existing_timetable":
        if text == "Yes":
            # Proceed with existing timetable
            await update.message.reply_text(
                "✅ Timetable confirmed! Aapka schedule ready hai.",
                reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True)
            )
            upd_user(uid, {"step": "mentor_ready"})
            return True
        elif text == "No":
            # Allow user to update/edit timetable
            target_day = u.get("pending_timetable_day", "")
            await update.message.reply_text(
                f"📝 {target_day} ka naya timetable enter karo:\n"
                "Example: Physics 9 am, Chemistry 11 am. Agar class nahi hai toh 'Off'.",
                reply_markup=ReplyKeyboardMarkup([["Off"], ["Back"]], resize_keyboard=True)
            )
            upd_user(uid, {"step": "mentor_daily_timetable_update"})
            return True
        elif text == "Back":
            upd_user(uid, {"step": "mentor_ready"})
            await update.message.reply_text("Timetable update cancelled.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True))
            return True
        return True

    if step == "mentor_ready":
        await update.message.reply_text("Mentorship mode active. Use Backlog, Medical Leave, Ask Doubt, ya Timetable Input.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True))
        return True

    return step.startswith("mentor_")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if is_admin_user(uid):
        await update.message.reply_text("Admin mode active. Use teacher group claims and private DM for solving.")
        return
    
    ensure_user(uid)
    u = get_user(uid)

    if int(u["is_blocked"]) == 1:
        await update.message.reply_text(blocked_message())
        return
    
    if int(u["awaiting_rating"]) == 1:
        await update.message.reply_text("Feedback ⭐ pehle dijiye (1-10):", reply_markup=ReplyKeyboardMarkup(RATING_OPTIONS, resize_keyboard=True))
        return
    
    # AUTO-SKIP: Check if profile complete
    if int(u["profile_complete"]) == 0:
        # Check if user exists in portal (Telegram ID match)
        portal_user = check_telegram_user_in_portal(uid)
        
        if portal_user:
            # User found in portal - load profile automatically
            existing_name = portal_user.get("name") or "Student"
            existing_class = portal_user.get("class_current") or ""
            existing_phone = portal_user.get("phone") or ""
            
            upd_user(uid, {
                "name": existing_name,
                "phone": existing_phone,
                "class_current": existing_class,
                "profile_complete": 1,
                "step": "ready_for_new_doubt",
                "email": portal_user.get("email") or "skip"
            })
            
            await update.message.reply_text(
                f"✅ Welcome back! Your profile loaded from Doubt Portal.\n"
                f"Name: {existing_name}\n"
                f"Class: {existing_class}",
                reply_markup=ReplyKeyboardRemove()
            )
            await update.message.reply_text(
                "Ask Doubt ya My Mentorship me se choose karein 👇",
                reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True)
            )
            return
        
        # If not in portal, start fresh registration
        upd_user(uid, {
            "step": "name",
            "name": None,
            "phone": None,
            "email": None,
            "class_current": None,
            "subject": None,
            "stream": None,
            "chapter": None,
            "current_qid": None,
            "question_text": None,
            "question_photo": None,
            "last_answer": None,
            "awaiting_feedback": 0,
            "awaiting_no_choice": 0,
            "awaiting_teacher_feedback_qid": None
        })
        await update.message.reply_text(
            "Aapka poora naam kya hai? (First Name + Last Name)",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # User already registered
    upd_user(uid, {"step": "ready_for_new_doubt", "awaiting_feedback": 0, "awaiting_no_choice": 0})
    await update.message.reply_text(
        "Ask Doubt ya My Mentorship me se choose karein 👇",
        reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True)
    )

async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /claim <claim_code_or_qid>"); return
    code = parts[1].strip()
    t = get_ticket(code)
    if not t:
        t = get_ticket_by_claim_code(code)
    if not t:
        await update.message.reply_text("Ticket not found."); return
    qid = t["qid"]
    if t["status"] != "pending_teacher":
        await update.message.reply_text("Ticket already closed."); return
    if t.get("claimed_by"):
        await update.message.reply_text(f"Already claimed by @{t.get('claimed_by_name','teacher')}."); return
    tid = update.message.from_user.id
    tname = update.message.from_user.username or update.message.from_user.first_name or "teacher"
    expires_at = (datetime.now(UTC) + timedelta(seconds=CLAIM_TIMEOUT_SECONDS)).isoformat()
    upd_ticket(qid, {"claimed_by": tid, "claimed_by_name": tname, "claim_expires_at": expires_at})
    upsert_teacher_session(tid, qid, "awaiting_solution")
    start_claim_timeout(context.bot, qid)

    doubt = get_user(int(t["user_id"]))
    claim_code = t.get("claim_code") or claim_code_from_qid(qid)
    # Fetch AI answer for this ticket from doubts table
    c_db = db(); cur_db = db_cursor(c_db)
    cur_db.execute("SELECT ai_answer FROM doubts WHERE qid=%s", (qid,))
    d_row = cur_db.fetchone(); c_db.close()
    ai_ans_for_claim = (d_row.get("ai_answer") if d_row else None) or doubt.get("last_answer") or ""
    await update.message.reply_text(f"Claimed ✅ {qid} | Code: {claim_code} by @{tname}")
    try:
        await send_ticket_to_teacher_dm(
            context,
            tid,
            qid,
            doubt.get("question_text", ""),
            doubt.get("question_photo"),
            claim_code,
            followup=False,
            ai_answer=ai_ans_for_claim,
        )
    except Exception:
        upd_ticket(qid, {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None})
        clear_teacher_session(tid)
        stop_claim_timeout(qid)
        await update.message.reply_text("Teacher DM failed. Ask teacher to start the bot in private once, then claim again.")

async def unclaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /unclaim <claim_code_or_qid>"); return
    code = parts[1].strip()
    t = get_ticket(code)
    if not t:
        t = get_ticket_by_claim_code(code)
    if not t or t["status"] != "pending_teacher":
        await update.message.reply_text("Active ticket not found."); return
    qid = t["qid"]
    tid = update.message.from_user.id
    if t.get("claimed_by") and int(t["claimed_by"]) != tid:
        await update.message.reply_text("Only claimer can unclaim."); return
    upd_ticket(qid, {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None})
    clear_teacher_session(tid)
    stop_claim_timeout(qid)
    await update.message.reply_text(f"Unclaimed {qid}")

async def hold_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return
    if not is_owner_user(update.message.from_user.id):
        await update.message.reply_text("Only owner/admin can hold a ticket.")
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /hold <claim_code_or_qid>")
        return
    code = parts[1].strip()
    t = get_ticket(code)
    if not t:
        t = get_ticket_by_claim_code(code)
    if not t:
        await update.message.reply_text("Ticket not found.")
        return

    qid = t["qid"]
    claimed_by = t.get("claimed_by")
    upd_ticket(qid, {"status": "on_hold", "claim_expires_at": None})
    stop_reminder(qid)
    stop_claim_timeout(qid)
    if claimed_by:
        clear_teacher_session(int(claimed_by))
    upd_doubt(qid, {"status": "on_hold"})
    await update.message.reply_text(f"Ticket put on hold for {qid}. Timers stopped.")

async def resume_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return
    if not is_owner_user(update.message.from_user.id):
        await update.message.reply_text("Only owner/admin can resume a ticket.")
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text("Usage: /resume <claim_code_or_qid>")
        return
    code = parts[1].strip()
    t = get_ticket(code)
    if not t:
        t = get_ticket_by_claim_code(code)
    if not t:
        c = db(); cur = db_cursor(c)
        cur.execute("SELECT * FROM tickets WHERE claim_code=%s AND status='on_hold'", (code,))
        row = cur.fetchone()
        c.close()
        t = dict(row) if row else None
    if not t or t["status"] not in {"on_hold", "pending_teacher"}:
        await update.message.reply_text("Held ticket not found.")
        return

    qid = t["qid"]
    upd_ticket(qid, {"status": "pending_teacher", "claimed_by": None, "claimed_by_name": None, "claim_expires_at": None})
    upd_doubt(qid, {"status": "pending_teacher"})
    start_reminder(context.bot, qid)
    claim_code = t.get("claim_code") or claim_code_from_qid(qid)
    await update.message.reply_text(f"Ticket resumed for {qid}. Use /claim {claim_code}")

async def reset_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not is_admin_user(uid):
        await update.message.reply_text("Sirf admin use kar sakte hain.")
        return
        
    target_uid = uid
    if is_owner_user(uid):
        parts = (update.message.text or "").split()
        if len(parts) > 1:
            val = parts[1]
            clean_val = re.sub(r"\D", "", val)
            if len(clean_val) > 10: clean_val = clean_val[-10:]
            c = db(); cur = db_cursor(c)
            cur.execute("SELECT user_id FROM users WHERE user_id::text = %s OR phone LIKE %s", (val, f"%{clean_val}%" if clean_val else val))
            r = cur.fetchone(); c.close()
            if not r:
                await update.message.reply_text("User not found.")
                return
            target_uid = int(r["user_id"])
            
    c = db(); cur = db_cursor(c)
    past_date = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    cur.execute("UPDATE doubts SET created_at = %s WHERE user_id = %s", (past_date, target_uid))
    c.commit(); c.close()
    
    ensure_user(target_uid)
    upd_user(target_uid, {"step": "ready_for_new_doubt"})
    if target_uid == uid:
        await update.message.reply_text("🛠️ Your 24-hour daily quota has been successfully reset! You now have 5 free doubts restored for testing.")
    else:
        await update.message.reply_text(f"🛠️ Quota reset securely for user {target_uid}.")

async def set_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not is_admin_user(update.message.from_user.id):
            return await update.message.reply_text("⛔ You are not authorized to use this command.")
        args = (update.message.text or "").split()
        if len(args) < 2: return await update.message.reply_text("Usage: /setpremium <id_or_phone_or_me>")
        val = args[1]
        
        if val.lower() == "me":
            target = update.message.from_user.id
        else:
            # Clean phone if passed with +91 or spaces
            clean_val = re.sub(r"\D", "", val)
            if len(clean_val) > 10: clean_val = clean_val[-10:]
            
            c = db(); cur = db_cursor(c)
            # Using concatenation for LIKE to avoid potential f-string/placeholder confusion
            query = "SELECT user_id FROM users WHERE user_id::text = %s OR phone LIKE %s"
            param = f"%{clean_val}%" if clean_val else val
            cur.execute(query, (val, param))
            r = cur.fetchone(); c.close()
            if not r:
                return await update.message.reply_text("User not found in DB by that phone/ID.")
            target = int(r["user_id"])
            
        upd_user(target, {"user_type": "premium", "is_paid": 1})
        await update.message.reply_text(f"User {target} is now premium.")
    except Exception as e:
        await update.message.reply_text(f"❌ Database/System Error: {str(e)}")
        traceback.print_exc()

async def check_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if is_admin_user(uid):
        await update.message.reply_text(f"Yes! You are an Admin. Your ID is {uid}.")
    else:
        await update.message.reply_text(f"No. You are not an Admin. Your ID is {uid}.")

async def set_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.message.from_user.id):
        return await update.message.reply_text("⛔ You are not authorized to use this command.")
    args = (update.message.text or "").split()
    if len(args) < 2: return await update.message.reply_text("Usage: /setfree <id_or_phone_or_me>")
    val = args[1]
    
    if val.lower() == "me":
        target = update.message.from_user.id
    else:
        # Clean phone if passed with +91 or spaces
        clean_val = re.sub(r"\D", "", val)
        if len(clean_val) > 10: clean_val = clean_val[-10:]
        
        c = db(); cur = db_cursor(c)
        cur.execute("SELECT user_id FROM users WHERE user_id::text = %s OR phone LIKE %s", (val, f"%{clean_val}%" if clean_val else val))
        r = cur.fetchone(); c.close()
        if not r:
            return await update.message.reply_text("User not found in DB by that phone/ID.")
        target = int(r["user_id"])
        
    upd_user(target, {"user_type": "free", "is_paid": 0})
    await update.message.reply_text(f"User {target} is now free.")

async def add_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not is_admin_user(uid):
        return await update.message.reply_text("⛔ You are not authorized to use this command.")
    # Start conversational flow
    add_teacher_sessions[uid] = {"step": "awaiting_name"}
    await update.message.reply_text(
        "✅ Teacher add karne ki process shuru ho gayi!\n\nStep 1/4: Teacher ka poora naam likhein:",
        reply_markup=ReplyKeyboardRemove()
    )

async def log_availability(teacher_id: int, status: str):
    c = db(); cur = db_cursor(c)
    cur.execute("INSERT INTO teacher_availability_logs (teacher_id, status, timestamp) VALUES (%s, %s, %s)", (teacher_id, status, now_iso()))
    c.commit(); c.close()

async def set_available(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.message.from_user.id
    args = context.args
    target_tid = sender_id
    target_name = update.message.from_user.username or update.message.from_user.first_name
    
    if args and is_admin_user(sender_id):
        val = args[0]
        clean_val = re.sub(r"\D", "", val)
        if len(clean_val) > 10: clean_val = clean_val[-10:]
        c = db(); cur = db_cursor(c)
        cur.execute("SELECT user_id, name FROM users WHERE user_id::text = %s OR phone LIKE %s", (val, f"%{clean_val}%" if clean_val else val))
        r = cur.fetchone(); c.close()
        if r:
            target_tid = int(r["user_id"])
            target_name = r.get("name") or f"Teacher {target_tid}"
        else:
            return await update.message.reply_text("❌ User not found for that Phone/ID.")

    c = db(); cur = db_cursor(c)
    cur.execute("SELECT * FROM teachers WHERE teacher_id=%s", (target_tid,))
    row = cur.fetchone()
    if not row and not is_admin_user(target_tid):
        c.close()
        return await update.message.reply_text("⛔ Teacher not registered. Use /addteacher first.")
    
    cur.execute("""
        INSERT INTO teachers (teacher_id, teacher_name, availability_status, last_availability_update) 
        VALUES (%s, %s, 'live', %s)
        ON CONFLICT (teacher_id) DO UPDATE SET 
            availability_status='live', last_availability_update=%s, teacher_name=%s
    """, (target_tid, target_name, now_iso(), now_iso(), target_name))
    c.commit(); c.close()
    
    await log_availability(target_tid, 'live')
    
    msg = "You are now LIVE 🟢" if target_tid == sender_id else f"Teacher {target_name} ({target_tid}) is now LIVE 🟢"
    await update.message.reply_text(msg)

async def set_offline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.message.from_user.id
    args = context.args
    target_tid = sender_id
    target_name = "Self"

    if args and is_admin_user(sender_id):
        val = args[0]
        clean_val = re.sub(r"\D", "", val)
        if len(clean_val) > 10: clean_val = clean_val[-10:]
        c = db(); cur = db_cursor(c)
        cur.execute("SELECT user_id, name FROM users WHERE user_id::text = %s OR phone LIKE %s", (val, f"%{clean_val}%" if clean_val else val))
        r = cur.fetchone(); c.close()
        if r:
            target_tid = int(r["user_id"])
            target_name = r.get("name") or f"Teacher {target_tid}"
        else:
            return await update.message.reply_text("❌ User not found.")

    c = db(); cur = db_cursor(c)
    cur.execute("UPDATE teachers SET availability_status='offline', last_availability_update=%s WHERE teacher_id=%s", (now_iso(), target_tid))
    if cur.rowcount == 0 and is_admin_user(sender_id):
        # If not found, maybe they are not in teachers table yet, but we should let admins set it
        cur.execute("""
            INSERT INTO teachers (teacher_id, teacher_name, availability_status, last_availability_update) 
            VALUES (%s, %s, 'offline', %s)
            ON CONFLICT (teacher_id) DO UPDATE SET availability_status='offline', last_availability_update=%s
        """, (target_tid, target_name, now_iso(), now_iso()))
    c.commit(); c.close()
    
    await log_availability(target_tid, 'offline')
    
    msg = "You are now OFFLINE 🔴" if target_tid == sender_id else f"Teacher {target_name} ({target_tid}) is now OFFLINE 🔴"
    await update.message.reply_text(msg)

async def assign_teacher_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.message.from_user.id): return
    args = (update.message.text or "").split()
    if len(args) < 4: return await update.message.reply_text("Usage: /teacher_subj <tid_or_phone> <subject> <stream>")
    try:
        val = args[1]
        subj = args[2].lower()
        strm = args[3].lower()
        
        # Resolve target UID from users table first (like addteacher)
        clean_val = re.sub(r"\D", "", val)
        if len(clean_val) > 10: clean_val = clean_val[-10:]
        
        c = db(); cur = db_cursor(c)
        cur.execute("SELECT user_id, name FROM users WHERE user_id::text = %s OR phone LIKE %s", (val, f"%{clean_val}%" if clean_val else val))
        u_row = cur.fetchone()
        
        if not u_row:
            c.close()
            return await update.message.reply_text(f"❌ User '{val}' not found in DB. Teacher must start the bot first!")
        
        tid = int(u_row["user_id"])
        tname = u_row.get("name") or f"Teacher {tid}"
        
        # Check if record exists in teachers
        cur.execute("SELECT teacher_id FROM teachers WHERE teacher_id=%s", (tid,))
        if cur.fetchone():
            cur.execute("UPDATE teachers SET subject_supported=%s, stream_supported=%s WHERE teacher_id=%s", (subj, strm, tid))
            c.commit()
            await update.message.reply_text(f"✅ Updated teacher {tname} ({tid}): {subj} / {strm}")
        else:
            cur.execute("""
                INSERT INTO teachers (teacher_id, teacher_name, subject_supported, stream_supported, availability_status, last_availability_update)
                VALUES (%s, %s, %s, %s, 'offline', %s)
            """, (tid, tname, subj, strm, now_iso()))
            c.commit()
            await update.message.reply_text(f"✨ New Faculty Added: {tname} ({tid}) with subject {subj} / {strm}")
        c.close()
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def viewimg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.message.from_user.id): return
    txt = update.message.text or ""
    m = re.search(qid_pattern(), txt)
    if m:
        qid = m.group()
    else:
        parts = txt.split()
        if len(parts) < 2:
            return await update.message.reply_text("Usage: /viewimg QID")
        qid = parts[-1].strip()
        
    d = get_doubt(qid)
    if not d:
        return await update.message.reply_text("Doubt not found.")
    if not d.get("question_photo"):
        return await update.message.reply_text("No image found for this doubt.")
    await update.message.reply_photo(photo=d["question_photo"], caption=f"View: {qid}")

async def handle_uturn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if update.message.chat.id == GROUP_CHAT_ID: return
    upd_user(uid, {"step":"ready_for_new_doubt", "awaiting_feedback":0, "awaiting_no_choice":0, "awaiting_rating":0})
    await update.message.reply_text("Flow reset kar diya gaya hai. Wapas menu par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))

async def reset_registration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if update.message.chat.id == GROUP_CHAT_ID: return
    
    # We allow the user to reset their own registration for testing purposes
    c = db(); cur = db_cursor(c)
    try:
        # Get student ID
        cur.execute("SELECT mentorship_student_id FROM users WHERE telegram_id=%s", (uid,))
        res = cur.fetchone()
        if res and res["mentorship_student_id"]:
            sid = res["mentorship_student_id"]
            # Delete from related tables to ensure a clean slate
            cur.execute("DELETE FROM weekly_timetable WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM tasks WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM daily_logs WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM backlogs WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM medical_leaves WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM test_weeks WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM reports WHERE student_id=%s", (sid,))
            cur.execute("DELETE FROM students WHERE id=%s", (sid,))
        
        # Reset user record to initial state
        cur.execute("""
            UPDATE users 
            SET step='ready_for_new_doubt', 
                mentorship_mode='none', 
                mentorship_student_id=NULL, 
                mentorship_temp=NULL 
            WHERE telegram_id=%s
        """, (uid,))
        c.commit()
        await update.message.reply_text("✅ Registration reset ho gayi hai! Ab aap fresh registration start kar sakte hain via /start.")
    except Exception as e:
        logger.error(f"Reset error: {e}")
        await update.message.reply_text("❌ Registration reset karne mein error aaya.")
    finally:
        c.close()

async def handle_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id == GROUP_CHAT_ID:
        return

    uid = update.message.from_user.id
    # Block student flow if admin is adding a teacher
    if uid in add_teacher_sessions:
        return

    # Admin logic removed so they can ask doubts too
    ensure_user(uid)
    u = get_user(uid)

    if int(u["is_blocked"]) == 1:
        await update.message.reply_text(blocked_message())
        return

    text = (update.message.text or "").strip()
    caption = (update.message.caption or "").strip()
    incoming = text or caption

    # FIX: Buttons ko priority dena taaki "Reserved Command" trap mein na fasein
    if text == "Ask Doubt":
        upd_user(uid, {"step": "subject"})
        await update.message.reply_text("Select Subject:", reply_markup=ReplyKeyboardMarkup(SUBJECT_OPTIONS, resize_keyboard=True))
        return
    elif text == "My Mentorship":
        await mentorship(update, context)
        return
    elif text == "Show Mentorship Progress":
        student = get_student_by_telegram(uid)
        if not student: return
        today = today_ist_date()
        tasks = get_student_tasks(student["id"], scheduled_date=today)
        completion = calculate_completion_percentage(tasks)
        pending_count = sum(1 for t in tasks if t.get("status") == "pending")
        dashboard_text = (
            f"📊 *Mentorship Progress*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 Student: {student['name']}\n"
            f"🎯 Goal: {student.get('exam_target', 'JEE')}\n"
            f"📅 Date: {today.strftime('%d %b %Y')}\n\n"
            f"✅ Aaj ka kaam: {completion}%\n"
            f"⏳ Pending Tasks: {pending_count}\n"
            f"💪 Consistency Score: {student.get('consistency_score', 0)}%\n\n"
            f"Aap bahut acha kar rahe ho, bas lage raho! Happy learning with MP Sir! ✨"
        )
        await update.message.reply_text(dashboard_text, parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_CLEAN_MENU, resize_keyboard=True))
        return
    elif text == "Start Mentorship Flow":
        await update.message.reply_text("📋 Mentorship actions available 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True))
        return
    elif text == "Backlogs":
        upd_user(uid, {"step": "backlog_selection"})
        await update.message.reply_text(
            "Backlog ka kya karna hai? Select one tab for next process:",
            reply_markup=ReplyKeyboardMarkup(BACKLOGS_MENU, resize_keyboard=True)
        )
        return
    elif text == "Check Backlogs":
        # Logic from Current Backlogs
        upd_user(uid, {"step":"viewing_current_backlogs"})
        student = get_student_by_telegram(uid)
        if not student: return
        c = db(); cur = db_cursor(c)
        try:
            cur.execute(
                "SELECT * FROM backlogs WHERE student_id=%s AND status IN ('active', 'pending') ORDER BY created_at DESC",
                (student["id"],)
            )
            backlogs = [dict(r) for r in cur.fetchall()]
            c.close()
            if not backlogs:
                await update.message.reply_text("📭 No current backlogs", reply_markup=ReplyKeyboardMarkup(BACKLOGS_MENU, resize_keyboard=True))
            else:
                backlog_list = "📋 **Current Active Backlogs:**\n\n"
                for idx, bl in enumerate(backlogs, 1):
                    backlog_list += f"{idx}. {bl.get('subject')} - {bl.get('topic')}\n"
                await update.message.reply_text(backlog_list, reply_markup=ReplyKeyboardMarkup(BACKLOGS_MENU, resize_keyboard=True))
        except Exception as e:
            logger.error(f"Error: {e}")
            await update.message.reply_text("Error loading backlogs.", reply_markup=ReplyKeyboardMarkup(BACKLOGS_MENU, resize_keyboard=True))
        return
    elif text == "Add Backlogs":
        upd_user(uid, {"step":"entering_new_backlogs"})
        await update.message.reply_text("📝 Naya backlog enter karo\n\nFormat: Subject - Topic", reply_markup=ReplyKeyboardMarkup([["Back"]], resize_keyboard=True))
        return
    elif text == "Others":
        upd_user(uid, {"step": "others_selection"})
        await update.message.reply_text("Other options available 👇", reply_markup=ReplyKeyboardMarkup(OTHERS_MENU, resize_keyboard=True))
        return
    elif text == "Medical Leave":
        upd_user(uid, {"step": "mentor_medical_confirm"})
        await update.message.reply_text("Kya aap medical leave par jana chahte hain?", reply_markup=ReplyKeyboardMarkup(YES_NO_OPTIONS, resize_keyboard=True))
        return
    elif text == "Send me Summary":
        upd_user(uid, {"step": "summary_selection"})
        await update.message.reply_text("Konse period ki summary chahiye?", reply_markup=ReplyKeyboardMarkup(SUMMARY_MENU, resize_keyboard=True))
        return
    elif text == "Weekly Summary":
        student = get_student_by_telegram(uid)
        if not student: return
        await update.message.reply_text("⌛ Summary generate ho rahi hai (Weekly, ~30 words)...")
        summary = get_custom_summary(student["id"], 7, 30)
        await update.message.reply_text(f"📊 *Weekly Summary*\n\n{summary}", parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup(SUMMARY_MENU, resize_keyboard=True))
        return
    elif text == "Monthly Summary":
        student = get_student_by_telegram(uid)
        if not student: return
        await update.message.reply_text("⌛ Summary generate ho rahi hai (Monthly, ~100 words)...")
        summary = get_custom_summary(student["id"], 30, 100)
        await update.message.reply_text(f"📊 *Monthly Summary*\n\n{summary}", parse_mode="Markdown", reply_markup=ReplyKeyboardMarkup(SUMMARY_MENU, resize_keyboard=True))
        return
    elif text == "Back":
        # Issue #6: Back button takes to main menu while preserving previous data
        upd_user(uid, {"step":"ready_for_new_doubt", "awaiting_feedback":0, "awaiting_no_choice":0, "awaiting_rating":0})
        await update.message.reply_text("Main menu par wapas. 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return
    elif text == "Timetable Input":
        await timetable_command(update, context)
        return

    if await handle_mentorship_message(update, context, u):
        return

    if await handle_parent_language(update, context, u):
        return
    if await handle_parent_steps(update, context, u):
        return

    plain_lower = (text or "").strip().lower().lstrip("/")
    if plain_lower in RESERVED_TEXT_COMMANDS:
        await update.message.reply_text(f"Is action ke liye proper command use karo: `/{plain_lower}`", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return

    # Issue #9: Handle entering new backlogs
    if u.get("step") == "entering_new_backlogs" and text:
        if text.lower() == "back":
            upd_user(uid, {"step": "backlog_selection"})
            await update.message.reply_text(
                "Backlog ka kya karna hai?",
                reply_markup=ReplyKeyboardMarkup(BACKLOGS_MENU, resize_keyboard=True)
            )
            return
        
        # Parse backlog entry (Subject - Topic format)
        if "-" not in text:
            await update.message.reply_text(
                "❌ Invalid format. Use: Subject - Topic\nExample: Physics - Newton's Laws",
                reply_markup=ReplyKeyboardMarkup([["Back"]], resize_keyboard=True)
            )
            return
        
        try:
            subject, topic = [x.strip() for x in text.split("-", 1)]
            student = get_student_by_telegram(uid)
            if not student:
                await update.message.reply_text("Student record not found.")
                return
            
            # Create backlog entry
            c = db(); cur = db_cursor(c)
            cur.execute(
                """INSERT INTO backlogs (student_id, subject, topic, status, created_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (student["id"], subject, topic, "active", now_iso())
            )
            backlog = dict(cur.fetchone())
            c.commit(); c.close()
            
            # Issue #9: After backlog entry, don't show /start, activate Goal B step by step
            upd_user(uid, {"step": "backlog_entered"})
            
            await update.message.reply_text(
                f"✅ Backlog saved!\n\n"
                f"📚 Subject: {subject}\n"
                f"📖 Topic: {topic}\n\n"
                f"Ab Goal B ko activate karenge step by step...",
                reply_markup=ReplyKeyboardMarkup([["Back", "Continue"]], resize_keyboard=True)
            )
            return
        except Exception as e:
            logger.error(f"Error saving backlog: {e}")
            await update.message.reply_text(
                "Error saving backlog. Please try again.",
                reply_markup=ReplyKeyboardMarkup([["Back"]], resize_keyboard=True)
            )
            return

    low_incoming = incoming.lower()
    if low_incoming in ["uturn", "/uturn", "cancel doubt"]:
        upd_user(uid, {"step":"ready_for_new_doubt", "awaiting_feedback":0, "awaiting_no_choice":0, "awaiting_rating":0})
        await update.message.reply_text("Action cancel, flow reset. Wapas menu par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return

    if low_incoming == "ask doubt":
        upd_user(uid, {"step": "subject", "awaiting_feedback":0, "awaiting_no_choice":0, "awaiting_rating":0})
        await update.message.reply_text("Poochiye apna doubt 👇", reply_markup=ReplyKeyboardMarkup(SUBJECT_OPTIONS, resize_keyboard=True))
        return

    if low_incoming == "back":
        step = u.get("step", "")
        # Mentorship Registration Steps
        if step == "mentor_exam_target":
            upd_user(uid, {"step": "ready_for_new_doubt"})
            await update.message.reply_text("Wapas main menu par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return
        if step == "mentor_coaching_timing":
            upd_user(uid, {"step": "mentor_exam_target"})
            await update.message.reply_text("Please choose exam target.", reply_markup=ReplyKeyboardMarkup(EXAM_TARGET_OPTIONS, resize_keyboard=True))
            return
        if step == "mentor_classes_per_day":
            upd_user(uid, {"step": "mentor_coaching_timing"})
            await update.message.reply_text("Coaching timing bhejo. Example: 07:00-13:00", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_preferred_study_time":
            upd_user(uid, {"step": "mentor_classes_per_day"})
            await update.message.reply_text("Classes per day kitni hoti hain?", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_self_study_hours":
            upd_user(uid, {"step": "mentor_preferred_study_time"})
            await update.message.reply_text("Select your preferred study time slot:", reply_markup=ReplyKeyboardMarkup(PREFERRED_TIME_SLOTS, resize_keyboard=True))
            return
        if step == "mentor_goal_mode":
            upd_user(uid, {"step": "mentor_self_study_hours"})
            await update.message.reply_text("Daily self study hours kitne target karte ho?", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_batch":
            upd_user(uid, {"step": "mentor_goal_mode"})
            await update.message.reply_text("Goal mode choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_GOAL_OPTIONS, resize_keyboard=True))
            return
        if step == "mentor_parent_id":
            upd_user(uid, {"step": "mentor_batch"})
            await update.message.reply_text("Batch name bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_timetable_date":
            upd_user(uid, {"step": "mentor_ready"})
            await update.message.reply_text("Wapas My Mentorship par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True))
            return
        if step == "mentor_daily_timetable_update":
            upd_user(uid, {"step": "mentor_timetable_date"})
            await update.message.reply_text("Kis date ka timetable bhejna chahte hain? (Format: DD/MM/YYYY)", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_timetable_scope":
            upd_user(uid, {"step": "mentor_daily_timetable_update"})
            temp = get_mentorship_temp(u)
            target_date_str = temp.get("timetable_target_date")
            day_name = temp.get("timetable_target_day")
            await update.message.reply_text(
                f"📝 {target_date_str} ({day_name}) ka timetable bhejiye.\n"
                "Example: Physics 9 am, Chemistry 11 am. Agar class nahi hai toh 'Off'.",
                reply_markup=ReplyKeyboardMarkup([["Off"], ["Back", "Ask Doubt"]], resize_keyboard=True)
            )
            return
        if step == "mentor_testweek_physics":
            upd_user(uid, {"step": "mentor_ready"})
            await update.message.reply_text("Wapas My Mentorship par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_DASHBOARD_KB, resize_keyboard=True))
            return
        if step == "mentor_testweek_chemistry":
            upd_user(uid, {"step": "mentor_testweek_physics"})
            await update.message.reply_text("Physics test syllabus bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_testweek_maths":
            upd_user(uid, {"step": "mentor_testweek_chemistry"})
            await update.message.reply_text("Chemistry test syllabus bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "mentor_testweek_consent":
            upd_user(uid, {"step": "mentor_testweek_maths"})
            await update.message.reply_text("Mathematics test syllabus bhejo.", reply_markup=ReplyKeyboardMarkup([["Back", "Ask Doubt"]], resize_keyboard=True))
            return
        if step == "backlog_selection" or step == "others_selection":
            upd_user(uid, {"step": "ready_for_new_doubt"})
            await update.message.reply_text("Wapas Main Menu par 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return
        if step == "viewing_current_backlogs" or step == "entering_new_backlogs":
            upd_user(uid, {"step": "backlog_selection"})
            await update.message.reply_text("Backlog menu 👇", reply_markup=ReplyKeyboardMarkup(BACKLOGS_MENU, resize_keyboard=True))
            return
        if step == "summary_selection":
            upd_user(uid, {"step": "others_selection"})
            await update.message.reply_text("Other options available 👇", reply_markup=ReplyKeyboardMarkup(OTHERS_MENU, resize_keyboard=True))
            return
        if step == "mentor_medical_confirm":
            upd_user(uid, {"step": "others_selection"})
            await update.message.reply_text("Other options available 👇", reply_markup=ReplyKeyboardMarkup(OTHERS_MENU, resize_keyboard=True))
            return

        # Original Doubt Steps
        if u["step"] == "stream":
            upd_user(uid, {"step":"subject"})
            await update.message.reply_text("Peeche Subject par wapas 👇", reply_markup=ReplyKeyboardMarkup(SUBJECT_OPTIONS, resize_keyboard=True))
            return
        if u["step"] == "chapter":
            upd_user(uid, {"step":"stream"})
            u2 = get_user(uid)
            await update.message.reply_text("Peeche Stream par wapas 👇", reply_markup=ReplyKeyboardMarkup(stream_kb(u2), resize_keyboard=True))
            return
        if u["step"] == "question":
            upd_user(uid, {"step":"chapter"})
            u2 = get_user(uid)
            await update.message.reply_text("Peeche Chapter par wapas 👇", reply_markup=ReplyKeyboardMarkup(chapter_kb(u2), resize_keyboard=True))
            return

    if contains_abuse_words(incoming):
        v = int(u["violation_count"]) + 1
        block = 1 if v > 3 else 0
        upd_user(uid, {"violation_count": v, "is_blocked": block})
        if block:
            await update.message.reply_text(blocked_message(), reply_markup=ReplyKeyboardRemove())
        else:
            await update.message.reply_text(
                f"⚠️ Your message violates academic policy (abusive/sexual/inappropriate content).\n"
                f"Please send only study-related doubts.\n"
                f"Warnings: {v}/3"
            )
        return
    
    # Enhanced abuse check for images
    if update.message.photo:
        caption = (update.message.caption or "").strip()
        if contains_abuse_words(caption):
            v = int(u["violation_count"]) + 1
            block = 1 if v > 3 else 0
            upd_user(uid, {"violation_count": v, "is_blocked": block})
            if block:
                await update.message.reply_text(blocked_message(), reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text(
                    f"⚠️ Image caption violates policy.\n"
                    f"Warnings: {v}/3"
                )
            return
        
        # Basic image content check
        if not analyze_image_for_abuse(caption):
            v = int(u["violation_count"]) + 1
            block = 1 if v > 3 else 0
            upd_user(uid, {"violation_count": v, "is_blocked": block})
            if block:
                await update.message.reply_text(blocked_message(), reply_markup=ReplyKeyboardRemove())
            else:
                await update.message.reply_text(
                    f"⚠️ Image content appears inappropriate.\n"
                    f"Warnings: {v}/3"
                )
            return

    if int(u["awaiting_rating"]) == 1:
        if re.fullmatch(r"(10|[1-9])", text):
            add_rating(uid, u.get("current_qid"), int(text))
            upd_user(uid, {"awaiting_rating":0, "step":"ready_for_new_doubt"})
            await update.message.reply_text("Thanks for your feedback.")
            await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        else:
            await update.message.reply_text("Please select rating 1 to 10.", reply_markup=ReplyKeyboardMarkup(RATING_OPTIONS, resize_keyboard=True))
        return

    if u["step"] == "ready_for_new_doubt":
        if text == "Ask Doubt":
            upd_user(uid, {"step":"subject"})
            await update.message.reply_text("Select Subject:", reply_markup=ReplyKeyboardMarkup(SUBJECT_OPTIONS, resize_keyboard=True))
        elif text == "My Mentorship":
            await mentorship(update, context)
        else:
            await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return

    t_qid = u.get("awaiting_teacher_feedback_qid")
    if t_qid and text:
        low = text.lower()
        if low == "yes":
            upd_user(uid, {"awaiting_teacher_feedback_qid": None, "resolved_count": int(u["resolved_count"]) + 1, "step":"ready_for_new_doubt"})
            await maybe_ask_rating(context, uid)
            u2 = get_user(uid)
            if int(u2["awaiting_rating"]) == 0:
                await update.message.reply_text("Great.")
                await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return
        if low == "no":
            t = get_ticket(t_qid)
            if not t:
                upd_user(uid, {"awaiting_teacher_feedback_qid": None, "step":"subject"})
                await update.message.reply_text("Ticket not found. Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
                return
            if int(t["reopen_count"]) >= 1:
                upd_ticket(t_qid, {"status":"closed"})
                upd_doubt(t_qid, {"status":"closed"})
                upd_user(uid, {"awaiting_teacher_feedback_qid": None, "step":"ready_for_new_doubt"})
                await update.message.reply_text("Is QID ka one-time follow-up already used ho chuka hai. Please new doubt bhejein.")
                await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
                return
            upd_user(uid, {"step":"followup_text"})
            await update.message.reply_text("Apna follow-up question bhejiye (same QID pe one-time).", reply_markup=ReplyKeyboardRemove())
            return

    if u["step"] == "followup_text":
        qid = u.get("awaiting_teacher_feedback_qid")
        t = get_ticket(qid) if qid else None
        if not qid or not t:
            upd_user(uid, {"step":"subject"})
            await update.message.reply_text("Session expired. Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return
        claimed_teacher_id = t.get("claimed_by")
        claimed_teacher_name = t.get("claimed_by_name")
        upd_ticket(qid, {"status":"pending_teacher","reopen_count":1,"created_at":now_iso()})
        upd_doubt(qid, {"status":"reopened_once"})

        if claimed_teacher_id:
            expires_at = (datetime.now(UTC) + timedelta(seconds=CLAIM_TIMEOUT_SECONDS)).isoformat()
            upd_ticket(qid, {"claim_expires_at": expires_at})
            upsert_teacher_session(int(claimed_teacher_id), qid, "awaiting_solution")
            try:
                await send_ticket_to_teacher_dm(
                    context,
                    int(claimed_teacher_id),
                    qid,
                    text,
                    None,
                    t.get("claim_code") or claim_code_from_qid(qid),
                    followup=True,
                )
                start_claim_timeout(context.bot, qid)
                upd_user(uid, {"step":"subject"})
                await update.message.reply_text(f"Follow-up same teacher ko bhej diya gaya hai.\nQID: {qid}")
            except Exception:
                upd_ticket(qid, {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None})
                clear_teacher_session(int(claimed_teacher_id))
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Follow-up Ticket (One-time) ❗\nCode: {t.get('claim_code') or claim_code_from_qid(qid)}\nQID: {qid}\nUse /claim {t.get('claim_code') or claim_code_from_qid(qid)}\n\nStudent follow-up:\n{text}")
                start_reminder(context.bot, qid)
                upd_user(uid, {"step":"subject"})
                await update.message.reply_text(f"Teacher DM unavailable, follow-up group me bhej diya gaya hai.\nQID: {qid}")
        else:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"Follow-up Ticket (One-time) ❗\nCode: {t.get('claim_code') or claim_code_from_qid(qid)}\nQID: {qid}\nUse /claim {t.get('claim_code') or claim_code_from_qid(qid)}\n\nStudent follow-up:\n{text}")
            start_reminder(context.bot, qid)
            upd_user(uid, {"step":"subject"})
            await update.message.reply_text(f"Follow-up bhej diya gaya hai.\nQID: {qid}")
        return

    if int(u["awaiting_feedback"]) == 1 and text:
        low = text.lower()
        if low == "yes":
            upd_user(uid, {"awaiting_feedback":0, "resolved_count":int(u["resolved_count"])+1, "step":"ready_for_new_doubt"})
            await maybe_ask_rating(context, uid)
            u2 = get_user(uid)
            if int(u2["awaiting_rating"]) == 0:
                await update.message.reply_text("Great.")
                await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return
        if low == "no":
            upd_user(uid, {"awaiting_feedback":0, "awaiting_no_choice":1})
            await update.message.reply_text("Select one option:", reply_markup=ReplyKeyboardMarkup(NO_OPTIONS, resize_keyboard=True))
            return

    if int(u["awaiting_no_choice"]) == 1 and text:
        qid = u.get("current_qid")
        if not qid:
            upd_user(uid, {"awaiting_no_choice":0, "step":"subject"})
            await update.message.reply_text("No active doubt found. Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return

        # Issue #3: Handle Cancel button in doubt flow
        if text == "Cancel":
            upd_user(uid, {"awaiting_no_choice":0, "step":"ready_for_new_doubt", "awaiting_feedback":0, "awaiting_rating":0})
            await update.message.reply_text("Doubt canceled. Main menu par wapas. 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            return

        if text == "1. Explain Concept Better":
            upd_user(uid, {"awaiting_no_choice":0})
            prompt = (
                f"Subject: {u.get('subject','NA')}\n"
                f"Class: {u.get('class_current','NA')}\n"
                f"Stream: {u.get('stream','NA')}\n"
                f"Chapter: {u.get('chapter','NA')}\n\n"
                f"{CONCEPT_ENHANCE_PROMPT}\n\n"
                f"Question:\n{u.get('question_text','')}\n\n"
                f"Previous Answer:\n{u.get('last_answer','')}"
            )
            try:
                ans = clean_answer(anthropic_text(prompt, get_system_prompt(u.get("subject", ""), u.get("stream", ""), u.get("chapter", "")), model=MODEL_SONNET))
                ans, _, _, _, _ = extract_tags(ans)
                await update.message.reply_text(ans, reply_markup=ReplyKeyboardRemove())
                upd_user(uid, {"awaiting_feedback":1})
                await update.message.reply_text("Did your doubt get solved? (Yes/No)", reply_markup=ReplyKeyboardMarkup(DOUBT_SOLVED_OPTIONS, resize_keyboard=True))
            except Exception:
                await update.message.reply_text("Error while generating detailed explanation.")
            return

        if text == "2. Send to Doubt Guru":
            upd_user(uid, {"awaiting_no_choice":0, "step": "choose_doubt_guru_mode"})
            await update.message.reply_text("Kaise aage badhna chahte hain?", reply_markup=ReplyKeyboardMarkup(DOUBT_GURU_CHOOSE_OPTIONS, resize_keyboard=True))
            return

    if u.get("step") == "choose_doubt_guru_mode" and text:
        if text == "Back":
            upd_user(uid, {"step": "ready_for_new_doubt", "awaiting_no_choice": 1})
            await update.message.reply_text("Wapas options par 👇", reply_markup=ReplyKeyboardMarkup(NO_OPTIONS, resize_keyboard=True))
            return

        qid = u.get("current_qid")
        t = get_ticket(qid)
        if t and t["status"] == "pending_teacher":
            return await update.message.reply_text("Already sent to Doubt Guru. Please wait.")
            
        user_type = u.get("user_type", "free")
        is_paid = (user_type == "premium" or int(u.get("is_paid", 0) or 0) == 1)
        
        now_ist = datetime.now(IST)
        if (now_ist.hour >= 22 or now_ist.hour < 10) and not is_admin_user(uid):
            upd_user(uid, {"step":"subject"})
            await update.message.reply_text("Doubt Guru routing disabled: Faculty are offline from 10 PM to 10 AM. Only AI flow is active.")
            return await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))

        if not is_paid:
            lifetime_used = int(u.get("doubt_guru_lifetime_used") or 0)
            if lifetime_used >= 5:
                upd_user(uid, {"step":"subject"})
                await update.message.reply_text("You are not a premium user on this platform. Free trial over.")
                return await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            # Quota update will happen when actually sent
        
        if text == "Send in Group (Fast Process)":
            if not is_paid:
                lifetime_used = int(u.get("doubt_guru_lifetime_used") or 0)
                upd_user(uid, {"doubt_guru_lifetime_used": lifetime_used + 1})
                await update.message.reply_text(f"Notice: You are using Free Doubt Guru quota ({5 - lifetime_used - 1} left).")

            qtxt = u.get("question_text", "")
            qphoto = u.get("question_photo")
            claim_code = claim_code_from_qid(qid)
            if qphoto:
                sent = await context.bot.send_photo(chat_id=GROUP_CHAT_ID, photo=qphoto, caption=f"New Doubt Guru Ticket\nCode: {claim_code}\nQID: {qid}\nUse /claim {claim_code}\n\nQuestion: {qtxt}")
            else:
                sent = await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"New Doubt Guru Ticket\nCode: {claim_code}\nQID: {qid}\nUse /claim {claim_code}\n\nQuestion:\n{qtxt}")
            upsert_ticket({"qid": qid, "user_id": uid, "status":"pending_teacher", "created_at":now_iso(), "group_msg_id":sent.message_id, "claimed_by":None, "claimed_by_name":None, "reply_count":0, "reopen_count":0, "claim_code": claim_code, "claim_expires_at": None})
            upd_doubt(qid, {"status":"pending_teacher"})
            start_reminder(context.bot, qid)
            upd_user(uid, {"step":"subject"})
            return await update.message.reply_text(f"Sent to Doubt Guru group.\nQID: {qid}", reply_markup=ReplyKeyboardRemove())

        if text == "Select your Faculty":
            subj = (u.get("subject") or "").lower()
            strm = (u.get("stream") or "").lower()
            c = db(); cur = db_cursor(c)
            cur.execute("SELECT * FROM teachers ORDER BY priority_order DESC")
            all_teachers = cur.fetchall()
            c.close()
            
            faculties = []
            for t_row in all_teachers:
                t_subj = (t_row.get("subject_supported") or "").lower()
                t_strm = (t_row.get("stream_supported") or "").lower()
                if t_subj and t_subj != "none" and t_subj != subj: continue
                if subj == "chemistry" and t_strm and t_strm != "none" and strm and t_strm != strm: continue
                faculties.append(t_row)

            if not faculties:
                return await update.message.reply_text("Filhal koi specific faculty available nahi hai. Please use 'Send in Group'.", reply_markup=ReplyKeyboardMarkup(DOUBT_GURU_CHOOSE_OPTIONS, resize_keyboard=True))

            fac_options = []
            for f in faculties:
                lbl_status = "🟢 Live" if f.get("availability_status") == "live" else "🔴 Offline"
                fac_options.append(f"👨‍🏫 {f['teacher_name']} ({lbl_status})")
            fac_kb = [fac_options[i:i+2] for i in range(0, len(fac_options), 2)]
            fac_kb.append(["Back"])
            
            upd_user(uid, {"step": "select_faculty"})
            await update.message.reply_text("Please select a faculty member:", reply_markup=ReplyKeyboardMarkup(fac_kb, resize_keyboard=True))
            return

        await update.message.reply_text("Please choose from buttons.", reply_markup=ReplyKeyboardMarkup(DOUBT_GURU_CHOOSE_OPTIONS, resize_keyboard=True))
        return

        await update.message.reply_text("Please choose from options.", reply_markup=ReplyKeyboardMarkup(NO_OPTIONS, resize_keyboard=True))
        return

    step = u["step"]

    if step == "select_faculty":
        qid = u.get("current_qid")
        if not qid:
            upd_user(uid, {"step":"subject"})
            return await update.message.reply_text("No active doubt found. Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            
        if text == "Back":
            upd_user(uid, {"step": "choose_doubt_guru_mode"})
            await update.message.reply_text("Kaise aage badhna chahte hain?", reply_markup=ReplyKeyboardMarkup(DOUBT_GURU_CHOOSE_OPTIONS, resize_keyboard=True))
            return
            
        c = db(); cur = db_cursor(c)
        cur.execute("SELECT * FROM teachers")
        all_teachers = cur.fetchall()
        c.close()
            
        selected_fac = None
        for f in all_teachers:
            if text == f"👨‍🏫 {f['teacher_name']} (🟢 Live)" or text == f"👨‍🏫 {f['teacher_name']} (🔴 Offline)":
                selected_fac = f
                break
                
        if not selected_fac:
            return await update.message.reply_text("Please use the buttons below to select a faculty.")
            
        upd_user(uid, {"step":"subject"})
        claim_code = claim_code_from_qid(qid)
        next_escalation = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        ticket_data = {
            "qid": qid, "user_id": uid, "status": "pending_teacher",
            "created_at": now_iso(), "group_msg_id": 0,
            "claimed_by": selected_fac["teacher_id"], "claimed_by_name": selected_fac["teacher_name"],
            "reply_count": 0, "reopen_count": 0,
            "claim_code": claim_code, "claim_expires_at": next_escalation,
            "assigned_teacher_id": selected_fac["teacher_id"], "assigned_subject": u.get("subject"),
            "assigned_stream": u.get("stream"), "assigned_at": now_iso(), "next_escalation_at": next_escalation
        }
        upsert_ticket(ticket_data)
        upd_doubt(qid, {"status": "pending_teacher"})
        upsert_teacher_session(selected_fac["teacher_id"], qid, "awaiting_solution")
        
        qtxt = u.get("question_text", "")
        qphoto = u.get("question_photo")
        ai_ans = u.get("last_answer") or ""
        try:
            await send_ticket_to_teacher_dm(context, selected_fac["teacher_id"], qid, qtxt, qphoto, claim_code, followup=False, ai_answer=ai_ans)
            await update.message.reply_text(f"Doubt sent directly to {selected_fac['teacher_name']}'s inbox.\nQID: {qid}", reply_markup=ReplyKeyboardRemove())
        except Exception:
            await update.message.reply_text(f"Could not reach {selected_fac['teacher_name']}. Re-routing to group.")
            upd_ticket(qid, {"claimed_by": None, "claimed_by_name": None, "claim_expires_at": None, "assigned_teacher_id": None})
            clear_teacher_session(selected_fac["teacher_id"])
            if qphoto:
                sent = await context.bot.send_photo(chat_id=GROUP_CHAT_ID, photo=qphoto, caption=f"New Doubt Guru Ticket (Rerouted)\nCode: {claim_code}\nQID: {qid}\nUse /claim {claim_code}\n\nQuestion: {qtxt}")
            else:
                sent = await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"New Doubt Guru Ticket (Rerouted)\nCode: {claim_code}\nQID: {qid}\nUse /claim {claim_code}\n\nQuestion:\n{qtxt}")
            upd_ticket(qid, {"group_msg_id": sent.message_id})
        
        start_reminder(context.bot, qid)
        start_claim_timeout(context.bot, qid)
        return

    if step == "name":
        if not text:
            await update.message.reply_text("Aapka poora naam kya hai? (First Name + Last Name)")
            return
        
        # Validate name format
        if not is_valid_name_format(text):
            await update.message.reply_text(
                "❌ Name contains invalid characters or abuse words.\n"
                "Please enter your name properly (letters, spaces, hyphens only)."
            )
            return
        
        upd_user(uid, {"name": text.strip(), "step": "phone"})
        contact_kb = [[KeyboardButton("Share Contact", request_contact=True)]]
        await update.message.reply_text(
            "Ab apna Phone Number verify karne ke liye niche 'Share Contact' button dabayein 👇",
            reply_markup=ReplyKeyboardMarkup(contact_kb, resize_keyboard=True)
        )
        return

    if step == "phone":
        phone = None
        if update.message.contact:
            c = update.message.contact
            if c.user_id and c.user_id != uid:
                await update.message.reply_text("❌ Kripya APNA contact share karein, kisi aur ka nahi.")
                return
            phone = c.phone_number
        else:
            await update.message.reply_text(
                "❌ Valid number ke liye niche diya gaya 'Share Contact' button hi use karein 👇",
                reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Share Contact", request_contact=True)]], resize_keyboard=True)
            )
            return
        
        # Validate phone format
        if not is_valid_phone_format(phone):
            await update.message.reply_text(
                "❌ Invalid phone number. Please use 'Share Contact' button.",
                reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Share Contact", request_contact=True)]], resize_keyboard=True)
            )
            return
        
        phone_clean = re.sub(r"\D", "", phone)
        if len(phone_clean) > 10:
            phone_clean = phone_clean[-10:]
        
        # Check if phone already exists in doubt portal
        portal_match = check_phone_in_doubt_portal(phone_clean)
        if portal_match:
            upd_user(uid, {
                "phone": phone_clean,
                "class_current": portal_match.get("class_current") or "11",
                "profile_complete": 1,
                "step": "email"
            })
            await update.message.reply_text("✅ Phone matched with Doubt Portal account!")
        else:
            upd_user(uid, {"phone": phone_clean, "step": "email"})
        
        await update.message.reply_text(
            "Apna Email Id likhein (agar nahi hai toh 'skip' type karein):",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if step == "email":
        # Validate email format
        if text.strip().lower() != "skip" and not is_valid_email_format(text.strip()):
            await update.message.reply_text(
                "❌ Invalid email format. Enter valid email or type 'skip'."
            )
            return
        
        # Check if already has class from portal
        u_check = get_user(uid)
        if u_check.get("class_current"):
            # Already has class from portal, skip class selection
            upd_user(uid, {
                "email": text.strip() if text.strip().lower() != "skip" else "skip",
                "profile_complete": 1,
                "step": "ready_for_new_doubt"
            })
            await update.message.reply_text("✅ Profile complete! Welcome to Doubt Guru 🚀")
            await update.message.reply_text(
                "Ask Doubt ya My Mentorship me se choose karein 👇",
                reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True)
            )
        else:
            # Ask for class
            upd_user(uid, {"email": text.strip() if text.strip().lower() != "skip" else "skip", "step": "class_select"})
            await update.message.reply_text(
                "Aap kis Class me hain?",
                reply_markup=ReplyKeyboardMarkup(CLASS_OPTIONS, resize_keyboard=True)
            )
        return

    if step == "class_select":
        if text not in {"11", "12"}:
            await update.message.reply_text("Buttons me se apni class select karein 👇", reply_markup=ReplyKeyboardMarkup(CLASS_OPTIONS, resize_keyboard=True))
            return
        upd_user(uid, {"class_current": text, "profile_complete": 1, "step": "ready_for_new_doubt"})
        await update.message.reply_text("Ask Doubt ya My Mentorship me se choose karein 👇", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
        return

    if step == "subject":
        s = text.lower()
        if s not in {"physics","chemistry","mathematics"}:
            await update.message.reply_text("Please choose subject from buttons.", reply_markup=ReplyKeyboardMarkup(SUBJECT_OPTIONS, resize_keyboard=True))
            return
        upd_user(uid, {"subject":s, "step":"stream"})
        u2 = get_user(uid)
        await update.message.reply_text("Kaunsi stream?", reply_markup=ReplyKeyboardMarkup(stream_kb(u2), resize_keyboard=True))
        return

    if step == "stream":
        kb = stream_kb(u)
        if text.lower() not in flatten_rows(kb):
            await update.message.reply_text("Please choose stream from buttons.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            return
        upd_user(uid, {"stream": text, "step":"chapter"})
        u2 = get_user(uid)
        kb = chapter_kb(u2)
        await update.message.reply_text("Kaunsa chapter?", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    if step == "chapter":
        kb = chapter_kb(u)
        valid = [x for row in kb for x in row]
        if text not in valid:
            await update.message.reply_text("Sahi chapter chunein buttons se 👇", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
            return
        upd_user(uid, {"chapter":text, "step":"question"})
        q_kb = [["Back", "Cancel Doubt"]]
        await update.message.reply_text("Badhiya! Ab apna Doubt (Photo ya Text) bhejein 👇", reply_markup=ReplyKeyboardMarkup(q_kb, resize_keyboard=True))
        return

    if step == "question":
        image_data_url = None
        if update.message.text:
            qtext = update.message.text
        elif update.message.caption:
            qtext = update.message.caption
        else:
            qtext = "Solve the question in image."

        qphoto = update.message.photo[-1].file_id if update.message.photo else None
        upd_user(uid, {"question_text": qtext, "question_photo": qphoto})
        u = get_user(uid)
        
        user_type = u.get("user_type", "free")
        is_paid = (user_type == "premium" or int(u.get("is_paid", 0) or 0) == 1)
        if not is_paid:
            c = db(); cur = db_cursor(c)
            last_24h = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
            cur.execute("SELECT COUNT(*) as cnt FROM doubts WHERE user_id=%s AND created_at >= %s", (uid, last_24h))
            r = cur.fetchone(); c.close()
            d_count = r["cnt"] if r else 0
            
            if d_count >= 5:
                await update.message.reply_text("⛔ You have exhausted your free limit of 5 AI doubts for the last 24 hours.\nPlease wait before trying again.")
                upd_user(uid, {"step": "ready_for_new_doubt"})
                await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
                return

        prompt = build_prompt(u, qtext)

        try:
            has_image = bool(update.message.photo)
            chosen_model = select_model(qtext, u.get("subject", ""), has_image)

            if update.message.photo:
                tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
                img_bytes = await tg_file.download_as_bytearray()
                img_b64 = base64.b64encode(bytes(img_bytes)).decode("utf-8")
                image_data_url = f"data:image/jpeg;base64,{img_b64}"
                raw = clean_answer(
                    anthropic_with_image(
                        prompt,
                        img_b64,
                        system_prompt=get_system_prompt(u.get("subject", ""), u.get("stream", ""), u.get("chapter", "")),
                        model=chosen_model,
                    )
                )
            else:
                raw = clean_answer(anthropic_text(prompt, get_system_prompt(u.get("subject", ""), u.get("stream", ""), u.get("chapter", "")), model=chosen_model))

            answer, diff, needs_teacher, diagram_yes, ddata = extract_tags(raw)
            explicit_teacher_review = bool(needs_teacher)
            visual_review_risk = False

            if image_data_url and needs_visual_symbol_verification(qtext, answer, u.get("subject", ""), True):
                try:
                    verified_answer, verification_risk = verify_visual_symbol_answer(prompt, image_data_url, answer, u.get("subject", ""))
                    if verified_answer:
                        answer = verified_answer
                    if verification_risk:
                        visual_review_risk = True
                        diff = "H"
                except Exception:
                    visual_review_risk = True
                    diff = "H"

            needs_teacher = explicit_teacher_review or visual_review_risk
            auto_send_to_teacher = explicit_teacher_review
            review_note = ""
            if visual_review_risk and not explicit_teacher_review:
                review_note = "Note: Is image-based doubt me reading precision sensitive ho sakti hai. Agar answer unsolved lage, aap 'Send to Doubt Guru' choose kar sakte ho."

            qid = gen_qid(u, "H" if needs_teacher else diff)
            upd_user(uid, {"current_qid": qid, "last_answer": answer})

            ins_doubt({
                "qid": qid,
                "user_id": uid,
                "class_current": u.get("class_current"),
                "subject": u.get("subject"),
                "stream": u.get("stream"),
                "chapter": u.get("chapter"),
                "question_text": qtext,
                "question_photo": qphoto,
                "ai_answer": answer,
                "difficulty": ("H" if needs_teacher else diff),
                "needs_teacher_review": int(needs_teacher),
                "diagram_required": int(diagram_yes),
                "diagram_data": ddata,
                "status": "pending_teacher" if auto_send_to_teacher else "ai_done"
            })

            if auto_send_to_teacher:
                if not is_paid:
                    upd_doubt(qid, {"status": "ai_done"})
                    await update.message.reply_text(f"QID: {qid}\n\n{answer}")
                    await update.message.reply_text("⚠️ This doubt requires a Doubt Guru's review for 100% accuracy.\nNote: Auto-routing to Doubt Guru is disabled for Free accounts.")
                    upd_user(uid, {"step": "ready_for_new_doubt", "awaiting_feedback": 0})
                    await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
                    return

                now_ist = datetime.now(IST)
                if (now_ist.hour >= 22 or now_ist.hour < 10) and not is_admin_user(uid):
                    upd_doubt(qid, {"status": "ai_done"})
                    await update.message.reply_text(f"QID: {qid}\n\n{answer}")
                    await update.message.reply_text("⚠️ This doubt requires a Doubt Guru's review, but faculty routing is disabled at night (10 PM - 10 AM). Only AI flow is active.")
                    upd_user(uid, {"step": "ready_for_new_doubt", "awaiting_feedback": 0})
                    await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
                    return

                await update.message.reply_text(f"QID: {qid}\nIs doubt me accuracy-sensitive exception ho sakta hai, isliye we need to send it to a Doubt Guru.")
                subj = (u.get("subject") or "").lower()
                strm = (u.get("stream") or "").lower()
                c = db(); cur = db_cursor(c)
                cur.execute("SELECT * FROM teachers ORDER BY priority_order DESC")
                all_teachers = cur.fetchall()
                c.close()
                faculties = []
                for t_row in all_teachers:
                    t_subj = (t_row.get("subject_supported") or "").lower()
                    t_strm = (t_row.get("stream_supported") or "").lower()
                    if t_subj and t_subj != "none" and t_subj != subj: continue
                    if subj == "chemistry" and t_strm and t_strm != "none" and strm and t_strm != strm: continue
                    faculties.append(t_row)

                if faculties:
                    fac_options = [f"👨‍🏫 {f['teacher_name']} ({'🟢 Live' if f.get('availability_status') == 'live' else '🔴 Offline'})" for f in faculties]
                    fac_kb = [fac_options[i:i+2] for i in range(0, len(fac_options), 2)]
                    fac_kb.append(["⏭️ Skip"])
                    upd_user(uid, {"step": "select_faculty", "awaiting_feedback": 0})
                    await update.message.reply_text("Please select a faculty member to send your doubt to, or tap Skip to ask all Doubt Gurus:", reply_markup=ReplyKeyboardMarkup(fac_kb, resize_keyboard=True))
                else:
                    claim_code = claim_code_from_qid(qid)
                    if qphoto:
                        sent = await context.bot.send_photo(chat_id=GROUP_CHAT_ID, photo=qphoto, caption=f"AI Review Needed\nCode: {claim_code}\nQID: {qid}\nUse /claim {claim_code}\n\nQuestion: {qtext}")
                    else:
                        sent = await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"AI Review Needed\nCode: {claim_code}\nQID: {qid}\nUse /claim {claim_code}\n\nQuestion:\n{qtext}")

                    upsert_ticket({
                        "qid": qid,
                        "user_id": uid,
                        "status": "pending_teacher",
                        "created_at": now_iso(),
                        "group_msg_id": sent.message_id,
                        "claimed_by": None,
                        "claimed_by_name": None,
                        "reply_count": 0,
                        "reopen_count": 0,
                        "claim_code": claim_code,
                        "claim_expires_at": None
                    })
                    start_reminder(context.bot, qid)
                    upd_user(uid, {"step": "ready_for_new_doubt", "awaiting_feedback": 0})
                    await update.message.reply_text("Sent to Doubt Guru group directly.", reply_markup=ReplyKeyboardRemove())
                return

            await update.message.reply_text(f"QID: {qid}")
            await update.message.reply_text(answer)
            if review_note:
                await update.message.reply_text(review_note)

            if diagram_yes:
                subject_now = (u.get("subject") or "").lower()
                if not (subject_now == "chemistry" and not is_valid_chem_diagram_data(ddata)):
                    p = generate_diagram(qid, answer, ddata)
                    if p and p.exists():
                        try:
                            with open(p, "rb") as f:
                                await context.bot.send_photo(chat_id=uid, photo=f, caption=f"Diagram for {qid}")
                        finally:
                            try:
                                p.unlink(missing_ok=True)
                            except Exception:
                                pass

            upd_user(uid, {"awaiting_feedback": 1})
            await update.message.reply_text("Did your doubt get solved? (Yes/No)", reply_markup=ReplyKeyboardMarkup(DOUBT_SOLVED_OPTIONS, resize_keyboard=True))
            return

        except Exception as e:
            print("ERROR solve:", e)
            traceback.print_exc()
            logger.error(f"Doubt solving failed for user {uid}: {str(e)}", exc_info=True)
            # Issue #1: Better error handling with retry option
            err_details = str(e)
            await update.message.reply_text(
                f"❌ Doubt solving failed.\n\n"
                f"🔍 Error Details: {err_details}\n\n"
                "Possible reasons:\n"
                "• API key/Quota issue\n"
                "• File size/Format issue\n"
                "• Network connection\n\n"
                "Please try again or contact support."
            )
            # Offer retry
            await update.message.reply_text("Ask Doubt ya My Mentorship choose karo.", reply_markup=ReplyKeyboardMarkup(MENTORSHIP_ENTRY_OPTIONS, resize_keyboard=True))
            upd_user(uid, {"step": "ready_for_new_doubt", "awaiting_feedback": 0})
            return

    await update.message.reply_text("Please press /start.")

async def handle_teacher_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id < 0:
        return

    teacher_id = update.message.from_user.id

    # --- Admin: conversational add-teacher flow ---
    if teacher_id in add_teacher_sessions and is_admin_user(teacher_id):
        sess = add_teacher_sessions[teacher_id]
        # Accept text or caption (in case they send an image)
        text_in = (update.message.text or update.message.caption or "").strip()

        if sess["step"] == "awaiting_name":
            if not text_in:
                return await update.message.reply_text("Naam khaali nahi ho sakta. Dobara likhein:")
            sess["name"] = text_in
            sess["step"] = "awaiting_phone"
            return await update.message.reply_text("Step 2/4: Teacher ka Phone Number ya Telegram ID likhein:")

        if sess["step"] == "awaiting_phone":
            if not text_in:
                return await update.message.reply_text("Phone/ID khaali nahi ho sakta. Dobara likhein:")
            sess["phone"] = text_in
            sess["step"] = "awaiting_subject"
            subj_kb = [["Physics", "Chemistry", "Mathematics"], ["All (Koi bhi subject)"]]
            return await update.message.reply_text(
                "Step 3/4: Teacher kaunsa subject padhata hai?",
                reply_markup=ReplyKeyboardMarkup(subj_kb, resize_keyboard=True, one_time_keyboard=True)
            )

        if sess["step"] == "awaiting_subject":
            subj_map = {
                "physics": "physics", "chemistry": "chemistry",
                "mathematics": "mathematics", "maths": "mathematics",
                "all (koi bhi subject)": "none", "all": "none"
            }
            subj_val = subj_map.get(text_in.lower())
            if not subj_val:
                return await update.message.reply_text("Buttons me se chunein: Physics / Chemistry / Mathematics / All")
            sess["subject"] = subj_val
            if subj_val == "chemistry":
                strm_kb = [["Organic", "Physical", "Inorganic"], ["All Streams"]]
                sess["step"] = "awaiting_stream"
                return await update.message.reply_text(
                    "Step 4/4: Chemistry ka kaunsa stream?",
                    reply_markup=ReplyKeyboardMarkup(strm_kb, resize_keyboard=True, one_time_keyboard=True)
                )
            else:
                # For Physics/Maths/All — stream not needed
                sess["stream"] = "none"
                sess["step"] = "saving"

        if sess["step"] == "awaiting_stream":
            stream_map = {
                "organic": "organic", "physical": "physical",
                "inorganic": "inorganic", "all streams": "none", "all": "none"
            }
            strm_val = stream_map.get(text_in.lower())
            if not strm_val:
                return await update.message.reply_text("Buttons me se chunein: Organic / Physical / Inorganic / All Streams")
            sess["stream"] = strm_val
            sess["step"] = "saving"

        if sess.get("step") == "saving":
            name = sess["name"]
            val = sess["phone"]
            subj = sess.get("subject", "none")
            strm = sess.get("stream", "none")

            clean_val = re.sub(r"\D", "", val)
            if len(clean_val) > 10: clean_val = clean_val[-10:]

            c = db(); cur = db_cursor(c)
            cur.execute("SELECT user_id FROM users WHERE user_id::text = %s OR phone LIKE %s", (val, f"%{clean_val}%" if clean_val else val))
            r = cur.fetchone()
            if not r:
                c.close()
                del add_teacher_sessions[teacher_id]
                return await update.message.reply_text(
                    f"❌ '{val}' se koi user nahi mila DB mein.\nTeacher ne pehle bot start kiya hona chahiye!\n"
                    "Dobara /addteacher se shuru karein.",
                    reply_markup=ReplyKeyboardRemove()
                )
            target_id = int(r["user_id"])

            cur.execute("""
                INSERT INTO teachers (teacher_id, teacher_name, subject_supported, stream_supported, availability_status, last_availability_update)
                VALUES (%s, %s, %s, %s, 'offline', %s)
                ON CONFLICT (teacher_id) DO UPDATE SET
                    teacher_name=excluded.teacher_name,
                    subject_supported=excluded.subject_supported,
                    stream_supported=excluded.stream_supported
            """, (target_id, name, subj, strm, now_iso()))
            c.commit(); c.close()
            
            await log_availability(target_id, 'offline')
            
            del add_teacher_sessions[teacher_id]

            subj_display = subj.capitalize() if subj != "none" else "All Subjects"
            strm_display = strm.capitalize() if strm != "none" else "All Streams"
            return await update.message.reply_text(
                f"✅ Faculty successfully added!\n\n"
                f"👨‍🏫 Naam: {name}\n"
                f"🆔 ID: {target_id}\n"
                f"📚 Subject: {subj_display}\n"
                f"🔬 Stream: {strm_display}\n"
                f"Status: 🔴 Offline (teacher /available se live ho sakta hai)",
                reply_markup=ReplyKeyboardRemove()
            )
        return
    # --- End admin add-teacher flow ---

    session = get_teacher_session(teacher_id)
    if not session:
        return

    qid = session.get("qid")
    ticket = get_ticket(qid) if qid else None
    if not qid or not ticket or ticket["status"] != "pending_teacher":
        clear_teacher_session(teacher_id)
        await update.message.reply_text("No active claimed ticket found.")
        raise ApplicationHandlerStop

    if ticket.get("claimed_by") and int(ticket["claimed_by"]) != teacher_id:
        clear_teacher_session(teacher_id)
        await update.message.reply_text("This ticket is no longer assigned to you.")
        raise ApplicationHandlerStop

    teacher_name = update.message.from_user.username or update.message.from_user.first_name or "teacher"
    mode = session.get("mode")

    if mode == "awaiting_feedback":
        feedback_text = (update.message.text or "").strip()
        if update.message.photo:
            feedback_text = (update.message.caption or "").strip()
        if not feedback_text:
            await update.message.reply_text("Send feedback text, or type skip.")
            raise ApplicationHandlerStop

        await deliver_teacher_feedback(context, qid, teacher_id, feedback_text)
        await update.message.reply_text(f"Feedback sent to student for {qid}.")
        raise ApplicationHandlerStop

    if update.message.photo:
        caption = (update.message.caption or "").strip()
        solution_text, same_msg_feedback = parse_teacher_dm_text(caption)
        await deliver_teacher_solution(
            context,
            qid,
            teacher_id,
            teacher_name,
            solution_text or caption or "See attached solution image.",
            update.message.photo[-1].file_id,
            caption,
            same_msg_feedback,
        )
        if same_msg_feedback.strip():
            await update.message.reply_text(f"Solution and feedback sent to student for {qid}.")
        else:
            upsert_teacher_session(
                teacher_id,
                qid,
                "awaiting_feedback",
                draft_solution=solution_text or caption or "See attached solution image.",
                draft_photo=update.message.photo[-1].file_id,
                draft_caption=caption,
            )
            await update.message.reply_text("Solution sent. Any feedback for student? Send text now, or type skip.")
        raise ApplicationHandlerStop

    raw_text = (update.message.text or "").strip()
    if not raw_text:
        await update.message.reply_text("Send text/photo solution in DM.")
        raise ApplicationHandlerStop

    solution_text, same_msg_feedback = parse_teacher_dm_text(raw_text)
    if not solution_text:
        await update.message.reply_text("Please include a solution. Example:\nSolution: ...")
        raise ApplicationHandlerStop

    await deliver_teacher_solution(
        context,
        qid,
        teacher_id,
        teacher_name,
        solution_text,
        None,
        "",
        same_msg_feedback,
    )
    if same_msg_feedback.strip():
        await update.message.reply_text(f"Solution and feedback sent to student for {qid}.")
    else:
        upsert_teacher_session(
            teacher_id,
            qid,
            "awaiting_feedback",
            draft_solution=solution_text,
            draft_photo=None,
            draft_caption="",
        )
        await update.message.reply_text("Solution sent. Any feedback for student? Send text now, or type skip.")
    raise ApplicationHandlerStop

async def handle_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id != GROUP_CHAT_ID:
        return

    if update.message.text and update.message.text.startswith("/"):
        return

    qid = None
    txt = (update.message.text or "") + "\n" + (update.message.caption or "")
    m = re.search(qid_pattern(), txt)
    if m:
        qid = m.group()
    elif update.message.reply_to_message:
        ref = (update.message.reply_to_message.text or "") + "\n" + (update.message.reply_to_message.caption or "")
        m2 = re.search(qid_pattern(), ref)
        if m2:
            qid = m2.group()
    if not qid:
        return

    t = get_ticket(qid)
    if not t or t["status"] != "pending_teacher":
        return

    teacher_id = update.message.from_user.id
    teacher_name = update.message.from_user.username or update.message.from_user.first_name or "teacher"

    if t.get("claimed_by") and int(t["claimed_by"]) != teacher_id:
        await update.message.reply_text(f"This ticket is claimed by @{t.get('claimed_by_name','teacher')}.")
        return

    if not t.get("claimed_by"):
        await update.message.reply_text(f"Claim this ticket first using /claim {t.get('claim_code') or claim_code_from_qid(qid)}")
        return

    await update.message.reply_text("Please send the solution in private DM to the bot. Group replies are disabled after claim.")

async def post_init(app):
    DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await resume_reminders(app.bot)
    except Exception as e:
        print(f"⚠️ resume_reminders failed (DB issue): {e}")
    try:
        start_student_reminders(app.bot)
    except Exception as e:
        print(f"⚠️ start_student_reminders failed: {e}")

    try:
        start_mentorship_scheduler(app.bot)
    except Exception as e:
        print(f"start_mentorship_scheduler failed: {e}")

def run_flask_server():
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Flask health check server starting on port {port}")
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def main():
    flask_thread = Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    print("✅ Flask thread started")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    non_command_messages = filters.PHOTO | filters.CONTACT | (filters.TEXT & ~filters.COMMAND)

    app.add_handler(CommandHandler("start", start), group=0)
    app.add_handler(CommandHandler("mentorship", mentorship), group=0)
    app.add_handler(CommandHandler("accept_student", accept_student), group=0)
    app.add_handler(CommandHandler("timetable", timetable_command), group=0)
    app.add_handler(CommandHandler("showbacklog", show_backlog_command), group=0)
    app.add_handler(CommandHandler("mentorreply", mentorreply), group=0)
    app.add_handler(CommandHandler("claim", claim), group=0)
    app.add_handler(CommandHandler("unclaim", unclaim), group=0)
    app.add_handler(CommandHandler("hold", hold_ticket), group=0)
    app.add_handler(CommandHandler("resume", resume_ticket), group=0)
    app.add_handler(CommandHandler("resetlimit", reset_limit), group=0)
    app.add_handler(CommandHandler("setpremium", set_premium), group=0)
    app.add_handler(CommandHandler("setfree", set_free), group=0)
    app.add_handler(CommandHandler("available", set_available), group=0)
    app.add_handler(CommandHandler("offline", set_offline), group=0)
    app.add_handler(CommandHandler("checkadmin", check_admin), group=0)
    app.add_handler(CommandHandler("addteacher", add_teacher), group=0)
    app.add_handler(CommandHandler("viewimg", viewimg), group=0)
    app.add_handler(CommandHandler("uturn", handle_uturn), group=0)

    app.add_handler(CallbackQueryHandler(handle_callback_query), group=0)
    app.add_handler(CommandHandler("resetregistration", reset_registration_command))
    
    app.add_handler(MessageHandler(filters.Chat(GROUP_CHAT_ID) & non_command_messages, handle_group_reply), group=0)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & non_command_messages, handle_teacher_dm), group=0)
    app.add_handler(MessageHandler(non_command_messages & ~filters.Chat(GROUP_CHAT_ID), handle_user), group=1)

    print("🤖 AI Doubt Bot (PostgreSQL) is running...")
    print("📡 Polling for messages...")
    app.run_polling()

if __name__ == "__main__":
    main()
