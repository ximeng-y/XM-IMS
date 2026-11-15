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
;   - 中断更新先确认退出、备份 data 到 repair-backups，再恢复旧选择后继续安装

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
Var CheckPath
Var RepairPython
Var RepairExit
Var RepairOutput
Var RepairMessage
Var RepairBackup
Var VersionStaging
Var VersionTarget

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

; 检查指定程序是否仍被使用；不把中断更新的阶段标记当作运行进程。
Function CheckOneProgram
  ${If} ${FileExists} "$CheckPath"
    System::Call 'kernel32::CreateFileW(w "$CheckPath", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
    ${If} $R3 == -1
      StrCpy $R5 "1"
    ${ElseIf} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
    ${EndIf}
  ${EndIf}
FunctionEnd

; 根入口、legacy、当前槽与候选槽都要检查，不能遗漏已退出启动器的版本进程。
Function CheckProgramFiles
  StrCpy $R5 "0"
  StrCpy $CheckPath "$INSTDIR\${LAUNCHER}"
  Call CheckOneProgram
  StrCpy $CheckPath "$INSTDIR\${LEGACY}"
  Call CheckOneProgram
  ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "active"
  ${If} $R6 != ""
  ${AndIf} $R6 != "legacy"
    StrCpy $CheckPath "$INSTDIR\versions\$R6\xm-ims.exe"
    Call CheckOneProgram
  ${EndIf}
  ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "candidate"
  ${If} $R6 != ""
  ${AndIf} $R6 != "legacy"
    StrCpy $CheckPath "$INSTDIR\versions\$R6\xm-ims.exe"
    Call CheckOneProgram
  ${EndIf}
FunctionEnd

