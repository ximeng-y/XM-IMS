; XM-IMS Windows 用户级安装包
; 构建参数（由 desktop/build.py 传入）：
;   APP_VERSION  应用版本（来自 app/version.py）
;   STAGE_DIR    暂存目录（含 xm-ims.exe、resources\、installed-program-files.txt）
;   OUT_FILE     安装包输出路径
;
; 约定：
;   - 只装到当前用户可写目录，默认 %LOCALAPPDATA%\Programs\XM-IMS
;   - 不装 Python / Git，不改 PATH，不设开机启动；Git 由应用内引导准备
;   - data、tools、.XMTEMP 从不作为安装输入，也从不删除
;   - 升级卸载只处理清单登记的程序文件，不做递归删除

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
!include "StrFunc.nsh"
${StrLoc}
${UnStrLoc}
!include "WinVer.nsh"
!include "x64.nsh"

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\XM-IMS"
!define MANIFEST "installed-program-files.txt"

Var UpdateMode
Var UpdatePid
Var UpdateDir
Var ManifestHandle
Var ManifestLine

; ---------- 命令行参数 ----------
; /S 静默安装（更新时使用）；/D=<目录> 为 NSIS 内置目录参数
; /XM_PID=<pid> 等待该进程退出后再覆盖程序文件
; /XM_DIR=<目录> 本次更新的安装位置（不静默改到默认目录）
!macro ParseArgs
  ${GetParameters} $R9
  ${GetOptions} $R9 "/XM_PID=" $UpdatePid
  ${GetOptions} $R9 "/XM_DIR=" $UpdateDir
  ${If} $UpdatePid != ""
    StrCpy $UpdateMode "1"
    StrCpy $INSTDIR $UpdateDir
  ${EndIf}
!macroend
!define ParseArgs "!insertmacro ParseArgs"

Function .onInit
  ${ParseArgs}
  ${IfNot} ${IsNativeAMD64}
    MessageBox MB_ICONSTOP|MB_OK "XM-IMS 只支持 64 位 Windows。"
    Abort
  ${EndIf}
FunctionEnd

; 等待传入的进程真正结束再覆盖程序文件；超时停止更新，不按进程名杀程序
Function WaitForPrevious
  ${If} $UpdatePid == ""
    Return
  ${EndIf}
  ; SYNCHRONIZE 权限；进程已不存在时返回 0，直接继续
  System::Call 'kernel32::OpenProcess(i 0x00100000, i 0, i $UpdatePid) p .r1'
  ${If} $r1 == 0
    Return
  ${EndIf}
  StrCpy $R8 0
  ${Do}
    System::Call 'kernel32::WaitForSingleObject(p r1, i 1000) i .r2'
    ${If} $r2 == 0
      ${Break}
    ${EndIf}
    IntOp $R8 $R8 + 1
    ${If} $R8 >= 120
      System::Call 'kernel32::CloseHandle(p r1)'
      MessageBox MB_ICONSTOP|MB_OK "XM-IMS 仍在运行，已停止更新。请在托盘菜单中选择退出后重试。"
      Abort
    ${EndIf}
  ${Loop}
  System::Call 'kernel32::CloseHandle(p r1)'
FunctionEnd

; 程序是否被占用：以独占方式打开可执行文件判断，不按进程名结束任何进程
Function CheckRunning
  StrCpy $R5 "0"
  ${IfNot} ${FileExists} "$INSTDIR\xm-ims.exe"
    Return
  ${EndIf}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\xm-ims.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .r3'
  ; INVALID_HANDLE_VALUE(-1) 表示文件仍被占用
  ${If} $r3 == -1
    StrCpy $R5 "1"
  ${Else}
    ${If} $r3 != 0
      System::Call 'kernel32::CloseHandle(p r3)'
    ${EndIf}
  ${EndIf}
FunctionEnd

; 读取清单并删除登记的程序文件；只接受安装根下的相对路径
; 先读完并关闭句柄，再逐项删除，避免边读边删导致后续行丢失
Function DeleteManifestFiles
  ${IfNot} ${FileExists} "$INSTDIR\${MANIFEST}"
    Return
  ${EndIf}
  FileOpen $ManifestHandle "$INSTDIR\${MANIFEST}" r
  ${If} $ManifestHandle == ""
    Return
  ${EndIf}
  Call ReadManifestLines
  FileClose $ManifestHandle
  Call DeleteListedFiles
  Delete "$INSTDIR\${MANIFEST}"
FunctionEnd

; 把清单逐行读进 $R6（以 CRLF 分隔），跳过清单自身与卸载程序
Function ReadManifestLines
  StrCpy $R6 ""
  ${Do}
    FileReadUTF16LE $ManifestHandle $ManifestLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    ${If} $ManifestLine == ""
      ${Break}
    ${EndIf}
    StrCpy $R6 "$R6$ManifestLine$\r$\n"
  ${Loop}
FunctionEnd

