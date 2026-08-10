#!/usr/bin/env python3
"""Touch Barのステータス表示タップで、注意が必要なセッションへフォーカス移動する。

対象の選び方: permission待ち > 入力待ち > busy > idle（同格は更新が新しい順）。
iTerm2がフロントで先頭候補が既にアクティブなら、リスト内の次へ巡回する
（連打でセッションをループできる）。フロントでなければ常に先頭候補へ飛ぶ。
cc-send.py と同じく iTerm2 のセッション GUID 直指定なので誤爆しない。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_common as cc

ATTENTION = {"waiting": 0, "busy": 1, "idle": 2}

OSA = '''
on run argv
  set n to count of argv
  if n is 0 then return "noargs"
  tell application "iTerm2"
    set activeId to ""
    try
      set activeId to id of current session of current tab of current window
    end try
    set isFront to frontmost
    -- アクティブセッションが候補リスト内にあれば、その次から巡回する
    set idx to 0
    repeat with i from 1 to n
      if item i of argv is activeId then
        set idx to i
        exit repeat
      end if
    end repeat
    if isFront and idx > 0 then
      set target to item ((idx mod n) + 1) of argv
    else
      set target to item 1 of argv
    end if
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if id of s is target then
            select s
            select t
            select w
            activate
            return "ok " & target
          end if
        end repeat
      end repeat
    end repeat
    activate
    return "notfound " & target
  end tell
end run
'''


def rank(rec):
    st = rec.get("state", "idle")
    perm = 0 if (st == "waiting" and rec.get("waiting_kind") == "permission") else 1
    return (perm, ATTENTION.get(st, 3), -rec.get("updated_at", 0))


def main():
    recs = sorted(cc.load_sessions(), key=rank)

    # 先頭候補がpeers(デスクトップアプリ駆動)ならアプリを前面に出す。
    # 既にアプリが前面のときは下のiTerm巡回へ落ちる(タップでアプリ⇄端末を回れる)
    if recs and cc.is_peers(recs[0]) and cc.front_bundle() != cc.CLAUDE_DESKTOP:
        subprocess.run(["/usr/bin/open", "-b", cc.CLAUDE_DESKTOP], timeout=10)
        print("focus -> Claude app (peers session)")
        return

    guids = []
    for rec in recs:
        g = rec.get("term_guid")
        if g and not cc.is_peers(rec) and g not in guids:
            guids.append(g)
    if not guids:
        if recs and cc.is_peers(recs[0]):
            print("peers session already frontmost; nothing else to focus")
        else:
            subprocess.run(["/usr/bin/open", "-a", "iTerm"], timeout=10)
            print("no session with term_guid; activated iTerm only")
        return
    r = subprocess.run(["/usr/bin/osascript", "-e", OSA] + guids,
                       capture_output=True, text=True, timeout=15)
    print("focus ->", (r.stdout or r.stderr).strip())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("cc-focus failed:", e)
