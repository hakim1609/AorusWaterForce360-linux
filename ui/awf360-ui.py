#!/usr/bin/env python3
"""WaterForce 360 — CPU monitor and control panel for the AorusWaterForce360-linux service."""

import os
import subprocess
import sys
import threading
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

APP_ID = "com.github.fourgl.awf360"
UNIT = "AorusWaterForce360-linux"
CTL = "/usr/local/bin/awf360-ctl"
USB_ID = ("1044", "7a4d")
DEFAULT_SENSOR, DEFAULT_INTERVAL = "Tccd1", 5
HISTORY = 120  # seconds of graph
# RPM ranges must match the daemon (main.go) and the root helper (awf360-ctl)
FAN_RANGE, PUMP_RANGE, RPM_STEP = (750, 2750), (1600, 3200), 50


# ---------- system readers (no root needed) ----------

def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_cpu_sensors():
    """label -> tempN_input path of the k10temp/coretemp hwmon, same lookup as the daemon."""
    base = "/sys/class/hwmon"
    sensors = {}
    for d in sorted(os.listdir(base)):
        if read(f"{base}/{d}/name") not in ("k10temp", "coretemp"):
            continue
        for f in sorted(os.listdir(f"{base}/{d}")):
            if f.startswith("temp") and f.endswith("_label"):
                sensors[read(f"{base}/{d}/{f}")] = f"{base}/{d}/{f.replace('_label', '_input')}"
    return sensors


def read_temp(path):
    v = read(path)
    return int(v) / 1000 if v else None


def cpu_mhz():
    """First "cpu MHz" line — exactly what the daemon sends to the display."""
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            return float(line.split(":", 1)[1])
    return 0.0


