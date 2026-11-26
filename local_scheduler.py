import time
import subprocess
import sys
import os
from datetime import datetime, timezone

import analytics_engine

# Fallback slots (IST): 11:15 AM and 4:45 PM daily
DEFAULT_SCHEDULED_HOURS = [(11, 15), (16, 45)]
WEEKLY_AUDIT_SLOT = (10, 0)  # Sundays at 10:00 AM IST (or automatically whenever >= 7 days due)


def _get_scheduled_slots():
    strat = analytics_engine.load_active_strategy()
    slots = strat.get("trigger_policy", {}).get("daily_slots_ist")
    if isinstance(slots, list) and slots:
        parsed = []
        for s in slots:
            if isinstance(s, (list, tuple)) and len(s) == 2:
                parsed.append((int(s[0]), int(s[1])))
        if parsed:
            return parsed
    return DEFAULT_SCHEDULED_HOURS


def run_generator():
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 Triggering Scheduled Video Generator...")
    python_exe = sys.executable
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generator.py")
    try:
        proc = subprocess.run([python_exe, script_path], check=True)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✅ Run completed successfully (code {proc.returncode}).")
    except subprocess.CalledProcessError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ Run failed with code {e.returncode}.")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ❌ Run exception: {e}")


def run_weekly_audit_if_needed(force=False):
    if not force and not analytics_engine.is_weekly_analysis_due():
        return
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 📊 Running Weekly YouTube Dashboard Audit...")
    try:
        strat = analytics_engine.run_weekly_dashboard_analysis(force=force, send_telegram=True)
        tp = strat.get("trigger_policy", {})
        if tp.get("breakout_bonus_dispatch"):
            print("🚀 Breakout winner detected by weekly audit — triggering bonus video run!")
            run_generator()
    except Exception as e:
        print(f"⚠️ Weekly dashboard audit error: {e}")


def main():
    print("⏰ CineMind Analytics-Driven Auto-Scheduler active.")
    print("Scheduled trigger times (IST): Dynamic from channel_strategy.json (Default: 11:15 AM & 04:45 PM).")
    print("Weekly YouTube Dashboard Audit: Sundays 10:00 AM IST (and auto-catchup when >= 7 days due).")
    print("Press Ctrl+C to exit.\n")

    # Catch up immediately on startup if weekly audit is missing or >= 7 days old
    run_weekly_audit_if_needed(force=False)

    last_triggered_slot = None
    last_audit_date = None

    while True:
        now = datetime.now()
        utc_hour = datetime.now(timezone.utc).hour
        current_time = (now.hour, now.minute)
        today = now.strftime("%Y-%m-%d")

        # Once per week on Sunday at WEEKLY_AUDIT_SLOT
        if now.strftime("%A") == "Sunday" and current_time == WEEKLY_AUDIT_SLOT and last_audit_date != today:
            run_weekly_audit_if_needed(force=True)
            last_audit_date = today

        for sched_h, sched_m in _get_scheduled_slots():
            if current_time == (sched_h, sched_m) and last_triggered_slot != (today, sched_h, sched_m):
                should_run, reason = analytics_engine.should_trigger_generation(
                    event_name="schedule", current_utc_hour=utc_hour, now_ist=now
                )
                print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] 🎯 Trigger Gate: should_run={should_run} | {reason}")
                if should_run:
                    run_generator()
                last_triggered_slot = (today, sched_h, sched_m)

        time.sleep(30)


if __name__ == "__main__":
    main()
