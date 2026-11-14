; XM-IMS 固定根启动器：选择版本、首次启动监督、程序交接与失败恢复。
;
; 构建参数（由 desktop/build.py 传入）：
;   APP_VERSION  应用版本
;   LAUNCHER_OUT 输出路径
;   ICON_FILE    图标
;
; 约定：
;   - 固定在安装根 xm-ims.exe；普通启动只按选择资料启动当前版本后退出
;   - pending 时启动候选并做一次首次启动监督：等待数据与首屏就绪，提交数据指针，
;     持久确认程序选择，再放行候选进入正常同步启动
;   - 失败或中断时终止本次 Job、确认候选已清退，再由旧版 Python 运行
;     bootstrap-support\update_trial.py 恢复原数据指针，最后启动上一个稳定版本
;   - 日常运行不常驻：确认完成即退出

Unicode true
SetCompressor /SOLID lzma
RequestExecutionLevel user
ManifestDPIAware true
ManifestSupportedOS all
Name "XM-IMS"
OutFile "${LAUNCHER_OUT}"
InstallDir "$EXEDIR"
Icon "${ICON_FILE}"
ShowInstDetails nevershow
BrandingText "XM-IMS Launcher"

!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!define SELECTION "data\update\desktop\selection.ini"
!define ATTEMPTS "data\update\desktop\attempts"
!define RECOVERY "bootstrap-support\update_trial.py"
!define STAGE_TIMEOUT_TICKS 360
!define POLL_MS 500

Var Phase
Var ActiveSlot
Var PrevSlot
Var CandSlot
Var CandVersion
Var CandTag
Var Attempt
Var AttemptDir
Var Exe
Var Job
Var LimitInfo
Var StartupInfo
Var ProcInfo
Var ProcHandle
Var ThreadHandle
Var ProcPid
Var ThreadId
Var StatusPhase
Var StatusProgress
Var LastPhase
Var LastProgress
Var Ticks
Var ExitCode
Var RecoveryOut
Var FallbackCode
Var FallbackTarget
Var FallbackVersion
Var Handle
Var Line
Var FirstChar
Var Result
Var Failed
Var Error
Var WaitPid
Var KeptAttempt
Var SweepFailed
Var SlotName

; ---------- 通用 ----------

; 输入：$0 = 提示文本
Function FailHard
  MessageBox MB_ICONSTOP|MB_OK "$0"
  SetErrorLevel 2
  Quit
FunctionEnd

; 等待发起本次启动的进程退出；更新重启时旧进程仍在运行，不能提前启动新进程
Function WaitForPrevious
  ${If} $WaitPid == ""
    Return
  ${EndIf}
  System::Call 'kernel32::OpenProcess(i 0x00100000, i 0, i $WaitPid) p .R1'
  ${If} $R1 == 0
    Return
  ${EndIf}
  StrCpy $R2 0
  ${Do}
    System::Call 'kernel32::WaitForSingleObject(p R1, i 1000) i .R3'
    ${If} $R3 == 0
      ${Break}
    ${EndIf}
    IntOp $R2 $R2 + 1
    ${If} $R2 >= 60
      ${Break}
    ${EndIf}
  ${Loop}
  System::Call 'kernel32::CloseHandle(p R1)'
FunctionEnd

; 去掉清单行尾的 CR / LF；清单按 CRLF 写出
Function TrimLineEnd
  ${Do}
    StrCpy $R4 $Line 1 -1
    ${If} $R4 == "$\r"
    ${OrIf} $R4 == "$\n"
      StrCpy $Line $Line -1
    ${Else}
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd

; 递归移除空目录：只删除已经空的目录，不删除任何文件。
; 输入 $0 = 目录；目录中已空的子目录与目录本身都会被移除，未空的原样保留。
Function PruneEmptyDirs
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
      Call PruneEmptyDirs
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

Function IsHex
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

Function IsDigit
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

