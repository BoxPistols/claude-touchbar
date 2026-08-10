#!/usr/bin/env python3
"""Claude Code の hook から呼ばれ、セッション状態を JSON に書き出す。

usage: cc-hook.py <state>
  state: busy | idle | waiting | permission | end
  stdin: Claude Code の hook 入力 JSON

permission は PermissionRequest イベント専用。Notification の文字列判定と違い
「許可プロンプトが出た」ことを確定的に表す(waiting_kind=permission 固定)。
"""
import json
import os
import subprocess
import sys
import time

STATE = sys.argv[1] if len(sys.argv) > 1 else "idle"
BASE = os.path.expanduser("~/.claude/btt/sessions")


PEERS_TOKEN = "server:claude-peers"


def is_peers_command(cmd):
    """デスクトップアプリ駆動（UIがアプリ側にある）セッションかを判定する。

    **部分一致で見ないこと。** `server:claude-peers` は「チャンネルを読み込む
    引数」としても現れる:

        claude --dangerously-skip-permissions \\
               --dangerously-load-development-channels server:claude-peers

    これは iTerm2 で動く**普通の TUI セッション**（term_program=iTerm.app）で、
    peers 扱いにすると `session_host()` が Claude デスクトップを返すため、
    許可応答ボタンもコマンドボタンも**一切出なくなる**（実際に全セッションが
    この状態だった。--dangerously-skip-permissions で許可プロンプト自体が
    出ないため、長く気づかれなかった）。

    サブコマンドとして置かれている場合だけ peers とみなす（直前がフラグなら
    そのフラグの値）。分類を誤っても、送信側は GUID が iTerm2 に存在するかを
    確かめて "notfound" を返すので、実害は片側に閉じる。
    """
    toks = cmd.split()
    for i, t in enumerate(toks):
        if t == PEERS_TOKEN and (i == 0 or not toks[i - 1].startswith("-")):
            return True
    return False


def permission_options(payload):
    """許可プロンプトに実際に並んでいる選択肢のラベル。取れなければ []。

    **ここが唯一の入手経路。** payload は hook にしか渡らず、transcript にも
    セッション状態にも残らない。取り逃すと描画側は当てずっぽうのラベルを
    出すしかなくなる（それが `2 常に許可` が選択肢プロンプトに出ていた原因）。

    AskUserQuestion は `tool_input.questions[].options[].label` を持つ。
    質問は順に1つずつ提示されるので **先頭の質問の選択肢だけ**を採る。
    末尾の "Other" は含めない — 番号が振られるかを実測していないため、
    ずれた番号を送るくらいなら宣言された選択肢だけに絞る。
    """
    if (payload.get("tool_name") or "") != "AskUserQuestion":
        return []
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return []
    qs = ti.get("questions")
    if not isinstance(qs, list) or not qs or not isinstance(qs[0], dict):
        return []
    out = []
    for o in (qs[0].get("options") or []):
        if isinstance(o, dict) and str(o.get("label") or "").strip():
            out.append(str(o["label"]).strip())
    return out


def log_permission_shape(payload):
    """PermissionRequest の payload の「形」だけを控える。

    許可応答ボタンのラベルは現状 `1 許可 / 2 常に許可 / esc 拒否` の固定で、
    実際の選択肢（ExitPlanMode の auto-accept/manually approve、AskUserQuestion の
    任意の N 択）と一致しない。実ラベルを出すには payload に何が来るのかを
    確定させる必要があるが、hook の入力を目視できる場所が無いため記録する。

    **値は残さずキー名だけにする。** tool_input には Bash のコマンド本文や
    Write のファイル内容がそのまま入るので、平文で溜めるとログ自体が
    秘密情報になる（このファイルは Git 管理外だが、それは理由にならない）。
    """
    try:
        ti = payload.get("tool_input")
        rec = {
            "at": time.strftime("%m-%d %H:%M:%S"),
            "tool": payload.get("tool_name") or "",
            "keys": sorted(payload.keys()),
            "input_keys": sorted(ti.keys()) if isinstance(ti, dict) else type(ti).__name__,
        }
        path = os.path.join(os.path.dirname(BASE), "perm.log")
        # 無制限に伸ばさない。形を知るのが目的なので直近だけあればよい
        old = []
        if os.path.exists(path):
            with open(path) as f:
                old = f.read().splitlines()[-49:]
        with open(path, "w") as f:
            f.write("\n".join(old + [json.dumps(rec, ensure_ascii=False)]) + "\n")
    except Exception:
        pass


def probe_process(pid, prev):
    """(UI種別, プロセス開始時刻) を返す。ps は pid が変わったときだけ実行する。

    UI種別: "tty"=ターミナルTUI / "peers"=デスクトップアプリ駆動(server:claude-peers)。
    peersはUIがアプリ側にあり、継承環境のITERM_SESSION_IDは実際の表示位置を
    指さない(キー送信しても読む相手がいない)ため、送信・フォーカスの分岐に使う。

    開始時刻も控えるのは **pid 再利用**への対策。生存確認が os.kill(pid,0) だけだと、
    Claude が強制終了され(SessionEnd 未発火)その pid が別プロセスに割り当てられた
    場合に状態ファイルが永久に消えず、実在しないセッションの ⏳ が残り続ける。
    """
    keep = (prev.get("ui") or "", prev.get("pid_start") or "")
    # pid_start が無い記録は旧形式。判定を作り直させるため再取得する
    if pid and prev.get("pid") == pid and prev.get("ui") and prev.get("pid_start"):
        return keep
    if not pid:
        return keep
    try:
        # LC_ALL=C 必須。lstart はロケール依存で書式が変わり、照合する側
        # (BTT から起動される描画側)と環境が違うと一致せず、生きている
        # セッションを死んだと誤判定してしまう
        out = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart=,command="],
                             capture_output=True, text=True, timeout=2,
                             env=dict(os.environ, LC_ALL="C")).stdout
    except Exception:
        return keep
    # lstart は必ず5トークン("Sun Aug  3 01:00:00 2026")。残りが command
    parts = out.split(None, 5)
    if len(parts) < 6:
        return keep
    return ("peers" if is_peers_command(parts[5]) else "tty"), " ".join(parts[:5])


