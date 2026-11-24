#!/usr/bin/env python3
"""P26 Passport keyboard -> digit bridge (evdev -> uinput).

Why this exists
---------------
The Zinwa P26 / BlackBerry Passport keyboard has 29 physical keys:
26 letters, backspace(14), enter(28), wide space(57). It has NO number
keys and NO Alt/SYM key. keyd v2.5.0 refuses to manage a device unless
its evdev capability bitmap contains KEY_1..KEY_0, so it silently drops
this keyboard and no keyd configuration can ever help.

This daemon bypasses keyd entirely:
  * grab the physical keyboard (EVIOCGRAB) so events don't double-fire
  * create a uinput virtual keyboard that DOES advertise digits
  * forward every physical key to the virtual keyboard unchanged
  * hold SPACE longer than --hold seconds -> enter a symbol layer:
        q w e r t y u i o p  ->  1 2 3 4 5 6 7 8 9 0
        a s d f              ->  UP DOWN LEFT RIGHT
        z                    ->  ESC
        b n m                ->  , . ?   (m = Shift+slash)
        c v x                ->  Ctrl+C / Ctrl+V / Ctrl+X
        g                    ->  F5
        l                    ->  toggle on-screen keyboard (squeekboard)
        backspace            ->  Ctrl+Shift+Space (WowType 中/英 toggle)
    (all mappings apply ONLY inside the hold-SPACE layer; plain typing is
     untouched. digit row per user request 2026-11-24.)
  * tap SPACE -> ordinary space, so normal typing is unaffected
  * (optional, --touch-name) grab touch_kbd and forward its arrow-key pulses
    (KEY_UP/DOWN/LEFT/RIGHT) straight to the virtual keyboard, ALWAYS active
    (no need to hold SPACE). libinput/phoc must NOT own touch_kbd: keep the
    91-p26-touchpad-off.rules IGNORE so only this bridge reads it. This gives
    a real "touchpad as arrow keys" without a virtual pointer or a seat0 hack.

Requires root (EVIOCGRAB + /dev/uinput). Intended to run as systemd
service p26-digit-bridge.service.
"""

import argparse
import fcntl
import glob
import os
import pwd
import re
import select
import signal
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------- ioctl ---
# Compute ioctl numbers instead of hardcoding hex (avoids past EINVAL bug).
def _ioc(direction, kind, nr, size):
    return (direction << 30) | (kind << 8) | (nr << 0) | (size << 16)


def _io(kind, nr):
    return _ioc(0, kind, nr, 0)


def _iow(kind, nr, size):
    return _ioc(1, kind, nr, size)


EV_KEY = 1
EV_SYN = 0
SYN_REPORT = 0

UI_DEV_CREATE = _io(ord("U"), 1)
UI_DEV_DESTROY = _io(ord("U"), 2)
UI_DEV_SETUP = _iow(ord("U"), 3, 92)   # sizeof(struct uinput_setup)
UI_SET_EVBIT = _iow(ord("U"), 100, 4)  # int
UI_SET_KEYBIT = _iow(ord("U"), 101, 4) # int

# EVIOCGRAB is _IOW('E', 0x90, int). Historical notes in this project had
# nr=0x04 (that is EVIOCGKEYCODE) which silently fails with EINVAL — do not
# reuse it. EVIOCGNAME(len) is _IOC(READ, 'E', 0x06, len).
EVIOCGRAB = _iow(ord("E"), 0x90, 4)
EVIOCGNAME = _ioc(2, ord("E"), 0x06, 128)


def grab(fd, on):
    fcntl.ioctl(fd, EVIOCGRAB, int(on))

BUS_USB = 0x03
EVENT_FMT = "qqHHi"                    # struct input_event: timeval(2x64) + type/code/value
EVENT_SIZE = struct.calcsize(EVENT_FMT)  # 24 on arm64/amd64

# ------------------------------------------------------------- key maps ---
KEY_ESC, KEY_1, KEY_0 = 1, 2, 11
KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_SPACE = 14, 15, 28, 57
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT, KEY_SLASH = 42, 53
KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN = 103, 105, 106, 108
KEY_F5 = 63
KEY_Q, KEY_P = 16, 25
KEY_A, KEY_L = 30, 38
KEY_X, KEY_C, KEY_V = 45, 46, 47
KEY_Z, KEY_M = 44, 50

