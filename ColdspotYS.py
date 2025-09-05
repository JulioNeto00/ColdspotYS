# -*- coding: utf-8 -*-
from json import loads
from subprocess import run as sp_run, PIPE, STDOUT
from tkinter import Tk, Canvas
from keyboard import add_hotkey, unhook_all_hotkeys
from atexit import register as atexit_register
from signal import signal as sig
from signal import SIGINT, SIGTERM, SIGBREAK
from ctypes import WINFUNCTYPE, c_bool, c_uint, windll

HOTKEY_TOGGLE = "alt+c"
NAME = "ColdspotYS"
VSWITCH   = f"{NAME}-Switch"
VNIC_NAME = f"vEthernet ({VSWITCH})"

V4_NH   = "10.250.250.254"
V6_MAIN = ("fd00:250:250::1", 64)
V6_NH   = "fd00:250:250::fe"
FAKE_MAC = "02-00-5D-00-00-FE"

HUD_TEXT_ENABLED   = f"{NAME}  Enabled"
HUD_TEXT_DISABLED  = f"{NAME}  Disabled"
HUD_TEXT_ENABLING  = f"{NAME}  Enabling"
HUD_TEXT_DISABLING = f"{NAME}  Disabling"
HUD_FONT  = ("Segoe UI", 16, "bold")
HUD_BG    = "#000000"
HUD_ALPHA = 0.7
HUD_PADDING = (16, 12)
HUD_POSITION = "top-left"

PS = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
hotspot_on = False
root = None

_cleanup_done = False
_console_handler_ref = None

def log(msg):
    print(f"[{NAME}] {msg}", flush=True)

def run(ps):
    r = sp_run(PS + [ps], stdout=PIPE, stderr=STDOUT, text=True)
    out = (r.stdout or "").strip()
    return r.returncode, out

def ensure_hyperv_switch():
    rc, out = run(f"Get-VMSwitch -Name '{VSWITCH}' -ErrorAction SilentlyContinue | ConvertTo-Json")
    if not out or out in ("","null"):
        rc, out = run(f"New-VMSwitch -SwitchName '{VSWITCH}' -SwitchType Internal -ErrorAction Stop")
        if rc != 0:
            log("无法创建 Hyper-V 内部交换机；请在“启用或关闭 Windows 功能”启用 Hyper-V。")
            raise SystemExit(1)

def get_adapter(name):
    _, out = run(f"Get-NetAdapter -Name '{name}' -ErrorAction SilentlyContinue "
                  f"| Select-Object ifIndex,Name,MacAddress | ConvertTo-Json")
    if not out or out in ("","null"): return None
    j = loads(out);  j = j[0] if isinstance(j, list) else j
    return j

def ensure_ip(ifindex, ip, prefix):
    run(
        f"if(-not (Get-NetIPAddress -InterfaceIndex {ifindex} -IPAddress {ip} -ErrorAction SilentlyContinue)) "
        f"{{ New-NetIPAddress -InterfaceIndex {ifindex} -IPAddress {ip} -PrefixLength {prefix} -ErrorAction SilentlyContinue }}"
    )

def set_static_neighbor_v6(ifindex, ip, mac):
    run(f"netsh interface ipv6 delete neighbors {ifindex} {ip} all >$null 2>&1")
    run(f"netsh interface ipv6 add neighbors {ifindex} {ip} {mac}")

def set_static_neighbor_v4(name, ip, mac):
    run(f"netsh interface ipv4 delete neighbors \"{name}\" {ip} all >$null 2>&1")
    run(f"netsh interface ipv4 add neighbors \"{name}\" {ip} {mac}")

def check_neighbor_bits():
    adp = get_adapter(VNIC_NAME)
    if not adp:
        return (False, False)
    ifi = adp["ifIndex"]

    def _has_neighbor(ipaddr):
        rc, out = run(
            f"Get-NetNeighbor -InterfaceIndex {ifi} -IPAddress {ipaddr} -ErrorAction SilentlyContinue "
            f"| Select-Object IPAddress,LinkLayerAddress,State | ConvertTo-Json"
        )
        if not out or out in ("", "null"):
            return False
        j = loads(out)
        if isinstance(j, list):
            j = j[0] if j else None
        if not j:
            return False
        lla = (j.get("LinkLayerAddress") or "").strip().upper()
        return lla == FAKE_MAC.upper()

    v6_ok = _has_neighbor(V6_NH)
    v4_ok = _has_neighbor(V4_NH)
    return (v4_ok, v6_ok)

def enable_hotspot():
    ensure_hyperv_switch()
    adp = get_adapter(VNIC_NAME)
    if not adp:
        log("未找到虚拟网卡，请确认 Hyper-V 已启用。")
        raise SystemExit(1)
    ifi = adp["ifIndex"]
    name = adp["Name"]

    ensure_ip(ifi, V6_MAIN[0], V6_MAIN[1])

    set_static_neighbor_v6(ifi, V6_NH, FAKE_MAC)
    set_static_neighbor_v4(name, V4_NH, FAKE_MAC)

