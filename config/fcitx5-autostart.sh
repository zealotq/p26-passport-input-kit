#!/bin/sh
# 启动 fcitx5 并确保默认输入法为 wowtype。
# 关键：必须回读 CurrentInputMethod 确认为 wowtype 才算成功；
# 只调 SetCurrentIM 一次可能在 fcitx5 未就绪时被覆盖回 keyboard-us。
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/32011/bus}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/32011}"
# systemd autostart 不注入图形环境；显式补上，保证 GTK 输入法模块可用
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
fcitx5 -d >/dev/null 2>&1
i=0
while [ $i -lt 30 ]; do
  cur=$(gdbus call --session --dest org.fcitx.Fcitx5 --object-path /controller \
        --method org.fcitx.Fcitx.Controller1.CurrentInputMethod 2>/dev/null)
  case "$cur" in
    *wowtype*) exit 0 ;;
  esac
  gdbus call --session --dest org.fcitx.Fcitx5 --object-path /controller \
        --method org.fcitx.Fcitx.Controller1.SetCurrentIM wowtype >/dev/null 2>&1
  i=$((i+1))
  sleep 1
done
