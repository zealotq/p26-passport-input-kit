# 完整解决过程与方案

本文记录 Zinwa P26 / 黑莓 Passport 在 Droidian (Phosh) 上输入问题的完整解决过程：
从硬件事实、根因判定，到桥接实现、输入法加固、触摸板方向键，以及真机验证。
2026-11-24 完成，最终状态为「符号层键盘映射 + 触摸板方向键 + WowType 中文默认」。

---

## 1. 问题起点

P26 的硬键盘没有数字键，在 Phosh 登录界面输 sudo 密码时软键盘不会自动弹出，
用户无法输入数字。最初设想是「让软键盘弹出来点数字」，实测可用 D-Bus 强制
`sm.puri.OSK0.SetVisible(true)` 弹出 squeekboard，但每次登录都要弹一次、点屏输数字，
体验很差。目标是让硬键盘自己能输出数字和符号。

---

## 2. 硬件与底层事实（先坐实，别猜）

| 项目 | 实测结论 |
|---|---|
| 键盘形态 | 黑莓 Passport 原厂三排，29 键 = 26 字母 + 退格 + 回车 + 宽空格 |
| 没有 | 独立数字键、左/右 Alt、Shift、独立 SYM 键、Back/Home/Grave |
| 控制器 | aw95016（I2C 2-0020）；内核 dmesg 打印 `aw95016 report: key_code = N` |
| 真实键值 | 16(Q)、57(SPACE)、28(ENTER)、14(BACKSPACE)；字母覆盖 16-25 / 30-38 / 44-50 |
| 幽灵键码 | evdev 能力位图里的 222（KEY_ALTERASE）实际从未上报，不能拿来做层触发 |
| 用户纠正过的错误假设 | 没有 Alt；没有独立 SYM；222 不是真实按键；LOS 下键位本身是正确的 |

**教训 1：能力位图 ≠ 物理按键。** 位图只说明驱动「能上报」，坐实真实按键必须看驱动
dmesg 或 `keyd monitor` 的实际事件。

**教训 2：LOS 正确 ≠ Droidian 正确。** LOS 下数字/符号是 IME 层实现的
（SYM = Android keycode 58，Alt+W/E/R/S/D/F/Z/X/C → 1..9）；Android keycode 与
Linux evdev keycode 是两层，跨系统必须同时核对。BB10 原生靠符号选择器，也不能照搬。

---

## 3. 为什么 keyd 走不通（根因判定）

`keyd v2.5.0` 在 `resolve_device_capabilities()` 里要求设备能力位图包含
`KEY_1..KEY_0`；P26 没有数字键，于是被**静默丢弃**——keyd 配置层无论怎么写都不生效。
`keyd monitor` 也看不到这个设备。

结论：keyd 在此硬件上配置层无解，必须绕过它。

---

## 4. 核心方案：常驻 evdev → uinput 桥接

守护进程 `scripts/p26-digit-bridge.py`，以 root 常驻运行：

```
物理键盘 /dev/input/eventN
    │  EVIOCGRAB 独占抓取（防止事件双发）
    ▼
p26-digit-bridge.py
    │  普通键原样转发；空格长按进入符号层做映射
    ▼
uinput 虚拟键盘（声明数字/方向/标点/修饰键能力）
    ▼
fcitx5 → WowType → 应用
```

### 4.1 关键实现点

- **独占抓取**：`EVIOCGRAB = _IOW('E', 0x90, int)`，抓物理键盘避免事件重复。
- **虚拟键盘**：`UI_DEV_SETUP = _IOW('U', 3, 92)`；必须声明 `KEY_1..KEY_0`、
  方向键、标点、修饰键，否则上层收不到注入的符号。
- **符号层触发**：按住空格超过 `--hold`（默认 0.20s）进入符号层；轻点空格在
  release 时补发普通空格，打字不受影响。
- **组合键顺序**：`emit_seq()` 按正序 press、逆序 release，修饰键最后松开，避免
  `Ctrl+C` 变成 `Ctrl` 与 `C` 错序。
- **动作型键**：软键盘开关（空格+L）、截图（空格+J）只在按下时触发一次 D-Bus 调用，不发键码。
  截图调用 `org.gnome.Shell.Screenshot.Screenshot(false, false, path)`，一步把当前屏幕存为
  `~/图片/P26_YYYYmmdd_HhHmMsS.png`。没有采用 Print 键，因为 Phosh 把 Print 绑定到
  `show-screenshot-ui`，按一下只弹出截图界面、还需再点一次确认；D-Bus 方式按一下立即存文件。
