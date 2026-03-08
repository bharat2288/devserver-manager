' Start Dev Server Manager silently (no console window)
' Used for Windows startup so DSM runs in background
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "python main.py", 0, False
