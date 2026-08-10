#!/usr/bin/env python3
"""許可プロンプトへの応答をターミナルセッションに直接送信する。

フロントアプリへのキーストロークではなく、状態ファイルに記録した iTerm2 の
セッション GUID に `write text` で送るため、どのアプリがフロントでも誤爆しない。
送信直前に「対象セッションが今も許可待ちか」を再確認する。

usage: cc-send.py <allow|always|reject>
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_common as cc

KEYS = {"allow": "1", "always": "2", "reject": "esc"}

OSA = '''
on run argv
  set theGuid to item 1 of argv
  set payload to item 2 of argv
  if payload is "esc" then set payload to character id 27
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if id of s is theGuid then
            tell s to write text payload newline NO
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "allow"
    key = KEYS.get(kind, "1")

    sessions = cc.load_sessions()
    waiting = [r for r in sessions
               if r.get("state") == "waiting"
               and r.get("waiting_kind") == "permission"]

    # 送信先は**いま実際にフォーカスしているセッション**に限る。
    # 以前は「全許可待ちのうち updated_at 最大」を選んでいたが、表示側
    # (cc-btn.py)は別の基準で出すかどうかを決めていたため、2つが同時に
    # 許可待ちだと**見て押したのと違うセッションに 1/2/esc が入る**。
    # 表示は近似・送信は実測、という他の送信経路と同じ役割分担に揃える
    rec = cc.send_target(sessions)
    if rec is not None and rec.get("waiting_kind") == "permission" \
            and rec.get("state") == "waiting":
        # ボタンに出ていたラベルと同じ解決結果でなければ押した意味が変わる。
        # 4択以上は番号ボタンの担当なので、ここに来たら送らない
        labels = cc.permission_labels(rec)
        if not 2 <= len(labels) <= 3:
            print("target has %d options (not 1/2/esc shaped); nothing sent"
                  % len(labels))
            return
        if kind == "always" and len(labels) < 3:
            print("target has no 2nd option; nothing sent")
            return
        r = subprocess.run(["/usr/bin/osascript", "-e", OSA,
                            rec["term_guid"], key],
                           capture_output=True, text=True, timeout=15)
        print("sent %r to %s -> %s" % (key, rec["term_guid"][:8],
                                       (r.stdout or r.stderr).strip()))
        return

    if waiting and all(cc.is_peers(r) for r in waiting):
        # peers待ちのみ: 承認はアプリ内でしかできないので前面に出すまで
        subprocess.run(["/usr/bin/open", "-b", cc.CLAUDE_DESKTOP], timeout=10)
        print("peers session waiting; activated Claude app (approve there)")
        return
    print("focused session is not permission-waiting; nothing sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("cc-send failed:", e)
