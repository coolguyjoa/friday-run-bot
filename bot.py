"""
Friday Run Bot v2 — always-on Telegram bot.

Flow each Thursday 8pm SGT:
  1. Post Friday weather (6/7/8/9/10pm)
  2. Each participant picks their end-work time (buttons)
  3. Once all have answered -> area-to-run vote (buttons + free-text "Other")
  4. Once all have answered -> meet time at Yio Chu Kang MRT (buttons + free-text "Other")
  5. Once all have answered -> auto-summary. Rain overrides area choice to Stadium.

10pm SGT: if the flow isn't finished, nudge whoever hasn't answered the current step.

Admin-only commands: /pause /resume /addlocation <name> /testrun /testreminder
Everyone: /myid (find your Telegram ID to give to the admin)

Env vars required:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID     (negative number, the group chat)
  ADMIN_IDS            comma-separated Telegram user IDs, e.g. "111,222"
  PARTICIPANTS         "id:Name,id:Name,id:Name" — the fixed roster of runners
"""

import os
import json
import logging
from datetime import datetime, timedelta, time, timezone
from collections import Counter

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ForceReply,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("friday-run-bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
PARTICIPANTS = {}
for pair in os.environ.get("PARTICIPANTS", "").split(","):
    if ":" in pair:
        uid, name = pair.split(":", 1)
        PARTICIPANTS[int(uid.strip())] = name.strip()

STATE_FILE = "state.json"
SGT = timezone(timedelta(hours=8))
LAT, LON = 1.3868, 103.8449  # near Yio Chu Kang

EWT_OPTIONS = ["Before 6pm", "6-7pm", "7-8pm", "After 8pm"]
MEET_TIME_OPTIONS = ["6:30pm", "7:00pm", "7:30pm", "8:00pm"]
DEFAULT_LOCATIONS = ["MBS", "Bishan", "ECP", "Stadium"]


# ---------- persistence ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"paused": False, "locations": DEFAULT_LOCATIONS[:], "cycle": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


STATE = load_state()


def new_cycle(weather_text, rainy, date_label):
    return {
        "stage": "ewt",  # ewt -> area -> meet -> done
        "date_label": date_label,
        "weather_text": weather_text,
        "rainy": rainy,
        "answers": {"ewt": {}, "area": {}, "meet": {}},
        "awaiting_other": {},  # user_id -> "area" | "meet", set while waiting for a free-text reply
    }


# ---------- weather ----------

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
    for h in [18, 19, 20, 21, 22]:
        idx = hours.index(f"{date_str}T{h:02d}:00")
        r, t = rain[idx], temps[idx]
        icon = "\U0001F327" if r >= 50 else ("\u26C5" if r >= 20 else "\u2600")
        if r >= 50:
            rainy = True
        lines.append(f"{h}:00 {icon}  {t:.0f}\u00b0C, {r}% rain")
    return "\n".join(lines), rainy, friday.strftime("%A %d %b")


# ---------- keyboards ----------

def kb_from_options(prefix, options, include_other=True):
    rows = [[InlineKeyboardButton(o, callback_data=f"{prefix}:{o}")] for o in options]
    if include_other:
        rows.append([InlineKeyboardButton("Other \u270D", callback_data=f"{prefix}:__other__")])
    return InlineKeyboardMarkup(rows)


def progress_text(stage, answers):
    lines = []
    for uid, name in PARTICIPANTS.items():
        val = answers[stage].get(str(uid))
        mark = f"\u2705 {val}" if val else "\u23F3 pending"
        lines.append(f"{name}: {mark}")
    return "\n".join(lines)


# ---------- flow steps ----------

async def start_prompt(context: ContextTypes.DEFAULT_TYPE):
    if STATE["paused"]:
        log.info("Bot paused, skipping Thursday prompt.")
        return
    weather_text, rainy, date_label = get_friday_weather()
    STATE["cycle"] = new_cycle(weather_text, rainy, date_label)
    save_state(STATE)

    rain_note = (
        "\U0001F327 Rain likely \u2014 default run spot will be *Singapore Stadium* (sheltered) unless it clears up."
        if rainy
        else "\u2600 Looking dry for the run."
    )
    await context.bot.send_message(
        CHAT_ID,
        f"*Friday Run Check-in \u2014 {date_label}*\n\n*Weather:*\n{weather_text}\n\n{rain_note}",
        parse_mode="Markdown",
    )
    msg = await context.bot.send_message(
        CHAT_ID,
        f"*Step 1/3 \u2014 What time do you end work?*\n\n{progress_text('ewt', STATE['cycle']['answers'])}",
        parse_mode="Markdown",
        reply_markup=kb_from_options("ewt", EWT_OPTIONS, include_other=False),
    )
    STATE["cycle"]["ewt_message_id"] = msg.message_id
    save_state(STATE)


async def advance_to_area(context: ContextTypes.DEFAULT_TYPE):
    cycle = STATE["cycle"]
    cycle["stage"] = "area"
    save_state(STATE)
    msg = await context.bot.send_message(
        CHAT_ID,
        f"*Step 2/3 \u2014 Where should we run?*\n\n{progress_text('area', cycle['answers'])}",
        parse_mode="Markdown",
        reply_markup=kb_from_options("area", STATE["locations"]),
    )
    cycle["area_message_id"] = msg.message_id
    save_state(STATE)


async def advance_to_meet(context: ContextTypes.DEFAULT_TYPE):
    cycle = STATE["cycle"]
    cycle["stage"] = "meet"
    save_state(STATE)
    msg = await context.bot.send_message(
        CHAT_ID,
        f"*Step 3/3 \u2014 What time to meet at Yio Chu Kang MRT?*\n\n{progress_text('meet', cycle['answers'])}",
        parse_mode="Markdown",
        reply_markup=kb_from_options("meet", MEET_TIME_OPTIONS),
    )
    cycle["meet_message_id"] = msg.message_id
    save_state(STATE)


async def finish_cycle(context: ContextTypes.DEFAULT_TYPE):
    cycle = STATE["cycle"]
    cycle["stage"] = "done"
    save_state(STATE)

    area_votes = list(cycle["answers"]["area"].values())
    meet_votes = list(cycle["answers"]["meet"].values())
    final_area = "Singapore Stadium (sheltered \u2014 rain called it)" if cycle["rainy"] else Counter(area_votes).most_common(1)[0][0]
    final_meet = Counter(meet_votes).most_common(1)[0][0]

    lines = [f"*Summary \u2014 {cycle['date_label']}*", ""]
    for uid, name in PARTICIPANTS.items():
        ewt = cycle["answers"]["ewt"].get(str(uid), "?")
        area = cycle["answers"]["area"].get(str(uid), "?")
        meet = cycle["answers"]["meet"].get(str(uid), "?")
        lines.append(f"{name}: ends {ewt}, wants {area}, meet {meet}")
    lines += [
        "",
        f"\U0001F4CD Run spot: *{final_area}*",
        f"\U0001F550 Meet at Yio Chu Kang MRT: *{final_meet}*",
    ]
    await context.bot.send_message(CHAT_ID, "\n".join(lines), parse_mode="Markdown")


async def record_answer(context, stage, user_id, value):
    cycle = STATE["cycle"]
    cycle["answers"][stage][str(user_id)] = value
    save_state(STATE)

    message_id = cycle.get(f"{stage}_message_id")
    if message_id:
        options = {"ewt": EWT_OPTIONS, "area": STATE["locations"], "meet": MEET_TIME_OPTIONS}[stage]
        include_other = stage != "ewt"
        step_label = {"ewt": "Step 1/3 \u2014 What time do you end work?",
                      "area": "Step 2/3 \u2014 Where should we run?",
                      "meet": "Step 3/3 \u2014 What time to meet at Yio Chu Kang MRT?"}[stage]
        try:
            await context.bot.edit_message_text(
                chat_id=CHAT_ID,
                message_id=message_id,
                text=f"*{step_label}*\n\n{progress_text(stage, cycle['answers'])}",
                parse_mode="Markdown",
                reply_markup=kb_from_options(stage, options, include_other=include_other),
            )
        except Exception as e:
            log.warning(f"edit failed: {e}")

    if len(cycle["answers"][stage]) >= len(PARTICIPANTS):
        if stage == "ewt":
            await advance_to_area(context)
        elif stage == "area":
            await advance_to_meet(context)
        elif stage == "meet":
            await finish_cycle(context)


# ---------- handlers ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if STATE["paused"] or not STATE.get("cycle"):
        return

    user_id = query.from_user.id
    if PARTICIPANTS and user_id not in PARTICIPANTS:
        return  # not part of the roster, ignore

    stage, value = query.data.split(":", 1)
    cycle = STATE["cycle"]
    if cycle["stage"] != stage:
        return  # tapped a stale/old-stage button

    if value == "__other__":
        cycle["awaiting_other"][str(user_id)] = stage
        save_state(STATE)
        name = PARTICIPANTS.get(user_id, query.from_user.first_name)
        await context.bot.send_message(
            CHAT_ID,
            f"{name}, reply to this message with your suggestion \u2b07",
            reply_markup=ForceReply(selective=True),
        )
        return

    await record_answer(context, stage, user_id, value)


async def on_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STATE.get("cycle"):
        return
    user_id = update.effective_user.id
    cycle = STATE["cycle"]
    stage = cycle["awaiting_other"].pop(str(user_id), None)
    if not stage:
        return
    save_state(STATE)
    await record_answer(context, stage, user_id, update.message.text.strip())


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_IDS


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    STATE["paused"] = True
    save_state(STATE)
    await update.message.reply_text("\u23F8 Bot paused. No prompts or reminders until /resume.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    STATE["paused"] = False
    save_state(STATE)
    await update.message.reply_text("\u25B6 Bot resumed.")


async def cmd_addlocation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /addlocation <name>")
        return
    name = " ".join(context.args)
    if name not in STATE["locations"]:
        STATE["locations"].append(name)
        save_state(STATE)
    await update.message.reply_text(f"Added \"{name}\" to the location list.")


async def cmd_testrun(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await start_prompt(context)


async def cmd_testreminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await send_reminder(context)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    if STATE["paused"] or not STATE.get("cycle"):
        return
    cycle = STATE["cycle"]
    if cycle["stage"] == "done":
        return
    missing = [
        name for uid, name in PARTICIPANTS.items()
        if str(uid) not in cycle["answers"][cycle["stage"]]
    ]
    if not missing:
        return
    await context.bot.send_message(
        CHAT_ID,
        f"\u23F0 Still waiting on: {', '.join(missing)} \u2014 please fill in the current step above \U0001F446",
    )


async def scheduled_reminder(context: ContextTypes.DEFAULT_TYPE):
    await send_reminder(context)


# ---------- app ----------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("addlocation", cmd_addlocation))
    app.add_handler(CommandHandler("testrun", cmd_testrun))
    app.add_handler(CommandHandler("testreminder", cmd_testreminder))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, on_text_reply))

    jq = app.job_queue
    jq.run_daily(start_prompt, time=time(hour=12, minute=0, tzinfo=timezone.utc), days=(3,))
    jq.run_daily(scheduled_reminder, time=time(hour=14, minute=0, tzinfo=timezone.utc), days=(3,))

    log.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