def main():
    os.makedirs(BASE, exist_ok=True)
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    sid = payload.get("session_id")
    if not sid:
        # 共通の "unknown.json" に落とすと、session_id を持たないセッションが
        # 2つ以上あったとき同じファイルを奪い合い、pid は A から state は B から
        # という混ざった record ができる。表示されない方がまだ安全
        return
    path = os.path.join(BASE, sid + ".json")

    if STATE == "end":
        try:
            os.remove(path)
        except OSError:
            pass
        return

    prev = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                prev = json.load(f)
        except Exception:
            pass

    now = time.time()
    env = os.environ
    term_id = env.get("ITERM_SESSION_ID") or env.get("TERM_SESSION_ID") or ""
    pid = int(env.get("CLAUDE_PID") or prev.get("pid") or 0) or None
    ui, pid_start = probe_process(pid, prev)
    rec = {
        "session_id": sid,
        "state": "waiting" if STATE == "permission" else STATE,
        # cwd はセッション内の cd で変わり表示が紛らわしいため、初回の値を保持する
        "cwd": prev.get("cwd") or payload.get("cwd") or "",
        "transcript": payload.get("transcript_path") or prev.get("transcript") or "",
        "updated_at": now,
        "tool": (payload.get("tool_name") or "") if STATE == "busy" else "",
        # CLI プロセスの pid。生存確認 (os.kill(pid,0)) による sweep に使う
        "pid": pid,
        # pid 単体では再利用と区別できないため開始時刻も控える
        "pid_start": pid_start,
        "ui": ui,
        # iTerm2 のセッション GUID。フォーカス追従・write text 直接送信に使う
        "term_guid": (term_id.split(":")[-1] if term_id else prev.get("term_guid") or ""),
        "term_program": env.get("TERM_PROGRAM") or prev.get("term_program") or "",
        # 起動元アプリ（GUI起動なら bundle id が入る。Desktop App 判別に使う）
        "host_bundle": env.get("__CFBundleIdentifier") or prev.get("host_bundle") or "",
    }

    # PermissionRequest 由来は確定で permission
    if STATE == "permission":
        rec["waiting_kind"] = "permission"
        # どのツールの許可かは応答ボタンのラベルを決めるのに要る。
        # busy のときしか拾っていなかったため、許可待ちでは捨てていた
        rec["tool"] = payload.get("tool_name") or ""
        rec["perm_options"] = permission_options(payload)
        log_permission_shape(payload)
    # Notification は許可プロンプト以外（60秒アイドル等）でも発火するため区別する
    # (PermissionRequest が主経路。こちらは取りこぼし時の保険)
    elif STATE == "waiting":
        msg = (payload.get("message") or "").lower()
        is_perm = any(k in msg for k in ("permission", "approval", "許可", "承認"))
        rec["waiting_kind"] = "permission" if is_perm else "input"
        # 既に許可待ちになっているセッションを弱い通知で上書きしない
        if not is_perm and prev.get("state") == "waiting" \
           and prev.get("waiting_kind") == "permission":
            rec["waiting_kind"] = "permission"

    # **許可待ちが続く間は選択肢を引き継ぐ。** 選択肢を運ぶのは PermissionRequest の
    # payload だけで、そこから漏れると復元経路が無い。実際、PermissionRequest が
    # 書いた直後に Notification(cc-hook.py waiting)が発火して rec を作り直し、
    # `perm_options` を消していた（実測: 表示中の記録は tool="" / perm_options 無しで、
    # ラベルが既定の `1 許可 / 2 常に許可` に戻る＝直したはずの不一致がそのまま出る）。
    # **この if/elif 連鎖の後ろに置くこと** — 途中に挟むと elif がこちらに結合して
    # Notification の分岐が死ぬ（構文は通るので compile では気づけない）
    if rec.get("waiting_kind") == "permission" \
            and not rec.get("perm_options") \
            and prev.get("waiting_kind") == "permission":
        rec["perm_options"] = prev.get("perm_options") or []
        rec["tool"] = rec.get("tool") or prev.get("tool") or ""

    # ターン開始時刻。維持してよいのは「許可待ちから同じターンに戻った」ときだけ。
    # waiting を一律で継続扱いにすると、60秒アイドル通知で waiting に落ちた後に
    # ユーザーが新しい指示を入力しても started_at が前のターンのままになり、
    # 経過時間が放置時間ぶん水増しされる（⏳ 3h20m のような表示になる）
    prev_state = prev.get("state")
    resume = (prev_state == "busy"          # 同じターンの継続(ツールごとに発火する)
              or (prev_state == "waiting"   # 許可待ちから戻った = 同じターン
                  and prev.get("waiting_kind") == "permission"))
    if STATE == "busy" and not resume:
        rec["started_at"] = now
    else:
        rec["started_at"] = prev.get("started_at", now)

    # 並行 hook (async) と衝突しないよう pid 入り一時ファイルで atomic に書く
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # hook がセッションを壊さないよう、失敗しても常に正常終了する
        pass
