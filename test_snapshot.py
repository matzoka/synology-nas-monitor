import os
import struct
import sys

sys.path.insert(0, os.path.join("src", "package", "nasmon"))
import snapshot
import telegram


def check(name, condition):
    print(("PASS" if condition else "FAIL"), name)
    if not condition:
        global failed
        failed = True


failed = False
data = {
    "model": "TestNAS",
    "temperature": 76,
    "cpu": {"total": 24},
    "memory": {"usage": 36},
    "network": {"rxBps": 12345, "txBps": 6789},
    "disks": [
        {"name": "Drive 1", "unit": "TestNAS", "temp": 40, "status": "normal"},
        {"name": "Drive 2", "unit": "DX517-1", "temp": 44, "status": "normal"},
    ],
}
samples = [
    {"t": 1000, "cpu": 18, "mem": 35, "sys": 48, "disks": {"Drive 1": 39, "Drive 2": 41}},
    {"t": 1010, "cpu": 24, "mem": 36, "sys": 76, "disks": {"Drive 1": 40, "Drive 2": 44}},
]
cfg = {"warnTemp": 75, "critTemp": 85, "emergTemp": 90}
state = {"severity": "WARNING"}

png = snapshot.render(data, samples, cfg, state, "2026-08-20T21:45:00+09:00")
check("PNG signature", png.startswith(b"\x89PNG\r\n\x1a\n"))
width, height = struct.unpack(">II", png[16:24])
check("snapshot dimensions", width == 1200 and height >= 1200)
check("PNG has content", len(png) > 1000)
check("snapshot button is present", any(button.get("callback_data") == "snapshot_now"
                                         for row in telegram.BUTTONS["inline_keyboard"] for button in row))

sent = {}
old_upload = telegram._post_multipart
try:
    def fake_upload(token, method, fields, filename, content, content_type="image/png", timeout=20):
        sent.update({"token": token, "method": method, "fields": fields, "filename": filename,
                     "content": content, "content_type": content_type})
        return {"message_id": 1}
    telegram._post_multipart = fake_upload
    telegram.send_photo("123456:abcdefghijklmnopqrstuv", "99", png, "test caption", buttons=True)
finally:
    telegram._post_multipart = old_upload

check("sendPhoto endpoint", sent.get("method") == "sendPhoto")
check("photo payload", sent.get("content", b"").startswith(b"\x89PNG"))
check("photo has controls", "snapshot_now" in sent.get("fields", {}).get("reply_markup", ""))

print()
print("FAILED" if failed else "ALL SNAPSHOT TESTS PASSED")
sys.exit(1 if failed else 0)
