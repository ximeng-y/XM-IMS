; XM-IMS Windows 用户级安装包
; 构建参数（由 desktop/build.py 传入）：
;   APP_VERSION    应用版本（来自 app/version.py）
;   BUILD_ID       本次构建标识（Setup 与更新 ZIP 共用）
;   SLOT           版本槽名 <X.Y.Z>-<构建标识>
;   STAGE_DIR      版本载荷暂存目录（xm-ims.exe、resources\、清单与版本清单）
;   LAUNCHER_FILE  固定根启动器（xm-ims.exe）
;   SUPPORT_DIR    独立恢复支持源码（update_trial.py、process_lock.py）
;   OUT_FILE       安装包输出路径
;
; 约定：
;   - 只装到当前用户可写目录，默认 %LOCALAPPDATA%\Programs\XM-IMS
;   - 不装 Python / Git，不改 PATH，不设开机启动；Git 由应用内引导准备
;   - data、tools、.XMTEMP 从不作为安装输入，也从不删除
;   - 版本载荷独立保存在 versions\<槽名>；固定根只放启动器与恢复支持
;   - 旧平铺布局首次迁移保留旧程序为 xm-ims-legacy.exe，作为回退目标

Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user
ManifestDPIAware true
ManifestSupportedOS all
Name "XM-IMS"
OutFile "${OUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\XM-IMS"
InstallDirRegKey HKCU "Software\XM-IMS" "InstallDir"
ShowInstDetails show
ShowUninstDetails show
BrandingText "XM-IMS ${APP_VERSION}"

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\XM-IMS"
!define MANIFEST "installed-program-files.txt"
!define LAUNCHER "xm-ims.exe"
!define LEGACY "xm-ims-legacy.exe"
!define SELECTION "data\update\desktop\selection.ini"
!define ROOT_MANIFEST "installed-program-files.txt"

Var UpdateMode
Var UpdatePid
Var UpdateDir
Var ManifestHandle
Var ManifestLine
Var ManifestFailed
Var TailChar
Var LegacyMode
Var EntryReady

; 停止本次安装 / 卸载：静默调用不能弹窗等用户确认，直接以非零退出码结束，
; 由调用方判定失败；交互安装才提示原因。Abort 在静默模式下会卡在 MessageBox，
; 卸载段同样使用本宏。
!macro FailStop Message
  ${If} ${Silent}
    SetErrorLevel 2
    Quit
  ${EndIf}
  MessageBox MB_ICONSTOP|MB_OK "${Message}"
  Abort
!macroend

; ---------- 命令行参数 ----------
; /S 静默安装（更新时使用）
; /XM_PID=<pid> 等待该进程退出后再覆盖程序文件
; 安装目录由调用方通过环境变量 XM_UPDATE_DIR 传入：NSIS 的 /D= 必须放在最后，
; 且会吞掉其后所有参数；路径含空格时还会被整体加引号，无法与自定义参数共存。
!macro ParseArgs
  ${GetParameters} $R9
  ${GetOptions} $R9 "/XM_PID=" $UpdatePid
  ${If} $UpdatePid != ""
    StrCpy $UpdateMode "1"
  ${EndIf}
  ; 更新由程序发起时用环境变量指定目标目录：用户手动安装不会设置这个变量
  ReadEnvStr $UpdateDir "XM_UPDATE_DIR"
  ${If} $UpdateDir != ""
    StrCpy $INSTDIR $UpdateDir
  ${EndIf}
!macroend
!define ParseArgs "!insertmacro ParseArgs"

Function .onInit
  ${ParseArgs}
  ${IfNot} ${IsNativeAMD64}
    !insertmacro FailStop "XM-IMS 只支持 64 位 Windows。"
  ${EndIf}
  StrCpy $LegacyMode "0"
  StrCpy $EntryReady "0"
FunctionEnd

