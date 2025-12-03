#!/bin/bash
# فایل start.sh
# Make sure the script is executable: chmod +x start.sh

# نصب پکیج‌ها (اگر Render نصب نکرده باشه)
pip install -r requirements.txt

# اجرای ربات
python bot.py
