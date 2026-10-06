' ============================================================
'  EVA BOT — APP LAUNCHER (windowless)
'  Double-click me: opens ONLY the control panel.
'  No CMD / console window appears at all.
'  (Console output, if any, goes to data\logs\gui_console.log)
' ============================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = dir

' 1) Project venv pythonw (best - installed by install.bat)
py = dir & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(py) Then
    shell.Run """" & py & """ -m entry.main", 0, False
    WScript.Quit
End If

' 2) System pythonw
If shell.Run("cmd /c where pythonw >nul 2>&1", 0, True) = 0 Then
    shell.Run "pythonw -m entry.main", 0, False
    WScript.Quit
End If

' 3) py launcher (windowless)
If shell.Run("cmd /c py -3 -c ""import sys"" >nul 2>&1", 0, True) = 0 Then
    shell.Run "py -3w -m entry.main", 0, False
    WScript.Quit
End If

MsgBox "Python not found on this PC." & vbCrLf & vbCrLf & _
       "Please run install.bat FIRST (one-time setup)," & vbCrLf & _
       "then double-click this app again.", 48, "EVA Bot"
