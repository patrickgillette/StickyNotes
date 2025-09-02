import sys, json, os, ctypes, time
from ctypes import wintypes
import ctypes.wintypes as wt
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets

APP_NAME = "ActiveSticky"
STATE_PATH = Path(os.getenv("APPDATA", ".")) / APP_NAME / "state.json"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---- Call this BEFORE creating QApplication ----
# High-DPI rounding policy must be set pre-app
try:
    QtCore.QCoreApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
except Exception:
    pass

# ---- Win32 bits ----
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# GWL/WS constants
GWL_EXSTYLE = -20
WS_EX_LAYERED      = 0x00080000
WS_EX_TRANSPARENT  = 0x00000020
WS_EX_TOOLWINDOW   = 0x00000080
WS_EX_NOACTIVATE   = 0x08000000

WM_HOTKEY = 0x0312
WM_CLOSE  = 0x0010

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004

VK_LEFT, VK_RIGHT, VK_UP, VK_DOWN = 0x25, 0x27, 0x26, 0x28
HK_EDIT, HK_CLICK, HK_LEFT, HK_RIGHT, HK_UP, HK_DOWN, HK_MINIMIZE, HK_OPACITY_UP, HK_OPACITY_DOWN = 1, 2, 3, 4, 5, 6, 7, 8, 9

# Per-Monitor-V2 DPI aware (Windows 10+), with fallback — also must be pre-app
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
except Exception:
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        pass

# Prefer *Ptr functions for 64-bit safety; define ctypes signatures
try:
    GetWindowLongPtrW = user32.GetWindowLongPtrW
    SetWindowLongPtrW = user32.SetWindowLongPtrW
    GetWindowLongPtrW.restype = ctypes.c_longlong
    GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    SetWindowLongPtrW.restype = ctypes.c_longlong
    SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
except AttributeError:
    GetWindowLongPtrW = user32.GetWindowLongW
    SetWindowLongPtrW = user32.SetWindowLongW
    GetWindowLongPtrW.restype = ctypes.c_long
    GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    SetWindowLongPtrW.restype = ctypes.c_long
    SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]

# RegisterHotKey/UnregisterHotKey signatures
user32.RegisterHotKey.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]

# EnumWindows callback type (correct one)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes   = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

def _enum_windows_titles():
    titles = []
    @WNDENUMPROC
    def _cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append((hwnd, buf.value))
        except Exception:
            pass
        return True
    user32.EnumWindows(_cb, 0)
    return titles

def close_existing_instance_by_title(title: str, wait_ms: int = 2000) -> bool:
    """If a window titled `title` exists, send WM_CLOSE and wait briefly."""
    found = False
    for hwnd, text in _enum_windows_titles():
        if text == title:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            found = True
    if not found:
        return False
    end = time.time() + (wait_ms / 1000.0)
    while time.time() < end:
        still_there = any(text == title for _, text in _enum_windows_titles())
        if not still_there:
            break
        time.sleep(0.05)
    return True

# ---- State helpers ----
DEFAULT_STATE = {
    "text": "Active task: (Ctrl+Alt+T to edit)",
    "x": 80, "y": 80, "w": 350, "h": 120,
    "click_through": False,
    "opacity": 0.85,
    "font_pt": 13.0,
    "theme": "dark",  # dark, light, blue, green, amber
    "auto_hide_timer": 0,  # minutes, 0 = never
    "word_wrap": True,
    "font_family": "Segoe UI"
}

THEMES = {
    "dark":  {"bg": (25, 25, 25, 200),  "text": "white", "border": (255, 255, 255, 140)},
    "light": {"bg": (245, 245, 245, 220), "text": "black", "border": (100, 100, 100, 180)},
    "blue":  {"bg": (30, 50, 90, 200), "text": "white", "border": (100, 150, 255, 160)},
    "green": {"bg": (25, 60, 40, 200), "text": "white", "border": (100, 255, 150, 160)},
    "amber": {"bg": (80, 50, 20, 200), "text": "white", "border": (255, 200, 100, 160)},
}

def load_state():
    try:
        return { **DEFAULT_STATE, **json.loads(STATE_PATH.read_text(encoding="utf-8")) }
    except Exception:
        return DEFAULT_STATE.copy()

def save_state(d):
    try:
        STATE_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass

def delete_state():
    try:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
    except Exception:
        pass