Function ValidSlotName
  ; 输入：$1 = 槽名；输出：$2 = "1" 表示受管理的版本槽名
  ; 只接受 legacy 或 <X.Y.Z>-<32 位小写十六进制构建标识>。字符集不含路径分隔符，
  ; 且显式拒绝 . 与 ..，因此不会跳出 versions 目录。
  StrCpy $2 "0"
  ${If} $1 == ""
    Return
  ${EndIf}
  ${If} $1 == "."
  ${OrIf} $1 == ".."
  ${OrIf} $1 == "legacy"
    ${If} $1 == "legacy"
      StrCpy $2 "1"
    ${EndIf}
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
    Call IsHex
    ${If} $R2 != "1"
      Return
    ${EndIf}
    IntOp $R5 $R5 + 1
  ${Loop}
  ${If} $R5 != 32
    Return
  ${EndIf}
  ; 版本段只能是 数字与点，且恰好两个点
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
      Call IsDigit
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

Function ValidSlot
  ; 输入：$1 = 槽名；输出：$2 = "1" 表示名字合法且对应程序文件存在
  Call ValidSlotName
  ${If} $2 != "1"
    Return
  ${EndIf}
  Call ResolveProgram
  ${If} $2 == ""
    StrCpy $2 "0"
  ${Else}
    StrCpy $2 "1"
  ${EndIf}
FunctionEnd

; ---------- 选择资料 ----------
Function WriteSelection
  CreateDirectory "$INSTDIR\data\update\desktop"
  FileOpen $1 "$INSTDIR\${SELECTION}.writing" w
  FileWrite $1 "[selection]$\r$\n"
  FileWrite $1 "protocol=1$\r$\n"
  FileWrite $1 "phase=$Phase$\r$\n"
  FileWrite $1 "active=$ActiveSlot$\r$\n"
  FileWrite $1 "previous=$PrevSlot$\r$\n"
  FileWrite $1 "candidate=$CandSlot$\r$\n"
  FileWrite $1 "candidate_version=$CandVersion$\r$\n"
  FileWrite $1 "candidate_tag=$CandTag$\r$\n"
  FileWrite $1 "attempt=$Attempt$\r$\n"
  FileWrite $1 "fallback_error=$FallbackCode$\r$\n"
  FileWrite $1 "fallback_target=$FallbackTarget$\r$\n"
  FileWrite $1 "fallback_version=$FallbackVersion$\r$\n"
  FileClose $1
  ; 同目录替换：MoveFileEx 的 MOVEFILE_REPLACE_EXISTING
  System::Call 'kernel32::MoveFileExW(w "$INSTDIR\${SELECTION}.writing", w "$INSTDIR\${SELECTION}", i 0x9) i .r8'
FunctionEnd

Function ReadSelection
  StrCpy $Phase ""
  ReadINIStr $Phase "$INSTDIR\${SELECTION}" "selection" "phase"
  ReadINIStr $ActiveSlot "$INSTDIR\${SELECTION}" "selection" "active"
  ReadINIStr $PrevSlot "$INSTDIR\${SELECTION}" "selection" "previous"
  ReadINIStr $CandSlot "$INSTDIR\${SELECTION}" "selection" "candidate"
  ReadINIStr $CandVersion "$INSTDIR\${SELECTION}" "selection" "candidate_version"
  ReadINIStr $CandTag "$INSTDIR\${SELECTION}" "selection" "candidate_tag"
  ReadINIStr $Attempt "$INSTDIR\${SELECTION}" "selection" "attempt"
  ReadINIStr $FallbackTarget "$INSTDIR\${SELECTION}" "selection" "fallback_target"
  ReadINIStr $FallbackVersion "$INSTDIR\${SELECTION}" "selection" "fallback_version"
FunctionEnd

