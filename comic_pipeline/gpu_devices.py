"""Read-only DXGI adapter discovery; no CUDA or model runtime dependency."""
import ctypes as ct
from ctypes import wintypes as wt
import sys


def adapters():
    if sys.platform != "win32":
        return []
    class GUID(ct.Structure):
        _fields_ = [("a", wt.DWORD), ("b", wt.WORD), ("c", wt.WORD), ("d", ct.c_ubyte*8)]
    class LUID(ct.Structure):
        _fields_ = [("low", wt.DWORD), ("high", wt.LONG)]
    class DESC(ct.Structure):
        _fields_ = [("name", wt.WCHAR*128), ("vendor", wt.UINT), ("device", wt.UINT),
                    ("subsystem", wt.UINT), ("revision", wt.UINT), ("video", ct.c_size_t),
                    ("system", ct.c_size_t), ("shared", ct.c_size_t), ("luid", LUID), ("flags", wt.UINT)]
    iid = GUID(0x770aae78, 0xf26f, 0x4dba, (ct.c_ubyte*8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
    factory = ct.c_void_p()
    create = ct.WinDLL("dxgi.dll").CreateDXGIFactory1
    create.argtypes = [ct.POINTER(GUID), ct.POINTER(ct.c_void_p)]
    create.restype = ct.c_long
    if create(ct.byref(iid), ct.byref(factory)) < 0:
        raise RuntimeError("Cannot enumerate DXGI adapters")
    def method(pointer, index, restype, *argtypes):
        table = ct.cast(pointer, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        return ct.WINFUNCTYPE(restype, ct.c_void_p, *argtypes)(table[index])
    result = []
    try:
        for index in range(16):
            adapter = ct.c_void_p()
            if method(factory, 12, ct.c_long, wt.UINT, ct.POINTER(ct.c_void_p))(factory, index, ct.byref(adapter)) < 0:
                break
            try:
                desc = DESC()
                if method(adapter, 10, ct.c_long, ct.POINTER(DESC))(adapter, ct.byref(desc)) >= 0:
                    result.append(dict(index=index, name=desc.name, vendor=int(desc.vendor), vram_mib=desc.video//1048576))
            finally:
                method(adapter, 2, wt.ULONG)(adapter)
    finally:
        method(factory, 2, wt.ULONG)(factory)
    return result


def tested_adapter(devices):
    # Only auto-enable hardware actually benchmarked for this reader.
    return next((d for d in devices if d["vendor"] == 0x10de and "P3000" in d["name"]), None)