# ---- Native event filter for global hotkeys ----
class HotkeyFilter(QtCore.QAbstractNativeEventFilter):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler
    def nativeEventFilter(self, eventType, message):
        if eventType == "windows_generic_MSG":
            try:
                addr = int(message) if isinstance(message, int) else int(message.__int__())
                msg = ctypes.cast(addr, ctypes.POINTER(wt.MSG)).contents
                if msg.message == WM_HOTKEY:
                    self.handler(msg.wParam)
            except KeyboardInterrupt:
                # Ignore Ctrl+C in console without spamming errors
                return False, 0
            except Exception:
                return False, 0
        return False, 0

# ---- Settings Dialog ----
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.sticky = parent
        self.setWindowTitle("ActiveSticky Settings")
        self.setModal(True)
        self.resize(400, 500)

        layout = QtWidgets.QVBoxLayout(self)

        # Font settings
        font_group = QtWidgets.QGroupBox("Font")
        font_layout = QtWidgets.QFormLayout(font_group)
        self.font_family = QtWidgets.QFontComboBox()
        self.font_family.setCurrentFont(QtGui.QFont(self.sticky.state["font_family"]))
        self.font_size = QtWidgets.QSpinBox()
        self.font_size.setRange(8, 72)
        self.font_size.setValue(int(self.sticky.state["font_pt"]))
        font_layout.addRow("Family:", self.font_family)
        font_layout.addRow("Size:", self.font_size)

        # Theme settings
        theme_group = QtWidgets.QGroupBox("Theme")
        theme_layout = QtWidgets.QFormLayout(theme_group)
        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.addItems(list(THEMES.keys()))
        self.theme_combo.setCurrentText(self.sticky.state["theme"])
        self.opacity_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(int(self.sticky.state["opacity"] * 100))
        self.opacity_label = QtWidgets.QLabel(f"{int(self.sticky.state['opacity'] * 100)}%")
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_label.setText(f"{v}%"))
        self.opacity_slider.valueChanged.connect(lambda v: self.sticky.setWindowOpacity(v / 100.0))
        opacity_layout = QtWidgets.QHBoxLayout()
        opacity_layout.addWidget(self.opacity_slider)
        opacity_layout.addWidget(self.opacity_label)
        theme_layout.addRow("Theme:", self.theme_combo)
        theme_layout.addRow("Opacity:", opacity_layout)

        # Behavior settings
        behavior_group = QtWidgets.QGroupBox("Behavior")
        behavior_layout = QtWidgets.QFormLayout(behavior_group)
        self.word_wrap = QtWidgets.QCheckBox()
        self.word_wrap.setChecked(self.sticky.state["word_wrap"])
        self.auto_hide = QtWidgets.QSpinBox()
        self.auto_hide.setRange(0, 1440)
        self.auto_hide.setValue(self.sticky.state["auto_hide_timer"])
        self.auto_hide.setSuffix(" minutes (0 = never)")
        behavior_layout.addRow("Word wrap:", self.word_wrap)
        behavior_layout.addRow("Auto-hide after:", self.auto_hide)

        # Hotkeys info
        hotkeys_group = QtWidgets.QGroupBox("Hotkeys")
        hotkeys_layout = QtWidgets.QVBoxLayout(hotkeys_group)
        hotkey_text = """• Ctrl+Alt+T: Edit text
• Ctrl+Alt+Space: Toggle click-through
• Ctrl+Alt+M: Minimize/Hide
• Ctrl+Alt+Arrows: Move window
• Ctrl+Shift+Alt+↑/↓: Adjust opacity
• Right-click: Context menu"""
        hotkeys_layout.addWidget(QtWidgets.QLabel(hotkey_text))

        layout.addWidget(font_group)
        layout.addWidget(theme_group)
        layout.addWidget(behavior_group)
        layout.addWidget(hotkeys_group)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_settings(self):
        return {
            "font_family": self.font_family.currentFont().family(),
            "font_pt": float(self.font_size.value()),
            "theme": self.theme_combo.currentText(),
            "opacity": self.opacity_slider.value() / 100.0,
            "word_wrap": self.word_wrap.isChecked(),
            "auto_hide_timer": self.auto_hide.value()
        }

