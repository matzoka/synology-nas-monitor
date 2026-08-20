import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

AUTH_ERRORS = {105, 106, 107, 119}

ERROR_NAMES = {
    100: "不明なエラー",
    101: "パラメータ不正",
    102: "API が存在しない",
    103: "メソッドが存在しない",
    104: "API バージョン非対応",
    105: "セッション期限切れ",
    106: "セッション無効",
    107: "セッション重複",
    119: "SID 無効",
    400: "認証失敗（アカウントまたはパスワード）",
    401: "アカウント無効",
    402: "アカウントの権限不足",
    403: "2段階認証が必要",
    404: "2段階認証コード不正",
    406: "OTP（2段階認証）が必要",
    407: "IP からのアクセスが拒否",
}


class SynoError(Exception):
    def __init__(self, code=None, message="", category="api"):
        self.code = code
        self.category = category
        name = ERROR_NAMES.get(code, "API エラー")
        super().__init__(f"[{category}] {name} (code={code}) {message}".strip())


def now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")


class SynoClient:
    def __init__(self, base, account, passwd):
        self.base = base.rstrip("/")
        self.account = account
        self.passwd = passwd
        self.sid = None

    def _get(self, path, params, timeout=20):
        url = f"{self.base}/webapi/{path}?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, context=_SSL_CTX, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.URLError as e:
            raise SynoError(None, str(e.reason if hasattr(e, "reason") else e), category="接続不能")
        except TimeoutError as e:
            raise SynoError(None, str(e), category="タイムアウト")

    def login(self):
        d = self._get("entry.cgi", {
            "api": "SYNO.API.Auth", "version": "7", "method": "login",
            "account": self.account, "passwd": self.passwd,
            "session": "NasMon", "format": "sid",
        })
        if not d.get("success"):
            code = (d.get("error") or {}).get("code")
            raise SynoError(code, category="認証失効")
        self.sid = d["data"]["sid"]

    def logout(self):
        if not self.sid:
            return
        try:
            self._get("entry.cgi", {
                "api": "SYNO.API.Auth", "version": "7", "method": "logout",
                "session": "NasMon", "_sid": self.sid,
            })
        except Exception:
            pass
        self.sid = None

    def query(self, api, version, method, **extra):
        if not self.sid:
            self.login()
        p = {"api": api, "version": str(version), "method": method, "_sid": self.sid}
        p.update(extra)
        d = self._get("entry.cgi", p)
        if not d.get("success"):
            code = (d.get("error") or {}).get("code")
            if code in AUTH_ERRORS:
                self.sid = None
                self.login()
                p["_sid"] = self.sid
                d = self._get("entry.cgi", p)
        if not d.get("success"):
            code = (d.get("error") or {}).get("code")
            raise SynoError(code, category="API仕様不一致")
        return d["data"]

    def collect(self):
        info = self.query("SYNO.DSM.Info", 2, "getinfo")
        util = self.query("SYNO.Core.System.Utilization", 1, "get")
        storage = self.query("SYNO.Storage.CGI.Storage", 1, "load_info")

        cpu_raw = util.get("cpu", {})
        cpu_total = (cpu_raw.get("user_load", 0) or 0) + (cpu_raw.get("system_load", 0) or 0) + (cpu_raw.get("other_load", 0) or 0)
        mem = util.get("memory", {})
        net_list = util.get("network", []) or []
        net_total = next((n for n in net_list if n.get("device") == "total"), net_list[0] if net_list else {})

        volumes = []
        for v in storage.get("volumes", []):
            size = v.get("size", {})
            total = int(size.get("total", 0) or 0)
            used = int(size.get("used", 0) or 0)
            volumes.append({
                "name": (v.get("id") or "").replace("_", " "),
                "status": v.get("status", "unknown"),
                "fs": v.get("fs_type", ""),
                "raid": v.get("device_type", ""),
                "totalBytes": total,
                "usedBytes": used,
                "percent": round(used * 100.0 / total, 1) if total else 0.0,
            })

        disks = []
        for d in storage.get("disks", []):
            container = d.get("container", {}) or {}
            disks.append({
                "name": d.get("longName") or d.get("name", ""),
                "model": d.get("model", ""),
                "temp": d.get("temp"),
                "status": d.get("overview_status", "unknown"),
                "smart": d.get("smart_status", ""),
                "sizeBytes": int(d.get("size_total", 0) or 0),
                "unit": container.get("str", ""),
                "ssd": bool(d.get("isSsd")),
            })

        return {
            "model": info.get("model", ""),
            "dsm": info.get("version_string", ""),
            "uptimeSec": int(info.get("uptime", 0) or 0),
            "temperature": info.get("temperature"),
            "temperatureWarn": bool(info.get("temperature_warn")),
            "cpu": {
                "total": cpu_total,
                "user": cpu_raw.get("user_load", 0) or 0,
                "system": cpu_raw.get("system_load", 0) or 0,
                "load1": cpu_raw.get("1min_load", 0) or 0,
                "load5": cpu_raw.get("5min_load", 0) or 0,
                "load15": cpu_raw.get("15min_load", 0) or 0,
            },
            "memory": {
                "usage": mem.get("real_usage", 0) or 0,
                "totalMb": round((mem.get("memory_size", 0) or 0) / 1024),
                "swapUsage": mem.get("swap_usage", 0) or 0,
            },
            "network": {
                "rxBps": net_total.get("rx", 0) or 0,
                "txBps": net_total.get("tx", 0) or 0,
            },
            "volumes": volumes,
            "disks": disks,
            "fetchedAt": now_iso(),
            "source": "SYNO.API @ " + self.base,
        }