- **越权 D-Bus**：桥接是 root，直连 droidian 用户总线会被拒；须用
  `sudo -n -u droidian env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus gdbus ...`。
- **keyd 冲突**：keyd 的 `[ids] *` 会抢抓本项目创建的 uinput 虚拟键盘，故 service 里
  声明 `Conflicts=keyd.service`，两者不能共存。

### 4.2 符号层映射

| 物理键 | 输出 |
|---|---|
| `q w e r t y u i o p` | `1 2 3 4 5 6 7 8 9 0` |
| `a s d f` | `↑ ↓ ← →` |
| `z` | `Esc` |
| `b n m` | `， 。 ？`（m = Shift+Slash） |
| `退格` | `Ctrl+Shift+Space` → WowType 中英切换 |
| `c v x` | `Ctrl+C / Ctrl+V / Ctrl+X` |
| `g` | `F5` |
| `l` | 唤出 / 隐藏软键盘 |

未进入符号层时所有按键原样转发，包括退格与回车。

---

## 5. 输入法体系加固（WowType）

三处独立问题，三处修复：

### 5.1 设置窗口空白 = GTK4 GSK gl 渲染后端坏

`wowtype-config` 是 Python 3.13 + GTK4 + libadwaita 脚本。矩阵实测
`GSK_RENDERER=gl` / 默认 / `ngl` 下 `window.present()` 全部 SIGSEGV（RC=-11），
`cairo` 3/3 正常；stderr 里的 `libpq_cust.so not found` 来自 Android libhybris 链，
不是 WowType 缺文件。

修复：桌面项 `Exec=env GSK_RENDERER=cairo wowtype-config`（见 `config/`）。
只影响该程序，不动系统渲染。

### 5.2 开机不切拼音 = autostart 假成功 + 缺图形环境

原脚本只调一次 `SetCurrentIM`，D-Bus 未就绪时假成功退出；systemd autostart 也不注入
`DISPLAY` / `WAYLAND_DISPLAY`。

修复：`config/fcitx5-autostart.sh` 显式补图形环境，循环最多 30 次，每次先回读
`CurrentInputMethod`，含 `wowtype` 才退出，否则 `SetCurrentIM wowtype` 重试。

### 5.3 打开新窗口又回落英文 = 启动默认看 Items/0

fcitx5 5.1.12 纯启动时取 `Groups/0/Items/0`（列表第一项），**不是** profile 的
`DefaultIM`。把 `~/.config/fcitx5/profile` 的 `Items/0=Name=wowtype`、
`Items/1=Name=keyboard-us` 对调后，任何形式的重启默认都是中文输入法。
另在 GUI 关闭 `org.gnome.desktop.input-sources per-window`（每窗口独立记忆输入源），
避免新窗口绕开 fcitx5 当前 IM 用回 xkb us。

### 5.4 中英切换键

WowType 自己的 zh/en 模式切换，绑定 `ToggleKey=Control+Shift+space`，由桥接的
「空格 + 退格」发出。注意这是 WowType 模式切换，不是切到 fcitx5 英文键盘。

---

## 6. 触摸板方向键（默认开，空格+k 可关）

### 6.1 事件格式的关键发现

`touch_kbd`（Synaptics DSX，I2C 6-0020）虽然能力位图声明了 ABS_MT_* 坐标，
但**实际滑动发的是 EV_KEY 方向键脉冲**：KEY_UP(103)/DOWN(108)/LEFT(105)/RIGHT(106)，
各带 SYN_REPORT，没有坐标流。这一点否决了所有基于坐标的鼠标方案。

### 6.2 当前实现

桥接按设备名找到 `touch_kbd` 节点并常驻 `EVIOCGRAB` 抓取，把它的方向键脉冲
**原样转发**到虚拟键盘——不生成鼠标事件、不解除原有 91 屏蔽规则、不动 seat0、
不重启 phoc。默认状态下用户可「随时」用触摸板当方向键，与空格符号层无关。

主循环把触摸板 fd 加入 `select`；空格长按超时改为显式 `now >= deadline` 判断，
避免触摸事件持续刷屏导致符号层超时失效。