def disable_hotspot():
    adp = get_adapter(VNIC_NAME)
    if adp:
        ifi = adp["ifIndex"]
        name = adp["Name"]
        run(f"netsh interface ipv6 delete neighbors {ifi} {V6_NH} all >$null 2>&1")
        run(f"netsh interface ipv4 delete neighbors \"{name}\" {V4_NH} all >$null 2>&1")

def _canvas_draw_text_stroked(canvas, x, y, text, font, fill="#FFFFFF", stroke="#000000"):
    offsets = [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(1,-1),(-1,1),(1,1)]
    for dx, dy in offsets:
        canvas.create_text(x+dx, y+dy, text=text, anchor="nw", font=font, fill=stroke)
    return canvas.create_text(x, y, text=text, anchor="nw", font=font, fill=fill)

def _hud_show_text(text):
    if not hasattr(root, "_hud_canvas"):
        return
    canvas = root._hud_canvas
    canvas.delete("all")
    x0, y0 = HUD_PADDING
    t_id = _canvas_draw_text_stroked(canvas, x0, y0, text, HUD_FONT, fill="#FFFFFF", stroke="#000000")
    root.update_idletasks()
    bbox = canvas.bbox(t_id) if canvas.bbox(t_id) else (0,0,x0+200,y0+40)
    w = max(bbox[2] + HUD_PADDING[0], 160)
    h = max(bbox[3] + HUD_PADDING[1], 40)
    canvas.config(width=w, height=h)
    sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
    if   HUD_POSITION == "top-left":     x,y = 15, 12
    elif HUD_POSITION == "top-right":    x,y = sw - w - 15, 12
    elif HUD_POSITION == "bottom-left":  x,y = 15, sh - h - 15
    else:                                x,y = sw - w - 15, sh - h - 15
    root.geometry(f"{w}x{h}+{x}+{y}")

def init_hud():
    global root
    root = Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", HUD_ALPHA)
    root.configure(bg=HUD_BG)

    canvas = Canvas(root, bg=HUD_BG, highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    root._hud_canvas = canvas

    def _on_close():
        cleanup()
        try:
            root.destroy()
        except Exception:
            pass
    root.protocol("WM_DELETE_WINDOW", _on_close)

    _hud_show_text(HUD_TEXT_DISABLED)

def hud_set(state_on: bool):
    _hud_show_text(HUD_TEXT_ENABLED if state_on else HUD_TEXT_DISABLED)

def register_hotkey():
    log(f"注册快捷键：{HOTKEY_TOGGLE}")
    add_hotkey(HOTKEY_TOGGLE, on_toggle_hotspot)

def on_toggle_hotspot():
    global hotspot_on
    if not hotspot_on:
        log("快捷键触发，尝试启用")
        try:
            _hud_show_text(HUD_TEXT_ENABLING)
            enable_hotspot()
            hotspot_on = True
            hud_set(True)
            log("启用成功")
        except Exception as e:
            log(f"启用失败：{e}")
            hud_set(False)
    else:
        log("快捷键触发，尝试禁用")
        try:
            _hud_show_text(HUD_TEXT_DISABLING)
            disable_hotspot()
            hotspot_on = False
            hud_set(False)
            log("禁用成功")
        except Exception as e:
            log(f"禁用失败：{e}")
            hud_set(True)

def cleanup():
    global _cleanup_done, hotspot_on
    if _cleanup_done:
        return
    _cleanup_done = True
    try:
        if hotspot_on:
            log("收到退出信号，正在还原设置…")
            disable_hotspot()
            hotspot_on = False
            log("已还原设置。")
    except Exception as e:
        log(f"还原失败：{e}")

    try:
        unhook_all_hotkeys()
    except Exception:
        pass
    try:
        if root:
            try:
                root.quit()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass
    except Exception:
        pass

def _install_exit_hooks():
    atexit_register(cleanup)

    try:
        sig(SIGINT,  lambda s, f: cleanup())
    except Exception:
        pass
    try:
        sig(SIGTERM, lambda s, f: cleanup())
    except Exception:
        pass
    if SIGBREAK is not None:
        try:
            sig(SIGBREAK, lambda s, f: cleanup())
        except Exception:
            pass

    try:
        PHANDLER_ROUTINE = WINFUNCTYPE(c_bool, c_uint)
        def _handler(ctrl_type):
            cleanup()
            return True
        global _console_handler_ref
        _console_handler_ref = PHANDLER_ROUTINE(_handler)
        windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)
    except Exception:
        pass

def main():
    global hotspot_on
    _install_exit_hooks()
    init_hud()

    try:
        log("正在进行启动时自检...")
        v4_ok, v6_ok = check_neighbor_bits()
        if v4_ok and v6_ok:
            hotspot_on = True
            log("启动时自检：已启用")
            hud_set(True)
        elif v4_ok or v6_ok:
            log("启动时自检：检测到部分启用，正在还原…")
            disable_hotspot()
            hotspot_on = False
            hud_set(False)
            log("已还原为未启用状态。")
        else:
            hotspot_on = False
            log("启动时自检：未启用")
            hud_set(False)
    except Exception as e:
        log(f"启动时自检失败：{e}")
        hud_set(False)
        hotspot_on = False

    register_hotkey()
    try:
        root.mainloop()
    finally:
        cleanup()

if __name__ == "__main__":
    main()