; 逐行取出 $R6 中的相对路径并删除
Function DeleteListedFiles
  ${Do}
    ${If} $R6 == ""
      ${Break}
    ${EndIf}
    ${StrLoc} $R7 $R6 "$\r$\n" ">"
    ${If} $R7 == ""
      StrCpy $ManifestLine $R6
      StrCpy $R6 ""
    ${Else}
      StrCpy $ManifestLine $R6 $R7
      IntOp $R7 $R7 + 2
      StrCpy $R6 $R6 "" $R7
    ${EndIf}
    ; 只接受安装根下的相对路径：不含起始反斜杠、../ 上跳或盘符
    StrCpy $R4 $ManifestLine 1
    StrCpy $R3 $ManifestLine 2
    ${If} $ManifestLine != "${MANIFEST}"
    ${AndIf} $ManifestLine != "uninstall.exe"
    ${AndIf} $R4 != "\"
    ${AndIf} $R3 != "../"
    ${AndIf} $R3 != "%1"
      Delete "$INSTDIR\$ManifestLine"
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
  ; 升级：先按上一份清单清掉旧程序文件，避免旧模块留在资源目录
  ${If} ${FileExists} "$INSTDIR\${MANIFEST}"
    Call DeleteManifestFiles
  ${EndIf}

  SetOutPath "$INSTDIR"
  File /r "${STAGE_DIR}\*.*"

  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateDirectory "$SMPROGRAMS\XM-IMS"
  CreateShortcut "$SMPROGRAMS\XM-IMS\XM-IMS.lnk" "$INSTDIR\xm-ims.exe" "" "$INSTDIR\xm-ims.exe" 0
  CreateShortcut "$DESKTOP\XM-IMS.lnk" "$INSTDIR\xm-ims.exe" "" "$INSTDIR\xm-ims.exe" 0

  WriteRegStr HKCU "Software\XM-IMS" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "XM-IMS"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\xm-ims.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "XM-IMS"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

; ---------- 卸载 ----------
Section "Uninstall"
  ; 运行中不按进程名杀进程，只提示用户从托盘退出
  Call un.CheckRunning
  ${If} $R5 == "1"
    MessageBox MB_ICONSTOP|MB_OK "XM-IMS 正在运行。请在托盘菜单中选择退出后重试。"
    Abort
  ${EndIf}
  Call un.DeleteManifestFiles
  Delete "$INSTDIR\uninstall.exe"
  ; 只删除非递归空目录：data、tools 与用户文件保留
  RMDir "$INSTDIR\resources"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\XM-IMS\XM-IMS.lnk"
  RMDir "$SMPROGRAMS\XM-IMS"
  Delete "$DESKTOP\XM-IMS.lnk"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\XM-IMS"
SectionEnd

; 程序是否被占用：以独占方式打开可执行文件判断，不按进程名结束任何进程
Function un.CheckRunning
  StrCpy $R5 "0"
  ${IfNot} ${FileExists} "$INSTDIR\xm-ims.exe"
    Return
  ${EndIf}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\xm-ims.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .r3'
  ${If} $r3 == -1
    StrCpy $R5 "1"
  ${Else}
    ${If} $r3 != 0
      System::Call 'kernel32::CloseHandle(p r3)'
    ${EndIf}
  ${EndIf}
FunctionEnd

; 卸载同样只删除清单登记的程序文件；data、tools 与用户文件保留
Function un.DeleteManifestFiles
  ${IfNot} ${FileExists} "$INSTDIR\${MANIFEST}"
    Return
  ${EndIf}
  FileOpen $ManifestHandle "$INSTDIR\${MANIFEST}" r
  ${If} $ManifestHandle == ""
    Return
  ${EndIf}
  Call un.ReadManifestLines
  FileClose $ManifestHandle
  Call un.DeleteListedFiles
  Delete "$INSTDIR\${MANIFEST}"
FunctionEnd

Function un.ReadManifestLines
  StrCpy $R6 ""
  ${Do}
    FileReadUTF16LE $ManifestHandle $ManifestLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    ${If} $ManifestLine == ""
      ${Break}
    ${EndIf}
    StrCpy $R6 "$R6$ManifestLine$\r$\n"
  ${Loop}
FunctionEnd

Function un.DeleteListedFiles
  ${Do}
    ${If} $R6 == ""
      ${Break}
    ${EndIf}
    ${UnStrLoc} $R7 $R6 "$\r$\n" ">"
    ${If} $R7 == ""
      StrCpy $ManifestLine $R6
      StrCpy $R6 ""
    ${Else}
      StrCpy $ManifestLine $R6 $R7
      IntOp $R7 $R7 + 2
      StrCpy $R6 $R6 "" $R7
    ${EndIf}
    ; 只接受安装根下的相对路径：不含起始反斜杠、../ 上跳或盘符
    StrCpy $R4 $ManifestLine 1
    StrCpy $R3 $ManifestLine 2
    ${If} $ManifestLine != "${MANIFEST}"
    ${AndIf} $ManifestLine != "uninstall.exe"
    ${AndIf} $R4 != "\"
    ${AndIf} $R3 != "../"
    ${AndIf} $R3 != "%1"
      Delete "$INSTDIR\$ManifestLine"
    ${EndIf}
  ${Loop}
FunctionEnd