### 6.3 空格+k 触控板开关

在符号层内按 `k`（物理键码 37）是**动作型**键：只切换一个布尔状态，不注入任何键码。
- 关闭时桥接**继续 grab 并排空**触摸板 fd，只是不转发方向键，事件不会泄漏给其他消费者；
- 状态写入 `/var/lib/p26-digit-bridge/touchpad-enabled`（`1`=开 / `0`=关），重启服务与重启设备都保持；
- 删除该状态文件即恢复默认开；日志输出 `touchpad arrows -> ON / OFF` 可供验证；
- 自测 case11 用隔离的临时状态文件验证 `<missing> -> 0 -> 1` 翻转且无 `k` 键码泄漏。

### 6.4 灵敏度

旋钮是 sysfs 节点：

```
/sys/devices/platform/soc/1101a000.i2c/i2c-6/6-0020/input/input4/key_x_threshold
/sys/devices/platform/soc/1101a000.i2c/i2c-6/6-0020/input/input4/key_y_threshold
```

数值越大越不灵敏。默认 `450/250`，最终调为 `600/350`（约降 25–30%），
写入立即生效，由 `config/93-p26-touchpad-sensitivity.rules` 持久化。
若仍偏灵敏，改规则数值（如 `800/450`）后 `udevadm control --reload-rules`；
可先 `echo N > .../key_x_threshold` 临时验证手感。

---

## 7. 部署

```sh
# 停用 keyd
sudo systemctl disable --now keyd

# 安装桥接
sudo install -m 755 scripts/p26-digit-bridge.py /usr/local/bin/p26-digit-bridge.py
sudo install -m 644 config/p26-digit-bridge.service /etc/systemd/system/p26-digit-bridge.service
sudo systemctl daemon-reload && sudo systemctl enable --now p26-digit-bridge

# 触摸板灵敏度（可选）
sudo install -m 644 config/93-p26-touchpad-sensitivity.rules /etc/udev/rules.d/

# 隐藏软键盘（可选，D-Bus，在用户会话里）
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus \
  busctl --user call sm.puri.OSK0 /sm/puri/OSK0 sm.puri.OSK0 SetVisible b false

# 输入法加固（可选）
sudo install -m 755 config/fcitx5-autostart.sh /usr/local/bin/fcitx5-autostart.sh
sudo install -m 644 config/io.github.Gloridust.WowType.Config.desktop \
  /usr/share/applications/io.github.Gloridust.WowType.Config.desktop
```

---

## 8. 验证与证据

1. 设备端自测 10 项全 PASS（`scripts/p26-bridge-selftest.py`）：
   - 空格长按 + q/p → `[(2,1),(2,0),(11,1),(11,0)]`
   - 轻点空格 → `[(57,1),(57,0)]`
   - 普通 a → `[(30,1),(30,0)]`
   - m → `?` → `[(42,1),(53,1),(53,0),(42,0)]`
   - 空格+退格 → `[(29,1),(42,1),(57,1),(57,0),(42,0),(29,0)]`
   - c/v/x → `[(29,1),(46,1),(46,0),(29,0)]` 等；g → `[(63,1),(63,0)]`
   - 真实翻转 squeekboard Visible false→true→false 并还原
   - 触摸板方向键脉冲透传
2. 服务日志 layer 串：`q=1 ... p=0 a=UP s=DOWN d=LEFT f=RIGHT z=ESC b=, n=. m=? g=F5 l=OSK ...`。
3. 真机用户确认：数字层可用，sudo 密码输入成功；触摸板方向键起作用。
4. 输入法判定以 `CurrentInputMethod`、`profile Items/0`、`fcitx5-remote` 与实际输入框为准。
   `fcitx5-remote -n` 在无焦点输入框时可能显示 keyboard-us/state=1，属正常，不能据此判故障。

---

## 9. 已否决方案

触摸板曾被尝试做成「空格层内鼠标」，后出现两个独立问题：滑动事件不是坐标而是方向键脉冲；
虚拟鼠标未纳入 phoc seat 导致光标不渲染。加 udev 规则纳入 seat0 后虽能看到光标，
用户仍明确否决「不要鼠标了」。所有鼠标改动已彻底清零，相关排障细节与经验教训见
[TROUBLESHOOTING.md](TROUBLESHOOTING.md)。
