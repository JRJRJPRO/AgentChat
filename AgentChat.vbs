' AgentChat 一键启动：双击即可（可发送到桌面快捷方式）
' 1. 后台无窗口启动服务器（已在运行则新实例绑定端口失败自动退出，无影响）
' 2. 稍等后用 Edge 应用模式打开，观感就是一个独立的聊天软件窗口
Dim sh, fso, base
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)

sh.Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & base & "\start.ps1""", 0, False
WScript.Sleep 2500
sh.Run "cmd /c start msedge --app=http://127.0.0.1:8787", 0, False
