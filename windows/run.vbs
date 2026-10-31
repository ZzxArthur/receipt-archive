Option Explicit
Dim shell, files, root, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & root & "\runtime\pythonw.exe" & Chr(34) & " " & Chr(34) & root & "\windows\launcher.py" & Chr(34)
shell.Run command, 0, False