Function WriteControl
  ; $0 = wait / commit / release
  FileOpen $1 "$AttemptDir\control.ini" w
  FileWrite $1 "[control]$\r$\n"
  FileWrite $1 "protocol=1$\r$\n"
  FileWrite $1 "attempt=$Attempt$\r$\n"
  FileWrite $1 "command=$0$\r$\n"
  FileClose $1
FunctionEnd

Function ReadStatus
  StrCpy $StatusPhase ""
  StrCpy $StatusProgress ""
  StrCpy $0 ""
  ReadINIStr $0 "$AttemptDir\status.ini" "status" "attempt"
  ${If} $0 == "$Attempt"
    ReadINIStr $StatusPhase "$AttemptDir\status.ini" "status" "phase"
    ReadINIStr $StatusProgress "$AttemptDir\status.ini" "status" "progress"
  ${EndIf}
FunctionEnd

; ---------- 启动 ----------
Function ResolveProgram
  ; $1 = 槽名；输出 $2 = exe 路径；该槽不完整时 $2 为空
  StrCpy $2 ""
  ${If} $1 == "legacy"
    ${If} ${FileExists} "$INSTDIR\xm-ims-legacy.exe"
      StrCpy $2 "$INSTDIR\xm-ims-legacy.exe"
    ${EndIf}
  ${ElseIf} $1 != ""
    ${If} ${FileExists} "$INSTDIR\versions\$1\xm-ims.exe"
      StrCpy $2 "$INSTDIR\versions\$1\xm-ims.exe"
    ${EndIf}
  ${EndIf}
FunctionEnd

Function LaunchNormal
  StrCpy $1 "$ActiveSlot"
  Call ValidSlot
  ${If} $2 != "1"
    StrCpy $0 "版本选择资料无法识别，请重新安装 XM-IMS。"
    Call FailHard
  ${EndIf}
  Call ResolveProgram
  StrCpy $Exe "$2"
  ; 普通启动不携带首次启动标识：候选转稳定后残留的变量不能再触发试运行
  System::Call 'kernel32::SetEnvironmentVariableW(w "XM_DESKTOP_ATTEMPT", w "")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "XM_INSTALL_ROOT", w "$INSTDIR")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "XM_PROGRAM_ROOT", w "$INSTDIR")'
  Exec '"$Exe"'
FunctionEnd

Function MakeAttempt
  ; 本次标识：毫秒计数与进程号组成 16 位小写十六进制，只含 [0-9a-f]
  System::Call 'kernel32::GetTickCount() i .R1'
  System::Call 'kernel32::GetCurrentProcessId() i .R2'
  IntFmt $R3 "%08x" $R1
  IntFmt $R4 "%08x" $R2
  StrCpy $Attempt "$R3$R4"
  StrCpy $AttemptDir "$INSTDIR\${ATTEMPTS}\$Attempt"
  ${If} ${FileExists} "$AttemptDir\*.*"
    ; 极端情况下同一毫秒重试：追加计数后缀，仍只含十六进制字符
    StrCpy $R5 0
    ${Do}
      IntOp $R5 $R5 + 1
      StrCpy $R6 $R5
      IntFmt $R6 "%x" $R5
      StrCpy $Attempt "$R3$R4$R6"
      StrCpy $AttemptDir "$INSTDIR\${ATTEMPTS}\$Attempt"
      ${IfNot} ${FileExists} "$AttemptDir\*.*"
        ${Break}
      ${EndIf}
    ${LoopWhile} $R5 < 16
  ${EndIf}
  CreateDirectory "$AttemptDir"
  FileOpen $1 "$AttemptDir\request.ini" w
  FileWrite $1 "[request]$\r$\n"
  FileWrite $1 "protocol=1$\r$\n"
  FileWrite $1 "attempt=$Attempt$\r$\n"
  FileWrite $1 "from_slot=$ActiveSlot$\r$\n"
  FileWrite $1 "to_slot=$CandSlot$\r$\n"
  FileClose $1
  FileOpen $1 "$AttemptDir\control.ini" w
  FileWrite $1 "[control]$\r$\n"
  FileWrite $1 "protocol=1$\r$\n"
  FileWrite $1 "attempt=$Attempt$\r$\n"
  FileWrite $1 "command=wait$\r$\n"
  FileClose $1
