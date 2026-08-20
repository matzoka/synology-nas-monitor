#!/usr/bin/env python3
import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import alerts
import snapshot
import store
import syno
import telegram

PKG = store.PKG
WEB = os.path.join(PKG, "target", "nasmon", "web")
if not os.path.isdir(WEB):
    WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
PORT = 8811

REFRESH = threading.Event()
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")

_client = None
_client_key = None


def get_client(cfg):
    global _client, _client_key
    key = (cfg["baseUrl"], cfg["account"], cfg["passwd"])
    if _client is None or _client_key != key:
        if _client is not None:
            try:
                _client.logout()
            except Exception:
                pass
        _client = syno.SynoClient(cfg["baseUrl"], cfg["account"], cfg["passwd"])
        _client_key = key
    return _client


def _snapshot_bytes(status=None, cfg=None, state=None):
    status = status or store.read_status() or {}
    cfg = cfg or store.load_config()
    state = state or store.load_state()
    telemetry = store.read_telemetry("24h").get("samples", [])
    return snapshot.render(
        status.get("data"), telemetry, cfg, state,
        fetched_at=status.get("fetchedAt"), ok=status.get("ok", True),
        error=status.get("error"),
    )


def tg_send(text, status=None):
    """Deliver every automatic notification with a one-page status image.

    If photo delivery itself fails, a text-only alert is sent as a safety
    fallback; losing a temperature warning is worse than losing its image.
    """
    cfg = store.load_config()
    if not cfg["tgToken"] or not cfg["chatId"]:
        return False, "Telegram 未設定"
    try:
        telegram.send_photo(cfg["tgToken"], cfg["chatId"], _snapshot_bytes(status, cfg), text, buttons=True)
        return True, None
    except telegram.TelegramError as e:
        photo_error = str(e)
    except Exception as e:
        photo_error = "画像作成失敗: " + str(e)
    try:
        telegram.send_message(cfg["tgToken"], cfg["chatId"], text + "\n\n[状況画像を添付できませんでした: " + photo_error + "]", buttons=True)
        store.append_history("通知画像", "画像添付に失敗したため本文のみ送信", sent=False, error=photo_error)
        return True, None
    except telegram.TelegramError as e:
        return False, str(e)


def make_engine():
    return alerts.AlertEngine(
        cfg_getter=store.load_config,
        state=store.load_state(),
        save_state=store.save_state,
        send_fn=tg_send,
        history_fn=store.append_history,
    )


def nas_configured(cfg):
    return bool(cfg["baseUrl"] and cfg["account"] and cfg["passwd"])


def monitor_loop():
    engine = make_engine()
    while True:
        cfg = store.load_config()
        if not nas_configured(cfg):
            store.write_status({"configured": False, "fetchedAt": syno.now_iso()})
        else:
            try:
                data = get_client(cfg).collect()
                store.record_telemetry(data)
                st = store.load_state()
                store.write_status({
                    "configured": True, "ok": True, "data": data,
                    "severity": st.get("severity"),
                    "notifyEnabled": st.get("notifyEnabled"),
                    "hushUntil": st.get("hushUntil"),
                    "fetchedAt": syno.now_iso(),
                })
                engine.process_sample(data)
                st = store.load_state()
                store.write_status({
                    "configured": True, "ok": True, "data": data,
                    "severity": st.get("severity"),
                    "notifyEnabled": st.get("notifyEnabled"),
                    "hushUntil": st.get("hushUntil"),
                    "fetchedAt": syno.now_iso(),
                })
            except Exception as e:
                prev = store.read_status() or {}
                st = store.load_state()
                store.write_status({
                    "configured": True, "ok": False, "error": str(e),
                    "data": prev.get("data"),
                    "severity": st.get("severity"),
                    "notifyEnabled": st.get("notifyEnabled"),
                    "hushUntil": st.get("hushUntil"),
                    "fetchedAt": syno.now_iso(),
                })
                engine.process_failure(e)
                st = store.load_state()
                store.write_status({
                    "configured": True, "ok": False, "error": str(e),
                    "data": prev.get("data"),
                    "severity": st.get("severity"),
                    "notifyEnabled": st.get("notifyEnabled"),
                    "hushUntil": st.get("hushUntil"),
                    "fetchedAt": syno.now_iso(),
                })
        poll = max(5, int(cfg.get("pollSec") or 10))
        REFRESH.wait(poll)
        REFRESH.clear()


