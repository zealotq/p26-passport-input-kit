# 排障记录、勘误与被否决方案

本文保留解决过程中的关键弯路，便于后来者避开。按时间线整理，标注「覆盖旧结论」的地方以新结论为准。

---

## 1. 键码与硬件认知勘误

1. **能力位图 ≠ 物理按键**：`222`（KEY_ALTERASE）只在 P26 键盘能力位图出现，实际从未上报；不要用它做层触发。
2. **P26 没有 Alt / Shift / 独立 SYM**：早期误套 Q25 的 Alt+字母映射，被用户纠正；最终方案只用空格触发符号层。
3. **LOS 键位正确 ≠ Droidian 正确**：Android keycode 与 evdev keycode 是两层，必须同时核对。
4. **keyd 在此硬件配置层无解**：v2.5.0 要求数字键位图，P26 被静默丢弃；不要再试图改 keyd 配置。
5. **keyd 与桥接不能共存**：keyd 的 `[ids] *` 会抢抓桥接创建的 uinput 虚拟键盘，导致自测假 FAIL；service 用 `Conflicts=keyd.service`。

---

## 2. 桥接实现坑

6. **ioctl 常量不要硬编码历史值**：`EVIOCGRAB=_IOW('E',0x90,int)`、`EVIOCGNAME=_IOC(READ,'E',0x06,len)`、
   `UI_DEV_SETUP=_IOW('U',3,92)`；用 `_ioc()` 现算。
7. **触摸板事件不是 ABS_MT 坐标**：`touch_kbd` 能力位图声明了 ABS_MT_*，但真实滑动发
   `EV_KEY` 方向键脉冲（103/105/106/108），各带 SYN_REPORT，`first_abs=None`。
   基于坐标的鼠标方案一个事件都收不到（`rel_steps=0`）。
8. **动作型键只在按下触发**：软键盘开关若 press/release 都触发，会连续翻转两次等于没切。
9. **空格长按超时要显式判断**：触摸事件持续唤醒 select 时，若把超时塞在 `select()` 里，
   符号层永远进不去；改为循环开头 `now >= deadline` 判断。
10. **uinput 虚拟键盘在 Wayland 下不被 gnome-text-editor 接收**：打字截图无候选框是测试
    方法局限，不能据此判定输入法故障；判定运行态以 D-Bus 查询为准。

---

## 3. 输入法勘误

11. **WowType 有 arm64 包**：`0.7.4-1~debian13 arm64`，`/usr/lib/aarch64-linux-gnu/fcitx5/libwowtype.so`；
    `wowtype-check` 7 过 1 警告 0 失败。旧记忆「无 ARM 包」作废。
12. **ToggleKey 热重载坑**：`ReloadAddonConfig` 返回 0 也可能不生效；须按环境继承重启
    fcitx5，并以日志 `中英键=...` 为准。
13. **设置窗口空白 = GTK4 GSK gl 渲染后端坏**（覆盖旧结论「重启即可」）：gl/默认/ngl 下
    `window.present()` 必 SIGSEGV；cairo 正常。stderr 的 `libpq_cust.so not found` 来自
    Android libhybris 链，不是 WowType 缺文件。
14. **autostart 只调一次 SetCurrentIM 会假成功**：D-Bus 未就绪时返回成功但随后被覆盖；
    且 systemd autostart 不注入 DISPLAY/WAYLAND。脚本已改为补图形环境 + 循环回读。
15. **fcitx5 5.1.12 启动默认看 Items/0，不是 DefaultIM**（覆盖旧结论）：Items/0=keyboard-us
    时开机及 WowType 设置保存后的重启都会回落英文；已对调为 Items/0=wowtype。
    fcitx5 退出时会用最后输入上下文的 IM 覆盖 DefaultIM，但启动只看顺序。
16. **profile 改了不等于运行生效**：`fcitx5-remote -r` 只 reload 不切当前 IM；须
    `SetCurrentIM wowtype` 或在输入框实测。
17. **无输入上下文时**：`fcitx5-remote -n` 可能显示 keyboard-us、state=1，是没有焦点输入框
    时的正常表现；判定以 `CurrentInputMethod` 与实际输入框为准。
18. **GNOME per-window=true 会让新窗口用 xkb us**：每窗口独立记忆输入源会绕开 fcitx5 当前 IM；
    已在 GUI 关闭，值 false。

---

## 4. 被否决方案：触摸板当鼠标（已彻底清零）