; 目标目录确认后、覆盖程序前处理该目录的中断更新；始终运行包内最新恢复源码。
Function RepairInterruptedUpdate
  ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "phase"
  ${If} $R6 != "pending"
  ${AndIf} $R6 != "starting"
  ${AndIf} $R6 != "committing"
    Return
  ${EndIf}
  ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "active"
  ${If} $R6 == "legacy"
    StrCpy $RepairPython "$INSTDIR\resources\python\python.exe"
  ${Else}
    StrCpy $RepairPython "$INSTDIR\versions\$R6\resources\python\python.exe"
  ${EndIf}
  ${IfNot} ${FileExists} "$RepairPython"
    !insertmacro FailStop "找不到用于恢复的旧版程序，已停止安装。请保留原目录中的数据。"
  ${EndIf}

  InitPluginsDir
  SetOutPath "$PLUGINSDIR\repair"
  File /oname=installer_repair.py "${SUPPORT_DIR}\installer_repair.py"
  File /oname=update_trial.py "${SUPPORT_DIR}\update_trial.py"
  File /oname=process_lock.py "${SUPPORT_DIR}\process_lock.py"
  nsExec::ExecToStack '"$RepairPython" -B "$PLUGINSDIR\repair\installer_repair.py" check --root "$INSTDIR" --result "$PLUGINSDIR\repair-result.ini"'
  Pop $RepairExit
  Pop $RepairOutput
  ${If} $RepairExit == 0
    Return
  ${EndIf}
  ${If} $RepairExit != 10
    ReadINIStr $RepairMessage "$PLUGINSDIR\repair-result.ini" "repair" "message"
    ${If} $RepairMessage == ""
      StrCpy $RepairMessage "无法检查上次更新，已停止安装。请保留原目录中的数据。"
    ${EndIf}
    !insertmacro FailStop "$RepairMessage"
  ${EndIf}

  ; 补救可能恢复数据指针，静默安装不能代替用户确认。
  ${If} ${Silent}
    !insertmacro FailStop "上次更新未完成，请手动运行安装包，确认恢复后再安装。"
  ${EndIf}
  MessageBox MB_YESNO|MB_ICONEXCLAMATION|MB_DEFBUTTON2 "检测到上次更新未完成。继续安装将先创建保护备份，再恢复更新前状态，之后安装新版。$\r$\n$\r$\n备份目录：$INSTDIR\repair-backups$\r$\n$\r$\n是否继续？" IDYES repair_confirmed
  SetErrorLevel 2
  Quit
  repair_confirmed:
  Delete "$PLUGINSDIR\repair-result.ini"
  nsExec::ExecToStack '"$RepairPython" -B "$PLUGINSDIR\repair\installer_repair.py" repair --root "$INSTDIR" --result "$PLUGINSDIR\repair-result.ini"'
  Pop $RepairExit
  Pop $RepairOutput
  ReadINIStr $RepairBackup "$PLUGINSDIR\repair-result.ini" "repair" "backup"
  ${If} $RepairBackup != ""
    DetailPrint "保护备份保留在：$RepairBackup"
  ${EndIf}
  ${If} $RepairExit != 0
    ReadINIStr $RepairMessage "$PLUGINSDIR\repair-result.ini" "repair" "message"
    ${If} $RepairMessage == ""
      StrCpy $RepairMessage "上次更新未能恢复，已停止安装。请保留原目录中的数据。"
    ${EndIf}
    ${If} $RepairBackup != ""
      StrCpy $RepairMessage "$RepairMessage$\r$\n$\r$\n备份目录：$RepairBackup"
    ${EndIf}
    !insertmacro FailStop "$RepairMessage"
  ${EndIf}
  DetailPrint "上次更新已恢复，继续安装新版。"
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
  GetFullPathName $INSTDIR "$INSTDIR"
  Call WaitForPrevious
  ; 手动重装 / 升级没有 /XM_PID，同样不能在程序运行时覆盖程序文件：
  ; 托盘隐藏窗口后进程仍在，覆盖会失败并留下新旧混合的安装
  Call CheckProgramFiles
  ${If} $R5 == "1"
    !insertmacro FailStop "XM-IMS 正在运行。请退出后重新安装。"
  ${EndIf}
  Call RepairInterruptedUpdate

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
      ClearErrors
      Rename "$INSTDIR\${LAUNCHER}" "$INSTDIR\${LEGACY}"
      ${If} ${Errors}
        !insertmacro FailStop "旧版程序未能保留，已停止安装。请保留原目录后重试。"
      ${EndIf}
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
  GetFullPathName $VersionStaging "$INSTDIR\versions\${SLOT}.installing"
  GetFullPathName $VersionTarget "$INSTDIR\versions\${SLOT}"
  ; 删除目标先核对绝对目录边界，只处理本包对应的版本与暂存槽。
  GetFullPathName $R7 "$INSTDIR\versions"
  StrLen $R8 "$R7\"
  StrCpy $R9 $VersionStaging $R8
  ${If} $R9 != "$R7\"
    !insertmacro FailStop "安装暂存目录不正确，已停止安装。"
  ${EndIf}
  StrCpy $R9 $VersionTarget $R8
  ${If} $R9 != "$R7\"
    !insertmacro FailStop "安装版本目录不正确，已停止安装。"
  ${EndIf}
  ${If} ${FileExists} "$VersionStaging"
    ClearErrors
    RMDir /r "$VersionStaging"
    ${If} ${Errors}
      !insertmacro FailStop "暂存程序文件无法清理，已停止安装。请退出程序后重试。"
    ${EndIf}
  ${EndIf}
  ClearErrors
  SetOutPath "$VersionStaging"
  File /r "${STAGE_DIR}\*.*"
  ${If} ${Errors}
    !insertmacro FailStop "新版程序文件没有完整写入，已停止安装。"
  ${EndIf}
  ; File /r 会把工作目录恢复到暂存槽，必须先切出，再删除或改名目录。
  ClearErrors
  SetOutPath "$INSTDIR"
  ${If} ${Errors}
    !insertmacro FailStop "无法进入安装目录，已停止安装。"
  ${EndIf}
  ${If} ${FileExists} "$VersionTarget"
    ClearErrors
    RMDir /r "$VersionTarget"
    ${If} ${Errors}
      !insertmacro FailStop "旧程序文件无法替换，已停止安装。请退出程序后重试。"
    ${EndIf}
  ${EndIf}
  ClearErrors
  Rename "$VersionStaging" "$VersionTarget"
  ${If} ${Errors}
    !insertmacro FailStop "新版程序目录未能准备完成，已停止安装。"
  ${EndIf}
  ${IfNot} ${FileExists} "$VersionTarget\xm-ims.exe"
  ${OrIfNot} ${FileExists} "$VersionTarget\resources\python\python.exe"
  ${OrIfNot} ${FileExists} "$VersionTarget\resources\app\main.js"
    !insertmacro FailStop "新版程序文件不完整，已停止安装。"
  ${EndIf}

  ; 3) 手动安装刷新固定根组件，已有恢复脚本也必须获得本包的修复。
  ;    启动器源码文件名与入口名不同，必须用 /oname 落到固定入口名
  ClearErrors
  SetOutPath "$INSTDIR"
  File /oname=${LAUNCHER} "${LAUNCHER_FILE}"
  CreateDirectory "$INSTDIR\bootstrap-support"
  File /oname=bootstrap-support\update_trial.py "${SUPPORT_DIR}\update_trial.py"
  File /oname=bootstrap-support\process_lock.py "${SUPPORT_DIR}\process_lock.py"
  File /oname=bootstrap-support\installer_repair.py "${SUPPORT_DIR}\installer_repair.py"
  ${If} ${Errors}
    !insertmacro FailStop "恢复组件未能安装完整，已停止安装。"
  ${EndIf}

  ; 4) 选择资料：新槽一律先作为待确认候选，由启动器首次启动监督后写入 stable
  ;    - 旧平铺迁移：回退目标为刚保留的 legacy，不沿用迁移前的选择
  ;    - 已安装版本的手动升级：旧 active 与回退链保持不变，新槽只进候选
  ;    - 全新首装：没有旧数据可回退，直接以新槽为稳定版本
  ;    已有 prepared / pending 等待应用时不覆盖，避免丢掉已准备候选
  ${If} ${FileExists} "$INSTDIR\${SELECTION}"
    ReadINIStr $R6 "$INSTDIR\${SELECTION}" "selection" "phase"
    ${If} $R6 == "stable"
      ${If} $LegacyMode == "1"
        WriteINIStr "$INSTDIR\${SELECTION}" "selection" "active" "legacy"
        WriteINIStr "$INSTDIR\${SELECTION}" "selection" "previous" ""
      ${EndIf}
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "phase" "pending"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate" "${SLOT}"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_version" "${APP_VERSION}"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_tag" "v${APP_VERSION}"
      WriteINIStr "$INSTDIR\${SELECTION}" "selection" "attempt" ""
    ${EndIf}
  ${Else}
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "protocol" "1"
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "phase" "stable"
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "active" "${SLOT}"
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "previous" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_version" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "candidate_tag" ""
    WriteINIStr "$INSTDIR\${SELECTION}" "selection" "attempt" ""
  ${EndIf}

  ; 5) 固定根的受管理文件清单：启动器与恢复支持
  FileOpen $ManifestHandle "$INSTDIR\${ROOT_MANIFEST}" w
  FileWriteUTF16LE $ManifestHandle "${LAUNCHER}$\r$\n"
  FileWriteUTF16LE $ManifestHandle "bootstrap-support\update_trial.py$\r$\n"
  FileWriteUTF16LE $ManifestHandle "bootstrap-support\process_lock.py$\r$\n"
  FileWriteUTF16LE $ManifestHandle "bootstrap-support\installer_repair.py$\r$\n"
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

