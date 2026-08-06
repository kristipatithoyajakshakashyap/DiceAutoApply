import sys

if sys.version_info < (3, 11):
    print("❌ ERROR: Python 3.11+ required. Download from python.org/downloads")
    sys.exit(1)

try:
    import playwright
except ImportError:
    print("❌ ERROR: Run this first: pip install -r requirements.txt")
    sys.exit(1)

import getpass
import os
import re
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
KEYWORDS_FILE = Path(__file__).parent / "keywords.txt"
CONFIG_FILE = Path(__file__).parent / "config.py"

# ── Speed presets (batch_size, batch_break_minutes) ──────────
SPEED_PRESETS = {
    "1": {"label": "50–100  (Low — very safe, slower results)",    "batch_size": (10, 15),  "break": (8, 12)},
    "2": {"label": "100–200 (Medium — safe, steady results)",      "batch_size": (20, 30),  "break": (6, 10)},
    "3": {"label": "200–300 (High — recommended ✅)",              "batch_size": (60, 80),  "break": (3, 5)},
    "4": {"label": "300–400 (Max — faster but higher visibility)", "batch_size": (80, 100), "break": (2, 3)},
}


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def banner():
    print("Welcome to DiceBot Setup 🤖")
    print("━" * 30)
    print()


def prompt_choice(prompt: str, valid: set) -> str:
    while True:
        val = input(prompt).strip()
        if val in valid:
            return val
        print(f"  Please enter one of: {', '.join(sorted(valid))}")


def prompt_multiselect(prompt: str, options: dict, min_required: int = 0) -> list:
    """options: {key: label}. User enters comma-separated keys, e.g. "1,3".
    Returns the list of selected keys in options' insertion order."""
    while True:
        print(prompt)
        for k, v in options.items():
            print(f"  [{k}] {v}")
        raw = input("  Enter choice(s), comma-separated (or leave blank): ").strip()
        if not raw:
            selected = []
        else:
            picks = {p.strip() for p in raw.split(",")}
            if not picks.issubset(options.keys()):
                print(f"  Please enter one or more of: {', '.join(options.keys())}")
                continue
            selected = [k for k in options if k in picks]
        if len(selected) < min_required:
            print(f"  Please select at least {min_required} option(s).")
            continue
        return selected


# ── Step 1 — Credentials ─────────────────────────────────────
def step_credentials():
    print("Step 1/5 — Dice.com Credentials")
    print()

    while True:
        email = input("  Enter your Dice email: ").strip()
        if re.match(r"[^@]+@[^@]+\.[^@]+", email):
            print("  ✅ Format looks good")
            break
        print("  ❌ That doesn't look like a valid email, try again.")

    print()
    while True:
        password = getpass.getpass("  Enter your Dice password: ")
        if password:
            break
        print("  ❌ Password cannot be empty.")

    return email, password


# ── Step 2 — Job Type ─────────────────────────────────────────
JOB_TYPE_OPTIONS = {
    "1": ("fulltime",     "Full-time"),
    "2": ("contract_w2",  "Contract — W2"),
    "3": ("contract_c2c", "Contract — Corp-to-Corp (C2C)"),
}


def step_job_type():
    print()
    print("Step 2/6 — Job Type Preference")
    print()
    print("  What type(s) of roles are you targeting? (select any combination)")
    print()
    keys = prompt_multiselect(
        "  Choose one or more:",
        {k: v[1] for k, v in JOB_TYPE_OPTIONS.items()},
        min_required=1,
    )
    job_types = [JOB_TYPE_OPTIONS[k][0] for k in keys]
    label = " + ".join(JOB_TYPE_OPTIONS[k][1] for k in keys)
    return job_types, label


# ── Step 3 — Workplace Type ─────────────────────────────────────
WORKPLACE_TYPE_OPTIONS = {
    "1": ("onsite", "On-Site"),
    "2": ("hybrid", "Hybrid"),
    "3": ("remote", "Remote"),
}


