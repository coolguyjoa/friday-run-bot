"""
Friday Run Bot — final version (4 scheduled touches), with persistent answers.

MODE=prompt  (5:30pm SGT Thu) -> weather + 3 polls, all at once (join / area / meet time)
MODE=check1  (7:00pm SGT Thu) -> if everyone's done (or cancelled), post summary now.
                                  If not, stay silent — no message sent.
MODE=remind  (8:00pm SGT Thu) -> if still not done, send a "still missing" nudge.
MODE=final   (9:00pm SGT Thu) -> post the summary no matter what, marking anyone
                                  who never answered as "no response".

IMPORTANT: answers are accumulated permanently in state.json as they come in
(state["cycle"]["answers"]) — each run only reads *new* votes from Telegram
since the last run and merges them in, so a vote cast at 7:10pm is still
remembered at the 9pm final check even though Telegram only reports it once.

A vote for "Event Cancelled" on the join poll, seen at any of the three
check-type runs, immediately posts a cancellation message and ends the cycle.

Votes can be changed freely — Telegram polls support this natively as long
as the poll is never closed, which this bot never does.

Kill switch and locations are controlled by a separate admin workflow
(admin_action.py) that edits state.json — this script just reads it.

Env vars required:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID   (negative number, the group chat)
  PARTICIPANTS       "id:Name,id:Name,id:Name" — the fixed roster of runners
  MODE               "prompt" | "check1" | "remind" | "final"
"""

import os
import json
from datetime import datetime, timedelta, timezone
from collections import Counter

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
MODE = os.environ["MODE"]

PARTICIPANTS = {}
for pair in os.environ.get("PARTICIPANTS", "").split(","):
    if ":" in pair:
        uid, name = pair.split(":", 1)
        PARTICIPANTS[str(uid.strip())] = name.strip()  # keys are STRINGS throughout

STATE_FILE = "state.json"
SGT = timezone(timedelta(hours=8))
LAT, LON = 1.3868, 103.8449  # near Yio Chu Kang

JOIN_OPTIONS = ["Yes", "No", "Event Cancelled"]
MEET_TIME_OPTIONS = [
    "6pm - 7pm", "7pm - 8pm", "8pm - 9pm", "9pm - 10pm",
    "I'm okay to meet somewhere else",
]
RAIN_THRESHOLD = 75  # percent

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg(method, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error on {method}: {data}")
    return data["result"]


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_friday_weather():
    friday = datetime.now(SGT) + timedelta(days=1)
    date_str = friday.strftime("%Y-%m-%d")
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT,
            "longitude": LON,
            "hourly": "temperature_2m,precipitation_probability",
            "timezone": "Asia/Singapore",
            "start_date": date_str,
            "end_date": date_str,
        },
        timeout=15,
    ).json()
    hours = resp["hourly"]["time"]
    temps = resp["hourly"]["temperature_2m"]
    rain = resp["hourly"]["precipitation_probability"]
    lines, rainy = [], False
    for h in [18, 19, 20, 21]:
        idx = hours.index(f"{date_str}T{h:02d}:00")
        r, t = rain[idx], temps[idx]
        icon = "\U0001F327" if r >= 50 else ("\u26C5" if r >= 20 else "\u2600")
        if r >= RAIN_THRESHOLD:
            rainy = True
        lines.append(f"{h}:00 {icon}  {t:.0f}\u00b0C, {r}% rain")
    return "\n".join(lines), rainy, friday.strftime("%A %d %b")


def run_prompt(state):
    if state["paused"]:
        print("Paused, skipping prompt.")
        return

    weather_text, rainy, date_label = get_friday_weather()
    rain_note = (
        f"\U0001F327 Rain \u2265{RAIN_THRESHOLD}% likely \u2014 default run spot will be *Singapore Stadium* (sheltered)."
        if rainy
        else "\u2600 Looking fine for the run."
    )
    tg(
        "sendMessage",
        chat_id=CHAT_ID,
        text=(
            f"*Friday Run Check-in \u2014 {date_label}*\n\n*Weather:*\n{weather_text}\n\n{rain_note}\n\n"
            f"Fill in the polls below \U0001F447 (you can change your vote anytime)\n"
            f"First summary attempt 7pm, reminder 8pm if needed, final summary 9pm."
        ),
        parse_mode="Markdown",
    )

    area_options = state["locations"] + ["Others"]

    poll_join = tg("sendPoll", chat_id=CHAT_ID, question="Are you able to join Friday's run?",
                   options=json.dumps(JOIN_OPTIONS), is_anonymous=False)
    poll_area = tg("sendPoll", chat_id=CHAT_ID, question="Where should we run?",
                   options=json.dumps(area_options), is_anonymous=False)
    poll_meet = tg("sendPoll", chat_id=CHAT_ID, question="What time to meet at Yio Chu Kang MRT? (if you're meeting there)",
                   options=json.dumps(MEET_TIME_OPTIONS), is_anonymous=False)

    state["cycle"] = {
        "date_label": date_label,
        "rainy": rainy,
        "polls": {
            "join": poll_join["poll"]["id"],
            "area": poll_area["poll"]["id"],
            "meet": poll_meet["poll"]["id"],
        },
        "answers": {"join": {}, "area": {}, "meet": {}},  # persists across every future run
        "done": False,
    }
    save_state(state)