; 逐个删除 versions 下每个版本槽的受管理文件；data、tools 与未登记文件保留。
; 只处理校验过的真实版本槽，严格按清单删文件；删除不完整时保留清单，
; 目录只在已空时移除，绝不递归或通配删除未登记文件。
Function un.DeleteVersionFiles
  FindFirst $R8 $R9 "$INSTDIR\versions\*.*"
  ${Do}
    ${If} $R9 == ""
      ${Break}
    ${EndIf}
    StrCpy $1 "$R9"
    Call un.ValidSlotName
    ${If} $2 == "1"
      ${If} ${FileExists} "$INSTDIR\versions\$R9\${MANIFEST}"
        StrCpy $ManifestFailed "0"
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
            Call un.DeleteSlotListedFile
          ${Loop}
          FileClose $ManifestHandle
        ${Else}
          StrCpy $ManifestFailed "1"
        ${EndIf}
        ${If} $ManifestFailed == "0"
          Delete "$INSTDIR\versions\$R9\${MANIFEST}"
          StrCpy $0 "$INSTDIR\versions\$R9"
          Call un.PruneEmptyDirs
        ${EndIf}
      ${EndIf}
    ${EndIf}
    FindNext $R8 $R9
  ${Loop}
  FindClose $R8
FunctionEnd