def step_workplace_type():
    print()
    print("Step 3/6 — Workplace Type Preference")
    print()
    print("  Which workplace arrangement(s)? Leave blank for open to any.")
    print()
    keys = prompt_multiselect(
        "  Choose zero or more:",
        {k: v[1] for k, v in WORKPLACE_TYPE_OPTIONS.items()},
        min_required=0,
    )
    workplace_types = [WORKPLACE_TYPE_OPTIONS[k][0] for k in keys]
    label = " + ".join(WORKPLACE_TYPE_OPTIONS[k][1] for k in keys) if keys else "Open to any"
    return workplace_types, label


# ── Step 4 — Screening Questions ────────────────────────────────
RACE_OPTIONS = {
    "1": "Asian",
    "2": "White",
    "3": "Black or African American",
    "4": "Hispanic or Latino",
    "5": "Two or More Races",
    "6": "Prefer not to answer",
}


def step_screening():
    print()
    print("Step 4/7 — Screening Question Answers")
    print()
    print("  Some Easy Apply forms ask EEO/screening questions.")
    print("  These answers will be used to auto-fill matching questions.")
    print()

    onsite = prompt_choice("  Are you OK with onsite interviews? (y/n): ", {"y", "n", "Y", "N"})
    veteran = prompt_choice("  Are you a veteran? (y/n): ", {"y", "n", "Y", "N"})
    disability = prompt_choice("  Do you have a disability? (y/n): ", {"y", "n", "Y", "N"})

    print()
    race_key = prompt_choice(
        "  What is your race/ethnicity?\n"
        + "\n".join(f"  [{k}] {v}" for k, v in RACE_OPTIONS.items())
        + "\n  Enter choice: ",
        set(RACE_OPTIONS.keys()),
    )

    return {
        "onsite_interview": "yes" if onsite.lower() == "y" else "no",
        "veteran": "yes" if veteran.lower() == "y" else "no",
        "disability": "yes" if disability.lower() == "y" else "no",
        "race": RACE_OPTIONS[race_key],
    }


# ── Step 5 — Keywords ─────────────────────────────────────────
def step_keywords():
    print()
    print("Step 5/7 — Your Keywords")
    print()
    print('  Enter your target role/skills (e.g. Python, AWS, DevOps)')
    print()
    print("  💡 Not sure what keywords to use? Ask ChatGPT this:")
    print("  ┌─────────────────────────────────────────────────────┐")
    print('  │ "Give me 30 Dice.com job search keywords for a      │')
    print('  │  [YOUR ROLE] with skills in [YOUR SKILLS].          │')
    print('  │  Format: one keyword per line, no bullets."         │')
    print("  └─────────────────────────────────────────────────────┘")
    print()
    print("  Paste your keywords below (one per line, empty line to finish):")
    print()

    keywords = []
    while True:
        line = input("  ").strip()
        if not line:
            if keywords:
                break
            print("  (Enter at least one keyword)")
            continue
        if not line.startswith("#"):
            keywords.append(line)

    return keywords


# ── Step 6 — Speed ────────────────────────────────────────────
def step_speed():
    print()
    print("Step 6/7 — Daily Application Speed")
    print()
    print("  How many applications per day?")
    print("  Recommended: 250 (safe) | Max: 400 | Minimum: 50")
    print()
    for k, v in SPEED_PRESETS.items():
        print(f"  [{k}] {v['label']}")
    print()
    choice = prompt_choice("  Enter choice (1/2/3/4): ", set(SPEED_PRESETS.keys()))
    return choice, SPEED_PRESETS[choice]


