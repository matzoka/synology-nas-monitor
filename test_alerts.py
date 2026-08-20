import sys
import os

sys.path.insert(0, os.path.join("src", "package", "nasmon"))
import alerts

CFG = {"warnTemp": 75, "critTemp": 85, "emergTemp": 90, "pollSec": 10, "predictSec": 180}

sent_messages = []
history_entries = []


def make_engine(state=None):
    st = state or {
        "notifyEnabled": True, "hushUntil": None, "severity": "NORMAL",
        "lastSent": {}, "approachSent": False,
        "apiFail": 0, "apiDown": False, "apiLastNotify": None,
        "tgOffset": 0, "prevTemp": None, "prevTempAt": None,
    }
    return alerts.AlertEngine(
        cfg_getter=lambda: dict(CFG),
        state=st,
        save_state=lambda s: None,
        send_fn=lambda text: (sent_messages.append(text), (True, None))[1],
        history_fn=lambda kind, detail, sent=None, error=None: history_entries.append((kind, detail, sent)),
    )


def sample(temp, t=1000):
    return {
        "model": "TestNAS", "temperature": temp, "temperatureWarn": False,
        "cpu": {"total": 20}, "memory": {"usage": 36},
        "disks": [
        {"name": "ディスク 1", "temp": 40, "ssd": False, "unit": "TestNAS"},
        {"name": "ディスク 4 (Expansion-1)", "temp": 43, "ssd": False, "unit": "Expansion-1"},
        {"name": "M.2 ドライブ 1", "temp": 41, "ssd": True, "unit": "TestNAS"},
        ],
    }


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        global failed
        failed = True


failed = False

# 1. 警告未満では通知しない
sent_messages.clear()
e = make_engine()
e.process_sample(sample(60), now=1000)
check("1. 60°C では通知なし", len(sent_messages) == 0)

# 2. 警告超過で即時1回
sent_messages.clear()
e = make_engine()
e.process_sample(sample(76), now=1000)
check("2. 76°C で警告1回", len(sent_messages) == 1 and "[警告]" in sent_messages[0])

# 3. 警告継続は3分後に再通知
sent_messages.clear()
e.process_sample(sample(76), now=1100)
check("3a. 100秒後は再通知なし", len(sent_messages) == 0)
e.process_sample(sample(76), now=1181)
check("3b. 181秒後に再通知", len(sent_messages) == 1 and "継続" in sent_messages[0])

# 4. 重大へは即時
sent_messages.clear()
e.process_sample(sample(86), now=1200)
check("4. 86°C で重大即時", len(sent_messages) == 1 and "[重大]" in sent_messages[0])

# 5. 緊急へは即時
sent_messages.clear()
e.process_sample(sample(91), now=1300)
check("5. 91°C で緊急即時", len(sent_messages) == 1 and "[緊急]" in sent_messages[0])

# 6. 低下と復帰
sent_messages.clear()
e.process_sample(sample(86), now=1400)
check("6a. 緊急→重大の低下通知", len(sent_messages) == 1 and "低下" in sent_messages[0])
sent_messages.clear()
e.process_sample(sample(50), now=1500)
check("6b. 重大→正常の復帰通知", len(sent_messages) == 1 and "[復帰]" in sent_messages[0])

# 7. API障害: 2回では通知なし、3回で通知、継続再通知、復旧通知
sent_messages.clear()
e = make_engine()
e.process_failure("接続不能", now=1000)
e.process_failure("接続不能", now=1010)
check("7a. 2回失敗では通知なし", len(sent_messages) == 0)
e.process_failure("接続不能", now=1020)
check("7b. 3回失敗でAPI取得不能", len(sent_messages) == 1 and "API取得不能" in sent_messages[0])
e.process_failure("接続不能", now=1100)
check("7c. 80秒後は再通知なし", len(sent_messages) == 1)
e.process_failure("接続不能", now=1201)
check("7d. 181秒後に再通知", len(sent_messages) == 2)
sent_messages.clear()
e.process_sample(sample(50), now=1300)
check("7e. 復旧通知", len(sent_messages) == 1 and "API復旧" in sent_messages[0])

# 8. API復旧直後は上昇率を計算しない（誤通知なし）
sent_messages.clear()
e = make_engine({
    "notifyEnabled": True, "hushUntil": None, "severity": "WARNING",
    "lastSent": {"WARNING": 900}, "approachSent": False,
    "apiFail": 3, "apiDown": True, "apiLastNotify": 900,
    "tgOffset": 0, "prevTemp": None, "prevTempAt": None,
})
e.process_sample(sample(84), now=1000)
api_recovery = [m for m in sent_messages if "API復旧" in m]
approach = [m for m in sent_messages if "接近" in m]
check("8. 復旧直後は接近通知なし", len(api_recovery) == 1 and len(approach) == 0)

# 9. 重大値接近の予測通知（連続した有効サンプル）
sent_messages.clear()
e = make_engine({
    "notifyEnabled": True, "hushUntil": None, "severity": "WARNING",
    "lastSent": {"WARNING": 1000}, "approachSent": False,
    "apiFail": 0, "apiDown": False, "apiLastNotify": None,
    "tgOffset": 0, "prevTemp": 80, "prevTempAt": 990,
})
e.process_sample(sample(82), now=1000)
check("9a. 接近通知が出る", len([m for m in sent_messages if "接近" in m]) == 1)
e.process_sample(sample(83), now=1010)
check("9b. 同一WARNING内で接近通知は1回だけ", len([m for m in sent_messages if "接近" in m]) == 1)

# 10. 通知停止中は送信しないが状態は更新される
sent_messages.clear()
st = {
    "notifyEnabled": False, "hushUntil": None, "severity": "NORMAL",
    "lastSent": {}, "approachSent": False,
    "apiFail": 0, "apiDown": False, "apiLastNotify": None,
    "tgOffset": 0, "prevTemp": None, "prevTempAt": None,
}
e = make_engine(st)
e.process_sample(sample(95), now=1000)
check("10a. 停止中は送信なし", len(sent_messages) == 0)
check("10b. 状態は EMERGENCY に更新", st["severity"] == "EMERGENCY")
check("10c. 履歴にはスキップが記録される", any(h[2] is False for h in history_entries))

# 11. 通知文の内容（第7節）
sent_messages.clear()
e = make_engine()
e.process_sample(sample(76), now=1000)
msg = sent_messages[0]
check("11a. 監視対象名", "TestNAS" in msg)
check("11b. 閾値", "75" in msg and "85" in msg and "90" in msg)
check("11c. CPU/メモリ", "CPU: 20%" in msg and "メモリ: 36%" in msg)
check("11d. 最高温度ディスク", "M.2 ドライブ 1 41°C" in msg or "ディスク 4 (Expansion-1) 43°C" in msg)
check("11e. 拡張ユニット最高温度", "拡張ユニットの最高温度: ディスク 4 (Expansion-1) 43°C" in msg)
check("11f. 送信先の明記", "Telegram" in msg)

print()
print("FAILED" if failed else "ALL TESTS PASSED")
sys.exit(1 if failed else 0)
