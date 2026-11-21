import time
import subprocess
import sys
import os
from datetime import datetime

# Dual-Peak Algorithmic Schedule (11:15 AM and 4:45 PM IST)
SCHEDULED_HOURS = [(11, 15), (16, 45)]

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

def main():
    print("⏰ CineMind Local Auto-Scheduler active.")
    print("Scheduled trigger times (IST): 11:15 AM and 04:45 PM daily.")
    print("Press Ctrl+C to exit.\n")
    
    last_triggered_date = None

    while True:
        now = datetime.now()
        current_time = (now.hour, now.minute)
        today = now.strftime("%Y-%m-%d")

        for sched_h, sched_m in SCHEDULED_HOURS:
            if current_time == (sched_h, sched_m) and last_triggered_date != (today, sched_h, sched_m):
                run_generator()
                last_triggered_date = (today, sched_h, sched_m)
        
        time.sleep(30)

if __name__ == "__main__":
    main()
