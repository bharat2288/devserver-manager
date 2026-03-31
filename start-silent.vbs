' Start Dev Server Manager silently (no console window)
' Used for Windows startup so DSM runs in background
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run """C:\Program Files\Python311\python.exe"" main.py", 0, False
