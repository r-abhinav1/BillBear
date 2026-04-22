#!/usr/bin/env python3
"""
BillBear startup script — always uses the right Python and dependencies.
Usage:  python3 run.py
"""
import os
import sys
import socket
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
PORT = int(os.environ.get("PORT", 5001))


def die(msg):
    print(f"\n❌  {msg}\n")
    sys.exit(1)


# 1. Make sure the venv exists
if not os.path.exists(VENV_PYTHON):
    die(
        "Virtual environment not found at .venv/\n"
        "   Fix: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    )

# 2. Make sure core packages are installed
check = subprocess.run(
    [VENV_PYTHON, "-c", "import flask, redis, qrcode, PIL, xhtml2pdf"],
    capture_output=True, text=True, cwd=ROOT
)
if check.returncode != 0:
    print("⚙️  Installing dependencies...")
    subprocess.run(
        [VENV_PYTHON, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=ROOT, check=True
    )

# 3. Make sure the port is free
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    if s.connect_ex(("127.0.0.1", PORT)) == 0:
        die(
            f"Port {PORT} is already in use.\n"
            f"   Fix: run  lsof -ti:{PORT} | xargs kill -9\n"
            f"   Or use a different port:  PORT=5002 python3 run.py"
        )

print(f"\n🐻 BillBear starting on  http://localhost:{PORT}")
print("   Press CTRL+C to stop\n")

# 4. Run the app with the venv Python
os.chdir(ROOT)
os.execv(VENV_PYTHON, [VENV_PYTHON, os.path.join(ROOT, "app.py")])