FunctionEnd

Function LaunchCandidate
  StrCpy $1 "$CandSlot"
  Call ValidSlot
  ${If} $2 != "1"
  ${OrIf} $CandSlot == "legacy"
    StrCpy $0 "新版本的选择资料无法识别，已停止本次更新。"
    Call FailHard
  ${EndIf}
  Call ResolveProgram
  ${If} $2 == ""
    StrCpy $0 "新版本的程序文件不完整，已停止本次更新。"
    Call FailHard
  ${EndIf}
  StrCpy $Exe "$2"
  ; 候选只继承启动器环境：显式注入本次标识与固定根，候选才会进入首次启动确认；
  ; 普通启动则清空残留标识，避免误入试运行
  System::Call 'kernel32::SetEnvironmentVariableW(w "XM_INSTALL_ROOT", w "$INSTDIR")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "XM_PROGRAM_ROOT", w "$INSTDIR")'
  System::Call 'kernel32::SetEnvironmentVariableW(w "XM_DESKTOP_ATTEMPT", w "$Attempt")'
  ; 启动器是 x86：CREATE_SUSPENDED(0x4)、CREATE_NEW_PROCESS_GROUP 不加；
  ; STARTUPINFO 与 PROCESS_INFORMATION 按调用端位宽（x86）组织
  System::Call 'kernel32::CreateJobObjectW(p 0, w 0) p .R1'
  StrCpy $Job $R1
  ; JOBOBJECT_EXTENDED_LIMIT_INFORMATION = 108 字节（x86）：27 个 DWORD，
  ; 第 5 个是 LimitFlags，只启用 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE(0x2000)
  System::Call '*(i 0,i 0,i 0,i 0,i 0x2000,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0) p .R2'
  StrCpy $LimitInfo $R2
  System::Call 'kernel32::SetInformationJobObject(p $Job, i 9, p $LimitInfo, i 108) i .R3'
  System::Call '*(&i2 68, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0, &i2 0) p .R4'
  StrCpy $StartupInfo $R4
  System::Call '*(i 0,i 0,i 0,i 0) p .R5'
  StrCpy $ProcInfo $R5
  System::Call 'kernel32::CreateProcessW(w "$Exe", w "\"$Exe\"", p 0, p 0, i 0, i 0x4, p 0, w "$INSTDIR", p $StartupInfo, p $ProcInfo) i .R6'
  ${If} $R6 == 0
    StrCpy $0 "无法启动新版本，已停止本次更新。"
    Call FailHard
  ${EndIf}
  System::Call '*$ProcInfo(i .R7, i .R8, i .R9, i .R10)'
  StrCpy $ProcHandle $R7
  StrCpy $ThreadHandle $R8
  StrCpy $ProcPid $R9
  StrCpy $ThreadId $R10
  ; 先加入本次 Job，再恢复线程：候选的子进程都在监督范围内
  System::Call 'kernel32::AssignProcessToJobObject(p $Job, p $ProcHandle) i .R11'
  System::Call 'kernel32::ResumeThread(p $ThreadHandle) i .R12'
FunctionEnd

Function CandidateExited
  ; 输出 $0：1 表示候选主进程已经退出
  StrCpy $0 "0"
  System::Call 'kernel32::WaitForSingleObject(p $ProcHandle, i 0) i .R1'
  ${If} $R1 == 0
    StrCpy $0 "1"
  ${EndIf}
FunctionEnd

