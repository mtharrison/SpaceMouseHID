"""Read a 3Dconnexion SpaceMouse via IOKit HID with ctypes: no driver, no packages."""
import ctypes as C
import threading

VENDOR_3DCONNEXION = 0x256F
AXIS_USAGES = {0x30: 0, 0x31: 1, 0x32: 2, 0x33: 3, 0x34: 4, 0x35: 5}  # X Y Z Rx Ry Rz
PAGE_DESKTOP, PAGE_BUTTON = 0x01, 0x09
SEIZE = 1  # kIOHIDOptionsTypeSeizeDevice

CF = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
IOK = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
vp = C.c_void_p

for fn, res, args in [
    (CF.CFStringCreateWithCString, vp, [vp, C.c_char_p, C.c_uint32]),
    (CF.CFNumberCreate, vp, [vp, C.c_int, vp]),
    (CF.CFDictionaryCreateMutable, vp, [vp, C.c_long, vp, vp]),
    (CF.CFDictionarySetValue, None, [vp, vp, vp]),
    (CF.CFRunLoopGetCurrent, vp, []),
    (CF.CFRunLoopRun, None, []),
    (CF.CFRunLoopStop, None, [vp]),
    (CF.CFRelease, None, [vp]),
    (IOK.IOHIDManagerCreate, vp, [vp, C.c_uint32]),
    (IOK.IOHIDManagerSetDeviceMatching, None, [vp, vp]),
    (IOK.IOHIDManagerRegisterInputValueCallback, None, [vp, vp, vp]),
    (IOK.IOHIDManagerScheduleWithRunLoop, None, [vp, vp, vp]),
    (IOK.IOHIDManagerUnscheduleFromRunLoop, None, [vp, vp, vp]),
    (IOK.IOHIDManagerOpen, C.c_int32, [vp, C.c_uint32]),
    (IOK.IOHIDManagerClose, C.c_int32, [vp, C.c_uint32]),
    (IOK.IOHIDValueGetElement, vp, [vp]),
    (IOK.IOHIDValueGetIntegerValue, C.c_long, [vp]),
    (IOK.IOHIDElementGetUsagePage, C.c_uint32, [vp]),
    (IOK.IOHIDElementGetUsage, C.c_uint32, [vp]),
]:
    fn.restype, fn.argtypes = res, args

VALUE_CB = C.CFUNCTYPE(None, vp, C.c_int32, vp, vp)


def _cfstr(s):
    return CF.CFStringCreateWithCString(None, s.encode(), 0x08000100)


def _cfint(i):
    v = C.c_int32(i)
    return CF.CFNumberCreate(None, 3, C.byref(v))  # kCFNumberSInt32Type


class SpaceMouse:
    """Background thread keeping the latest axes (-1..1-ish) and button state."""

    FULL_SCALE = 350.0

    def __init__(self, on_button=None):
        self.axes = [0.0] * 6
        self.buttons = 0
        self.on_button = on_button
        self.error = None
        self.seized = False
        self._loop = None
        self._ready = threading.Event()
        self._cb = VALUE_CB(self._on_value)  # keep a reference or ctypes frees it
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        self._ready.wait(2)
        return self.error is None

    def stop(self):
        if self._loop:
            CF.CFRunLoopStop(self._loop)
        self._thread.join(2)

    def _on_value(self, ctx, result, sender, value):
        el = IOK.IOHIDValueGetElement(value)
        page, usage = IOK.IOHIDElementGetUsagePage(el), IOK.IOHIDElementGetUsage(el)
        v = IOK.IOHIDValueGetIntegerValue(value)
        if page == PAGE_DESKTOP and usage in AXIS_USAGES:
            self.axes[AXIS_USAGES[usage]] = v / self.FULL_SCALE
        elif page == PAGE_BUTTON and 1 <= usage <= 32:
            bit = 1 << (usage - 1)
            self.buttons = self.buttons | bit if v else self.buttons & ~bit
            if v and self.on_button:
                self.on_button(usage)

    def _run(self):
        mgr = IOK.IOHIDManagerCreate(None, 0)
        match = CF.CFDictionaryCreateMutable(
            None, 0,
            C.byref(vp.in_dll(CF, 'kCFTypeDictionaryKeyCallBacks')),
            C.byref(vp.in_dll(CF, 'kCFTypeDictionaryValueCallBacks')))
        CF.CFDictionarySetValue(match, _cfstr('VendorID'), _cfint(VENDOR_3DCONNEXION))
        IOK.IOHIDManagerSetDeviceMatching(mgr, match)
        IOK.IOHIDManagerRegisterInputValueCallback(mgr, C.cast(self._cb, vp), None)
        mode = vp.in_dll(CF, 'kCFRunLoopDefaultMode').value
        self._loop = CF.CFRunLoopGetCurrent()
        IOK.IOHIDManagerScheduleWithRunLoop(mgr, self._loop, mode)
        # Seize, or macOS's own HID driver also turns the cap into scroll events.
        rc = IOK.IOHIDManagerOpen(mgr, SEIZE)
        self.seized = rc == 0
        if not self.seized:
            rc = IOK.IOHIDManagerOpen(mgr, 0)
        if rc != 0:
            self.error = 'IOHIDManagerOpen failed: 0x%08x' % (rc & 0xFFFFFFFF)
        self._ready.set()
        if self.error is None:
            CF.CFRunLoopRun()
        IOK.IOHIDManagerUnscheduleFromRunLoop(mgr, self._loop, mode)
        IOK.IOHIDManagerClose(mgr, 0)
        CF.CFRelease(match)
        CF.CFRelease(mgr)


if __name__ == '__main__':
    import time
    m = SpaceMouse(on_button=lambda b: print('button', b))
    print('started' if m.start() else m.error)
    end = time.time() + 12
    while time.time() < end:
        if any(m.axes):
            print(' '.join('%+.2f' % a for a in m.axes))
        time.sleep(0.5)
    m.stop()
    print('stopped cleanly')