def status_message():
    st = store.read_status() or {}
    state = store.load_state()
    cfg = store.load_config()
    sev = state.get("severity", "NORMAL")
    sev_name = alerts.SEV_NAMES.get(sev, sev)
    lines = ["[現在の監視状態] NAS Monitor"]
    lines.append(f"時刻: {syno.now_iso()}")
    lines.append(f"重大度: {sev_name}")
    data = st.get("data")
    if data:
        temp = data.get("temperature")
        cpu = (data.get("cpu") or {}).get("total", "—")
        mem = (data.get("memory") or {}).get("usage", "—")
        lines.append(f"システム温度: {temp if temp is not None else '—'}°C / CPU: {cpu}% / メモリ: {mem}%")
    if not st.get("ok", True) and st.get("error"):
        lines.append(f"取得エラー: {st['error']}")
    if not state.get("notifyEnabled", True):
        lines.append("通知状態: 停止中")
    elif state.get("hushUntil") and time.time() < state["hushUntil"]:
        remain = int((state["hushUntil"] - time.time()) / 60) + 1
        lines.append(f"通知状態: 1時間停止中（残り約 {remain} 分）")
    else:
        lines.append("通知状態: 有効")
    lines.append("送信先: 設定済み Telegram チャット")
    return "\n".join(lines)


def collect_snapshot_status(cfg):
    """Try a fresh SYNO.API read for a Telegram button request.

    A failed manual read never fabricates a result.  The image is labelled as
    cached data and includes the failure state instead.
    """
    try:
        data = get_client(cfg).collect()
        state = store.load_state()
        status = {
            "configured": True, "ok": True, "data": data,
            "severity": state.get("severity"),
            "notifyEnabled": state.get("notifyEnabled"),
            "hushUntil": state.get("hushUntil"),
            "fetchedAt": syno.now_iso(),
        }
        store.record_telemetry(data)
        store.write_status(status)
        return status, "最新データを取得しました"
    except Exception as e:
        cached = dict(store.read_status() or {"configured": True})
        cached["ok"] = False
        cached["error"] = str(e)
        cached["fetchedAt"] = cached.get("fetchedAt") or syno.now_iso()
        return cached, "最新取得に失敗したため、直近の保存データを表示しています"


def handle_callback(cb, cfg):
    cb_id = cb.get("id")
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if str(cid) != str(cfg["chatId"]):
        telegram.answer_callback(cfg["tgToken"], cb_id, "このチャットからは操作できません")
        store.append_history("操作拒否", f"未許可の Chat ID からの操作: {cid}")
        return
    action = cb.get("data")
    telegram.answer_callback(cfg["tgToken"], cb_id, "処理しています")
    if action == "snapshot_now":
        try:
            status, freshness = collect_snapshot_status(cfg)
            telegram.send_photo(
                cfg["tgToken"], cid, _snapshot_bytes(status, cfg),
                "[現在の状況] NAS Monitor\n" + freshness + "\n" + status_message(), buttons=True)
            store.append_history("状況画像", freshness, sent=True)
        except telegram.TelegramError as e:
            store.append_history("状況画像", "Telegramへ状況画像を送信できませんでした", sent=False, error=str(e))
        except Exception as e:
            store.append_history("状況画像", "状況画像の作成に失敗しました", sent=False, error=str(e))
        return
    st = store.load_state()
    label = None
    if action == "notify_hush_1h":
        # The three user-facing notification modes are exclusive.  Choosing a
        # one-hour pause must be able to replace a prior permanent pause.
        st["notifyEnabled"] = True
        st["hushUntil"] = time.time() + 3600
        label = "🔕 1時間停止中（60分後に自動再開）"
    elif action == "notify_pause":
        st["notifyEnabled"] = False
        st["hushUntil"] = None
        label = "🔕 通知停止中"
    elif action == "notify_resume":
        st["notifyEnabled"] = True
        st["hushUntil"] = None
        label = "🔔 通知再開済み"
    else:
        return
    store.save_state(st)
    store.append_history("通知操作", label)
    orig_caption = msg.get("caption")
    orig_text = msg.get("text")
    if orig_caption:
        telegram.edit_message_caption(cfg["tgToken"], cid, msg.get("message_id"), orig_caption + "\n\n✅ " + label)
    elif orig_text:
        telegram.edit_message_text(cfg["tgToken"], cid, msg.get("message_id"), orig_text + "\n\n✅ " + label)


