import time
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

ORDER = {"NORMAL": 0, "WARNING": 1, "CRITICAL": 2, "EMERGENCY": 3}
SEV_NAMES = {"NORMAL": "正常", "WARNING": "警告", "CRITICAL": "重大", "EMERGENCY": "緊急"}
REPEAT_SEC = 180
API_FAIL_THRESHOLD = 3


def classify(temp, cfg):
    if temp is None:
        return None
    if temp >= cfg["emergTemp"]:
        return "EMERGENCY"
    if temp >= cfg["critTemp"]:
        return "CRITICAL"
    if temp >= cfg["warnTemp"]:
        return "WARNING"
    return "NORMAL"


def _fmt_now(ts=None):
    return datetime.fromtimestamp(ts or time.time(), JST).strftime("%Y-%m-%d %H:%M:%S")


def _hottest(disks, pred):
    cand = [d for d in (disks or []) if d.get("temp") is not None and pred(d)]
    if not cand:
        return None
    return max(cand, key=lambda d: d["temp"])


def build_message(title, event, data, cfg, extra=None):
    lines = [f"[{title}] {data.get('model', 'NAS')}" if data else f"[{title}] NAS Monitor"]
    lines.append(f"発生時刻: {_fmt_now()}")
    lines.append(f"イベント: {event}")
    if data:
        temp = data.get("temperature")
        if temp is not None:
            lines.append(
                f"システム温度: {temp}°C"
                f"（警告 {cfg['warnTemp']}°C / 重大 {cfg['critTemp']}°C / 緊急 {cfg['emergTemp']}°C）"
            )
        cpu = data.get("cpu") or {}
        mem = data.get("memory") or {}
        lines.append(f"CPU: {cpu.get('total', '—')}% / メモリ: {mem.get('usage', '—')}%")
        disks = data.get("disks") or []
        hot = _hottest(disks, lambda d: True)
        if hot:
            label = "M.2" if hot.get("ssd") else "ディスク"
            lines.append(f"最高温度の{label}: {hot['name']} {hot['temp']}°C")
        dx = _hottest(disks, lambda d: any(mark in (d.get("unit") or "").upper() for mark in ("DX", "RX", "EXPANSION")))
        if dx:
            lines.append(f"拡張ユニットの最高温度: {dx['name']} {dx['temp']}°C")
    if extra:
        lines.append(f"根拠: {extra}")
    lines.append("送信先: 設定済み Telegram チャット（監視対象NASへの操作ではありません）")
    return "\n".join(lines)


class AlertEngine:
    def __init__(self, cfg_getter, state, save_state, send_fn, history_fn):
        self.cfg = cfg_getter
        self.state = state
        self.save_state = save_state
        self.send_fn = send_fn
        self.history = history_fn

    def _muted(self, now):
        st = self.state
        if not st.get("notifyEnabled", True):
            return True
        hu = st.get("hushUntil")
        return bool(hu and now < hu)

    def notify(self, kind, event, data=None, extra=None, now=None):
        now = now or time.time()
        cfg = self.cfg()
        text = build_message(kind, event, data, cfg, extra)
        if self._muted(now):
            self.history(kind, f"通知停止中のため送信スキップ: {event}", sent=False)
            return False
        ok, err = self.send_fn(text)
        self.history(kind, event, sent=ok, error=err)
        return ok

    def process_sample(self, data, now=None):
        now = now or time.time()
        cfg = self.cfg()
        st = self.state

        if st.get("apiDown"):
            st["apiDown"] = False
            st["apiFail"] = 0
            self.notify("API復旧", "SYNO API 監視が復旧しました", data, now=now)

        temp = data.get("temperature")
        if temp is None:
            st["prevTemp"] = None
            st["prevTempAt"] = None
            self.save_state(st)
            return

        new_sev = classify(temp, cfg)
        prev_sev = st.get("severity", "NORMAL")

        if new_sev != prev_sev:
            # The notification image is generated during notify().  Store the
            # new state first so its status banner agrees with the alert text.
            st["severity"] = new_sev
            if ORDER[new_sev] > ORDER[prev_sev]:
                kind = SEV_NAMES[new_sev]
                event = f"{SEV_NAMES[prev_sev]} → {SEV_NAMES[new_sev]}（{temp}°C が設定値を超過）"
            elif new_sev == "NORMAL":
                kind = "復帰"
                event = f"{SEV_NAMES[prev_sev]} → 正常へ復帰（{temp}°C）"
            else:
                kind = "低下"
                event = f"{SEV_NAMES[prev_sev]} から {SEV_NAMES[new_sev]} へ低下（{temp}°C）"
            self.notify(kind, event, data, now=now)
            st.setdefault("lastSent", {})[new_sev] = now
            if new_sev != "WARNING":
                st["approachSent"] = False
        elif new_sev in ("WARNING", "CRITICAL", "EMERGENCY"):
            last = st.get("lastSent", {}).get(new_sev, 0) or 0
            if now - last >= REPEAT_SEC:
                self.notify(
                    f"{SEV_NAMES[new_sev]}（継続）",
                    f"{SEV_NAMES[new_sev]}状態が継続しています（{temp}°C）",
                    data, now=now)
                st.setdefault("lastSent", {})[new_sev] = now

        if new_sev == "WARNING" and not st.get("approachSent"):
            prev_t = st.get("prevTemp")
            prev_at = st.get("prevTempAt")
            if prev_t is not None and prev_at:
                dt = now - prev_at
                if dt > 0:
                    rate = (temp - prev_t) / dt
                    if rate > 0:
                        eta = (cfg["critTemp"] - temp) / rate
                        if 0 <= eta <= cfg["predictSec"]:
                            extra = (
                                f"現在 {temp}°C、重大設定値 {cfg['critTemp']}°C、"
                                f"上昇率 {rate * 60:.2f}°C/分、約 {int(eta)} 秒後に重大値到達見込み"
                            )
                            self.notify("重大値へ接近中", "重大設定値へ急速に接近しています", data, extra=extra, now=now)
                            st["approachSent"] = True

        st["prevTemp"] = temp
        st["prevTempAt"] = now
        self.save_state(st)

    def process_failure(self, error, now=None):
        now = now or time.time()
        st = self.state
        st["apiFail"] = st.get("apiFail", 0) + 1
        st["prevTemp"] = None
        st["prevTempAt"] = None

        if st["apiFail"] >= API_FAIL_THRESHOLD:
            if not st.get("apiDown"):
                st["apiDown"] = True
                st["apiLastNotify"] = now
                self.notify("API取得不能", f"SYNO APIから情報を取得できません: {error}", now=now)
            elif now - (st.get("apiLastNotify") or 0) >= REPEAT_SEC:
                st["apiLastNotify"] = now
                self.notify("API取得不能（継続）", f"取得不能が継続しています: {error}", now=now)
        self.save_state(st)