; 等待传入的进程真正结束再覆盖程序文件；超时停止更新，不按进程名杀程序
; System 插件的 .R1/.R2 写入 $R1/$R2，判断必须读同一组寄存器
Function WaitForPrevious
  ${If} $UpdatePid == ""
    Return
  ${EndIf}
  ; SYNCHRONIZE 权限；进程已不存在时返回 0，直接继续
  System::Call 'kernel32::OpenProcess(i 0x00100000, i 0, i $UpdatePid) p .R1'
  ${If} $R1 == 0
    Return
  ${EndIf}
  StrCpy $R8 0
  ${Do}
    System::Call 'kernel32::WaitForSingleObject(p R1, i 1000) i .R2'
    ${If} $R2 == 0
      ${Break}
    ${EndIf}
    IntOp $R8 $R8 + 1
    ${If} $R8 >= 120
      System::Call 'kernel32::CloseHandle(p R1)'
      !insertmacro FailStop "XM-IMS 仍在运行，已停止更新。请在托盘菜单中选择退出后重试。"
    ${EndIf}
  ${Loop}
  System::Call 'kernel32::CloseHandle(p R1)'
FunctionEnd

; 程序是否被占用：以独占方式打开可执行文件判断，不按进程名结束任何进程
Function CheckRunning
  StrCpy $R5 "0"
  ${IfNot} ${FileExists} "$INSTDIR\${LAUNCHER}"
    Return
  ${EndIf}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\${LAUNCHER}", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
  ; INVALID_HANDLE_VALUE(-1) 表示文件仍被占用
  ${If} $R3 == -1
    StrCpy $R5 "1"
  ${Else}
    ${If} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
FunctionEnd

; 迁移阶段标记：记录“新载荷已准备／旧入口已改名／新根入口已安装”，成功后删除
Function WriteMigrateStage
  ; $0 = 阶段名
  CreateDirectory "$INSTDIR\data\update\desktop"
  WriteINIStr "$INSTDIR\data\update\desktop\migrate.ini" "migrate" "stage" "$0"
FunctionEnd

; 运行检查：当前版本、legacy 与首次启动事务都算运行中，不能只检查根启动器
Function CheckProgramFiles
  StrCpy $R5 "0"
  ${If} ${FileExists} "$INSTDIR\${LAUNCHER}"
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\${LAUNCHER}", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
    ${If} $R3 == -1
      StrCpy $R5 "1"
    ${ElseIf} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
  ${If} $R5 == "0"
  ${AndIf} ${FileExists} "$INSTDIR\${LEGACY}"
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\${LEGACY}", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
    ${If} $R3 == -1
      StrCpy $R5 "1"
    ${ElseIf} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
  ${If} $R5 == "0"
    ClearErrors
    ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "phase"
    ${If} $R6 == "pending"
    ${OrIf} $R6 == "starting"
    ${OrIf} $R6 == "committing"
      StrCpy $R5 "1"
    ${EndIf}
  ${EndIf}
FunctionEnd

; 去掉清单行尾的 CR / LF；写出的清单使用 CRLF
Function TrimLineEnd
  ${Do}
    StrCpy $TailChar $ManifestLine 1 -1
    ${If} $TailChar == "$\r"
    ${OrIf} $TailChar == "$\n"
      StrCpy $ManifestLine $ManifestLine -1
    ${Else}
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd

Function .onInstSuccess
  ${If} $UpdateMode == ""
    ${IfNot} ${Silent}
      MessageBox MB_YESNO|MB_ICONQUESTION "XM-IMS 已安装完成，是否现在启动？" IDNO +2
      Exec '"$INSTDIR\xm-ims.exe"'
    ${EndIf}
  ${Else}
    Exec '"$INSTDIR\xm-ims.exe"'
  ${EndIf}
FunctionEnd

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

; ---------- 图标 ----------
Icon "${STAGE_DIR}\resources\icons\icon.ico"
UninstallIcon "${STAGE_DIR}\resources\icons\icon.ico"

