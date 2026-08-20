import json
import os
import urllib.request

API_BASE = "https://api.telegram.org"

BUTTONS = {
    "inline_keyboard": [
        [
            {"text": "🔕 1時間停止", "callback_data": "notify_hush_1h"},
            {"text": "🔕 通知停止", "callback_data": "notify_pause"},
            {"text": "🔔 通知再開", "callback_data": "notify_resume"},
        ],
        [{"text": "📷 スクショ取得", "callback_data": "snapshot_now"}],
    ],
}


class TelegramError(Exception):
    pass


def _post(token, method, payload, timeout=10):
    url = f"{API_BASE}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception as e:
        raise TelegramError(f"HTTP/通信エラー: {e}")
    if not d.get("ok"):
        raise TelegramError(f"Bot API エラー: {d.get('description', d)}")
    return d.get("result")


def _post_multipart(token, method, fields, filename, content, content_type="image/png", timeout=20):
    """Call a Bot API file-upload endpoint without adding a third-party HTTP client."""
    boundary = "----nasmon" + os.urandom(12).hex()
    body = bytearray()
    for name, value in fields.items():
        body.extend((f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n").encode("utf-8"))
    body.extend((f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n").encode("utf-8"))
    body.extend(content)
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    url = f"{API_BASE}/bot{token}/{method}"
    req = urllib.request.Request(url, data=bytes(body), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except Exception as e:
        raise TelegramError(f"HTTP/通信エラー: {e}")
    if not d.get("ok"):
        raise TelegramError(f"Bot API エラー: {d.get('description', d)}")
    return d.get("result")


def send_message(token, chat_id, text, buttons=True):
    payload = {"chat_id": chat_id, "text": text}
    if buttons:
        payload["reply_markup"] = BUTTONS
    return _post(token, "sendMessage", payload, timeout=10)


def send_photo(token, chat_id, png_bytes, caption, buttons=True):
    fields = {"chat_id": str(chat_id), "caption": str(caption)[:1024]}
    if buttons:
        fields["reply_markup"] = json.dumps(BUTTONS, ensure_ascii=False, separators=(",", ":"))
    return _post_multipart(token, "sendPhoto", fields, "nas-monitor-snapshot.png", png_bytes)


def get_updates(token, offset, timeout=25):
    payload = {"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query", "message"]}
    return _post(token, "getUpdates", payload, timeout=timeout + 10)


def answer_callback(token, callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    try:
        _post(token, "answerCallbackQuery", payload, timeout=10)
    except TelegramError:
        pass


def edit_message_text(token, chat_id, message_id, text):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    try:
        _post(token, "editMessageText", payload, timeout=10)
    except TelegramError:
        pass


def edit_message_caption(token, chat_id, message_id, caption):
    payload = {"chat_id": chat_id, "message_id": message_id, "caption": str(caption)[:1024]}
    try:
        _post(token, "editMessageCaption", payload, timeout=10)
    except TelegramError:
        pass
