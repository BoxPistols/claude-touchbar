-- 最前面アプリと、その最前面ウィンドウのタイトル（取れれば Chrome の URL も）を
-- ~/.claude/btt/web-front に書き続ける常駐スクリプト。
--
-- ■ なぜ常駐なのか（実測に基づく）
--
-- ウィジェットの表示判定から直接これらを取ろうとすると、osascript の起動だけで 62ms、
-- 対象アプリへの初回 AppleEvent まで含めて 1 回 200ms かかる。2 秒間隔 × ボタン数ぶん
-- 効くので 1 コアの 10% を燃やす。同じ問いを**同一プロセス内で繰り返す**と 2 回目以降は
-- 15ms なので、常駐 1 本に集約して結果をファイルに置き、ウィジェットは bash 組み込みで
-- 読むだけにする。（web-sync.py 冒頭「重い判定を足したくなったら常駐 1 本に集約する」の実装）
--
-- ■ なぜ System Events でウィンドウタイトルを取るのか（2026-08-15 の修正）
--
-- 以前は Chrome の AppleScript（`URL of active tab of front window`）だけを見ていた。
-- **これは利用者の環境で 1 度も値を返さなかった。** 実測では `count of windows` が 0 を
-- 返す一方、同時刻に System Events では窓が見えていた（`… - Google Chrome - A`）。
-- 別の user-data-dir で起動した 2 つ目の Chrome インスタンスや、app モード / PWA の窓は
-- Chrome の AppleScript から見えない。結果、match 付きのボタンが**常に隠れたまま**になった。
--
-- System Events は最前面プロセスの窓を直接見るので、ブラウザの種類・インスタンス・
-- プロファイルに依存しない。Chrome の URL は取れたときだけ足す（取れなくても困らない）。
--
-- ■ 書式
--
--   "<最前面の bundle id>\t<ウィンドウタイトル>\t<URL または空>" の 1 行（改行なし）。
--   照合は web-shortcuts.json の match（部分一致・大文字小文字無視）で 1 行全体に対して行う。
--
-- ■ 書き込み
--
--   値が変わったときだけ tmp→mv で置き換える（毎秒 read する側に中途半端な行を見せない）。
--   ただし HEARTBEAT 回に 1 度は変化が無くても書く。**生きているのに更新しないのと、
--   固まっているのとを、読み手が mtime で区別できるようにするため**（TCC の承認待ちで
--   AppleEvent が mach_msg のまま止まる事故を実際に踏んだ。エラーは一切出ない）。

property INTERVAL : 1
property HEARTBEAT : 30
property BACKOFF : 60
property FLAG : ((POSIX path of (path to home folder)) & ".claude/btt/web-front")
property TAB : (character id 9)

-- 最前面プロセスの bundle id と窓タイトル。**System Events 経由なのでアプリを問わない。**
-- **短いタイムアウトを付けないこと。** TCC の承認ダイアログが出ている間 AppleEvent は
-- 応答を待つ。ここで 3 秒で打ち切ると、利用者が押す前にこちらがキャンセルし、次の周回で
-- また要求するのでダイアログが出続けて**永久に承認できない**（実際にそうなった）。
-- 既定の 120 秒で待ち、失敗が続いたら呼び出し側が間隔を空ける。
on frontInfo()
	set b to ""
	set t to ""
	try
		tell application "System Events"
			set p to first application process whose frontmost is true
			set b to bundle identifier of p
			-- 窓を持たないアプリ（メニューだけ残った状態等）ではここが失敗する。
			-- bundle id は返したいので内側で握りつぶす
			try
				set t to name of front window of p
			end try
		end tell
	on error
		return {"", ""}
	end try
	if b is missing value then set b to ""
	if t is missing value then set t to ""
	return {b, t}
end frontInfo

-- Chrome の現在 URL。取れない環境があるので**あくまで補助**。
-- `is running` を先に見るのは、tell application が起動していないアプリを起動するため。
on chromeURL(b)
	if b does not contain "com.google.Chrome" then return ""
	set u to ""
	try
		if application "Google Chrome" is running then
			with timeout of 3 seconds
				tell application "Google Chrome"
					if (count of windows) > 0 then set u to URL of active tab of front window
				end tell
			end timeout
		end if
	on error
		return ""
	end try
	if u is missing value then set u to ""
	return u
end chromeURL

on writeLine(s)
	set tmp to FLAG & ".tmp"
	do shell script "/usr/bin/printf '%s' " & quoted form of s & " > " & quoted form of tmp & ¬
		" && /bin/mv -f " & quoted form of tmp & " " & quoted form of FLAG
end writeLine

-- 起動直後に必ず 1 回書く（前回終了時の残骸をそのまま信じさせない）
set lastLine to "@@init@@"
set sinceWrite to HEARTBEAT
set fails to 0
set skipTicks to 0
repeat
	set cur to ""
	if skipTicks > 0 then
		-- 取得できない状態が続いている。承認が下りていない可能性が高いので間隔を空ける。
		-- 毎秒問い続けると、拒否のたびにダイアログを出す環境で操作不能になる
		set skipTicks to skipTicks - 1
	else
		try
			set info to frontInfo()
			set b to item 1 of info
			set t to item 2 of info
			if b is not "" or t is not "" then
				set cur to b & TAB & t & TAB & chromeURL(b)
				set fails to 0
			else
				set fails to fails + 1
			end if
		end try
		if fails ≥ 3 then
			set skipTicks to BACKOFF
			set fails to 0
		end if
	end if
	set sinceWrite to sinceWrite + 1
	if cur is not lastLine or sinceWrite ≥ HEARTBEAT then
		try
			writeLine(cur)
			set lastLine to cur
			set sinceWrite to 0
		end try
	end if
	delay INTERVAL
end repeat
