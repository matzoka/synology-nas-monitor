import os
import sys

sys.path.insert(0, os.path.join("src", "package", "nasmon"))
import server


def check(name, condition):
    print(("PASS" if condition else "FAIL"), name)
    if not condition:
        global failed
        failed = True


failed = False
cfg = {
    "baseUrl": "https://nas.example.test:5001", "account": "monitor", "passwd": "x",
    "tgToken": "test-token", "chatId": "99",
    "warnTemp": 75, "critTemp": 85, "emergTemp": 90, "pollSec": 10, "predictSec": 180,
}
state = {"severity": "WARNING", "notifyEnabled": True, "hushUntil": None}
data = {
    "model": "TestNAS", "temperature": 76,
    "cpu": {"total": 24}, "memory": {"usage": 36},
    "network": {"rxBps": 100, "txBps": 200},
    "disks": [{"name": "Drive 1", "unit": "TestNAS", "temp": 40, "status": "normal"}],
}
status = {"configured": True, "ok": True, "data": data, "severity": "WARNING",
          "notifyEnabled": True, "hushUntil": None, "fetchedAt": "2026-08-20T21:45:00+09:00"}

events = []
server.store.load_config = lambda: dict(cfg)
server.store.load_state = lambda: dict(state)
server.store.read_status = lambda: dict(status)
server.store.read_telemetry = lambda range_name: {"samples": [{"t": 1000, "cpu": 20, "mem": 35, "sys": 70, "disks": {"Drive 1": 40}}]}
server.store.append_history = lambda *args, **kwargs: events.append((args, kwargs))
server.store.record_telemetry = lambda sample: True
server.store.write_status = lambda value: events.append((("status", value), {}))

photos = []
server.telegram.send_photo = lambda token, chat_id, png, caption, buttons=True: photos.append(
    {"token": token, "chat": chat_id, "png": png, "caption": caption, "buttons": buttons})
server.telegram.send_message = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("text fallback not expected"))
server.telegram.answer_callback = lambda *args, **kwargs: None
server.telegram.edit_message_caption = lambda *args, **kwargs: None
server.telegram.edit_message_text = lambda *args, **kwargs: None

ok, error = server.tg_send("[警告] TestNAS")
check("automatic alert sends photo", ok and error is None and len(photos) == 1)
check("automatic alert uses PNG", photos[0]["png"].startswith(b"\x89PNG"))
check("automatic alert keeps controls", photos[0]["buttons"] is True)

class FreshClient:
    def collect(self):
        fresh = dict(data)
        fresh["temperature"] = 77
        return fresh

server.get_client = lambda current_cfg: FreshClient()
callback = {"id": "cb1", "data": "snapshot_now", "message": {"chat": {"id": 99}, "message_id": 7, "caption": "old"}}
server.handle_callback(callback, cfg)
check("snapshot button replies with photo", len(photos) == 2)
check("snapshot button requests fresh data", "最新データを取得しました" in photos[-1]["caption"])
check("snapshot button uses PNG", photos[-1]["png"].startswith(b"\x89PNG"))

print()
print("FAILED" if failed else "ALL TELEGRAM SNAPSHOT TESTS PASSED")
sys.exit(1 if failed else 0)
