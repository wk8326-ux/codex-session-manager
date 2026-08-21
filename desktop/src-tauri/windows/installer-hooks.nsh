!define LPC_INSTALLER_PREFLIGHT_SOURCE "${__FILEDIR__}\installer-preflight.ps1"

!macro NSIS_HOOK_PREINSTALL
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=lpc-installer-preflight.ps1 "${LPC_INSTALLER_PREFLIGHT_SOURCE}"
  SetOutPath "$INSTDIR"
  nsExec::ExecToStack '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\lpc-installer-preflight.ps1" -InstallDirectory "$INSTDIR"'
  Pop $0
  Pop $1
  StrCmp $0 "0" lpc_preinstall_done
  MessageBox MB_OK|MB_ICONSTOP "无法安全停止旧版 Local Project Console 后台，安装已中止。请从托盘停止后台后重试。$\r$\n$\r$\n$1"
  Abort
  lpc_preinstall_done:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  nsExec::ExecToLog '"$WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$INSTDIR\scripts\manage-system-startup.ps1" -Action Uninstall'
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION "是否同时删除 Local Project Console 的项目配置、监控记录、配对设备和日志？选择“否”将保留数据，重新安装后可以继续使用。" IDNO keep_local_data
  RMDir /r "$LOCALAPPDATA\LocalProjectConsole"
  keep_local_data:
!macroend