; 等待本次阶段推进；$Result = 0 成功，1 失败。失败原因写 $FallbackCode
Function WaitTrial
  StrCpy $Failed "0"
  StrCpy $LastPhase ""
  StrCpy $LastProgress ""
  StrCpy $Ticks 0
  ${Do}
    Sleep ${POLL_MS}
    Call ReadStatus
    ; 阶段或实际推进值变化才重新计时：不用无条件心跳把挂起伪装成工作中
    ${If} $StatusPhase != "$LastPhase"
      StrCpy $LastPhase $StatusPhase
      StrCpy $LastProgress $StatusProgress
      StrCpy $Ticks 0
    ${ElseIf} $StatusProgress != "$LastProgress"
      StrCpy $LastProgress $StatusProgress
      StrCpy $Ticks 0
    ${Else}
      IntOp $Ticks $Ticks + 1
    ${EndIf}
    ${If} $StatusPhase == "failed"
      StrCpy $FallbackCode "1"
      StrCpy $Failed "1"
      ${Break}
    ${EndIf}
    ${If} $StatusPhase == "commit_ready"
      ${Break}
    ${EndIf}
    Call CandidateExited
    ${If} $0 == "1"
      StrCpy $FallbackCode "2"
      StrCpy $Failed "1"
      ${Break}
    ${EndIf}
    ${If} $Ticks >= ${STAGE_TIMEOUT_TICKS}
      StrCpy $FallbackCode "3"
      StrCpy $Failed "1"
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd

; 数据与程序交接：committing → commit → 确认选择 → 解除清退 → release
Function Handoff
  StrCpy $Phase "committing"
  Call WriteSelection
  StrCpy $0 "commit"
  Call WriteControl
  StrCpy $Ticks 0
  ${Do}
    Sleep ${POLL_MS}
    Call ReadStatus
    ${If} $StatusPhase == "data_committed"
      ${Break}
    ${EndIf}
    ${If} $StatusPhase == "failed"
      StrCpy $FallbackCode "4"
      StrCpy $Failed "1"
      Return
    ${EndIf}
    Call CandidateExited
    ${If} $0 == "1"
      StrCpy $FallbackCode "5"
      StrCpy $Failed "1"
      Return
    ${EndIf}
    IntOp $Ticks $Ticks + 1
    ${If} $Ticks >= ${STAGE_TIMEOUT_TICKS}
      StrCpy $FallbackCode "6"
      StrCpy $Failed "1"
      Return
    ${EndIf}
  ${Loop}

  ; 首次启动成功的持久确认点：此后中断不再自动倒退数据
  ; 选择文件里的本次标识清空，但内存中的标识必须保留到 release 交接完成，
  ; 否则 control.ini 写不出本次标识，候选不会接受放行命令
  StrCpy $KeptAttempt "$Attempt"
  StrCpy $Phase "stable"
  StrCpy $PrevSlot "$ActiveSlot"
  StrCpy $ActiveSlot "$CandSlot"
  StrCpy $CandSlot ""
  StrCpy $CandVersion ""
  StrCpy $CandTag ""
  StrCpy $Attempt ""
  StrCpy $FallbackCode ""
  StrCpy $FallbackTarget ""
  StrCpy $FallbackVersion ""
  Call WriteSelection
  StrCpy $Attempt "$KeptAttempt"
  StrCpy $KeptAttempt ""

  ; 解除本次 Job 的“退出即清退”设置，再放行候选进入正常同步启动
  System::Call '*(i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0,i 0) p .R2'
  StrCpy $LimitInfo $R2
  System::Call 'kernel32::SetInformationJobObject(p $Job, i 9, p $LimitInfo, i 108) i .R3'
  StrCpy $0 "release"
  Call WriteControl
  StrCpy $Ticks 0
  ${Do}
    Sleep ${POLL_MS}
    Call ReadStatus
    ${If} $StatusPhase == "released"
      ${Break}
    ${EndIf}
    IntOp $Ticks $Ticks + 1
    ${If} $Ticks >= 120
      ${Break}
    ${EndIf}
  ${Loop}