def apply_new_answers(state):
    """Fetch NEW poll_answer updates since the saved offset and merge them
    directly into state['cycle']['answers'], which persists forever. This is
    the fix: previous versions computed answers fresh each run and never
    saved them, so anyone whose vote was consumed by an earlier run
    'disappeared' from later runs. Now every vote is remembered permanently
    once seen, and a retracted vote removes that person's entry."""
    poll_ids = state["cycle"]["polls"]
    id_to_stage = {v: k for k, v in poll_ids.items()}
    persistent = state["cycle"]["answers"]

    offset = state.get("update_offset", 0)
    updates = tg("getUpdates", offset=offset, allowed_updates=json.dumps(["poll_answer"]))
    new_offset = offset
    for u in updates:
        new_offset = max(new_offset, u["update_id"] + 1)
        pa = u.get("poll_answer")
        if not pa:
            continue
        stage = id_to_stage.get(pa["poll_id"])
        if not stage:
            continue
        uid = str(pa["user"]["id"])
        options = {
            "join": JOIN_OPTIONS,
            "area": state["locations"] + ["Others"],
            "meet": MEET_TIME_OPTIONS,
        }[stage]
        if pa["option_ids"]:
            persistent[stage][uid] = options[pa["option_ids"][0]]
        else:
            persistent[stage].pop(uid, None)  # vote retracted

    state["update_offset"] = new_offset
    save_state(state)


def build_summary_lines(cycle, joining, declined, force=False):
    answers = cycle["answers"]
    area_votes = [answers["area"][uid] for uid in joining if uid in answers["area"]]
    final_area = "Singapore Stadium (sheltered \u2014 rain called it)" if cycle["rainy"] else (
        Counter(area_votes).most_common(1)[0][0] if area_votes else "TBD"
    )

    lines = [f"*Summary \u2014 {cycle['date_label']}*", ""]
    for uid, name in PARTICIPANTS.items():
        if uid in declined:
            lines.append(f"{name}: not joining")
        elif uid in joining:
            area = answers["area"].get(uid, "no response" if force else None)
            meet = answers["meet"].get(uid, "no response" if force else None)
            lines.append(f"{name}: wants {area}, meet {meet}")
        elif force:
            lines.append(f"{name}: no response")
    lines += ["", f"\U0001F4CD Run spot: *{final_area}*"]
    return lines


def evaluate(state, mode):
    """mode: 'check1' (silent if incomplete), 'remind' (nudge if incomplete),
    'final' (force summary regardless)."""
    if state["paused"] or not state.get("cycle") or state["cycle"]["done"]:
        print("Nothing to do.")
        return

    apply_new_answers(state)
    cycle = state["cycle"]
    answers = cycle["answers"]

    if "Event Cancelled" in answers["join"].values():
        who = [PARTICIPANTS.get(uid, "Someone") for uid, v in answers["join"].items() if v == "Event Cancelled"]
        tg("sendMessage", chat_id=CHAT_ID,
           text=f"\u274C *Friday run cancelled* (called by {', '.join(who)}).",
           parse_mode="Markdown")
        cycle["done"] = True
        save_state(state)
        return

    joining = {uid for uid, choice in answers["join"].items() if choice == "Yes"}
    declined = {uid for uid, choice in answers["join"].items() if choice == "No"}

    missing = []
    for uid, name in PARTICIPANTS.items():
        if uid not in answers["join"]:
            missing.append(f"{name} \u2014 joining?")
            continue
        if uid in declined:
            continue
        for stage, label in [("area", "run area"), ("meet", "meet time")]:
            if uid not in answers[stage]:
                missing.append(f"{name} \u2014 {label}")

    if not missing:
        lines = build_summary_lines(cycle, joining, declined)
        tg("sendMessage", chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown")
        cycle["done"] = True
        save_state(state)
        return

    if mode == "check1":
        print("Incomplete at 8pm check — staying silent.")
        return

    if mode == "remind":
        tg("sendMessage", chat_id=CHAT_ID,
           text="\u23F0 *Still missing:*\n" + "\n".join(missing) + "\n\nFinal summary at 9pm \u2014 please fill in above \U0001F446",
           parse_mode="Markdown")
        return

    if mode == "final":
        lines = build_summary_lines(cycle, joining, declined, force=True)
        tg("sendMessage", chat_id=CHAT_ID, text="\n".join(lines), parse_mode="Markdown")
        cycle["done"] = True
        save_state(state)
        return


if __name__ == "__main__":
    state = load_state()
    if MODE == "prompt":
        run_prompt(state)
    elif MODE in ("check1", "remind", "final"):
        evaluate(state, MODE)
    else:
        raise ValueError(f"Unknown MODE: {MODE}")