; 删除版本槽清单登记的一行；清单自身与越出行跳过
Function un.DeleteSlotListedFile
  ${If} $ManifestLine == ""
  ${OrIf} $ManifestLine == "${MANIFEST}"
    Return
  ${EndIf}
  StrCpy $R4 $ManifestLine 1
  StrCpy $R7 $ManifestLine 2
  ${If} $R4 != "\"
  ${AndIf} $R4 != "."
  ${AndIf} $R7 != ".."
  ${AndIf} $R7 != "%1"
    ${If} ${FileExists} "$INSTDIR\versions\$R9\$ManifestLine"
      ClearErrors
      Delete "$INSTDIR\versions\$R9\$ManifestLine"
      ${If} ${Errors}
        StrCpy $ManifestFailed "1"
      ${EndIf}
    ${EndIf}
  ${EndIf}
FunctionEnd

; 旧平铺布局保留的 legacy 程序：按迁移时保留的清单删除。
; 清单登记的文件删除不完整时保留清单，目录只在已空时移除。
Function un.DeleteLegacyFiles
  ${If} ${FileExists} "$INSTDIR\installed-program-files.legacy.txt"
    StrCpy $ManifestFailed "0"
    FileOpen $ManifestHandle "$INSTDIR\installed-program-files.legacy.txt" r
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
        Call un.DeleteLegacyListedFile
      ${Loop}
      FileClose $ManifestHandle
    ${Else}
      StrCpy $ManifestFailed "1"
    ${EndIf}
    ${If} $ManifestFailed == "0"
      Delete "$INSTDIR\installed-program-files.legacy.txt"
    ${EndIf}
  ${EndIf}
  ; 迁移时复制的 WER 命名副本与改名后的入口不在旧清单登记范围，单独删除
  Delete "$INSTDIR\xm-ims_wer.dll"
  Delete "$INSTDIR\xm-ims-legacy_wer.dll"
  Delete "$INSTDIR\${LEGACY}"
  StrCpy $0 "$INSTDIR\resources"
  Call un.PruneEmptyDirs
  StrCpy $0 "$INSTDIR\locales"
  Call un.PruneEmptyDirs
FunctionEnd

; 删除旧平铺清单登记的一行；清单自身与固定根清单、越出行跳过
Function un.DeleteLegacyListedFile
  ${If} $ManifestLine == ""
  ${OrIf} $ManifestLine == "${MANIFEST}"
  ${OrIf} $ManifestLine == "uninstall.exe"
  ${OrIf} $ManifestLine == "installed-program-files.legacy.txt"
    Return
  ${EndIf}
  StrCpy $R4 $ManifestLine 1
  StrCpy $R7 $ManifestLine 2
  ${If} $R4 != "\"
  ${AndIf} $R4 != "."
  ${AndIf} $R7 != ".."
  ${AndIf} $R7 != "%1"
    ${If} ${FileExists} "$INSTDIR\$ManifestLine"
      ClearErrors
      Delete "$INSTDIR\$ManifestLine"
      ${If} ${Errors}
        StrCpy $ManifestFailed "1"
      ${EndIf}
    ${EndIf}
  ${EndIf}
FunctionEnd

