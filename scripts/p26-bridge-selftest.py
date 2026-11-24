#!/usr/bin/env python3
"""Self-test for p26-digit-bridge without touching real hardware.

Creates a fake source keyboard via uinput, runs the bridge against it,
injects SPACE-hold + letter sequences, and verifies the bridge's virtual
keyboard emits the expected digit. Must run as root (uinput access).

Usage: sudo python3 p26-bridge-selftest.py /path/to/p26-digit-bridge.py
"""

import fcntl
import importlib.util
import os
import pwd
import select
import signal
import struct
import subprocess
import sys
import time

EVENT_FMT = "qqHHi"
EV_KEY, EV_REL, EV_ABS, EV_SYN, SYN_REPORT = 1, 2, 3, 0, 0


def ioc(direction, kind, nr, size=0):
    return (direction << 30) | (kind << 8) | (nr << 0) | (size << 16)


IO_SETUP = ioc(1, ord("U"), 3, 92)
IO_CREATE = ioc(0, ord("U"), 1)
IO_DESTROY = ioc(0, ord("U"), 2)
IO_EVBIT = ioc(1, ord("U"), 100, 4)
IO_KEYBIT = ioc(1, ord("U"), 101, 4)


def make_uinput(name, codes, bustype=0x03):
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, IO_EVBIT, EV_SYN)
    fcntl.ioctl(fd, IO_EVBIT, EV_KEY)
    for c in codes:
        fcntl.ioctl(fd, IO_KEYBIT, c)
    setup = struct.pack("=HHHH80sI", bustype, 0x26, 0x01, 1,
                        name.encode("ascii")[:79], 0)
    assert len(setup) == 92
    fcntl.ioctl(fd, IO_SETUP, setup)
    fcntl.ioctl(fd, IO_CREATE)
    return fd


def find_event_by_name(name, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            text = open("/proc/bus/input/devices", encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        for block in text.split("\n\n"):
            if 'Name="%s"' % name not in block:
                continue
            for line in block.splitlines():
                if line.startswith("H: Handlers="):
                    for tok in line.split("=", 1)[1].split():
                        if tok.startswith("event"):
                            return "/dev/input/" + tok
        time.sleep(0.1)
    raise RuntimeError("event node for %r not found" % name)


def inject(fd, code, value):
    os.write(fd, struct.pack(EVENT_FMT, 0, 0, EV_KEY, code, value))
    os.write(fd, struct.pack(EVENT_FMT, 0, 0, EV_SYN, SYN_REPORT, 0))


def drain(fd, seconds):
    """Collect (code, value) EV_KEY events from fd for `seconds`."""
    out = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], max(0, end - time.monotonic()))
        if not r:
            continue
        try:
            data = os.read(fd, 64 * 24)
        except BlockingIOError:
            continue
        for i in range(0, len(data) - 23, 24):
            _s, _u, typ, code, value = struct.unpack(EVENT_FMT, data[i:i + 24])
            if typ == EV_KEY:
                out.append((code, value))
    return out