# Symbol-layer map: physical keycode -> (display label, injected key sequence).
# A sequence of length > 1 is a chord: press in order, release in reverse
# (modifier must be released last). Entries apply ONLY inside the hold-SPACE
# layer; all other keys are forwarded verbatim. OSK_TOGGLE is a special
# action (not a key sequence) and fires once on key-down.
OSK_TOGGLE = object()

LAYER_MAP = {
    # first row -> digits 1..0
    16: ("1", (2,)),    # q
    17: ("2", (3,)),    # w
    18: ("3", (4,)),    # e
    19: ("4", (5,)),    # r
    20: ("5", (6,)),    # t
    21: ("6", (7,)),    # y
    22: ("7", (8,)),    # u
    23: ("8", (9,)),    # i
    24: ("9", (10,)),   # o
    25: ("0", (11,)),   # p
    # home row -> arrows
    30: ("UP",    (KEY_UP,)),     # a
    31: ("DOWN",  (KEY_DOWN,)),   # s
    32: ("LEFT",  (KEY_LEFT,)),   # d
    33: ("RIGHT", (KEY_RIGHT,)),  # f
    # bottom row -> esc / punctuation
    44: ("ESC", (KEY_ESC,)),              # z
    48: (",",  (51,)),                    # b -> comma
    49: (".",  (52,)),                    # n -> dot
    50: ("?",  (KEY_LEFTSHIFT, KEY_SLASH)),  # m -> Shift+slash
    # clipboard / function / on-screen keyboard
    46: ("C+C", (KEY_LEFTCTRL, KEY_C)),      # c -> Ctrl+C
    47: ("C+V", (KEY_LEFTCTRL, KEY_V)),      # v -> Ctrl+V
    45: ("C+X", (KEY_LEFTCTRL, KEY_X)),      # x -> Ctrl+X
    34: ("F5",  (KEY_F5,)),                  # g -> F5
    38: ("OSK", OSK_TOGGLE),                 # l -> show/hide squeekboard
    # backspace -> WowType ToggleKey (Ctrl+Shift+Space) for zh/en switch
    14: ("DEL", (KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_SPACE)),
}
KEY_NAMES = {
    14: "DEL",
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t",
    21: "y", 22: "u", 23: "i", 24: "o", 25: "p",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g",
    38: "l",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m",
}

# Every keycode the virtual keyboard must advertise: physical keys + all
# injected symbols (digits, arrows, esc, punctuation, and shift for chords).
ADVERTISE = (
    {KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER, KEY_SPACE, KEY_LEFTCTRL,
     KEY_LEFTSHIFT, KEY_SLASH, KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN,
     KEY_F5, 51, 52}
    | set(range(KEY_1, KEY_0 + 1))           # digits 1..0
    | set(range(KEY_Q, KEY_P + 1))           # q..p
    | set(range(KEY_A, KEY_L + 1))           # a..l
    | set(range(KEY_Z, KEY_M + 1))           # z..m
)

IDLE, PENDING, SYM = 0, 1, 2


def log(msg):
    print("p26-digit-bridge: " + str(msg), flush=True)


def find_event(name):
    """Resolve /proc/bus/input/devices name -> /dev/input/eventN."""
    try:
        blocks = open("/proc/bus/input/devices", encoding="utf-8", errors="replace").read().split("\n\n")
    except OSError as e:
        raise RuntimeError("cannot read /proc/bus/input/devices: %s" % e)
    for block in blocks:
        if 'Name="%s"' % name not in block:
            continue
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                for token in line.split("=", 1)[1].split():
                    if token.startswith("event"):
                        return "/dev/input/" + token
    raise RuntimeError("input device %r not found" % name)


def open_source(path, name):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    # Verify identity so a renumbered eventN can never grab the wrong device.
    try:
        buf = bytearray(128)
        fcntl.ioctl(fd, EVIOCGNAME, buf)
        actual = bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        actual = ""
    if name and actual and actual != name:
        os.close(fd)
        raise RuntimeError("%s is %r, expected %r" % (path, actual, name))
    grab(fd, 1)  # exclusive: prevents duplicate keys in X/phoc
    return fd, actual