以下是排障历史，方案已被用户否决且所有改动撤销，仅保留技术结论。

### 4.1 空格层内鼠标（13:50）

尝试新增独立 uinput 虚拟鼠标 `p26-space-mouse`，仅在空格符号层内抓取 `touch_kbd`：
MT slot0 拖动累积转 `REL_X/REL_Y`，轻点转 `BTN_LEFT`。自测 8/8 PASS。

### 4.2 看不到光标的两个根因（14:10）

- **根因一：事件格式判断错误**。真实会话输出
  `raw_ev=80 rel_steps=0 first_abs=None codes=['0/0:40','1/105:20','1/106:20']`：
  滑动发的是 EV_KEY 方向键脉冲而非坐标流。给 TouchMouse 加 `ARROW_TO_REL`
  （LEFT→REL_X-1、RIGHT→REL_X+1、UP→REL_Y-1、DOWN→REL_Y+1）后自测 9/9 PASS。
- **根因二：虚拟鼠标未纳入 phoc seat**（真正导致看不到光标）。P26 是 Android 输入栈 +
  libhybris，phoc 0.47 用 `WLR_BACKENDS=hwcomposer,libinput`，输入经 libseat/logind 后端。
  uinput 设备无 `ID_PATH_TAG`，logind 不交给 phoc → `phoc_seat_has_pointer()` 恒 false →
  按 phoc 源码 `phoc_cursor_show()` 门槛（has_pointer && has_pointer_motion），光标永不渲染。
  佐证：`grim -c` 截图只有地址栏闪烁的 78 像素差异，连 `wlrctl pointer move` 都不画光标。
- 修复：新建 `92-p26-virtual-mouse.rules` 给虚拟鼠标打
  `ENV{ID_SEAT}="seat0", TAG+="seat", TAG+="seat0"`，重启桥接 + `phosh.service` 后
  phoc 日志确认 `Adding pointer: p26-space-mouse` —— 光标显示前提首次满足。

### 4.3 三个实现坑

- `UI_SET_RELBIT` 是 **102**，误用 104 实际是 `UI_SET_MSCBIT`，导致 EV=7 但 B: REL=0、光标不动。
- TouchMouse 必须定义 `EV_ABS=3`，漏掉时真实 MT 事件一进符号层就 NameError 崩服务。
- 新建 uinput 鼠标的自测不能靠读节点断言：phoc/libinput 会抢先打开指针设备造成竞态假 FAIL，
  改为捕获 `os.write` 序列。
- udev 规则只在设备 add 时生效；重启桥接会销毁重建 uinput，phoc 手中旧设备失效，
  必须同步重启 `phosh.service` 才能重新认领（「光标又没了」反复出现的原因）。

### 4.4 用户否决与彻底清零（14:15 / 14:18）

用户明确：「不要鼠标了，把触控板复原」「触控板没生效，要把鼠标做的改变复原干净」。
执行清理：

- 删除 `92-p26-virtual-mouse.rules`；
- 桥接恢复纯键盘版（md5 `12d88696e8f21316a8358c0bac877499`），鼠标符号计数 = 0；
- 删除 `93-p26-touchpad-sensitivity.rules` 并把阈值恢复默认 450/250；
- 保留原有 `91-p26-touchpad-off.rules`（本就是初始状态，未改动）；
- 核验：无 `p26-space-mouse`、seat0 无鼠标、服务 active / NRestarts=0。

### 4.5 最终方案：触摸板方向键（14:24 起）

用户新需求「触摸板当方向键用、随时都能用」。桥接改为常驻 grab `touch_kbd`，
把其 EV_KEY 方向键脉冲原样转发到虚拟键盘——不生成鼠标、不解禁 91 规则、不动 seat0。
用户确认真机起作用后，又把灵敏度从默认 450/250 微调为 600/350
（新建 93 规则持久化）。这是当前生效状态，见 [SOLUTION.md](SOLUTION.md) 第 6 节。

---

## 5. 通用排障方法论

- `loginctl seat-status seat0` 是判断输入设备是否进入会话 seat 的最快依据。
- `grim -c` 截图 + ImageMagick `compare -metric AE` 可量化「光标是否随事件移动」。
- phoc 可由 `mobi.phosh.Phoc.DebugControl` D-Bus 的 `LogDomains` 开日志，但某些构建无输出。
- 判断输入法状态优先用 `gdbus CurrentInputMethod` 和 profile 顺序，不要只看 `fcitx5-remote -n`。
