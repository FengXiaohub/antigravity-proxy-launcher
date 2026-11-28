on run
	set T to "agy update"
	try
		set myPath to POSIX path of (path to me)
		set base to quoted form of (myPath & "Contents/Resources/install.sh")
		activate
		with timeout of 600 seconds
			set chk to do shell script "/bin/sh " & base & " check"
		end timeout
		set parts to my fields(chk)
		set checkCode to item 1 of parts
		if checkCode is "FAIL" then
			my notice(T, my detail(chk), true)
		else if checkCode is "WARN" then
			my installationResult(chk, base, T)
		else if checkCode is "RESTORE_READY" and (count of parts) is 3 then
			-- "取消" is the cancel button (error -128); the explicit check guards any other return path.
			set restoreChoice to display dialog "当前 Antigravity 缺失或校验未通过。已找到并校验旧版本 " & item 2 of parts & "。" & return & return & "要恢复这个旧版本吗？如仍有 Antigravity 进程运行，会先正常退出；会话数据不会被删除。" buttons {"取消", "恢复旧版本"} default button "取消" cancel button "取消" with icon caution with title T
			if button returned of restoreChoice is "恢复旧版本" then
				my performRestore(item 3 of parts, base, T)
			end if
		else if checkCode is "RECOVERED" then
			my notice(T, "已恢复并检查旧版本 " & item 2 of parts & "。" & return & "请用原来的「Antigravity 代理启动」打开。", false)
		else if checkCode is "NONE" then
			my notice(T, "没有找到已下载的新更新包。当前版本是 " & item 2 of parts & "。" & return & return & "这不代表线上没有新版本。请先让 Antigravity 检查并下载更新，下载完成后再双击本图标。", false)
		else if checkCode is "SAME" then
			my notice(T, "本地更新包与当前版本 " & item 2 of parts & " 相同，无需重复安装。" & return & "本工具没有查询线上最新版本。", false)
		else if checkCode is "OLDER" then
			my notice(T, "本地更新包比当前版本 " & item 2 of parts & " 更旧，已跳过，不会降级。", false)
		else if checkCode is "READY" and (count of parts) is 4 then
			set curVer to item 2 of parts
			set newVer to item 3 of parts
			set ticket to item 4 of parts
			set updateChoice to display dialog "更新包已通过校验，准备从 " & curVer & " 更新到 " & newVer & "。" & return & return & "会先正常退出 Antigravity，进行中的任务会中断。请等任务完成后再继续。" & return & "整包替换为官方新版（如装过汉化补丁，更新后重打一次即可）；会保留最近一次旧版本以便恢复。" buttons {"取消", "继续更新"} default button "取消" cancel button "取消" with icon caution with title T
			if button returned of updateChoice is "继续更新" then
				with timeout of 600 seconds
					set inst to do shell script "/bin/sh " & base & " install " & quoted form of ticket
				end timeout
				my installationResult(inst, base, T)
			end if
		else
			my notice(T, "检查结果不完整，未执行新的安装。请关闭窗口后重试；若仍失败，请联系维护人员。", true)
		end if
	on error messageText number errorNumber
		if errorNumber is not -128 then
			my notice(T, "操作未完成：" & messageText & return & return & "若发生在更新过程中，请重新双击本图标检查恢复状态。", true)
		end if
	end try
end run

on installationResult(inst, base, T)
	set iparts to my fields(inst)
	set resultKind to item 1 of iparts
	if resultKind is "RECOVERED" and (count of iparts) is 2 then
		my notice(T, "已恢复并检查旧版本 " & item 2 of iparts & "。请用原来的代理启动图标打开。", false)
	else if resultKind is "OK" and (count of iparts) is 2 then
		my notice(T, "已更新到 " & item 2 of iparts & "，后台启动检查通过。" & return & "旧版本原件已保留。", false)
	else if resultKind is "WARN" and (count of iparts) is 3 then
		set choice to display dialog "已安装并校验版本 " & item 2 of iparts & "，但未能确认 Antigravity 启动完成。" & return & return & "你可以稍后用原来的代理启动图标重试，也可以现在恢复旧版本。恢复会正常退出 Antigravity。" buttons {"恢复旧版本", "稍后重试"} default button "稍后重试" with icon caution with title T
		if button returned of choice is "恢复旧版本" then
			my performRestore(item 3 of iparts, base, T)
		end if
	else
		my notice(T, my detail(inst), true)
	end if
end installationResult

on performRestore(ticket, base, T)
	with timeout of 600 seconds
		set restored to do shell script "/bin/sh " & base & " rollback " & quoted form of ticket
	end timeout
	set rparts to my fields(restored)
	if (item 1 of rparts) is "RESTORED" and (count of rparts) is 2 then
		my notice(T, "已恢复并校验旧版本 " & item 2 of rparts & "，已请求用代理启动器打开。", false)
	else if (item 1 of rparts) is "RESTORED_CLOSED" and (count of rparts) is 2 then
		my notice(T, "已恢复并校验旧版本 " & item 2 of rparts & "。请用原来的代理启动图标打开。", false)
	else
		my notice(T, my detail(restored), true)
	end if
end performRestore

on fields(valueText)
	set AppleScript's text item delimiters to " "
	set values to text items of valueText
	set AppleScript's text item delimiters to ""
	if (count of values) is 0 then error "未收到操作结果"
	return values
end fields

on detail(valueText)
	if valueText starts with "FAIL " and (length of valueText) > 5 then
		return text 6 thru -1 of valueText
	end if
	return "未能确认操作结果。请重新双击检查恢复状态；若仍失败，请联系维护人员。"
end detail

on notice(windowTitle, messageText, isWarning)
	tell current application to activate
	if isWarning then
		display dialog messageText buttons {"好"} default button 1 with icon caution with title windowTitle
	else
		display dialog messageText buttons {"好"} default button 1 with icon note with title windowTitle
	end if
end notice