def create_uinput(label):
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        for code in sorted(ADVERTISE):
            fcntl.ioctl(fd, UI_SET_KEYBIT, code)
        # struct uinput_setup { struct input_id id (4x u16); char name[80];
        #                       u32 ff_effects_max; } = 92 bytes, 4-byte aligned
        setup = struct.pack("=HHHH80sI", BUS_USB, 0x26, 0x01, 1,
                            label.encode("ascii")[:79], 0)
        assert len(setup) == 92, len(setup)
        fcntl.ioctl(fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(fd, UI_DEV_CREATE)
    except Exception:
        os.close(fd)
        raise
    return fd


def emit(fd, code, value):
    """Inject one EV_KEY event plus SYN_REPORT."""
    os.write(fd, struct.pack(EVENT_FMT, 0, 0, EV_KEY, code, value))
    os.write(fd, struct.pack(EVENT_FMT, 0, 0, EV_SYN, SYN_REPORT, 0))


def emit_seq(fd, codes, value):
    """Press a key sequence in order, release in reverse (modifiers last)."""
    seq = codes if value == 1 else tuple(reversed(codes))
    for code in seq:
        emit(fd, code, value)


def forward_touch_data(fd, data, arrow_keys):
    """Forward EV_KEY arrow pulses from touch_kbd to the virtual keyboard.

    Returns the number of arrow events forwarded. All other event types
    (EV_MSC/EV_SYN/EV_ABS) are ignored -- the virtual uinput has its own SYN.
    """
    forwarded = 0
    while len(data) >= EVENT_SIZE:
        _s, _u, typ, code, value = struct.unpack(EVENT_FMT, data[:EVENT_SIZE])
        data = data[EVENT_SIZE:]
        if typ == EV_KEY and code in arrow_keys:
            emit(fd, code, value)
            forwarded += 1
    return forwarded


def _osk_gdbus(method_args, timeout=2.0):
    """Call gdbus on the droidian user's session bus from the root service."""
    uid = pwd.getpwnam("droidian").pw_uid
    cmd = ["/usr/bin/sudo", "-n", "-u", "droidian", "/usr/bin/env",
           "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus" % uid,
           "/usr/bin/gdbus", "call", "--session",
           "--dest", "sm.puri.OSK0", "--object-path", "/sm/puri/OSK0",
           "--method"] + list(method_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def toggle_osk():
    """Flip squeekboard visibility once on SPACE+L key-down."""
    try:
        cur = _osk_gdbus(["org.freedesktop.DBus.Properties.Get",
                          "sm.puri.OSK0", "Visible"])
        if cur.returncode != 0:
            raise RuntimeError(cur.stderr.strip() or "Get Visible failed")
        new = "false" if "true" in cur.stdout else "true"
        res = _osk_gdbus(["sm.puri.OSK0.SetVisible", new])
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or "SetVisible failed")
        log("OSK visible -> %s" % new)
    except Exception as e:
        log("OSK toggle failed: %r" % e)


def main():
    ap = argparse.ArgumentParser(description="P26 evdev -> uinput digit bridge")
    ap.add_argument("--input", help="source event device (default: resolve by --name)")
    ap.add_argument("--name", default="P26-keyboard", help="source device name")
    ap.add_argument("--label", default="p26-digit-bridge", help="uinput device label")
    ap.add_argument("--hold", type=float, default=0.20,
                    help="seconds to hold SPACE before digit layer (default: 0.20)")
    ap.add_argument("--touch-name", default="touch_kbd",
                    help="touchpad device name to forward arrow keys from (default: touch_kbd; empty to disable)")
    ap.add_argument("--no-touch", action="store_true",
                    help="disable touchpad arrow forwarding entirely")
    args = ap.parse_args()

    source = args.input or find_event(args.name)
    src, actual = open_source(source, None if args.input else args.name)
    log("grabbed %s (%s)" % (source, actual or args.name))

    # Touchpad arrow forwarding: open/grab touch_kbd separately. Failure is
    # non-fatal (keyboard bridge keeps working) -- e.g. device absent.
    touch = None
    ARROW_KEYS = {KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT}
    if not args.no_touch and args.touch_name:
        try:
            tpath = find_event(args.touch_name)
            touch, _ = open_source(tpath, args.touch_name)
            log("grabbed touchpad %s for arrow forwarding" % tpath)
        except Exception as e:
            touch = None
            log("touchpad arrow forwarding disabled: %r" % e)

    try:
        out = create_uinput(args.label)
    except Exception as e:
        grab(src, 0)
        os.close(src)
        log("FATAL: uinput create failed: %r" % e)
        return 1
    log("created uinput %r; hold=%.2fs; layer: %s" % (
        args.label, args.hold,
        " ".join("%s=%s" % (KEY_NAMES[c], LAYER_MAP[c][0])
                 for c in sorted(LAYER_MAP))))

    running = True
    state = IDLE
    deadline = 0.0

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while running:
            now = time.monotonic()
            # Explicit hold-timeout check BEFORE select: a busy touchpad would
            # otherwise keep select readable and starve the SPACE->layer switch.
            if state == PENDING and now >= deadline:
                state = SYM
                log("digit layer ON")
            timeout = max(0.0, deadline - now) if state == PENDING else None
            fds = [src] + ([touch] if touch is not None else [])
            try:
                readable, _, _ = select.select(fds, [], [], timeout)
            except InterruptedError:
                continue
            except OSError as e:
                log("select failed: %r" % e)
                break

            # Touchpad: forward arrow-key pulses verbatim, always active.
            if touch is not None and touch in readable:
                try:
                    tdata = os.read(touch, EVENT_SIZE)
                except BlockingIOError:
                    tdata = b""
                except OSError as e:
                    log("touchpad read failed: %r" % e)
                    tdata = b""
                forward_touch_data(out, tdata, ARROW_KEYS)

            if not readable:
                # Timeout (no fd ready). Hold-timeout is handled at loop top.
                continue

            # A touchpad-only wake (or a spurious src wake with no data) must
            # not be mistaken for typing, but the hold-timeout check above has
            # already run. If SPACE is pending the deadline has not elapsed
            # yet, so simply skip the keyboard branch this round.
            if src not in readable:
                continue

            try:
                data = os.read(src, EVENT_SIZE)
            except BlockingIOError:
                continue
            except OSError as e:
                log("read failed: %r" % e)
                break
            while len(data) >= EVENT_SIZE:
                _sec, _usec, typ, code, value = struct.unpack(EVENT_FMT, data[:EVENT_SIZE])
                data = data[EVENT_SIZE:]
                if typ != EV_KEY:
                    continue  # drop EV_MSC / EV_SYN from source; uinput has its own

                if code == KEY_SPACE:
                    if value == 1:  # press
                        if state == IDLE:
                            state = PENDING
                            deadline = time.monotonic() + args.hold
                            continue  # swallow; decide on release or timeout
                        # PENDING repeat or SYM additional space: pass through
                        emit(out, KEY_SPACE, 1)
                        continue
                    if value == 0:  # release
                        if state == PENDING:
                            # short tap -> ordinary space (delivered on release)
                            emit(out, KEY_SPACE, 1)
                            emit(out, KEY_SPACE, 0)
                            state = IDLE
                            continue
                        if state == SYM:
                            state = IDLE  # swallow matching release
                            log("digit layer OFF")
                            continue
                    continue  # swallow auto-repeat of space

                if state == PENDING:
                    # Another key arrived before the hold threshold:
                    # this was ordinary typing, so restore the swallowed space.
                    emit(out, KEY_SPACE, 1)
                    state = IDLE

                if state == SYM and code in LAYER_MAP:
                    action = LAYER_MAP[code][1]
                    if action is OSK_TOGGLE:
                        if value == 1:  # fire once on key-down
                            toggle_osk()
                    else:
                        emit_seq(out, action, value)
                else:
                    emit(out, code, value)  # verbatim forwarding
    finally:
        fcntl.ioctl(out, UI_DEV_DESTROY)
        os.close(out)
        if touch is not None:
            try:
                grab(touch, 0)
            except OSError:
                pass
            os.close(touch)
        grab(src, 0)
        os.close(src)
        log("stopped, uinput destroyed, source released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
