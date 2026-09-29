"""Return the Windows top-level window below a screen point for screen grab."""
import ctypes
import json
import sys


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def main() -> None:
    if sys.platform != "win32" or len(sys.argv) != 3:
        print("{}")
        return
    user32 = ctypes.windll.user32
    # Electron converts the overlay click from DIP to physical pixels before
    # starting this helper. Use the same per-monitor DPI coordinate space.
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = ctypes.c_bool
    point = POINT(int(sys.argv[1]), int(sys.argv[2]))
    child = user32.WindowFromPoint(point)
    hwnd = user32.GetAncestor(child, 2) if child else None  # GA_ROOT
    rect = RECT()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        print(json.dumps({"hwnd": hwnd, "rect": [rect.left, rect.top, rect.right, rect.bottom]}))
    else:
        print("{}")


if __name__ == "__main__":
    main()
