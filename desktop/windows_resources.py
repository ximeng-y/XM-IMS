"""WIN32 EXE 资源写入：图标与版本信息，只接受构建暂存目录里的副本。

用标准库 struct 解析 ICO、ctypes 调用 Win32 资源 API，不引入第三方资源库。
所有函数都只处理本次构建产物，失败直接抛错让构建停下，不能生成还带 Electron
默认图标的“成功包”。原 ICO 的全部尺寸都会写入，不只写一张大图。
"""
import ctypes
import ctypes.wintypes as wintypes
import struct
from pathlib import Path

_RT_ICON = 3
_RT_GROUP_ICON = 14
_RT_VERSION = 16
_LANG_NEUTRAL = 0
_CODE_PAGE = 0x04B0

def _utf16(value):
    """版本资源里的字符串按 UTF-16LE 存储，并以 NUL 结尾。"""
    return str(value).encode("utf-16-le") + b"\x00\x00"


def _pad4(data):
    return data + b"\x00" * (-len(data) % 4)


def read_ico(path):
    """解析 ICO，返回 [(尺寸描述, 图像数据)]；保留全部原始尺寸。"""
    data = Path(path).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if reserved != 0 or kind != 1 or not count:
        raise ValueError("不是有效的 ICO 文件")
    entries = []
    offset = 6
    for _ in range(count):
        width, height, colors, _, planes, bits, size, image_offset = struct.unpack_from(
            "<BBBBHHII", data, offset)
        offset += 16
        image = data[image_offset:image_offset + size]
        if len(image) != size:
            raise ValueError("ICO 图像数据不完整")
        entries.append(((width or 256, height or 256, colors, planes, bits), image))
    return entries


def _group_icon(entries):
    """RT_GROUP_ICON：目录头 + 每个图像的描述项，尺寸信息沿用原 ICO。"""
    body = struct.pack("<HHH", 0, 1, len(entries))
    for index, ((width, height, colors, planes, bits), image) in enumerate(entries, start=1):
        body += struct.pack("<BBBBHHIH", width % 256, height % 256, colors, 0,
                            planes, bits, len(image), index)
    return body


def _version_keys(product, description, version):
    """版本资源字符串表的固定键；值与构建输入保持一致，不留可漂移的副本。"""
    return {
        "CompanyName": "XM-IMS",
        "FileDescription": description,
        "FileVersion": version,
        "InternalName": "xm-ims",
        "OriginalFilename": "xm-ims.exe",
        "ProductName": product,
        "ProductVersion": version,
    }


def _block(key, value, value_length, node_type, children):
    """版本资源通用块：头 + Unicode 键 + 值 + 子块，整块按 4 字节对齐后再回填长度。"""
    head = struct.pack("<HHH", 0, value_length, node_type) + _utf16(key)
    body = _pad4(head) + value
    if children:
        body = _pad4(body) + children
    # 同级块必须 4 字节对齐起始，块长度自身补齐最简单也最稳
    body = _pad4(body)
    return struct.pack("<H", len(body)) + body[2:]


def _string_block(key, value):
    raw = _utf16(value)
    return _block(key, raw, len(raw) // 2, 1, b"")


def _version_info(product, description, version):
    numbers = [int(part) for part in version.split(".")]
    while len(numbers) < 4:
        numbers.append(0)
    major, minor, build, patch = numbers[:4]
    file_ms, file_ls = (major << 16) | minor, (build << 16) | patch
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x00010000, file_ms, file_ls, file_ms, file_ls,
                        0x3F, 0, 0x00040004, 1, 0, 0, 0)

    strings = b"".join(_string_block(key, value)
                       for key, value in _version_keys(product, description, version).items())
    table = _block(f"{0x0409:04X}{_CODE_PAGE:04X}", strings, 0, 1, b"")
    string_info = _block("StringFileInfo", table, 0, 1, b"")
    translation = struct.pack("<HH", 0x0409, _CODE_PAGE)
    var_info = _block("VarFileInfo", _block("Translation", translation, 1, 0, b""), 0, 1, b"")
    return _block("VS_VERSION_INFO", fixed, len(fixed), 0, string_info + var_info)


def _apply(exe, entries, version_block):
    kernel32 = ctypes.windll.kernel32
    kernel32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel32.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel32.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        wintypes.WORD, ctypes.c_char_p, wintypes.DWORD]
    kernel32.UpdateResourceW.restype = wintypes.BOOL
    kernel32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    kernel32.EndUpdateResourceW.restype = wintypes.BOOL

    handle = kernel32.BeginUpdateResourceW(str(exe), False)
    if not handle:
        raise OSError("无法打开 EXE 资源表")
    try:
        for index, (_, image) in enumerate(entries, start=1):
            if not kernel32.UpdateResourceW(handle, _RT_ICON, index, _LANG_NEUTRAL,
                                            image, len(image)):
                raise OSError("写入图标资源失败")
        group = _group_icon(entries)
        if not kernel32.UpdateResourceW(handle, _RT_GROUP_ICON, 1, _LANG_NEUTRAL,
                                        group, len(group)):
            raise OSError("写入图标组失败")
        if not kernel32.UpdateResourceW(handle, _RT_VERSION, 1, _LANG_NEUTRAL,
                                        version_block, len(version_block)):
            raise OSError("写入版本资源失败")
    except BaseException:
        kernel32.EndUpdateResourceW(handle, True)  # 放弃本次修改
        raise
    # bDeleteExistingResources 必须为 False：只替换同名资源，保留其余二进制资源。
    if not kernel32.EndUpdateResourceW(handle, False):
        raise OSError("提交 EXE 资源失败")


def write_resources(exe, ico, product, description, version):
    """把 ICO 的全部尺寸与产品版本写入暂存 EXE。"""
    exe = Path(exe).resolve()
    if not exe.is_file():
        raise FileNotFoundError(f"暂存 EXE 不存在：{exe}")
    _apply(exe, read_ico(ico), _version_info(product, description, version))
