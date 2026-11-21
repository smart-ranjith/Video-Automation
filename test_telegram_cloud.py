import os
import sys
import requests

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("TELEGRAM_CHAT_ID")

print("Checking Telegram credentials...")
if not bot_token:
    print("❌ TELEGRAM_BOT_TOKEN is not set.")
    sys.exit(1)
if not chat_id:
    print("❌ TELEGRAM_CHAT_ID is not set.")
    sys.exit(1)

print(f"Bot token prefix: {bot_token[:8]}..., Chat ID: {chat_id}")
msg = (
    "🎉 *Telegram Connection Verified!*\n\n"
    "Your CineMind AI Bot is now active and authorized to message you.\n"
    "You will receive instant live links whenever a new video is published autonomously!\n\n"
    "⏰ *Next Scheduled Upload:* Today at 4:45 PM IST."
)

r = requests.post(
    f"https://api.telegram.org/bot{bot_token}/sendMessage",
    json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
    timeout=15
)

print(f"Status Code: {r.status_code}")
print(f"Response: {r.text}")

if r.status_code == 200:
    print("✅ Telegram notification test PASSED successfully!")
    sys.exit(0)
else:
    print("❌ Telegram notification test FAILED.")
    sys.exit(1)