FunctionEnd

; 成功或失败的清退都必须先终止本次 Job 并确认候选进程真正退出
Function TerminateCandidate
  ${If} $Job != ""
    System::Call 'kernel32::TerminateJobObject(p $Job, i 1) i .R1'
  ${EndIf}
  ${If} $ProcHandle != ""
    System::Call 'kernel32::WaitForSingleObject(p $ProcHandle, i 15000) i .R2'
  ${EndIf}
FunctionEnd

; 恢复原数据指针并启动上一个稳定版本
Function Recover
  Call TerminateCandidate
  ${If} $ActiveSlot == ""
    MessageBox MB_ICONSTOP|MB_OK "新版本没有启动成功，本机也没有可回退的旧版本。更新资料已保留，请重新安装 XM-IMS。"
    SetErrorLevel 3
    Quit
  ${EndIf}
  ; 恢复入口必须使用旧版 Python：候选版本可能无法运行。这里的 $ActiveSlot
  ; 仍是原稳定版本（Handoff 只在持久确认点才改写它）
  ${If} $ActiveSlot == "legacy"
    StrCpy $Exe "$INSTDIR\resources\python\python.exe"
  ${Else}
    StrCpy $Exe "$INSTDIR\versions\$ActiveSlot\resources\python\python.exe"
  ${EndIf}
  ${IfNot} ${FileExists} "$Exe"
    MessageBox MB_ICONSTOP|MB_OK "新版本没有启动成功，也找不到用于恢复的旧版程序。请不要继续使用，重新安装 XM-IMS。"
    SetErrorLevel 3
    Quit
  ${EndIf}
  ${If} $Attempt == ""
    ; 没有本次资料（例如提交前就被使用其他方式终止）：只恢复程序选择
    StrCpy $RecoveryOut ""
    StrCpy $ExitCode 0
  ${Else}
    nsExec::ExecToStack '"$Exe" -B "$INSTDIR\${RECOVERY}" restore --data "$INSTDIR\data" --attempt "$Attempt"'
    Pop $ExitCode
    Pop $RecoveryOut
  ${EndIf}
  ${If} $ExitCode != 0
    MessageBox MB_ICONSTOP|MB_OK "新版本没有启动成功，数据恢复也没有完成。请不要继续操作，重新启动程序后重试或重新安装 XM-IMS。"
    SetErrorLevel 3
    Quit
  ${EndIf}
  ; 回到原选择；记录一次回退结果供旧版重开后提示
  StrCpy $Phase "stable"
  StrCpy $CandSlot ""
  StrCpy $CandVersion ""
  StrCpy $CandTag ""
  StrCpy $Attempt ""
  StrCpy $FallbackTarget "$ActiveSlot"
  Call WriteSelection
  Call LaunchNormal
  Quit
FunctionEnd