# ── Step 7 — Review ───────────────────────────────────────────
def step_review(email, job_label, workplace_label, screening, keywords, speed_label):
    print()
    print("Step 7/7 — Review & Confirm")
    print()
    print(f"  ✅ Email:           {email}")
    print(f"  ✅ Job Type:        {job_label}")
    print(f"  ✅ Workplace Type:  {workplace_label}")
    print(f"  ✅ Onsite Interview:{'  Yes' if screening['onsite_interview'] == 'yes' else '  No'}")
    print(f"  ✅ Veteran:          {'Yes' if screening['veteran'] == 'yes' else 'No'}")
    print(f"  ✅ Disability:       {'Yes' if screening['disability'] == 'yes' else 'No'}")
    print(f"  ✅ Race/Ethnicity:   {screening['race']}")
    print(f"  ✅ Keywords:        {len(keywords)} loaded")
    print(f"  ✅ Daily Limit:     {speed_label}")
    print()
    choice = prompt_choice("  Save and start? (y/n): ", {"y", "n", "Y", "N"})
    return choice.lower() == "y"


# ── Write files ───────────────────────────────────────────────
def write_env(email: str, password: str):
    lines = []
    existing = {}

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    existing["DICE_EMAIL"] = email
    existing["DICE_PASSWORD"] = password

    for k, v in existing.items():
        lines.append(f"{k}={v}")

    ENV_FILE.write_text("\n".join(lines) + "\n")


def write_keywords(keywords: list):
    KEYWORDS_FILE.write_text("\n".join(keywords) + "\n")


def patch_config(job_types: list, workplace_types: list, screening: dict, batch_size: tuple, batch_break: tuple):
    text = CONFIG_FILE.read_text()

    # Job types
    types_repr = repr(job_types)
    text = re.sub(
        r"^JOB_TYPES\s*=\s*.+$",
        f"JOB_TYPES       = {types_repr}",
        text,
        flags=re.MULTILINE,
    )

    # Workplace types
    workplace_repr = repr(workplace_types)
    text = re.sub(
        r"^WORKPLACE_TYPES\s*=\s*.+$",
        f"WORKPLACE_TYPES = {workplace_repr}",
        text,
        flags=re.MULTILINE,
    )

    # Batch size
    text = re.sub(
        r"^BATCH_SIZE\s*=\s*.+$",
        f"BATCH_SIZE            = {batch_size}",
        text,
        flags=re.MULTILINE,
    )

    # Batch break minutes
    text = re.sub(
        r"^BATCH_BREAK_MINUTES\s*=\s*.+$",
        f"BATCH_BREAK_MINUTES   = {batch_break}",
        text,
        flags=re.MULTILINE,
    )

    # EEO / screening answers — multi-line dict, replace the whole block
    eeo_repr = (
        "EEO_ANSWERS = {\n"
        f"    \"onsite_interview\": {screening['onsite_interview']!r},\n"
        f"    \"veteran\":          {screening['veteran']!r},\n"
        f"    \"disability\":       {screening['disability']!r},\n"
        f"    \"race\":             {screening['race']!r},\n"
        "}"
    )
    text = re.sub(
        r"EEO_ANSWERS\s*=\s*\{[^}]*\}",
        eeo_repr,
        text,
        flags=re.MULTILINE | re.DOTALL,
    )

    CONFIG_FILE.write_text(text)


# ── Main ──────────────────────────────────────────────────────
def main():
    clear()
    banner()

    email, password = step_credentials()
    job_types, job_label = step_job_type()
    workplace_types, workplace_label = step_workplace_type()
    screening = step_screening()
    keywords = step_keywords()
    speed_choice, speed_preset = step_speed()
    confirmed = step_review(email, job_label, workplace_label, screening, keywords, speed_preset["label"])

    if not confirmed:
        print()
        print("  Setup cancelled. Run python setup.py again when ready.")
        sys.exit(0)

    write_env(email, password)
    write_keywords(keywords)
    patch_config(job_types, workplace_types, screening, speed_preset["batch_size"], speed_preset["break"])

    print()
    print("━" * 30)
    print("✅ Setup complete! Run:  python dice_bot.py")
    print()
    print("💬 Join the community: https://t.me/+rz7W7lhhUEkwOTM1")
    print("📧 Support: trinath.connect@proton.me")
    print()


if __name__ == "__main__":
    main()
