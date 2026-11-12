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
!include "WinVer.nsh"
!include "x64.nsh"

!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\XM-IMS"
!define MANIFEST "installed-program-files.txt"

Var UpdateMode
Var UpdatePid
Var UpdateDir
Var ManifestHandle
Var ManifestLine
Var TailChar

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
    MessageBox MB_ICONSTOP|MB_OK "XM-IMS 只支持 64 位 Windows。"
    Abort
  ${EndIf}
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
      MessageBox MB_ICONSTOP|MB_OK "XM-IMS 仍在运行，已停止更新。请在托盘菜单中选择退出后重试。"
      Abort
    ${EndIf}
  ${Loop}
  System::Call 'kernel32::CloseHandle(p R1)'
FunctionEnd

; 程序是否被占用：以独占方式打开可执行文件判断，不按进程名结束任何进程
Function CheckRunning
  StrCpy $R5 "0"
  ${IfNot} ${FileExists} "$INSTDIR\xm-ims.exe"
    Return
  ${EndIf}
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\xm-ims.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .R3'
  ; INVALID_HANDLE_VALUE(-1) 表示文件仍被占用
  ${If} $R3 == -1
    StrCpy $R5 "1"
  ${Else}
    ${If} $R3 != 0
      System::Call 'kernel32::CloseHandle(p R3)'
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

; 删除清单里的一行；只接受安装根下的相对路径
Function DeleteListedFile
  ${If} $ManifestLine == ""
    Return
  ${EndIf}
  ; 不含起始反斜杠、../ 上跳或盘符；清单自身与卸载程序单独处理
  StrCpy $R4 $ManifestLine 1
  StrCpy $R7 $ManifestLine 2
  ${If} $ManifestLine != "${MANIFEST}"
  ${AndIf} $ManifestLine != "uninstall.exe"
  ${AndIf} $R4 != "\"
  ${AndIf} $R7 != "../"
  ${AndIf} $R7 != "%1"
    Delete "$INSTDIR\$ManifestLine"
  ${EndIf}
FunctionEnd

; 逐行读取并删除登记的程序文件；清单自身不在登记列表内，边读边删不影响后续行
Function DeleteManifestFiles
  ${IfNot} ${FileExists} "$INSTDIR\${MANIFEST}"
    Return
  ${EndIf}
  FileOpen $ManifestHandle "$INSTDIR\${MANIFEST}" r
  ${If} $ManifestHandle == ""
    Return
  ${EndIf}
  ${Do}
    FileReadUTF16LE $ManifestHandle $ManifestLine
    ${If} ${Errors}
      ${Break}
    ${EndIf}
    ${If} $ManifestLine == ""
      ${Break}
    ${EndIf}
    Call TrimLineEnd
    Call DeleteListedFile
  ${Loop}
  FileClose $ManifestHandle
  Delete "$INSTDIR\${MANIFEST}"
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
  ; 只有这两处完全属于程序文件，可以整体清理；data、tools 与用户文件一律保留
  RMDir /r "$INSTDIR\resources"
  RMDir /r "$INSTDIR\locales"
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
    Delete "$INSTDIR\$ManifestLine"
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
  ${Do}
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
  Delete "$INSTDIR\${MANIFEST}"
FunctionEnd