def drain_all(fd, seconds):
    """Collect (type, code, value) for EV_KEY / EV_REL events."""
    out = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], max(0, end - time.monotonic()))
        if not r:
            continue
        try:
            data = os.read(fd, 64 * 24)
        except BlockingIOError:
            continue
        for i in range(0, len(data) - 23, 24):
            _s, _u, typ, code, value = struct.unpack(EVENT_FMT, data[i:i + 24])
            if typ in (EV_KEY, EV_REL):
                out.append((typ, code, value))
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: sudo python3 p26-bridge-selftest.py <bridge.py>", file=sys.stderr)
        return 2
    bridge = sys.argv[1]

    # A fake keyboard that looks like the real P26 (letters + space, no digits).
    fake_codes = ([14, 28, 57] + list(range(16, 26)) +
                  list(range(30, 39)) + list(range(44, 51)))
    fake = make_uinput("P26-test-source", fake_codes)
    fake_ev = find_event_by_name("P26-test-source")
    print("fake source:", fake_ev, flush=True)

    # Let bridge logs inherit stdout so failures are visible in real time.
    proc = subprocess.Popen(
        [sys.executable, bridge, "--input", fake_ev, "--label", "p26-test-out",
         "--hold", "0.15"])

    out_fd = None
    try:
        out_ev = find_event_by_name("p26-test-out", timeout=8)
        print("bridge output:", out_ev, flush=True)
        out_fd = os.open(out_ev, os.O_RDONLY | os.O_NONBLOCK)

        # Let bridge settle and grab the fake device.
        time.sleep(0.4)

        # --- case 1: hold SPACE > threshold, tap Q -> expect digit 1 (code 2)
        time.sleep(0.15)  # drain
        drain(out_fd, 0.1)
        inject(fake, 57, 1)          # space down
        time.sleep(0.30)             # > hold 0.15 -> layer on
        inject(fake, 16, 1)          # q down -> 1
        inject(fake, 16, 0)          # q up
        inject(fake, 25, 1)          # p down -> 0
        inject(fake, 25, 0)          # p up
        inject(fake, 57, 0)          # space up
        got = drain(out_fd, 0.5)
        print("case1 (hold space + q,p) events:", got, flush=True)
        ok1 = ((2, 1) in got and (2, 0) in got and      # q -> digit 1
               (11, 1) in got and (11, 0) in got and    # p -> digit 0
               (16, 1) not in got and (25, 1) not in got)  # no raw letters
        print("case1:", "PASS" if ok1 else "FAIL", flush=True)

        # --- case 2: short SPACE tap -> expect ordinary space forwarded
        time.sleep(0.2)
        drain(out_fd, 0.1)
        inject(fake, 57, 1)
        time.sleep(0.05)             # < hold
        inject(fake, 57, 0)
        got = drain(out_fd, 0.5)
        print("case2 (tap space) events:", got, flush=True)
        ok2 = (57, 1) in got and (57, 0) in got
        print("case2:", "PASS" if ok2 else "FAIL", flush=True)

        # --- case 3: plain letter -> expect verbatim forwarding
        time.sleep(0.2)
        drain(out_fd, 0.1)
        inject(fake, 30, 1)          # a
        inject(fake, 30, 0)
        got = drain(out_fd, 0.5)
        print("case3 (plain a) events:", got, flush=True)
        ok3 = (30, 1) in got and (30, 0) in got
        print("case3:", "PASS" if ok3 else "FAIL", flush=True)

        # --- case 4: hold SPACE -> arrows / esc / punctuation
        time.sleep(0.2)
        drain(out_fd, 0.1)
        inject(fake, 57, 1)          # space down
        time.sleep(0.30)             # > hold -> layer on
        ok4 = False
        for src, want in [(30, 103), (31, 108), (32, 105), (33, 106),  # a/s/d/f
                          (44, 1), (48, 51), (49, 52)]:                # z/b/n
            inject(fake, src, 1)
            inject(fake, src, 0)
            got = drain(out_fd, 0.15)
            if not ((want, 1) in got and (want, 0) in got):
                print("case4 FAIL at src=%d want=%d: %s" % (src, want, got), flush=True)
                break
        else:
            # m -> '?' = Shift(42) down, Slash(53) down, Slash up, Shift up
            inject(fake, 50, 1)
            inject(fake, 50, 0)
            got = drain(out_fd, 0.3)
            print("case4 (m -> ?) events:", got, flush=True)
            ok4 = ([(42, 1), (53, 1), (53, 0), (42, 0)] == got)
        inject(fake, 57, 0)          # space up
        print("case4:", "PASS" if ok4 else "FAIL", flush=True)

        # --- case 5: hold SPACE + backspace -> Ctrl+Shift+Space toggle chord
        time.sleep(0.2)
        drain(out_fd, 0.1)
        inject(fake, 57, 1)          # space down
        time.sleep(0.30)             # > hold -> layer on
        inject(fake, 14, 1)          # backspace down
        inject(fake, 14, 0)          # backspace up
        got = drain(out_fd, 0.5)
        inject(fake, 57, 0)          # space up
        print("case5 (hold space + backspace) events:", got, flush=True)
        ok5 = ([(29, 1), (42, 1), (57, 1), (57, 0), (42, 0), (29, 0)] == got)
        print("case5:", "PASS" if ok5 else "FAIL", flush=True)

        # --- case 6: hold SPACE + c/v/x/g -> Ctrl+C / Ctrl+V / Ctrl+X / F5
        time.sleep(0.2)
        drain(out_fd, 0.1)
        inject(fake, 57, 1)          # space down
        time.sleep(0.30)             # > hold -> layer on
        ok6 = True
        for src, seq in [(46, (29, 46)),   # c -> Ctrl+C
                         (47, (29, 47)),   # v -> Ctrl+V
                         (45, (29, 45)),   # x -> Ctrl+X
                         (34, (63,))]:     # g -> F5
            inject(fake, src, 1)
            inject(fake, src, 0)
            got = drain(out_fd, 0.30)
            want = (list(zip(seq, (1,) * len(seq))) +
                    list(zip(reversed(seq), (0,) * len(seq))))
            print("case6 src=%d got=%s want=%s" % (src, got, want), flush=True)
            if got != want:
                ok6 = False
                break
        inject(fake, 57, 0)          # space up
        print("case6:", "PASS" if ok6 else "FAIL", flush=True)

        # --- case 7: hold SPACE + l -> toggle squeekboard Visible via D-Bus
        def osk_visible():
            uid = pwd.getpwnam("droidian").pw_uid
            cmd = ["/usr/bin/sudo", "-n", "-u", "droidian", "/usr/bin/env",
                   "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%d/bus" % uid,
                   "/usr/bin/gdbus", "call", "--session",
                   "--dest", "sm.puri.OSK0", "--object-path", "/sm/puri/OSK0",
                   "--method", "org.freedesktop.DBus.Properties.Get",
                   "sm.puri.OSK0", "Visible"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.strip())
            return "true" in r.stdout

        def press_l_layer():
            inject(fake, 57, 1)
            time.sleep(0.30)         # > hold -> layer on
            inject(fake, 38, 1)      # l down
            inject(fake, 38, 0)      # l up
            time.sleep(0.20)         # allow gdbus round trip
            inject(fake, 57, 0)      # space up
            drain(out_fd, 0.2)

        ok7 = False
        try:
            time.sleep(0.2)
            drain(out_fd, 0.1)
            before = osk_visible()
            press_l_layer()
            after = osk_visible()
            print("case7 OSK %s -> %s" % (before, after), flush=True)
            ok7 = (after != before)
            if ok7:                  # restore the user's original state
                press_l_layer()
                print("case7 restored -> %s" % osk_visible(), flush=True)
        except Exception as e:
            # Non-Droidian host: no droidian user / session bus available.
            print("case7 SKIP (no Droidian session): %r" % e, flush=True)
            ok7 = None
        print("case7:", ("PASS" if ok7 else "FAIL") if ok7 is not None else "SKIP",
              flush=True)

        # --- case 8: TouchMouse unit test (capture writes, no uinput needed)
        # NOTE: reading a fresh uinput mouse from this test is racy because
        # phoc/libinput grabs new pointer devices; capture os.write instead.
        mod = None
        sink = None
        orig_write = None
        try:
            spec = importlib.util.spec_from_file_location("bridge_under_test", bridge)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sink = os.open("/dev/null", os.O_WRONLY)
            tm = mod.TouchMouse(sink, div=10, tap_slop=16)
            calls = []
            orig_write = mod.os.write

            def rec(f, b):
                calls.append(struct.unpack(EVENT_FMT, b))
                return orig_write(f, b)

            def frames(seq):
                return b"".join(struct.pack(EVENT_FMT, 0, 0, t, c, v)
                                for t, c, v in seq)

            mod.os.write = rec
            try:
                # drag: down (100,100) -> move (140,120) -> lift
                tm.feed(frames([
                    (EV_ABS, 0x2F, 0), (EV_ABS, 0x39, 1),
                    (EV_ABS, 0x35, 100), (EV_ABS, 0x36, 100), (EV_SYN, 0, 0),
                    (EV_ABS, 0x35, 140), (EV_ABS, 0x36, 120), (EV_SYN, 0, 0),
                    (EV_ABS, 0x39, -1), (EV_SYN, 0, 0),
                ]))
                drag = list(calls)
                calls.clear()
                # tap: down and lift with no movement -> BTN_LEFT press/release
                tm.feed(frames([
                    (EV_ABS, 0x2F, 0), (EV_ABS, 0x39, 2),
                    (EV_ABS, 0x35, 200), (EV_ABS, 0x36, 200), (EV_SYN, 0, 0),
                    (EV_ABS, 0x39, -1), (EV_SYN, 0, 0),
                ]))
                tap = list(calls)
            finally:
                mod.os.write = orig_write

            print("case8 drag writes:", drag, flush=True)
            print("case8 tap writes:", tap, flush=True)
            ok8a = (drag == [(0, 0, 2, 0, 1)] * 4 +
                            [(0, 0, 2, 1, 1)] * 2 + [(0, 0, 0, 0, 0)])
            ok8b = (tap == [(0, 0, 1, 0x110, 1), (0, 0, 0, 0, 0),
                            (0, 0, 1, 0x110, 0), (0, 0, 0, 0, 0)])
            ok8 = ok8a and ok8b
        except Exception as e:
            if mod is not None and not hasattr(mod, "TouchMouse"):
                print("case8 SKIP (keyboard-only bridge, no mouse)", flush=True)
                ok8 = None
            else:
                print("case8 ERROR: %r" % e, flush=True)
                ok8 = False
        finally:
            if sink is not None:
                os.close(sink)
        print("case8:", ("PASS" if ok8 else "FAIL") if ok8 is not None else "SKIP",
              flush=True)

        # --- case 9: touch_kbd arrow PULSES (real event format) -> relative motion
        amod = None
        try:
            spec = importlib.util.spec_from_file_location("bridge_arrow_test", bridge)
            amod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(amod)
            asink = os.open("/dev/null", os.O_WRONLY)
            atm = amod.TouchMouse(asink, arrow_step=4)
            acalls = []
            aorig = amod.os.write

            def arec(f, b):
                acalls.append(struct.unpack(EVENT_FMT, b))
                return aorig(f, b)

            def aframes(seq):
                return b"".join(struct.pack(EVENT_FMT, 0, 0, t, c, v)
                                for t, c, v in seq)

            amod.os.write = arec
            try:
                # LEFT pulse: value 1 then 0, each followed by SYN_REPORT.
                atm.feed(aframes([
                    (EV_KEY, 105, 1), (EV_SYN, 0, 0),
                    (EV_KEY, 105, 0), (EV_SYN, 0, 0),
                ]))
            finally:
                amod.os.write = aorig
                os.close(asink)
            print("case9 LEFT writes:", acalls, flush=True)
            # arrow_step=4 -> 4x REL_X(-1) then one SYN; release emits nothing
            ok9 = (acalls == [(0, 0, EV_REL, 0, -1)] * 4 +
                             [(0, 0, EV_SYN, 0, 0)])
        except Exception as e:
            if amod is not None and not hasattr(amod, "TouchMouse"):
                print("case9 SKIP (keyboard-only bridge, no mouse)", flush=True)
                ok9 = None
            else:
                print("case9 ERROR: %r" % e, flush=True)
                ok9 = False
        print("case9:", ("PASS" if ok9 else "FAIL") if ok9 is not None else "SKIP",
              flush=True)

        # --- case 10: touchpad arrow forwarder -> virtual keyboard ---------
        ok10 = None
        tmod = None
        try:
            spec = importlib.util.spec_from_file_location("bridge_touch_test", bridge)
            tmod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(tmod)
            if hasattr(tmod, "forward_touch_data"):
                sink = os.open("/dev/null", os.O_WRONLY)
                calls = []
                orig = tmod.os.write

                def rec(f, b):
                    calls.append(struct.unpack(EVENT_FMT, b))
                    return orig(f, b)

                tmod.os.write = rec
                try:
                    arrows = {tmod.KEY_UP, tmod.KEY_DOWN, tmod.KEY_LEFT, tmod.KEY_RIGHT}
                    # Two arrow pulses plus a non-arrow key and an EV_SYN; only
                    # the two arrows should be forwarded.
                    data = b"".join(struct.pack(EVENT_FMT, 0, 0, t, c, v)
                                    for t, c, v in [
                                        (EV_KEY, 105, 1), (EV_SYN, 0, 0),      # LEFT down
                                        (EV_KEY, 105, 0), (EV_SYN, 0, 0),      # LEFT up
                                        (EV_KEY, 103, 1), (EV_SYN, 0, 0),      # UP down
                                        (EV_KEY, 103, 0), (EV_SYN, 0, 0),      # UP up
                                        (EV_KEY, 30, 1),                        # 'a' -> ignore
                                        (EV_SYN, 0, 0),                         # SYN -> ignore
                                    ])
                    n = tmod.forward_touch_data(sink, data, arrows)
                finally:
                    tmod.os.write = orig
                    os.close(sink)
                print("case10 forwarded=%d writes=%s" % (n, calls), flush=True)
                # emit() appends a SYN_REPORT after each key, so 4 keys -> 8 writes.
                expected = [(0, 0, EV_KEY, 105, 1), (0, 0, EV_SYN, 0, 0),
                            (0, 0, EV_KEY, 105, 0), (0, 0, EV_SYN, 0, 0),
                            (0, 0, EV_KEY, 103, 1), (0, 0, EV_SYN, 0, 0),
                            (0, 0, EV_KEY, 103, 0), (0, 0, EV_SYN, 0, 0)]
                ok10 = (n == 4 and calls == expected)
            else:
                print("case10 SKIP (no forward_touch_data)", flush=True)
        except Exception as e:
            print("case10 ERROR: %r" % e, flush=True)
            ok10 = False
        print("case10:", ("PASS" if ok10 else "FAIL") if ok10 is not None else "SKIP",
              flush=True)

        results = (ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8, ok9, ok10)
        return 0 if all(x is not False for x in results) else 1
    finally:
        if out_fd is not None:
            os.close(out_fd)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        fcntl.ioctl(fake, IO_DESTROY)
        os.close(fake)


if __name__ == "__main__":
    sys.exit(main())