; ---------- 安装 ----------
Section "XM-IMS" SecMain
  Call WaitForPrevious
  ; 手动重装 / 升级没有 /XM_PID，同样不能在程序运行时覆盖程序文件：
  ; 托盘隐藏窗口后进程仍在，覆盖会失败并留下新旧混合的安装
  Call CheckProgramFiles
  ${If} $R5 == "1"
    !insertmacro FailStop "XM-IMS 正在运行或有更新待确认。请在托盘菜单中选择退出后重试。"
  ${EndIf}

  ; 1) 旧平铺布局：先把旧程序原位保留为 legacy，作为该次唯一回退目标
  StrCpy $LegacyMode "0"
  ${IfNot} ${FileExists} "$INSTDIR\versions"
    ${If} ${FileExists} "$INSTDIR\${LAUNCHER}"
    ${AndIf} ${FileExists} "$INSTDIR\resources\app\main.js"
      StrCpy $LegacyMode "1"
      Call WriteMigrateStage
      ; 保留旧清单：legacy 清理与卸载仍按原文件归属处理
      ${If} ${FileExists} "$INSTDIR\${ROOT_MANIFEST}"
        CopyFiles /SILENT "$INSTDIR\${ROOT_MANIFEST}" "$INSTDIR\installed-program-files.legacy.txt"
      ${EndIf}
      ${If} ${FileExists} "$INSTDIR\${LEGACY}"
        Delete "$INSTDIR\${LEGACY}"
      ${EndIf}
      Rename "$INSTDIR\${LAUNCHER}" "$INSTDIR\${LEGACY}"
      ; WER 文件需要 legacy 命名副本，原文件保留在原归属清单中
      ${If} ${FileExists} "$INSTDIR\xm-ims_wer.dll"
        CopyFiles /SILENT "$INSTDIR\xm-ims_wer.dll" "$INSTDIR\xm-ims-legacy_wer.dll"
      ${EndIf}
      ; 旧入口改名后仍保留可启动的 legacy，安装失败也不会留下无入口状态
    ${EndIf}
  ${EndIf}
  ${If} $LegacyMode == "1"
    WriteINIStr "$INSTDIR\data\update\desktop\selection.ini" "selection" "protocol" "1"
    WriteINIStr "$INSTDIR\data\update\desktop\selection.ini" "selection" "phase" "stable"
    WriteINIStr "$INSTDIR\data\update\desktop\selection.ini" "selection" "active" "legacy"
    WriteINIStr "$INSTDIR\data\update\desktop\selection.ini" "selection" "previous" ""
  ${EndIf}

  ; 2) 新版本载荷先写临时目录，成功后再提升为正式槽
  Call WriteMigrateStage
  RMDir /r "$INSTDIR\versions\${SLOT}.installing"
  SetOutPath "$INSTDIR\versions\${SLOT}.installing"
  File /r "${STAGE_DIR}\*.*"
  RMDir /r "$INSTDIR\versions\${SLOT}"
  Rename "$INSTDIR\versions\${SLOT}.installing" "$INSTDIR\versions\${SLOT}"

  ; 3) 固定根组件：启动器与独立恢复支持；同协议已存在时保留原组件
  SetOutPath "$INSTDIR"
  File "${LAUNCHER_FILE}"
  CreateDirectory "$INSTDIR\bootstrap-support"
  ${IfNot} ${FileExists} "$INSTDIR\bootstrap-support\update_trial.py"
    File /oname=bootstrap-support\update_trial.py "${SUPPORT_DIR}\update_trial.py"
    File /oname=bootstrap-support\process_lock.py "${SUPPORT_DIR}\process_lock.py"
  ${EndIf}

  ; 4) 选择资料：首装或旧平铺迁移后指向新槽；已有待应用状态不覆盖
  ${If} $LegacyMode == "1"
  ${OrIfNot} ${FileExists} "$INSTDIR\${SELECTION}"
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "protocol" "1"
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "phase" "stable"
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "active" "${SLOT}"
    ${If} $LegacyMode == "1"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "previous" "legacy"
    ${Else}
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "previous" ""
    ${EndIf}
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_version" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_tag" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "attempt" ""
  ${ElseIf} ${FileExists} "$INSTDIR\${SELECTION}"
    ; 已安装版本之间的手动升级：root 与新槽同时就位后，把选择资料切到新槽
    ; 已有 prepared / pending 等待应用时不覆盖，避免丢掉已准备候选
    ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "phase"
    ${If} $R6 == "stable"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "phase" "stable"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "previous" "${SLOT}"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "active" "${SLOT}"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate" ""
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_version" ""
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_tag" ""
    ${EndIf}
  ${EndIf}

  ; 5) 固定根的受管理文件清单：启动器与恢复支持
  FileOpen $ManifestHandle "$INSTDIR\${ROOT_MANIFEST}" w
  FileWriteUTF16LE $ManifestHandle "${LAUNCHER}$\r$\n"
  FileWriteUTF16LE $ManifestHandle "bootstrap-support\update_trial.py$\r$\n"
  FileWriteUTF16LE $ManifestHandle "bootstrap-support\process_lock.py$\r$\n"
  FileWriteUTF16LE $ManifestHandle "${ROOT_MANIFEST}$\r$\n"
  FileWriteUTF16LE $ManifestHandle "uninstall.exe$\r$\n"
  FileClose $ManifestHandle

  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateDirectory "$SMPROGRAMS\XM-IMS"
  CreateShortcut "$SMPROGRAMS\XM-IMS\XM-IMS.lnk" "$INSTDIR\${LAUNCHER}" "" "$INSTDIR\${LAUNCHER}" 0
  CreateShortcut "$DESKTOP\XM-IMS.lnk" "$INSTDIR\${LAUNCHER}" "" "$INSTDIR\${LAUNCHER}" 0

  WriteRegStr HKCU "Software\XM-IMS" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "XM-IMS"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${LAUNCHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "XM-IMS"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ; 迁移完成：清除阶段标记
  Delete "$INSTDIR\data\update\desktop\migrate.ini"
SectionEnd

; ---------- 卸载 ----------
Section "Uninstall"
  ; 运行中不按进程名杀进程，只提示用户从托盘退出；首次启动事务进行中同样拒绝
  Call un.CheckProgramFiles
  ${If} $R5 == "1"
    !insertmacro FailStop "XM-IMS 正在运行或有更新待确认。请在托盘菜单中选择退出后重试。"
  ${EndIf}
  ; 根组件、各版本槽与 legacy 各自按自己的清单删除登记文件
  Call un.DeleteManifestFiles
  Call un.DeleteVersionFiles
  Call un.DeleteLegacyFiles
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR\bootstrap-support"
  RMDir "$INSTDIR\versions"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\XM-IMS\XM-IMS.lnk"
  RMDir "$SMPROGRAMS\XM-IMS"
  Delete "$DESKTOP\XM-IMS.lnk"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\XM-IMS"
SectionEnd

; 逐个删除 versions 下每个版本槽的受管理文件；data、tools 与未登记文件保留
Function un.DeleteVersionFiles
  FindFirst $R8 $R9 "$INSTDIR\versions\*.*"
  ${Do}
    ${If} $R9 == ""
      ${Break}
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\versions\$R9\${MANIFEST}"
      FileOpen $ManifestHandle "$INSTDIR\versions\$R9\${MANIFEST}" r
      ${If} $ManifestHandle != ""
        ${Do}
          ClearErrors
          FileReadUTF16LE $ManifestHandle $ManifestLine
          ${If} ${Errors}
            ${Break}
          ${EndIf}
          ${If} $ManifestLine == ""
            ${Break}
          ${EndIf}
          Call un.TrimLineEnd
          StrCpy $R4 $ManifestLine 1
          StrCpy $R7 $ManifestLine 2
          ${If} $R4 != "\"
          ${AndIf} $R7 != "../"
          ${AndIf} $R7 != "%1"
          ${AndIf} $ManifestLine != "${MANIFEST}"
            Delete "$INSTDIR\versions\$R9\$ManifestLine"
          ${EndIf}
        ${Loop}
        FileClose $ManifestHandle
      ${EndIf}
      Delete "$INSTDIR\versions\$R9\${MANIFEST}"
    ${EndIf}
    RMDir /r "$INSTDIR\versions\$R9\resources"
    RMDir /r "$INSTDIR\versions\$R9\locales"
    Delete "$INSTDIR\versions\$R9\*.dll"
    Delete "$INSTDIR\versions\$R9\*.exe"
    Delete "$INSTDIR\versions\$R9\*.json"
    RMDir "$INSTDIR\versions\$R9"
    FindNext $R8 $R9
  ${Loop}
  FindClose $R8
FunctionEnd

; 旧平铺布局保留的 legacy 程序：按迁移时保留的清单删除
Function un.DeleteLegacyFiles
  ${IfNot} ${FileExists} "$INSTDIR\installed-program-files.legacy.txt"
    Return
  ${EndIf}
  FileOpen $ManifestHandle "$INSTDIR\installed-program-files.legacy.txt" r
  ${If} $ManifestHandle == ""
    Return
  ${EndIf}
  ${Do}
    ClearErrors
    FileReadUTF16LE $ManifestHandle $ManifestLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    ${If} $ManifestLine == ""
      ${Break}
    ${EndIf}
    Call un.TrimLineEnd
    StrCpy $R4 $ManifestLine 1
    StrCpy $R7 $ManifestLine 2
    ${If} $R4 != "\"
    ${AndIf} $R7 != "../"
    ${AndIf} $R7 != "%1"
      Delete "$INSTDIR\$ManifestLine"
    ${EndIf}
  ${Loop}
  FileClose $ManifestHandle
  Delete "$INSTDIR\installed-program-files.legacy.txt"
  Delete "$INSTDIR\xm-ims_wer.dll"
  Delete "$INSTDIR\xm-ims-legacy_wer.dll"
  Delete "$INSTDIR\${LEGACY}"
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
FunctionEnd

; 程序是否被占用：以独占方式打开可执行文件判断，不按进程名结束任何进程
Function un.CheckProgramFiles
  StrCpy $R5 "0"
  ${If} ${FileExists} "$INSTDIR\${LAUNCHER}"
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\${LAUNCHER}", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
    ${If} $R3 == -1
      StrCpy $R5 "1"
    ${ElseIf} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
  ${If} $R5 == "0"
  ${AndIf} ${FileExists} "$INSTDIR\${LEGACY}"
    System::Call 'kernel32::CreateFileW(w "$INSTDIR\${LEGACY}", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
    ${If} $R3 == -1
      StrCpy $R5 "1"
    ${ElseIf} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
  ${If} $R5 == "0"
    ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "phase"
    ${If} $R6 == "pending"
    ${OrIf} $R6 == "starting"
    ${OrIf} $R6 == "committing"
      StrCpy $R5 "1"
    ${EndIf}
  ${EndIf}
FunctionEnd

; 程序是否被占用：以独占方式打开可执行文件判断，不按进程名结束任何进程
Function un.CheckRunning
  StrCpy $R5 "0"
  ${IfNot} ${FileExists} "$INSTDIR\xm-ims.exe"
    Return
  ${EndIf}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\xm-ims.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
  ${If} $R3 == -1
    StrCpy $R5 "1"
  ${Else}
    ${If} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
FunctionEnd

Function un.TrimLineEnd
  ${Do}
    StrCpy $TailChar $ManifestLine 1 -1
    ${If} $TailChar == "$\r"
    ${OrIf} $TailChar == "$\n"
      StrCpy $ManifestLine $ManifestLine -1
    ${Else}
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd

Function un.DeleteListedFile
  ${If} $ManifestLine == ""
    Return
  ${EndIf}
  StrCpy $R4 $ManifestLine 1
  StrCpy $R7 $ManifestLine 2
  ${If} $ManifestLine != "${MANIFEST}"
  ${AndIf} $ManifestLine != "uninstall.exe"
  ${AndIf} $R4 != "\"
  ${AndIf} $R7 != "../"
  ${AndIf} $R7 != "%1"
    ClearErrors
    Delete "$INSTDIR\$ManifestLine"
    ${If} ${Errors}
      StrCpy $ManifestFailed "1"
    ${EndIf}
  ${EndIf}
FunctionEnd

; 卸载同样只删除清单登记的程序文件；data、tools 与用户文件保留
Function un.DeleteManifestFiles
  StrCpy $ManifestFailed "0"
  ${IfNot} ${FileExists} "$INSTDIR\${MANIFEST}"
    Return
  ${EndIf}
  FileOpen $ManifestHandle "$INSTDIR\${MANIFEST}" r
  ${If} $ManifestHandle == ""
    Return
  ${EndIf}
  ${Do}
    ClearErrors
    FileReadUTF16LE $ManifestHandle $ManifestLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    ${If} $ManifestLine == ""
      ${Break}
    ${EndIf}
    Call un.TrimLineEnd
    Call un.DeleteListedFile
  ${Loop}
  FileClose $ManifestHandle
  ${If} $ManifestFailed == "0"
    Delete "$INSTDIR\${MANIFEST}"
  ${EndIf}
FunctionEnd