def cpu_times():
    parts = read("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(v) for v in parts]
    return vals[3], sum(vals)


def read_cooler():
    """Readings of the gigabyte_waterforce hwmon: [(key, title, text)], None if the driver isn't bound.
    Each read makes the driver query the cooler over USB, so call it off the UI thread."""
    base = "/sys/class/hwmon"
    for d in os.listdir(base):
        if read(f"{base}/{d}/name") != "waterforce":
            continue
        p = f"{base}/{d}"
        files = sorted(os.listdir(p))
        rows = []
        for f in files:
            if f.startswith("fan") and f.endswith("_input"):
                n = f[3:-6]
                v = read(f"{p}/{f}")
                rows.append((f, read(f"{p}/fan{n}_label") or f"Fan {n}", f"{v} RPM" if v else "—"))
        for f in files:
            if f.startswith("pwm") and f[3:].isdigit():
                n = f[3:]
                v = read(f"{p}/{f}")
                title = (read(f"{p}/fan{n}_label") or f"Fan {n}") + " duty"
                rows.append((f, title, f"{round(int(v) * 100 / 255)}%" if v else "—"))
        for f in files:
            if f.startswith("temp") and f.endswith("_input"):
                n = f[4:-6]
                t = read_temp(f"{p}/{f}")
                rows.append((f, read(f"{p}/temp{n}_label") or f"Temperature {n}",
                             "—" if t is None else f"{t:.1f} °C"))
        return rows
    return None


def device_connected():
    base = "/sys/bus/usb/devices"
    for d in os.listdir(base):
        if (read(f"{base}/{d}/idVendor"), read(f"{base}/{d}/idProduct")) == USB_ID:
            return True
    return False


def systemctl(*args):
    r = subprocess.run(["systemctl", *args, UNIT], capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def service_config():
    env = dict(
        kv.split("=", 1) for kv in systemctl("show", "-p", "Environment", "--value").split() if "=" in kv
    )
    sensor = env.get("AWF_SENSOR") or DEFAULT_SENSOR
    try:
        interval = int(env.get("AWF_INTERVAL", DEFAULT_INTERVAL))
    except ValueError:
        interval = DEFAULT_INTERVAL
    cooling = []
    for name in ("AWF_FAN_RPM", "AWF_PUMP_RPM"):
        try:
            cooling.append(int(env.get(name, 0)))
        except ValueError:
            cooling.append(0)
    return (sensor, interval), tuple(cooling)


# ---------- UI ----------

class StatCard(Gtk.Box):
    def __init__(self, caption):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("card")
        self.set_hexpand(True)
        self.value = Gtk.Label(label="—", css_classes=["title-1", "numeric"])
        self.value.set_margin_top(14)
        self.caption = Gtk.Label(label=caption, css_classes=["dim-label", "caption"])
        self.caption.set_margin_bottom(14)
        self.append(self.value)
        self.append(self.caption)

    def set_level(self, level):
        for c in ("success", "warning", "error"):
            self.value.remove_css_class(c)
        if level:
            self.value.add_css_class(level)


class SpeedControl:
    """"Manual <x> speed" switch plus an RPM slider; 0 RPM means the cooler's own profile."""

    def __init__(self, group, name, rpm_range, rpm, on_change):
        lo, hi = rpm_range
        self.on_change = on_change
        self.switch = Adw.SwitchRow(title=f"Manual {name.lower()} speed", active=rpm > 0)
        group.add(self.switch)

        row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=10, margin_bottom=6, margin_start=12, margin_end=12)
        head = Gtk.Box()
        head.append(Gtk.Label(label=f"{name} speed", xalign=0, hexpand=True))
        self.value = Gtk.Label(css_classes=["numeric", "dim-label"])
        head.append(self.value)
        box.append(head)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, RPM_STEP)
        self.scale.set_draw_value(False)
        for mark in (lo, (lo + hi) // 2, hi):
            self.scale.add_mark(mark, Gtk.PositionType.BOTTOM, f"{mark}")
        self.scale.set_value(rpm or (lo + hi) // 2)
        box.append(self.scale)
        row.set_child(box)
        group.add(row)

        self.timer = 0
        self.refresh()
        self.switch.connect("notify::active", self.changed_now)
        self.scale.connect("value-changed", self.changed_later)

    def rpm(self):
        """Selected RPM, or 0 when the switch is off."""
        if not self.switch.get_active():
            return 0
        return int(round(self.scale.get_value() / RPM_STEP) * RPM_STEP)

    def refresh(self):
        self.scale.set_sensitive(self.switch.get_active())
        self.value.set_label(f"{self.rpm()} RPM" if self.rpm() else "Auto")

    def changed_now(self, *_):
        self.timer = 0
        self.refresh()
        self.on_change()
        return False

    def changed_later(self, *_):
        self.refresh()
        # a drag fires many events: apply once the slider rests
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(600, self.changed_now)


class Graph(Gtk.DrawingArea):
    """Temperature line over a filled CPU-load area, last HISTORY seconds."""

    def __init__(self, temps, loads):
        super().__init__()
        self.temps, self.loads = temps, loads
        self.set_content_height(160)
        self.set_hexpand(True)
        self.set_draw_func(self.draw)

    def draw(self, _area, cr, w, h):
        fg = self.get_color()
        pad_l, pad_r, pad_t, pad_b = 34, 8, 8, 18
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        lo, hi = 20, 100  # °C / % axis

        def y(v):
            return pad_t + gh * (1 - (min(max(v, lo), hi) - lo) / (hi - lo))

        def x(i):
            return pad_l + gw * i / (HISTORY - 1)

        cr.select_font_face("Sans")
        cr.set_font_size(10)
        cr.set_line_width(1)
        for v in (40, 60, 80, 100):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
            cr.move_to(pad_l, y(v))
            cr.line_to(w - pad_r, y(v))
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(4, y(v) + 3)
            cr.show_text(f"{v}°")
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.move_to(pad_l, h - 4)
        cr.show_text(f"−{HISTORY // 60} min")
        cr.move_to(w - pad_r - 24, h - 4)
        cr.show_text("now")

        offset = HISTORY - len(self.loads)
        if self.loads:  # load: filled area, % mapped onto the same axis
            cr.set_source_rgba(0.40, 0.55, 0.95, 0.22)
            cr.move_to(x(offset), pad_t + gh)
            for i, v in enumerate(self.loads):
                cr.line_to(x(offset + i), pad_t + gh * (1 - v / 100))
            cr.line_to(x(offset + len(self.loads) - 1), pad_t + gh)
            cr.close_path()
            cr.fill()

        pts = [(i, t) for i, t in enumerate(self.temps) if t is not None]
        if len(pts) > 1:
            cr.set_source_rgba(0.93, 0.33, 0.23, 1)
            cr.set_line_width(2)
            cr.move_to(x(offset + pts[0][0]), y(pts[0][1]))
            for i, t in pts[1:]:
                cr.line_to(x(offset + i), y(t))
            cr.stroke()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="WaterForce 360", default_width=520, default_height=820)
        self.sensors = find_cpu_sensors()
        self.temps = deque(maxlen=HISTORY)
        self.loads = deque(maxlen=HISTORY)
        self.prev_cpu = cpu_times()
        self.syncing = False
        self.applied, self.cooling = service_config()

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.scroll = scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=640)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        clamp.set_child(box)
        scroll.set_child(clamp)
        view.set_content(scroll)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # stats
        stats = Gtk.Box(spacing=10, homogeneous=True)
        self.temp_card = StatCard("Temperature")
        self.freq_card = StatCard("Frequency")
        self.load_card = StatCard("Load")
        for c in (self.temp_card, self.freq_card, self.load_card):
            stats.append(c)
        box.append(stats)

        # graph
        graph_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"])
        self.graph = Graph(self.temps, self.loads)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.graph, f"set_margin_{side}")(10)
        graph_card.append(self.graph)
        legend = Gtk.Label(
            use_markup=True, css_classes=["caption"], margin_bottom=10,
            label='<span foreground="#ed553b">━━</span> temperature   '
                  '<span foreground="#6b8cf2">▇▇</span> load, %',
        )
        graph_card.append(legend)
        box.append(graph_card)

        # all sensors
        sensors_group = Adw.PreferencesGroup(title="CPU sensors")
        self.sensor_rows = {}
        for label in self.sensors:
            row = Adw.ActionRow(title=label)
            value = Gtk.Label(css_classes=["numeric"])
            row.add_suffix(value)
            sensors_group.add(row)
            self.sensor_rows[label] = value
        if not self.sensors:
            sensors_group.add(Adw.ActionRow(title="k10temp/coretemp not found"))
        box.append(sensors_group)

        # cooler readings from the kernel driver
        self.cooler_group = Adw.PreferencesGroup(title="Cooler")
        self.cooler_rows = {}
        self.cooler_missing = Adw.ActionRow(
            title="No readings",
            subtitle="The waterforce kernel driver isn't bound to the cooler. "
                     "Reinstall the service (ui/install.sh) or replug the cooler.")
        self.cooler_group.add(self.cooler_missing)
        box.append(self.cooler_group)
        self.cooler_polling = False

        # fan / pump speed, sent to the cooler by the display service when it starts
        control = Adw.PreferencesGroup(
            title="Cooling control",
            description="Reverse-engineered commands, verified on this cooler. The display service "
                        "sends them when it starts, so it has to be running. Auto returns the "
                        "cooler to its factory profile. Check the Cooler readings above for the effect.")
        fan_rpm, pump_rpm = self.cooling
        self.fan = SpeedControl(control, "Fan", FAN_RANGE, fan_rpm, self.on_cooling_changed)
        self.pump = SpeedControl(control, "Pump", PUMP_RANGE, pump_rpm, self.on_cooling_changed)
        box.append(control)

        # display service
        group = Adw.PreferencesGroup(title="Cooler display")
        self.apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action", "pill"],
                                    valign=Gtk.Align.CENTER, sensitive=False)
        self.apply_btn.connect("clicked", self.on_apply)
        group.set_header_suffix(self.apply_btn)

        self.device_row = Adw.ActionRow(title="Device")
        group.add(self.device_row)

        self.run_row = Adw.SwitchRow(title="Show data on display")
        self.run_row.connect("notify::active", self.on_run_toggled)
        group.add(self.run_row)

        self.boot_row = Adw.SwitchRow(title="Start on boot")
        self.boot_row.connect("notify::active", self.on_boot_toggled)
        group.add(self.boot_row)

        labels = list(self.sensors) or [DEFAULT_SENSOR]
        if self.applied[0] not in labels:
            labels.append(self.applied[0])
        self.sensor_labels = labels
        self.sensor_row = Adw.ComboRow(title="Display sensor", model=Gtk.StringList.new(labels))
        self.sensor_row.set_selected(labels.index(self.applied[0]))
        self.sensor_row.connect("notify::selected", self.on_setting_changed)
        group.add(self.sensor_row)

        self.interval_row = Adw.SpinRow.new_with_range(1, 60, 1)
        self.interval_row.set_title("Display refresh, seconds")
        self.interval_row.set_value(self.applied[1])
        self.interval_row.connect("notify::value", self.on_setting_changed)
        group.add(self.interval_row)
        box.append(group)

        self.tick()
        self.poll_service()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add_seconds(2, self.poll_service)

    # --- periodic updates ---

    def tick(self):
        idle, total = cpu_times()
        d_total = total - self.prev_cpu[1]
        load = 100 * (1 - (idle - self.prev_cpu[0]) / d_total) if d_total else 0
        self.prev_cpu = (idle, total)

        values = {label: read_temp(p) for label, p in self.sensors.items()}
        for label, v in values.items():
            self.sensor_rows[label].set_label("—" if v is None else f"{v:.1f} °C")

        sensor = self.applied[0]
        temp = values.get(sensor)
        self.temp_card.caption.set_label(f"Temperature · {sensor}")
        if temp is None:
            self.temp_card.value.set_label("—")
            self.temp_card.set_level(None)
        else:
            self.temp_card.value.set_label(f"{temp:.0f}°")
            self.temp_card.set_level("success" if temp < 60 else "warning" if temp < 80 else "error")

        mhz = cpu_mhz()
        self.freq_card.value.set_label(f"{mhz / 1000:.1f} GHz")
        self.load_card.value.set_label(f"{load:.0f}%")

        self.temps.append(temp)
        self.loads.append(load)
        self.graph.queue_draw()
        return True

    def poll_service(self):
        connected = device_connected()
        self.device_row.set_subtitle("Connected (1044:7a4d)" if connected else "Not found on USB")
        active = systemctl("is-active")
        enabled = systemctl("is-enabled") == "enabled"
        self.syncing = True
        self.run_row.set_active(active == "active")
        self.boot_row.set_active(enabled)
        self.syncing = False
        self.run_row.set_subtitle({
            "active": "Service running",
            "inactive": "Service stopped",
            "failed": "Service failed — see journalctl -u " + UNIT,
            "activating": "Starting…",
        }.get(active, active))
        self.poll_cooler()
        return True

    def poll_cooler(self):
        if self.cooler_polling:
            return
        self.cooler_polling = True

        def work():
            rows = read_cooler()
            GLib.idle_add(self.show_cooler, rows)

        threading.Thread(target=work, daemon=True).start()

    def show_cooler(self, rows):
        self.cooler_polling = False
        rows = rows or []
        keys = {key for key, _, _ in rows}
        for key in list(self.cooler_rows):
            if key not in keys:
                self.cooler_group.remove(self.cooler_rows.pop(key)[0])
        for key, title, text in rows:
            if key not in self.cooler_rows:
                row = Adw.ActionRow(title=title)
                value = Gtk.Label(css_classes=["numeric"])
                row.add_suffix(value)
                self.cooler_group.add(row)
                self.cooler_rows[key] = (row, value)
            self.cooler_rows[key][1].set_label(text)
        self.cooler_missing.set_visible(not rows)
        return False

    # --- controls ---

    def selected_settings(self):
        return self.sensor_labels[self.sensor_row.get_selected()], int(self.interval_row.get_value())

    def on_setting_changed(self, *_):
        self.apply_btn.set_sensitive(self.selected_settings() != self.applied)

    def on_apply(self, _btn):
        sensor, interval = self.selected_settings()

        def done(ok):
            if ok:
                self.applied = (sensor, interval)
                self.on_setting_changed()
                self.toast("Settings applied")

        self.ctl(["apply", sensor, str(interval), *map(str, self.cooling)], done)

    def on_cooling_changed(self):
        wanted = (self.fan.rpm(), self.pump.rpm())
        if wanted == self.cooling:
            return
        sensor, interval = self.applied

        def done(ok):
            if ok:
                self.cooling = wanted
                fan, pump = (f"{v} RPM" if v else "auto" for v in wanted)
                self.toast(f"Fan {fan}, pump {pump}")

        self.ctl(["apply", sensor, str(interval), *map(str, wanted)], done)

    def on_run_toggled(self, row, _pspec):
        if not self.syncing:
            self.ctl(["start" if row.get_active() else "stop"])

    def on_boot_toggled(self, row, _pspec):
        if not self.syncing:
            self.ctl(["enable" if row.get_active() else "disable"])

    def ctl(self, args, done=None):
        """Run the root helper via pkexec without blocking the UI."""
        try:
            proc = Gio.Subprocess.new(["pkexec", CTL, *args],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self.toast(f"Could not run pkexec: {e.message}")
            return

        def finished(p, res):
            _, out, _ = p.communicate_utf8_finish(res)
            ok = p.get_successful()
            if not ok:
                self.toast((out or "").strip().splitlines()[-1:][0] if out and out.strip()
                           else "Action cancelled or failed")
            if done:
                done(ok)
            self.poll_service()

        proc.communicate_utf8_async(None, None, finished)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))


class App(Adw.Application):
    def __init__(self, screenshot=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE if screenshot else 0)
        self.screenshot = screenshot

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()
        if self.screenshot:
            win.set_default_size(520, 1640)  # tall enough to capture every section
            GLib.timeout_add(4000, self.save_screenshot, win)

    def save_screenshot(self, win):
        if not getattr(self, "scrolled", False):  # AWF_SHOT_SCROLL=bottom captures the lower half
            adj = win.scroll.get_vadjustment()
            adj.set_value(adj.get_upper() if os.environ.get("AWF_SHOT_SCROLL") == "bottom" else 0)
            self.scrolled = True
            GLib.timeout_add(500, self.save_screenshot, win)
            return False
        paintable = Gtk.WidgetPaintable(widget=win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, win.get_width(), win.get_height())
        node = snap.to_node()
        if node is None:  # not drawn yet
            GLib.timeout_add(500, self.save_screenshot, win)
            return False
        win.get_renderer().render_texture(node, None).save_to_png(self.screenshot)
        self.quit()
        return False


if __name__ == "__main__":
    shot = None
    if len(sys.argv) > 2 and sys.argv[1] == "--screenshot":
        shot = sys.argv[2]
    sys.exit(App(shot).run([sys.argv[0]]))
