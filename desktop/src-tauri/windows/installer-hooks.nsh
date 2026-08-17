!macro NSIS_HOOK_PREUNINSTALL
  nsExec::ExecToLog 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\scripts\manage-system-startup.ps1" -Action Uninstall'
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION "是否同时删除 Local Project Console 的项目配置、监控记录、配对设备和日志？选择“否”将保留数据，重新安装后可以继续使用。" IDNO keep_local_data
  RMDir /r "$LOCALAPPDATA\LocalProjectConsole"
  keep_local_data:
!macroend
