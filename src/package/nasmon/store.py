import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

PKG = os.environ.get("NASMON_PKG", "/var/packages/nasmon")
ETC = os.path.join(PKG, "etc")
VAR = os.path.join(PKG, "var")
CONFIG_FILE = os.path.join(ETC, "config.enc")
KEY_FILE = os.path.join(ETC, "key")
STATE_FILE = os.path.join(VAR, "state.json")
HISTORY_FILE = os.path.join(VAR, "history.jsonl")
STATUS_FILE = os.path.join(VAR, "status.json")
TELEMETRY_DIR = os.path.join(VAR, "telemetry")
JST = timezone(timedelta(hours=9))
TELEMETRY_RETENTION_DAYS = 30
_last_telemetry_minute = None
_last_telemetry_cleanup_day = None

CONFIG_DEFAULTS = {
    "baseUrl": "",
    "account": "",
    "passwd": "",
    "tgToken": "",
    "chatId": "",
    "warnTemp": 75,
    "critTemp": 85,
    "emergTemp": 90,
    "pollSec": 10,
    "predictSec": 180,
}

STATE_DEFAULTS = {
    "notifyEnabled": True,
    "hushUntil": None,
    "severity": "NORMAL",
    "lastSent": {},
    "approachSent": False,
    "apiFail": 0,
    "apiDown": False,
    "apiLastNotify": None,
    "tgOffset": 0,
    "prevTemp": None,
    "prevTempAt": None,
}


def _ensure_key():
    os.makedirs(ETC, exist_ok=True)
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f:
            f.write(os.urandom(32).hex())
        os.chmod(KEY_FILE, 0o600)
    with open(KEY_FILE) as f:
        return f.read().strip()


def encrypt(text):
    p = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-a", "-pass", "pass:" + _ensure_key()],
        input=text.encode("utf-8"), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("encryption failed")
    return p.stdout.decode("ascii")


def decrypt(blob):
    p = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-a", "-pass", "pass:" + _ensure_key()],
        input=blob.encode("ascii"), capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("decryption failed")
    return p.stdout.decode("utf-8")


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.loads(decrypt(f.read())))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    os.makedirs(ETC, exist_ok=True)
    merged = dict(CONFIG_DEFAULTS)
    merged.update(cfg)
    with open(CONFIG_FILE, "w") as f:
        f.write(encrypt(json.dumps(merged)))
    os.chmod(CONFIG_FILE, 0o600)


def load_state():
    st = dict(STATE_DEFAULTS)
    st["lastSent"] = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            st.update(saved)
        except Exception:
            pass
    return st


def save_state(st):
    os.makedirs(VAR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def write_status(obj):
    os.makedirs(VAR, exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, STATUS_FILE)


def append_history(kind, detail, sent=None, error=None):
    os.makedirs(VAR, exist_ok=True)
    from datetime import datetime, timezone, timedelta
    jst = timezone(timedelta(hours=9))
    entry = {
        "time": datetime.now(jst).isoformat(timespec="seconds"),
        "kind": kind,
        "detail": detail[:300],
    }
    if sent is not None:
        entry["sent"] = bool(sent)
    if error:
        entry["error"] = str(error)[:200]
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        if os.path.getsize(HISTORY_FILE) > 65536:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                lines = f.readlines()[-200:]
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
    except OSError:
        pass


def read_history(limit=50):
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def _telemetry_path(epoch):
    day = datetime.fromtimestamp(epoch, JST).strftime("%Y-%m-%d")
    return os.path.join(TELEMETRY_DIR, f"samples-{day}.jsonl")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def record_telemetry(data, now=None):
    """Persist one lightweight sample per minute after a successful SYNO.API read.

    Missing values remain null.  They are deliberately never converted to zero,
    so a collection gap cannot look like a temperature drop or spike.
    """
    global _last_telemetry_minute, _last_telemetry_cleanup_day
    now = time.time() if now is None else now
    minute = int(now // 60) * 60
    if minute == _last_telemetry_minute:
        return False

    disks = {}
    for disk in data.get("disks", []) or []:
        name = str(disk.get("name") or "").strip()
        if name:
            disks[name] = _number(disk.get("temp"))
    sample = {
        "t": minute,
        "cpu": _number((data.get("cpu") or {}).get("total")),
        "mem": _number((data.get("memory") or {}).get("usage")),
        "sys": _number(data.get("temperature")),
        "disks": disks,
    }
    try:
        os.makedirs(TELEMETRY_DIR, exist_ok=True)
        with open(_telemetry_path(minute), "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")) + "\n")
        _last_telemetry_minute = minute

        today = datetime.fromtimestamp(now, JST).date()
        if _last_telemetry_cleanup_day != today:
            cutoff = today - timedelta(days=TELEMETRY_RETENTION_DAYS - 1)
            for filename in os.listdir(TELEMETRY_DIR):
                if not (filename.startswith("samples-") and filename.endswith(".jsonl")):
                    continue
                try:
                    file_day = datetime.strptime(filename[8:18], "%Y-%m-%d").date()
                    if file_day < cutoff:
                        os.remove(os.path.join(TELEMETRY_DIR, filename))
                except (ValueError, OSError):
                    pass
            _last_telemetry_cleanup_day = today
        return True
    except OSError:
        return False


def read_telemetry(range_name="24h"):
    ranges = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600,
              "7d": 7 * 86400, "30d": 30 * 86400}
    seconds = ranges.get(range_name, ranges["24h"])
    now = time.time()
    cutoff = int(now - seconds)
    samples = []
    if not os.path.isdir(TELEMETRY_DIR):
        return {"range": range_name, "samples": samples, "recent": samples}

    for offset in range((seconds // 86400) + 2):
        day = datetime.fromtimestamp(now - offset * 86400, JST).strftime("%Y-%m-%d")
        path = os.path.join(TELEMETRY_DIR, f"samples-{day}.jsonl")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        sample = json.loads(line)
                        if int(sample.get("t", 0)) >= cutoff:
                            samples.append(sample)
                    except (ValueError, TypeError):
                        pass
        except OSError:
            pass

    samples.sort(key=lambda sample: sample.get("t", 0))
    # The table uses the newest exact 1-minute points even when a wide graph
    # range is downsampled for browser performance.
    recent = samples[-60:]
    # Keep browser rendering quick even for a full 30-day view.  The final
    # point is always kept so the graph agrees with the latest dashboard value.
    max_points = 720
    if len(samples) > max_points:
        step = (len(samples) + max_points - 1) // max_points
        reduced = samples[::step]
        if reduced[-1].get("t") != samples[-1].get("t"):
            reduced.append(samples[-1])
        samples = reduced
    return {"range": range_name, "samples": samples, "recent": recent}