# ---- Sticky window ----
class Sticky(QtWidgets.QWidget):
    def __init__(self, start_fresh: bool = False):
        super().__init__(None, QtCore.Qt.WindowType.FramelessWindowHint | QtCore.Qt.WindowType.Tool)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowTitle(APP_NAME)

        self.state = DEFAULT_STATE.copy() if start_fresh else load_state()
        self._click_through = self.state["click_through"]
        self._opacity = float(self.state["opacity"])
        self._radius = 12
        self._is_editing = False

        # Auto-hide timer
        self.auto_hide_timer = QtCore.QTimer()
        self.auto_hide_timer.setSingleShot(True)
        self.auto_hide_timer.timeout.connect(self.hide)
        self._reset_auto_hide_timer()

        # UI
        self._setup_ui()

        # Context menu
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_menu)

        # Initial geometry & opacity
        self._apply_initial_geometry_safe()
        self.setWindowOpacity(self._opacity)

        # Drag support
        self._drag_origin = None

        # System tray
        self.tray = self._create_tray_icon()
        self.tray.show()
        self._update_tray_tooltip()

        # Apply window properties
        QtCore.QTimer.singleShot(0, self._apply_click_through)

    # ---- geometry helpers ----
    def _apply_initial_geometry_safe(self):
        """Apply saved geometry but clamp to a visible screen."""
        x, y, w, h = self.state["x"], self.state["y"], self.state["w"], self.state["h"]
        rect = QtCore.QRect(x, y, w, h)
        if not self._rect_on_any_screen(rect):
            primary = QtGui.QGuiApplication.primaryScreen()
            ar = primary.availableGeometry() if primary else QtCore.QRect(0, 0, 800, 600)
            x, y = ar.left() + 80, ar.top() + 80
            w, h = 350, 120
            self.state.update({"x": x, "y": y, "w": w, "h": h})
            save_state(self.state)
        self.setGeometry(x, y, w, h)

    def _rect_on_any_screen(self, rect: QtCore.QRect) -> bool:
        for s in QtGui.QGuiApplication.screens():
            if s.availableGeometry().intersects(rect):
                return True
        return False

    # ---- paint & input ----
    def _setup_ui(self):
        self.label = QtWidgets.QLabel(self.state["text"])
        self.label.setWordWrap(self.state["word_wrap"])
        self._update_label_style()
        self.label.setContentsMargins(12, 10, 12, 10)
        self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)

        self.editor = QtWidgets.QTextEdit()
        self.editor.hide()
        self.editor.setPlainText(self.state["text"])
        self._update_editor_style()
        self.editor.installEventFilter(self)

        self.layout = QtWidgets.QStackedLayout(self)
        self.layout.setStackingMode(QtWidgets.QStackedLayout.StackingMode.StackAll)
        self.layout.addWidget(self.label)
        self.layout.addWidget(self.editor)

    def _update_label_style(self):
        theme = THEMES[self.state["theme"]]
        font = QtGui.QFont(self.state["font_family"])
        font.setPointSizeF(self.state["font_pt"])
        font.setBold(True)
        self.label.setFont(font)
        self.label.setStyleSheet(f"color: {theme['text']};")
        self.label.setWordWrap(self.state["word_wrap"])

    def _update_editor_style(self):
        theme = THEMES[self.state["theme"]]
        bg_color = theme["bg"]
        editor_bg = f"rgba({bg_color[0]}, {bg_color[1]}, {bg_color[2]}, {min(255, int(bg_color[3] * 1.3))})"
        font = QtGui.QFont(self.state["font_family"])
        font.setPointSizeF(self.state["font_pt"])
        font.setBold(True)
        self.editor.setFont(font)
        self.editor.setStyleSheet(f"""
            QTextEdit {{
                background-color: {editor_bg};
                color: {theme['text']};
                border: 2px solid rgba({theme['border'][0]}, {theme['border'][1]}, {theme['border'][2]}, {theme['border'][3]});
                border-radius: {self._radius}px;
                padding: 8px;
            }}
        """)

    def _reset_auto_hide_timer(self):
        if self.state["auto_hide_timer"] > 0:
            self.auto_hide_timer.start(self.state["auto_hide_timer"] * 60000)

    def paintEvent(self, ev):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        theme = THEMES[self.state["theme"]]
        bg = QtGui.QColor(*theme["bg"])
        pen = QtGui.QPen(QtGui.QColor(*theme["border"]))
        pen.setWidth(2 if self._is_editing else 1)
        path = QtGui.QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.fillPath(path, bg)
        painter.setPen(pen)
        painter.drawPath(path)
        if not self._is_editing:
            gradient = QtGui.QLinearGradient(0, 0, 0, rect.height())
            gradient.setColorAt(0, QtGui.QColor(255, 255, 255, 30))
            gradient.setColorAt(1, QtGui.QColor(0, 0, 0, 20))
            painter.fillPath(path, gradient)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton and not self._click_through:
            self._drag_origin = (ev.globalPosition().toPoint(), self.frameGeometry().topLeft())
            ev.accept()
        elif ev.button() == QtCore.Qt.MouseButton.RightButton:
            self._open_menu(ev.globalPosition().toPoint())
        self._reset_auto_hide_timer()

    def mouseMoveEvent(self, ev):
        if self._drag_origin:
            delta = ev.globalPosition().toPoint() - self._drag_origin[0]
            new_pos = self._drag_origin[1] + delta
            screen = QtGui.QGuiApplication.screenAt(new_pos)
            if screen:
                sr = screen.availableGeometry()
                wr = QtCore.QRect(new_pos, self.size())
                if wr.right() > sr.right():  new_pos.setX(sr.right() - self.width())
                if wr.bottom() > sr.bottom(): new_pos.setY(sr.bottom() - self.height())
                if wr.left() < sr.left():     new_pos.setX(sr.left())
                if wr.top() < sr.top():       new_pos.setY(sr.top())
            self.move(new_pos)

    def mouseReleaseEvent(self, ev):
        if self._drag_origin:
            self._drag_origin = None
            self._save_geometry()

    def _open_menu(self, pos):
        menu = QtWidgets.QMenu(self)
        act_edit = menu.addAction("✏️ Edit Text (Ctrl+Alt+T)")
        act_toggle = menu.addAction("👆 Toggle Click-Through (Ctrl+Alt+Space)")
        menu.addSeparator()
        size_menu = menu.addMenu("📏 Resize")
        act_resize_small = size_menu.addAction("Smaller")
        act_resize_large = size_menu.addAction("Larger")
        act_resize_reset = size_menu.addAction("Reset Size")
        theme_menu = menu.addMenu("🎨 Theme")
        theme_actions = {}
        for theme_name in THEMES.keys():
            action = theme_menu.addAction(theme_name.title())
            action.setCheckable(True)
            if theme_name == self.state["theme"]:
                action.setChecked(True)
            theme_actions[action] = theme_name
        menu.addSeparator()
        act_copy = menu.addAction("📋 Copy Text")
        act_clear = menu.addAction("🧹 Clear Text")
        act_settings = menu.addAction("⚙️ Settings...")
        act_about = menu.addAction("ℹ️ About")
        menu.addSeparator()
        act_exit = menu.addAction("❌ Exit")

        action = menu.exec(pos if isinstance(pos, QtCore.QPoint) else QtGui.QCursor.pos())

        if action == act_edit:
            self.begin_edit()
        elif action == act_toggle:
            self.toggle_click_through()
        elif action == act_resize_small:
            self._resize_by(-50, -30)
        elif action == act_resize_large:
            self._resize_by(50, 30)
        elif action == act_resize_reset:
            self._resize_to_default()
        elif action in theme_actions:
            self._change_theme(theme_actions[action])
        elif action == act_copy:
            QtGui.QGuiApplication.clipboard().setText(self.label.text())
        elif action == act_clear:
            self.label.setText("Active task")
            self._save_state()
            self._update_tray_tooltip()
        elif action == act_settings:
            self._show_settings()
        elif action == act_about:
            self._show_about()
        elif action == act_exit:
            QtWidgets.QApplication.quit()

    def _show_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            settings = dialog.get_settings()
            self.state.update(settings)
            self._apply_settings()
            self._save_state()
        else:
            self.setWindowOpacity(self._opacity)

    def _apply_settings(self):
        self._opacity = self.state["opacity"]
        self.setWindowOpacity(self._opacity)
        self._update_label_style()
        self._update_editor_style()
        self._reset_auto_hide_timer()
        self.update()

    def _show_about(self):
        QtWidgets.QMessageBox.about(self, "About ActiveSticky",
            f"""<h3>ActiveSticky</h3>
            <p>A customizable desktop sticky note application</p>
            <p><b>Hotkeys:</b><br>
            • Ctrl+Alt+T: Edit text<br>
            • Ctrl+Alt+Space: Toggle click-through<br>
            • Ctrl+Alt+M: Minimize<br>
            • Ctrl+Alt+Arrows: Move window<br>
            • Ctrl+Shift+Alt+↑/↓: Adjust opacity</p>
            <p><b>Version:</b> Enhanced v2.2</p>""")

    def _change_theme(self, theme_name):
        self.state["theme"] = theme_name
        self._update_label_style()
        self._update_editor_style()
        self._save_state()
        self.update()

    def begin_edit(self):
        if self._click_through:
            self.toggle_click_through()
        self._is_editing = True
        self.editor.setPlainText(self.label.text())
        self.editor.show()
        self.editor.setFocus()
        self.editor.selectAll()
        self.update()

    def commit_edit(self):
        txt = self.editor.toPlainText().strip() or "Active task"
        self.label.setText(txt)
        self.editor.hide()
        self._is_editing = False
        self._save_state()
        self._reset_auto_hide_timer()
        self._update_tray_tooltip()
        self.update()

    def cancel_edit(self):
        self.editor.hide()
        self._is_editing = False
        self.update()

    def eventFilter(self, obj, ev):
        if obj is self.editor and ev.type() == QtCore.QEvent.Type.KeyPress:
            if ev.key() == QtCore.Qt.Key.Key_Escape:
                self.cancel_edit()
                return True
            elif ev.key() == QtCore.Qt.Key.Key_Return and (ev.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier):
                self.commit_edit()
                return True
        return super().eventFilter(obj, ev)

    def toggle_click_through(self):
        self._click_through = not self._click_through
        self._apply_click_through()
        self._save_state()
        if hasattr(self, 'tray'):
            self.tray.showMessage("ActiveSticky",
                f"Click-through {'enabled' if self._click_through else 'disabled'}",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information, 2000)

    def adjust_opacity(self, delta):
        new_opacity = max(0.1, min(1.0, self._opacity + delta))
        if new_opacity != self._opacity:
            self._opacity = new_opacity
            self.setWindowOpacity(self._opacity)
            self.state["opacity"] = self._opacity
            self._save_state()

    def move_window(self, dx, dy):
        current = self.geometry()
        new_pos = QtCore.QPoint(current.x() + dx, current.y() + dy)
        screen = QtGui.QGuiApplication.screenAt(new_pos)
        if screen:
            sr = screen.availableGeometry()
            new_pos.setX(max(sr.left(), min(sr.right() - current.width(), new_pos.x())))
            new_pos.setY(max(sr.top(),  min(sr.bottom() - current.height(), new_pos.y())))
        self.move(new_pos)
        self._save_geometry()

    def _apply_click_through(self):
        hwnd = int(self.winId())
        ex_style = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        ex_style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if self._click_through:
            ex_style |= WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
        else:
            ex_style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style)

    def _apply_on_screen(self):
        screen = QtGui.QGuiApplication.screenAt(self.pos())
        if not screen:
            return
        sr = screen.availableGeometry()
        r = self.geometry()
        nx = min(max(sr.left(), r.x()), sr.right() - r.width())
        ny = min(max(sr.top(),  r.y()), sr.bottom() - r.height())
        if (nx, ny) != (r.x(), r.y()):
            self.move(nx, ny)

    def _resize_by(self, dw, dh):
        current = self.geometry()
        new_w = max(200, current.width() + dw)
        new_h = max(80,  current.height() + dh)
        self.setGeometry(current.x(), current.y(), new_w, new_h)
        self._apply_on_screen()
        self._save_geometry()

    def _resize_to_default(self):
        current = self.geometry()
        self.setGeometry(current.x(), current.y(), 350, 120)
        self._apply_on_screen()
        self._save_geometry()

    def _save_geometry(self):
        rect = self.geometry()
        self.state.update({
            "x": rect.x(), "y": rect.y(),
            "w": rect.width(), "h": rect.height()
        })
        save_state(self.state)

    def _save_state(self):
        rect = self.geometry()
        self.state.update({
            "text": self.label.text(),
            "x": rect.x(), "y": rect.y(),
            "w": rect.width(), "h": rect.height(),
            "click_through": self._click_through,
            "opacity": self._opacity,
            "font_pt": self.state["font_pt"]
        })
        save_state(self.state)

    def _create_tray_icon(self):
        tray = QtWidgets.QSystemTrayIcon(self)
        pixmap = QtGui.QPixmap(16, 16)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QBrush(QtGui.QColor(255, 255, 100)))
        painter.setPen(QtGui.QPen(QtGui.QColor(200, 200, 50), 1))
        painter.drawRoundedRect(2, 2, 12, 12, 2, 2)
        painter.setPen(QtGui.QPen(QtGui.QColor(150, 150, 50), 1))
        painter.drawLine(4, 6, 12, 6)
        painter.drawLine(4, 9, 11, 9)
        painter.drawLine(4, 12, 10, 12)
        painter.end()
        tray.setIcon(QtGui.QIcon(pixmap))
        menu = QtWidgets.QMenu()
        act_show = menu.addAction("👁️ Show/Hide")
        act_edit = menu.addAction("✏️ Edit")
        menu.addSeparator()
        act_quit = menu.addAction("❌ Quit")
        act_show.triggered.connect(self._toggle_visibility)
        act_edit.triggered.connect(self.begin_edit)
        act_quit.triggered.connect(QtWidgets.QApplication.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        return tray

    def _update_tray_tooltip(self):
        if hasattr(self, 'tray') and self.tray:
            t = self.label.text()
            suffix = "..." if len(t) > 30 else ""
            self.tray.setToolTip(f"{APP_NAME} - {t[:30]}{suffix}")

    def _toggle_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_visibility()

    def showEvent(self, event):
        super().showEvent(event)
        self._reset_auto_hide_timer()

    def closeEvent(self, event):
        self.hide()
        event.ignore()

# ---- App / Hotkeys ----
def register_hotkeys(hwnd):
    hotkeys = [
        (HK_EDIT, MOD_CONTROL | MOD_ALT, ord('T')),
        (HK_CLICK, MOD_CONTROL | MOD_ALT, 0x20),  # Space
        (HK_LEFT,  MOD_CONTROL | MOD_ALT, VK_LEFT),
        (HK_RIGHT, MOD_CONTROL | MOD_ALT, VK_RIGHT),
        (HK_UP,    MOD_CONTROL | MOD_ALT, VK_UP),
        (HK_DOWN,  MOD_CONTROL | MOD_ALT, VK_DOWN),
        (HK_MINIMIZE,     MOD_CONTROL | MOD_ALT, ord('M')),
        (HK_OPACITY_UP,   MOD_CONTROL | MOD_SHIFT | MOD_ALT, VK_UP),
        (HK_OPACITY_DOWN, MOD_CONTROL | MOD_SHIFT | MOD_ALT, VK_DOWN),
    ]
    for hk_id, modifiers, vk_code in hotkeys:
        ok = user32.RegisterHotKey(hwnd, hk_id, modifiers, vk_code)
        if not ok:
            print(f"[ActiveSticky] Hotkey registration failed: id={hk_id}")

def unregister_hotkeys(hwnd):
    for hk_id in range(1, 10):
        user32.UnregisterHotKey(hwnd, hk_id)

def main():
    # Destroy & start fresh: close existing window and wipe saved state.
    if close_existing_instance_by_title(APP_NAME):
        delete_state()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ActiveSticky")
    # Keep app running when main window is hidden (tray)
    app.setQuitOnLastWindowClosed(False)

    start_fresh = not STATE_PATH.exists()
    sticky = Sticky(start_fresh=start_fresh)
    sticky.show()

    hwnd = int(sticky.winId())
    register_hotkeys(hwnd)

    def on_hotkey(wparam):
        if wparam == HK_EDIT:           sticky.begin_edit()
        elif wparam == HK_CLICK:        sticky.toggle_click_through()
        elif wparam == HK_LEFT:         sticky.move_window(-10, 0)
        elif wparam == HK_RIGHT:        sticky.move_window(10, 0)
        elif wparam == HK_UP:           sticky.move_window(0, -10)
        elif wparam == HK_DOWN:         sticky.move_window(0, 10)
        elif wparam == HK_MINIMIZE:     sticky._toggle_visibility()
        elif wparam == HK_OPACITY_UP:   sticky.adjust_opacity(0.1)
        elif wparam == HK_OPACITY_DOWN: sticky.adjust_opacity(-0.1)

    hotkey_filter = HotkeyFilter(on_hotkey)
    app.installNativeEventFilter(hotkey_filter)

    def cleanup():
        unregister_hotkeys(hwnd)

    app.aboutToQuit.connect(cleanup)

    if start_fresh:
        sticky.tray.showMessage("ActiveSticky",
            "Welcome! Right-click for options, Ctrl+Alt+T to edit.",
            QtWidgets.QSystemTrayIcon.MessageIcon.Information, 5000)

    sys.exit(app.exec())

if __name__ == "__main__":
    if sys.platform != "win32":
        print("This application is designed for Windows.")
        sys.exit(1)
    main()