; 递归移除空目录：只删除已经空的目录，不删除任何文件。
; 输入 $0 = 目录；目录中已空的子目录与目录本身都会被移除，未空的原样保留。
Function un.PruneEmptyDirs
  Push $0
  Push $1
  Push $2
  Push $3
  FindFirst $3 $1 "$0\*.*"
  ${Do}
    ${If} $1 == ""
      ${Break}
    ${EndIf}
    ${If} $1 != "."
    ${AndIf} $1 != ".."
      StrCpy $2 "$0\$1"
      Push $0
      StrCpy $0 "$2"
      Call un.PruneEmptyDirs
      Pop $0
    ${EndIf}
    FindNext $3 $1
  ${Loop}
  FindClose $3
  RMDir "$0"
  Pop $3
  Pop $2
  Pop $1
  Pop $0
FunctionEnd

Function un.IsHex
  ; 输入：$R1 = 单字符；输出：$R2 = "1" 表示 0-9 或小写 a-f
  StrCpy $R2 "0"
  ${If} $R1 == "0"
  ${OrIf} $R1 == "1"
  ${OrIf} $R1 == "2"
  ${OrIf} $R1 == "3"
  ${OrIf} $R1 == "4"
  ${OrIf} $R1 == "5"
  ${OrIf} $R1 == "6"
  ${OrIf} $R1 == "7"
  ${OrIf} $R1 == "8"
  ${OrIf} $R1 == "9"
  ${OrIf} $R1 == "a"
  ${OrIf} $R1 == "b"
  ${OrIf} $R1 == "c"
  ${OrIf} $R1 == "d"
  ${OrIf} $R1 == "e"
  ${OrIf} $R1 == "f"
    StrCpy $R2 "1"
  ${EndIf}
FunctionEnd

Function un.IsDigit
  ; 输入：$R1 = 单字符；输出：$R2 = "1" 表示 0-9
  StrCpy $R2 "0"
  ${If} $R1 == "0"
  ${OrIf} $R1 == "1"
  ${OrIf} $R1 == "2"
  ${OrIf} $R1 == "3"
  ${OrIf} $R1 == "4"
  ${OrIf} $R1 == "5"
  ${OrIf} $R1 == "6"
  ${OrIf} $R1 == "7"
  ${OrIf} $R1 == "8"
  ${OrIf} $R1 == "9"
    StrCpy $R2 "1"
  ${EndIf}
FunctionEnd

; 输入：$1 = 槽名；输出：$2 = "1" 表示受管理的版本槽名。
; 只接受 <X.Y.Z>-<32 位小写十六进制构建标识>；字符集不含路径分隔符，
; 显式拒绝 . 与 ..，因此不会跳出 versions 目录。
Function un.ValidSlotName
  StrCpy $2 "0"
  ${If} $1 == ""
    Return
  ${EndIf}
  ${If} $1 == "."
  ${OrIf} $1 == ".."
    Return
  ${EndIf}
  StrLen $R0 "$1"
  ${If} $R0 < 38
    Return
  ${EndIf}
  ; 末尾 33 个字符必须是 "-" 加 32 位小写十六进制
  StrCpy $R3 "$1" 33 -33
  StrCpy $R4 "$R3" 1
  ${If} $R4 != "-"
    Return
  ${EndIf}
  StrCpy $R3 "$R3" "" 1
  StrCpy $R5 0
  ${Do}
    StrCpy $R1 "$R3" 1 $R5
    ${If} $R1 == ""
      ${Break}
    ${EndIf}
    Call un.IsHex
    ${If} $R2 != "1"
      Return
    ${EndIf}
    IntOp $R5 $R5 + 1
  ${Loop}
  ${If} $R5 != 32
    Return
  ${EndIf}
  ; 版本段只能是数字与点，且恰好两个点
  StrCpy $R3 "$1" -33
  ${If} $R3 == ""
    Return
  ${EndIf}
  StrCpy $R5 0
  StrCpy $R6 0
  ${Do}
    StrCpy $R1 "$R3" 1 $R5
    ${If} $R1 == ""
      ${Break}
    ${EndIf}
    ${If} $R1 == "."
      IntOp $R6 $R6 + 1
    ${Else}
      Call un.IsDigit
      ${If} $R2 != "1"
        Return
      ${EndIf}
    ${EndIf}
    IntOp $R5 $R5 + 1
  ${Loop}
  ${If} $R6 != 2
    Return
  ${EndIf}
  StrCpy $2 "1"
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
