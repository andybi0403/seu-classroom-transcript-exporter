from __future__ import annotations

import ctypes


LCMAP_SIMPLIFIED_CHINESE = 0x02000000
map_string = ctypes.windll.kernel32.LCMapStringEx
map_string.argtypes = (
    ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_int,
    ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ssize_t,
)
map_string.restype = ctypes.c_int


def simplified(text: str) -> str:
    if not text:
        return text
    source_length = len(text.encode("utf-16-le", errors="surrogatepass")) // 2
    arguments = ("zh-CN", LCMAP_SIMPLIFIED_CHINESE, text, source_length)
    length = map_string(*arguments, None, 0, None, None, 0)
    if not length:
        return text
    buffer = ctypes.create_unicode_buffer(length)
    written = map_string(*arguments, buffer, length, None, None, 0)
    return buffer[:written] if written else text
