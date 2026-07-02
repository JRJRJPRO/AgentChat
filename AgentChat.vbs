' AgentChat 一键启动：双击即可（可右键→发送到→桌面快捷方式）
' 1. 后台无窗口启动服务器（已在运行则新实例绑定端口失败自动退出，无影响）
' 2. 稍等后用 Chrome 应用模式打开（找不到 Chrome 再试 Edge，最后退化为默认浏览器）
Dim sh, fso, base, env, url, chrome, i
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set env = sh.Environment("Process")
base = fso.GetParentFolderName(WScript.ScriptFullName)
url = "http://127.0.0.1:8787"

sh.Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & base & "\start.ps1""", 0, False
WScript.Sleep 2500

Dim candidates(2)
candidates(0) = env("ProgramFiles") & "\Google\Chrome\Application\chrome.exe"
candidates(1) = env("ProgramFiles(x86)") & "\Google\Chrome\Application\chrome.exe"
candidates(2) = env("LocalAppData") & "\Google\Chrome\Application\chrome.exe"

chrome = ""
For i = 0 To 2
    If chrome = "" And fso.FileExists(candidates(i)) Then chrome = candidates(i)
Next

If chrome <> "" Then
    sh.Run """" & chrome & """ --app=" & url, 1, False
Else
    On Error Resume Next
    sh.Run "cmd /c start msedge --app=" & url, 0, False
    If Err.Number <> 0 Then sh.Run "cmd /c start " & url, 0, False
    On Error GoTo 0
End If
