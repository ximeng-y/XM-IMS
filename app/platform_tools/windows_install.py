"""显式系统安装所需的 Win32 提升执行。

只在用户明确选择「安装到系统位置」时使用：通过 ShellExecuteExW 的 runas
启动安装程序，等待真实退出码。普通后台调用继续走 transport.run_hidden，
不要用本模块绕过权限交互。
"""
import ctypes
import ctypes.wintypes as wintypes

# ShellExecuteExW 的 fMask 取值
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_SW_SHOWNORMAL = 1
_INFINITE = 0xFFFFFFFF


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def run_elevated(exe, parameters="", directory=None):
    """以管理员权限运行并等待结束，返回 (是否完成, 退出码或 None)；用户取消 UAC 时退出码为 None。"""
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(exe)
    info.lpParameters = parameters or None
    info.lpDirectory = str(directory) if directory else None
    info.nShow = _SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        # 1223：用户在权限提示上点了取消；其余情况同样没有启动安装程序
        return False, None
    try:
        kernel32.WaitForSingleObject(info.hProcess, _INFINITE)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return True, code.value
    finally:
        kernel32.CloseHandle(info.hProcess)