def process_telegram_updates(updates, cfg):
    """Apply Telegram callbacks without overwriting their persisted state."""
    st = store.load_state()
    offset = st.get("tgOffset", 0) or 0
    for u in updates or []:
        offset = max(offset, u["update_id"] + 1)
        cb = u.get("callback_query")
        if cb:
            try:
                handle_callback(cb, cfg)
            except Exception as e:
                store.append_history("Telegram受信", "callback 処理失敗", sent=False, error=str(e))
    if offset != (st.get("tgOffset", 0) or 0):
        # A callback persists notification settings on its own.  Reload that
        # state before writing the Telegram offset so it cannot be overwritten.
        latest_state = store.load_state()
        latest_state["tgOffset"] = offset
        store.save_state(latest_state)
    return offset


def telegram_loop():
    while True:
        cfg = store.load_config()
        if not cfg["tgToken"] or not cfg["chatId"]:
            time.sleep(5)
            continue
        offset = store.load_state().get("tgOffset", 0) or 0
        try:
            updates = telegram.get_updates(cfg["tgToken"], offset, timeout=25)
        except telegram.TelegramError as e:
            store.append_history("Telegram受信", "getUpdates 失敗", sent=False, error=str(e))
            time.sleep(10)
            continue
        process_telegram_updates(updates, cfg)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _serve_file(self, name):
        path = os.path.join(WEB, name)
        try:
            with open(path, "rb") as f:
                self._send(200, f.read())
        except OSError:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._serve_file("index.html")
        elif path == "/config":
            self._serve_file("config.html")
        elif path == "/api/status":
            self._json(store.read_status() or {"configured": False})
        elif path == "/api/config":
            cfg = store.load_config()
            self._json({
                "configured": nas_configured(cfg),
                "baseUrl": cfg["baseUrl"],
                "account": cfg["account"],
                "chatId": cfg["chatId"],
                "telegramConfigured": bool(cfg["tgToken"] and cfg["chatId"]),
                "warnTemp": cfg["warnTemp"],
                "critTemp": cfg["critTemp"],
                "emergTemp": cfg["emergTemp"],
                "pollSec": cfg["pollSec"],
                "predictSec": cfg["predictSec"],
            })
        elif path == "/api/history":
            self._json({"entries": store.read_history(50)})
        elif path == "/api/timeseries":
            query = urllib.parse.parse_qs(parsed.query)
            range_name = (query.get("range", ["24h"])[0] or "24h").strip()
            payload = store.read_telemetry(range_name)
            cfg = store.load_config()
            payload["thresholds"] = {
                "warn": cfg.get("warnTemp"),
                "critical": cfg.get("critTemp"),
                "emergency": cfg.get("emergTemp"),
            }
            self._json(payload)
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/config":
            self._post_config()
        elif path == "/telegram/test":
            self._post_telegram_test()
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def _post_telegram_test(self):
        cfg = store.load_config()
        if not cfg["tgToken"] or not cfg["chatId"]:
            self._json({"ok": False, "error": "Bot Token と Chat ID を先に保存してください"}, code=400)
            return
        try:
            ok, error = tg_send(
                "[テスト] NAS Monitor\nTelegram 通知テストです。監視対象NASへの操作ではありません。\n送信先: 設定済み Telegram チャット")
            store.append_history("通知テスト", "Telegram 通知テストを送信", sent=ok, error=error)
            self._json({"ok": ok, "error": error} if not ok else {"ok": True}, code=400 if not ok else 200)
        except Exception as e:
            store.append_history("通知テスト", "Telegram 通知テスト失敗", sent=False, error=str(e))
            self._json({"ok": False, "error": str(e)}, code=400)

    def _post_config(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))

        def f1(name):
            return (form.get(name, [""])[0] or "").strip()

        old = store.load_config()
        cfg = dict(old)

        base = f1("baseUrl")
        account = f1("account")
        passwd = form.get("passwd", [""])[0]
        tg_token = f1("tgToken")
        chat_id = f1("chatId")

        nas_changed = False
        if base or account or passwd:
            cfg["baseUrl"] = base or old["baseUrl"]
            cfg["account"] = account or old["account"]
            cfg["passwd"] = passwd or old["passwd"]
            nas_changed = (cfg["baseUrl"] != old["baseUrl"]
                          or cfg["account"] != old["account"]
                          or bool(passwd))

        if tg_token:
            if not TOKEN_RE.match(tg_token):
                self._redirect("/config?error=" + urllib.parse.quote("Bot Token の形式が正しくありません"))
                return
            cfg["tgToken"] = tg_token
        if chat_id:
            cfg["chatId"] = chat_id
        if cfg["tgToken"] and not cfg["chatId"]:
            self._redirect("/config?error=" + urllib.parse.quote("Telegram送信先 Chat ID（個人通知ではUser IDと同じ）を入力してください"))
            return

        def num(name, default, lo, hi):
            raw = f1(name)
            if not raw:
                return default
            try:
                v = float(raw)
            except ValueError:
                return default
            return max(lo, min(hi, v))

        cfg["warnTemp"] = num("warnTemp", cfg["warnTemp"], 30, 110)
        cfg["critTemp"] = num("critTemp", cfg["critTemp"], 30, 120)
        cfg["emergTemp"] = num("emergTemp", cfg["emergTemp"], 30, 130)
        cfg["pollSec"] = int(num("pollSec", cfg["pollSec"], 5, 3600))
        cfg["predictSec"] = int(num("predictSec", cfg["predictSec"], 30, 3600))
        if not (cfg["warnTemp"] < cfg["critTemp"] < cfg["emergTemp"]):
            self._redirect("/config?error=" + urllib.parse.quote("温度設定は 警告 < 重大 < 緊急 の順にしてください"))
            return

        if nas_changed and nas_configured(cfg):
            try:
                c = syno.SynoClient(cfg["baseUrl"], cfg["account"], cfg["passwd"])
                c.login()
                c.logout()
            except Exception as e:
                self._redirect("/config?error=" + urllib.parse.quote("NAS 接続テストに失敗しました: " + str(e)))
                return

        store.save_config(cfg)
        store.append_history("設定保存", "設定を更新しました")
        REFRESH.set()
        self._redirect("/config?saved=1")


CERT_FILE = os.path.join(store.ETC, "server.crt")
KEY_FILE_TLS = os.path.join(store.ETC, "server.key")


def _ensure_tls_cert():
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE_TLS):
        return True
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", KEY_FILE_TLS, "-out", CERT_FILE,
             "-days", "3650", "-subj", "/CN=nasmon"],
            capture_output=True, check=True, timeout=120)
        os.chmod(KEY_FILE_TLS, 0o600)
        os.chmod(CERT_FILE, 0o644)
        return True
    except Exception:
        return False


def make_server():
    if not _ensure_tls_cert():
        raise RuntimeError("TLS certificate generation failed; refusing to start an HTTP server")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE_TLS)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main():
    os.makedirs(store.VAR, exist_ok=True)
    os.makedirs(store.ETC, exist_ok=True)
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    make_server().serve_forever()


if __name__ == "__main__":
    main()
