#!/usr/bin/env python3
"""
Watchdog script to monitor Discord bot health
Checks if bot is responsive and restarts if needed
"""

import os
import subprocess
import time
from datetime import datetime

LOG_FILE = "logs/watchdog.log"
SERVICE_NAME = "discord-bot.service"
TIMEOUT = 300  # 5 minutes


def log(message):
    """Write message to log file"""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"[{timestamp}] {message}\n")


def is_service_active():
    """Check if systemd service is active"""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME], capture_output=True, text=True
        )
        return result.stdout.strip() == "active"
    except Exception as e:
        log(f"Error checking service status: {e}")
        return False


def restart_service():
    """Restart the systemd service"""
    try:
        subprocess.run(["sudo", "systemctl", "restart", SERVICE_NAME], check=True)
        log("Service restarted successfully")
        return True
    except Exception as e:
        log(f"CRITICAL: Failed to restart service: {e}")
        return False


def check_log_activity():
    """Check if bot has written to log recently"""
    log_path = "logs/discord.log"
    try:
        if not os.path.exists(log_path):
            return True  # New bot, no log yet

        mtime = os.path.getmtime(log_path)
        age = time.time() - mtime

        # If log hasn't been updated in TIMEOUT seconds, bot might be stuck
        return age < TIMEOUT
    except Exception as e:
        log(f"Error checking log activity: {e}")
        return True  # Assume OK if we can't check


def main():
    if not is_service_active():
        log("Service is not active")
        restart_service()
        return

    if not check_log_activity():
        log("Bot appears unresponsive (no log activity)")
        restart_service()
        return

    # Everything OK
    log("Health check passed")


if __name__ == "__main__":
    main()