; 成功后按清单清理更旧的受管理程序文件，只保留当前与最近上一版
Function CleanupOlder
  FindFirst $Handle $SlotName "$INSTDIR\versions\*.*"
  ${Do}
    ${If} $SlotName == ""
      ${Break}
    ${EndIf}
    ; 只处理校验过的真实版本槽，绝不触碰 .. 或未登记目录
    StrCpy $1 "$SlotName"
    Call ValidSlotName
    ${If} $2 == "1"
    ${AndIf} $SlotName != "legacy"
    ${AndIf} $SlotName != "$ActiveSlot"
    ${AndIf} $SlotName != "$PrevSlot"
      ${If} ${FileExists} "$INSTDIR\versions\$SlotName\installed-program-files.txt"
        StrCpy $SweepFailed "0"
        FileOpen $R2 "$INSTDIR\versions\$SlotName\installed-program-files.txt" r
        ${If} $R2 != ""
          ${Do}
            ClearErrors
            FileReadUTF16LE $R2 $Line
            ${If} ${Errors}
              ${Break}
            ${EndIf}
            ${If} $Line == ""
              ${Break}
            ${EndIf}
            Call TrimLineEnd
            StrCpy $FirstChar $Line 1
            StrCpy $R3 $Line 2
            ${If} $FirstChar != "\"
            ${AndIf} $FirstChar != "."
            ${AndIf} $R3 != ".."
            ${AndIf} $R3 != "%1"
            ${AndIf} $Line != "installed-program-files.txt"
              ClearErrors
              ${If} ${FileExists} "$INSTDIR\versions\$SlotName\$Line"
                Delete "$INSTDIR\versions\$SlotName\$Line"
              ${EndIf}
              ${If} ${Errors}
                StrCpy $SweepFailed "1"
              ${EndIf}
            ${EndIf}
          ${Loop}
          FileClose $R2
        ${Else}
          StrCpy $SweepFailed "1"
        ${EndIf}
        ; 只有清单登记的文件全部删除成功，才删清单并清理已空的目录
        ${If} $SweepFailed == "0"
          Delete "$INSTDIR\versions\$SlotName\installed-program-files.txt"
          StrCpy $0 "$INSTDIR\versions\$SlotName"
          Call PruneEmptyDirs
        ${EndIf}
      ${EndIf}
    ${EndIf}
    FindNext $Handle $SlotName
  ${Loop}
  FindClose $Handle
FunctionEnd

Function .onInit
  StrCpy $INSTDIR $EXEDIR
  StrCpy $Job ""
  StrCpy $ProcHandle ""
  StrCpy $FallbackCode ""
  StrCpy $FallbackTarget ""
  StrCpy $FallbackVersion ""
  ; /XM_PID=<pid>：更新重启时由旧进程发起，先等它真正退出
  ${GetParameters} $R9
  ${GetOptions} $R9 "/XM_PID=" $WaitPid
  ${If} $WaitPid == ""
    StrCpy $WaitPid ""
  ${EndIf}
  ${IfNot} ${IsNativeAMD64}
    StrCpy $0 "XM-IMS 只支持 64 位 Windows。"
    Call FailHard
  ${EndIf}

  Call ReadSelection
  ${If} $Phase == ""
    ${If} ${FileExists} "$INSTDIR\xm-ims-legacy.exe"
      StrCpy $Phase "stable"
      StrCpy $ActiveSlot "legacy"
    ${Else}
      StrCpy $0 "版本选择资料缺失，请重新安装 XM-IMS。"
      Call FailHard
    ${EndIf}
  ${EndIf}

  ; 普通启动：只启动当前版本，不进入回退监督
  ${If} $Phase == "stable"
  ${OrIf} $Phase == "prepared"
    Call WaitForPrevious
    Call LaunchNormal
    Quit
  ${EndIf}

  ${If} $Phase == "pending"
    ${If} $CandSlot == ""
      StrCpy $0 "没有待应用的新版本，请重新打开程序。"
      Call FailHard
    ${EndIf}
    Call WaitForPrevious
    Call MakeAttempt
    StrCpy $Phase "starting"
    Call WriteSelection
    Call LaunchCandidate
    Call WaitTrial
    ${If} $Failed == "0"
      Call Handoff
    ${EndIf}
    ${If} $Failed == "0"
      Call CleanupOlder
      Quit
    ${EndIf}
    Call Recover
    Quit
  ${EndIf}

  ${If} $Phase == "starting"
  ${OrIf} $Phase == "committing"
    ; 上次首次启动在确认前中断：先把该次未确认资料恢复，不能直接按候选继续
    MessageBox MB_ICONSTOP|MB_OK "上次更新没有完成，将恢复到上一个版本。"
    Call WaitForPrevious
    Call Recover
    Quit
  ${EndIf}

  StrCpy $0 "版本选择资料无法识别，请重新安装 XM-IMS。"
  Call FailHard
FunctionEnd

Section
SectionEnd
