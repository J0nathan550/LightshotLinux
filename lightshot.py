#!/usr/bin/env python3
"""
Lightshot for Linux - a Lightshot-style screenshot tool (PyQt5).

Press the hotkey (default: Print Screen) -> screen freezes and dims -> drag to
select -> annotate (pen, line, arrow, rectangle, marker, text) -> copy / save /
upload / search similar / print.

Run again (or `lightshot --capture`) while it is running to trigger a capture.
That is also how you bind it to a hotkey on Wayland, where global key grabs
by ordinary apps are not allowed.
"""
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import (QBuffer, QByteArray, QIODevice, QObject, QPoint, QPointF, QRect,
                          QSize, Qt, QTimer, pyqtSignal)
from PyQt5.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage,
                         QPainter, QPen, QPixmap, QPolygon, QPolygonF, QPainterPath, QRegion)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtPrintSupport import QPrintDialog, QPrinter
from PyQt5.QtWidgets import (QApplication, QColorDialog, QDialog, QFileDialog, QFormLayout,
                             QFrame, QHBoxLayout, QLineEdit, QMenu, QMessageBox, QPushButton,
                             QSystemTrayIcon, QToolButton, QToolTip, QVBoxLayout, QWidget,
                             QDialogButtonBox)

APP = "lightshot-linux"
CFG_FILE = Path.home() / ".config" / APP / "config.json"
DEFAULTS = {
    "save_dir": str(Path.home() / "Pictures"),
    "imgur_client_id": os.environ.get("IMGUR_CLIENT_ID", ""),
    "hotkey": "<print_screen>",
    "color": "#ff2d2d",
    "width": 3,
}
PALETTE = ["#ff2d2d", "#ff9500", "#ffd60a", "#34c759", "#0a84ff", "#bf5af2", "#ffffff", "#000000"]
HANDLE = 5


# ----------------------------------------------------------------- config ---
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CFG_FILE.read_text()))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CFG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception as ex:
        print("could not save config:", ex, file=sys.stderr)


# ------------------------------------------------------------------ icons ---
def make_icon(name, color="#ffffff"):
    pm = QPixmap(48, 48)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    if name == "pen":
        path = QPainterPath(QPointF(8, 36))
        path.cubicTo(16, 6, 28, 44, 40, 12)
        p.drawPath(path)
    elif name == "line":
        p.drawLine(10, 38, 38, 10)
    elif name == "arrow":
        p.drawLine(10, 38, 36, 12)
        p.drawLine(38, 10, 24, 12)
        p.drawLine(38, 10, 36, 24)
    elif name == "rect":
        p.drawRect(10, 12, 28, 24)
    elif name == "marker":
        p.setPen(QPen(QColor(255, 214, 10, 170), 14, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(10, 34, 38, 14)
    elif name == "text":
        p.setFont(QFont("sans-serif", 28, QFont.Bold))
        p.drawText(pm.rect(), Qt.AlignCenter, "T")
    elif name == "undo":
        p.drawLine(10, 22, 20, 13)
        p.drawLine(10, 22, 20, 31)
        path = QPainterPath(QPointF(10, 22))
        path.cubicTo(40, 12, 44, 42, 22, 40)
        p.drawPath(path)
    elif name == "copy":
        p.drawRect(8, 16, 22, 26)
        p.drawRect(18, 6, 22, 26)
    elif name == "save":
        p.drawRect(8, 8, 32, 32)
        p.drawRect(16, 8, 16, 10)
        p.drawRect(14, 26, 20, 14)
    elif name == "upload":
        p.drawLine(24, 36, 24, 10)
        p.drawLine(24, 10, 14, 20)
        p.drawLine(24, 10, 34, 20)
        p.drawLine(10, 41, 38, 41)
    elif name == "search":
        p.drawEllipse(8, 8, 22, 22)
        p.drawLine(27, 27, 40, 40)
    elif name == "print":
        p.drawRect(12, 8, 24, 12)
        p.drawRect(6, 20, 36, 16)
        p.drawRect(14, 30, 20, 12)
    elif name == "close":
        p.drawLine(12, 12, 36, 36)
        p.drawLine(36, 12, 12, 36)
    elif name == "color":
        p.setBrush(QColor(color))
        p.setPen(QPen(QColor("#ffffff"), 3))
        p.drawEllipse(9, 9, 30, 30)
    elif name == "app":
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#7b4bff"))
        p.drawRoundedRect(2, 2, 44, 44, 10, 10)
        p.setPen(QPen(QColor("#ffffff"), 4, Qt.SolidLine, Qt.SquareCap, Qt.MiterJoin))
        for (x, y, dx, dy) in ((12, 12, 1, 1), (36, 12, -1, 1), (12, 36, 1, -1), (36, 36, -1, -1)):
            p.drawLine(x, y, x + 9 * dx, y)
            p.drawLine(x, y, x, y + 9 * dy)
    p.end()
    return pm


# ---------------------------------------------------------------- capture ---
def looks_blank(pm):
    img = pm.toImage().scaled(32, 32, Qt.IgnoreAspectRatio, Qt.FastTransformation)
    return all(QColor(img.pixel(x, y)).value() < 4 for x in range(32) for y in range(32))


def external_grab():
    """Fallback for Wayland (or when Qt returns a black frame)."""
    f = tempfile.mktemp(suffix=".png")
    cmds = [["grim", f], ["gnome-screenshot", "-f", f], ["spectacle", "-b", "-n", "-f", "-o", f],
            ["scrot", "-o", f], ["import", "-window", "root", f]]
    for c in cmds:
        if not shutil.which(c[0]):
            continue
        try:
            subprocess.run(c, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            continue
        if os.path.exists(f):
            pm = QPixmap(f)
            os.remove(f)
            if not pm.isNull():
                return pm
    return None


def grab_screen():
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    g = screen.geometry()
    pm = screen.grabWindow(0)
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or pm.isNull() or looks_blank(pm):
        ext = external_grab()
        if ext is not None:
            pm = ext
            if pm.width() > g.width() * screen.devicePixelRatio() * 1.01:  # whole desktop -> crop
                vg = QGuiApplication.primaryScreen().virtualGeometry()
                k = pm.width() / vg.width()
                pm = pm.copy(QRect(int((g.x() - vg.x()) * k), int((g.y() - vg.y()) * k),
                                   int(g.width() * k), int(g.height() * k)))
    if pm.isNull():
        return screen, None
    pm.setDevicePixelRatio(pm.width() / g.width())
    return screen, pm


# -------------------------------------------------------------- annotation --
def draw_ann(p, a):
    c = QColor(a["color"])
    w = a["width"]
    t = a["tool"]
    if t == "marker":
        c.setAlpha(110)
        p.setPen(QPen(c, w * 5, Qt.SolidLine, Qt.FlatCap, Qt.RoundJoin))
    else:
        p.setPen(QPen(c, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    if t in ("pen", "marker"):
        pts = a["pts"]
        if len(pts) == 1:
            p.drawPoint(pts[0])
        else:
            p.drawPolyline(QPolygon(pts))
    elif t == "line":
        p.drawLine(a["p1"], a["p2"])
    elif t == "rect":
        p.drawRect(QRect(a["p1"], a["p2"]).normalized())
    elif t == "arrow":
        p1, p2 = a["p1"], a["p2"]
        p.drawLine(p1, p2)
        ang = math.atan2(p2.y() - p1.y(), p2.x() - p1.x())
        L = 10 + w * 3
        q = [QPointF(p2.x() - L * math.cos(ang + s * 0.45), p2.y() - L * math.sin(ang + s * 0.45))
             for s in (-1, 1)]
        p.setBrush(c)
        p.drawPolygon(QPolygonF([QPointF(p2), q[0], q[1]]))
    elif t == "text":
        f = QFont()
        f.setPixelSize(10 + w * 4)
        p.setFont(f)
        p.setPen(c)
        p.drawText(a["p1"] + QPoint(0, QFontMetrics(f).ascent()), a["text"])


class TextEdit(QLineEdit):
    done = pyqtSignal(bool)

    def __init__(self, parent):
        super().__init__(parent)
        self.textChanged.connect(lambda t: self.resize(max(60, self.fontMetrics().width(t) + 30),
                                                       self.height()))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.done.emit(True)
        elif e.key() == Qt.Key_Escape:
            self.done.emit(False)
        else:
            super().keyPressEvent(e)

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.done.emit(True)


# ----------------------------------------------------------------- overlay --
class Overlay(QWidget):
    CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
               "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}

    def __init__(self, screen, pm, ctl):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.scr, self.pm, self.ctl = screen, pm, ctl
        self.dpr = pm.devicePixelRatio()
        self.sel = QRect()
        self.mode = None
        self.tool = None
        self.color = QColor(ctl.cfg["color"])
        self.width_ = int(ctl.cfg["width"])
        self.anns, self.cur, self.edit = [], None, None
        self.anchor = self.drag_off = QPoint()
        self.handle = None
        self.rr = QRect()
        self.build_toolbars()

    def start(self):
        self.setGeometry(self.scr.geometry())
        self.create()
        if self.windowHandle():
            self.windowHandle().setScreen(self.scr)
        self.showFullScreen()
        self.raise_()
        self.activateWindow()

    def show_again(self):
        self.showFullScreen()
        self.activateWindow()

    # ---- toolbars
    def make_bar(self, vertical):
        f = QFrame(self)
        f.setObjectName("bar")
        f.setStyleSheet("#bar{background:rgba(28,28,30,235);border-radius:6px;}"
                        "QToolButton{background:transparent;border:none;border-radius:4px;}"
                        "QToolButton:hover{background:rgba(255,255,255,45);}"
                        "QToolButton:checked{background:rgba(80,140,255,210);}")
        lay = QVBoxLayout(f) if vertical else QHBoxLayout(f)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)
        f.hide()
        return f, lay

    def add_btn(self, lay, name, tip, slot, checkable=False):
        b = QToolButton()
        b.setIcon(QIcon(make_icon(name)))
        b.setIconSize(QSize(22, 22))
        b.setFixedSize(32, 32)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.NoFocus)
        b.setCheckable(checkable)
        b.clicked.connect(slot)
        lay.addWidget(b)
        return b

    def build_toolbars(self):
        self.vbar, vl = self.make_bar(True)
        self.tool_btns = {}
        for n, tip in (("pen", "Pen"), ("line", "Line"), ("arrow", "Arrow"), ("rect", "Rectangle"),
                       ("marker", "Marker"), ("text", "Text")):
            self.tool_btns[n] = self.add_btn(vl, n, tip, lambda _=False, n=n: self.set_tool(n), True)
        self.color_btn = self.add_btn(vl, "color", "Color", self.pick_color)
        self.set_color(self.color)
        self.add_btn(vl, "undo", "Undo (Ctrl+Z)", self.undo)

        self.hbar, hl = self.make_bar(False)
        self.add_btn(hl, "search", "Search similar images (Google Lens)", lambda: self.do_upload(True))
        self.add_btn(hl, "upload", "Upload to Imgur (Ctrl+D)", lambda: self.do_upload(False))
        self.add_btn(hl, "print", "Print (Ctrl+P)", self.do_print)
        self.add_btn(hl, "save", "Save (Ctrl+S)", self.do_save)
        self.add_btn(hl, "copy", "Copy to clipboard (Ctrl+C)", self.do_copy)
        self.add_btn(hl, "close", "Close (Esc)", self.close)

    def layout_toolbars(self):
        if self.sel.isNull():
            self.vbar.hide()
            self.hbar.hide()
            return
        for b in (self.vbar, self.hbar):
            b.adjustSize()
            b.show()
            b.raise_()
        W, H, s, g = self.width(), self.height(), self.sel, 6
        vw, vh = self.vbar.width(), self.vbar.height()
        x = s.right() + g
        if x + vw > W:
            x = s.left() - g - vw
        if x < 0:
            x = s.right() - vw - g
        self.vbar.move(x, max(0, min(s.top(), H - vh)))
        hw, hh = self.hbar.width(), self.hbar.height()
        y = s.bottom() + g
        if y + hh > H:
            y = s.top() - g - hh
        if y < 0:
            y = s.bottom() - hh - g
        self.hbar.move(max(0, min(s.right() - hw, W - hw)), y)

    def set_tool(self, name):
        self.tool = None if self.tool == name else name
        for n, b in self.tool_btns.items():
            b.setChecked(n == self.tool)

    def set_color(self, col):
        self.color = col
        self.ctl.cfg["color"] = col.name()
        self.color_btn.setIcon(QIcon(make_icon("color", col.name())))

    def pick_color(self):
        m = QMenu(self)
        for hx in PALETTE:
            pm = QPixmap(16, 16)
            pm.fill(QColor(hx))
            m.addAction(QIcon(pm), hx.upper()).setData(hx)
        m.addSeparator()
        custom = m.addAction("Custom…")
        act = m.exec_(QCursor.pos())
        if act is None:
            return
        if act is custom:
            col = QColorDialog.getColor(self.color, self)
            if col.isValid():
                self.set_color(col)
        else:
            self.set_color(QColor(act.data()))

    # ---- geometry helpers
    def clamp(self, pos):
        return QPoint(min(max(pos.x(), 0), self.width() - 1), min(max(pos.y(), 0), self.height() - 1))

    def hit(self, pos):
        if self.sel.isNull():
            return None
        r, cx, cy = self.sel, self.sel.center().x(), self.sel.center().y()
        pts = {"tl": r.topLeft(), "t": QPoint(cx, r.top()), "tr": r.topRight(),
               "r": QPoint(r.right(), cy), "br": r.bottomRight(), "b": QPoint(cx, r.bottom()),
               "bl": r.bottomLeft(), "l": QPoint(r.left(), cy)}
        for k, pt in pts.items():
            if QRect(pt.x() - HANDLE, pt.y() - HANDLE, 2 * HANDLE, 2 * HANDLE).contains(pos):
                return k
        return "move" if r.contains(pos) else None

    # ---- mouse
    def mousePressEvent(self, e):
        pos = self.clamp(e.pos())
        if self.edit:
            self.finish_text(True)
        if e.button() == Qt.RightButton:
            if self.sel.isNull():
                self.close()
            else:
                self.sel, self.anns = QRect(), []
                self.set_tool(self.tool) if self.tool else None
                self.layout_toolbars()
                self.update()
            return
        if e.button() != Qt.LeftButton:
            return
        h = self.hit(pos)
        if self.tool and self.sel.contains(pos):
            if self.tool == "text":
                self.start_text(pos)
            else:
                self.cur = {"tool": self.tool, "color": self.color.name(), "width": self.width_,
                            "p1": pos, "p2": pos, "pts": [pos]}
                self.mode = "draw"
            return
        if h == "move":
            self.mode, self.drag_off = "move", pos - self.sel.topLeft()
        elif h:
            self.mode, self.handle, self.rr = "resize", h, QRect(self.sel)
        else:
            self.mode, self.anchor, self.anns = "select", pos, []
            self.sel = QRect(pos, pos)
            if self.tool:
                self.set_tool(self.tool)
        if self.mode in ("move", "resize", "select"):
            self.vbar.hide()
            self.hbar.hide()

    def mouseMoveEvent(self, e):
        pos = self.clamp(e.pos())
        if self.mode == "select":
            self.sel = QRect(self.anchor, pos).normalized()
        elif self.mode == "move":
            tl = pos - self.drag_off
            tl.setX(max(0, min(tl.x(), self.width() - self.sel.width())))
            tl.setY(max(0, min(tl.y(), self.height() - self.sel.height())))
            self.sel.moveTo(tl)
        elif self.mode == "resize":
            h = self.handle
            if "l" in h:
                self.rr.setLeft(pos.x())
            if "r" in h:
                self.rr.setRight(pos.x())
            if "t" in h:
                self.rr.setTop(pos.y())
            if "b" in h:
                self.rr.setBottom(pos.y())
            self.sel = self.rr.normalized()
        elif self.mode == "draw":
            if self.cur["tool"] in ("pen", "marker"):
                self.cur["pts"].append(pos)
            self.cur["p2"] = pos
        else:
            h = self.hit(pos)
            if self.tool and self.sel.contains(pos):
                self.setCursor(Qt.IBeamCursor if self.tool == "text" else Qt.CrossCursor)
            else:
                self.setCursor(self.CURSORS.get(h, Qt.CrossCursor))
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.mode == "select" and (self.sel.width() < 3 or self.sel.height() < 3):
            self.sel = QRect()
        if self.mode == "draw" and self.cur:
            a = self.cur
            if a["tool"] in ("pen", "marker") or a["p1"] != a["p2"]:
                self.anns.append(a)
            self.cur = None
        self.mode = None
        self.layout_toolbars()
        self.update()

    def wheelEvent(self, e):
        self.width_ = max(1, min(20, self.width_ + (1 if e.angleDelta().y() > 0 else -1)))
        self.ctl.cfg["width"] = self.width_
        QToolTip.showText(QCursor.pos(), f"Thickness: {self.width_}", self)

    # ---- text tool
    def start_text(self, pos):
        f = QFont()
        f.setPixelSize(10 + self.width_ * 4)
        self.edit = TextEdit(self)
        self.edit.setFont(f)
        self.edit.setStyleSheet(f"background:transparent;border:1px dashed white;padding:0;"
                                f"color:{self.color.name()};")
        self.edit.setTextMargins(0, 0, 0, 0)
        self.edit_pos = pos
        self.edit.setGeometry(pos.x(), pos.y(), 80, f.pixelSize() + 10)
        self.edit.done.connect(self.finish_text)
        self.edit.show()
        self.edit.setFocus()

    def finish_text(self, ok):
        if not self.edit:
            return
        e, self.edit = self.edit, None
        txt = e.text()
        if ok and txt:
            h = e.height() - QFontMetrics(e.font()).height()
            self.anns.append({"tool": "text", "color": self.color.name(), "width": self.width_,
                              "p1": self.edit_pos + QPoint(1, h // 2), "text": txt})
        e.hide()
        e.deleteLater()
        self.setFocus()
        self.update()

    # ---- painting
    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pm)
        dim = QColor(0, 0, 0, 120)
        if self.sel.isNull():
            p.fillRect(self.rect(), dim)
            p.setPen(Qt.white)
            f = p.font()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(self.rect().adjusted(0, 24, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                       "Drag to select an area  •  Esc to cancel")
            return
        p.setClipRegion(QRegion(self.rect()).subtracted(QRegion(self.sel)))
        p.fillRect(self.rect(), dim)
        p.setClipping(False)

        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setClipRect(self.sel)
        for a in self.anns + ([self.cur] if self.cur else []):
            draw_ann(p, a)
        p.restore()

        r = self.sel.adjusted(0, 0, -1, -1)
        p.setPen(QPen(Qt.white, 1))
        p.drawRect(r)
        pen = QPen(QColor(0, 0, 0), 1, Qt.DashLine)
        p.setPen(pen)
        p.drawRect(r)
        if self.mode != "draw":
            p.setPen(QPen(QColor("#222"), 1))
            p.setBrush(Qt.white)
            c = self.sel.center()
            for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight(),
                       QPoint(c.x(), r.top()), QPoint(c.x(), r.bottom()),
                       QPoint(r.left(), c.y()), QPoint(r.right(), c.y())):
                p.drawRect(pt.x() - 3, pt.y() - 3, 6, 6)
        txt = f"{int(self.sel.width() * self.dpr)} × {int(self.sel.height() * self.dpr)}"
        w = QFontMetrics(p.font()).width(txt) + 12
        ly = self.sel.top() - 22 if self.sel.top() >= 22 else self.sel.top() + 2
        lr = QRect(self.sel.left(), ly, w, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        p.drawRoundedRect(lr, 3, 3)
        p.setPen(Qt.white)
        p.drawText(lr, Qt.AlignCenter, txt)

    # ---- output
    def result_image(self):
        s = self.sel
        img = QImage(max(1, round(s.width() * self.dpr)), max(1, round(s.height() * self.dpr)),
                     QImage.Format_ARGB32)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(self.dpr, self.dpr)
        p.translate(-s.x(), -s.y())
        p.drawPixmap(0, 0, self.pm)
        for a in self.anns:
            draw_ann(p, a)
        p.end()
        return img

    def undo(self):
        if self.anns:
            self.anns.pop()
            self.update()

    def do_copy(self):
        if self.sel.isNull():
            return
        QApplication.clipboard().setImage(self.result_image())
        self.ctl.notify("Screenshot copied to clipboard")
        self.close()

    def do_save(self):
        if self.sel.isNull():
            return
        img = self.result_image()
        self.hide()
        name = f"Screenshot_{datetime.now():%Y-%m-%d_%H-%M-%S}.png"
        path, _ = QFileDialog.getSaveFileName(
            None, "Save screenshot", os.path.join(self.ctl.cfg["save_dir"], name),
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;BMP (*.bmp)")
        if not path:
            self.show_again()
            return
        if not os.path.splitext(path)[1]:
            path += ".png"
        if img.save(path):
            self.ctl.cfg["save_dir"] = os.path.dirname(path)
            save_cfg(self.ctl.cfg)
            self.ctl.notify(f"Saved to {path}")
            self.close()
        else:
            QMessageBox.warning(None, "Lightshot", f"Could not save to {path}")
            self.show_again()

    def do_print(self):
        if self.sel.isNull():
            return
        img = self.result_image()
        self.hide()
        pr = QPrinter(QPrinter.HighResolution)
        if QPrintDialog(pr).exec_() == QDialog.Accepted:
            p = QPainter(pr)
            rect = p.viewport()
            size = img.size()
            size.scale(rect.size(), Qt.KeepAspectRatio)
            p.setViewport(rect.x(), rect.y(), size.width(), size.height())
            p.setWindow(img.rect())
            p.drawImage(0, 0, img)
            p.end()
            self.close()
        else:
            self.show_again()

    def do_upload(self, search):
        if self.sel.isNull():
            return
        if not self.ctl.cfg.get("imgur_client_id"):
            QMessageBox.information(self, "Lightshot",
                                    "Uploading uses Imgur. Create a free client ID at "
                                    "https://api.imgur.com/oauth2/addclient and paste it into "
                                    "Settings (tray icon → Settings).")
            return
        self.ctl.upload(self.result_image(), search)
        self.close()

    def keyPressEvent(self, e):
        ctrl = e.modifiers() & Qt.ControlModifier
        k = e.key()
        if k == Qt.Key_Escape:
            self.close()
        elif ctrl and k == Qt.Key_C or k in (Qt.Key_Return, Qt.Key_Enter):
            self.do_copy()
        elif ctrl and k == Qt.Key_S:
            self.do_save()
        elif ctrl and k == Qt.Key_Z:
            self.undo()
        elif ctrl and k == Qt.Key_P:
            self.do_print()
        elif ctrl and k == Qt.Key_D:
            self.do_upload(False)


# ---------------------------------------------------------------- settings --
class Settings(QDialog):
    def __init__(self, cfg):
        super().__init__()
        self.setWindowTitle("Lightshot settings")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.dir = QLineEdit(cfg["save_dir"])
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self.dir.setText(
            QFileDialog.getExistingDirectory(self, "Default save folder", self.dir.text()) or self.dir.text()))
        row = QHBoxLayout()
        row.addWidget(self.dir)
        row.addWidget(browse)
        form.addRow("Default save folder", row)
        self.imgur = QLineEdit(cfg["imgur_client_id"])
        form.addRow("Imgur client ID", self.imgur)
        self.hotkey = QLineEdit(cfg["hotkey"])
        self.hotkey.setToolTip("pynput format, e.g. <print_screen> or <ctrl>+<alt>+s (X11 only)")
        form.addRow("Global hotkey", self.hotkey)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def apply(self, cfg):
        cfg.update(save_dir=self.dir.text(), imgur_client_id=self.imgur.text().strip(),
                   hotkey=self.hotkey.text().strip() or "<print_screen>")


# -------------------------------------------------------------- controller --
class Controller(QObject):
    trigger = pyqtSignal()
    uploaded = pyqtSignal(str, str, bool)

    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.overlay = None
        self.hk = None
        self.tray = QSystemTrayIcon(QIcon(make_icon("app")))
        menu = QMenu()
        menu.addAction("Take screenshot", self.capture)
        menu.addAction("Settings…", self.settings)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Lightshot")
        self.tray.activated.connect(lambda r: self.capture() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.trigger.connect(self.capture)
        self.uploaded.connect(self.on_uploaded)
        self.start_hotkey()

    def notify(self, msg):
        self.tray.showMessage("Lightshot", msg, QSystemTrayIcon.Information, 3000)

    def start_hotkey(self):
        if self.hk:
            self.hk.stop()
            self.hk = None
        try:
            from pynput import keyboard
            self.hk = keyboard.GlobalHotKeys({self.cfg["hotkey"]: self.trigger.emit})
            self.hk.start()
        except Exception as ex:
            print("Global hotkey unavailable (Wayland?). Bind `lightshot --capture` "
                  "to a key in your desktop settings.", ex, file=sys.stderr)

    def settings(self):
        d = Settings(self.cfg)
        if d.exec_() == QDialog.Accepted:
            d.apply(self.cfg)
            save_cfg(self.cfg)
            self.start_hotkey()

    def capture(self):
        if not self.overlay:
            QTimer.singleShot(200, self._capture)  # let menus / notifications vanish first

    def _capture(self):
        if self.overlay:
            return
        screen, pm = grab_screen()
        if pm is None:
            self.notify("Screen capture failed. On Wayland install grim, gnome-screenshot or spectacle.")
            return
        self.overlay = Overlay(screen, pm, self)
        self.overlay.destroyed.connect(lambda: setattr(self, "overlay", None))
        self.overlay.start()

    def upload(self, img, search):
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        data, cid = base64.b64encode(bytes(ba)).decode(), self.cfg["imgur_client_id"]
        self.notify("Uploading…")

        def work():
            try:
                req = urllib.request.Request(
                    "https://api.imgur.com/3/image",
                    data=urllib.parse.urlencode({"image": data, "type": "base64"}).encode(),
                    headers={"Authorization": "Client-ID " + cid})
                with urllib.request.urlopen(req, timeout=30) as r:
                    self.uploaded.emit(json.load(r)["data"]["link"], "", search)
            except Exception as ex:
                self.uploaded.emit("", str(ex), search)
        threading.Thread(target=work, daemon=True).start()

    def on_uploaded(self, link, err, search):
        if err:
            self.notify("Upload failed: " + err)
            return
        QApplication.clipboard().setText(link)
        if search:
            webbrowser.open("https://lens.google.com/uploadbyurl?url=" + urllib.parse.quote(link, safe=""))
        else:
            self.notify("Link copied to clipboard: " + link)


# -------------------------------------------------------------------- main --
def main():
    args = sys.argv[1:]
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setQuitOnLastWindowClosed(False)

    sock = QLocalSocket()
    sock.connectToServer(APP)
    if sock.waitForConnected(300):  # already running -> forward the command
        sock.write(b"quit" if "--quit" in args else b"capture")
        sock.flush()
        sock.waitForBytesWritten(500)
        return 0
    if "--quit" in args:
        return 0

    ctl = Controller()
    QLocalServer.removeServer(APP)
    server = QLocalServer()
    server.listen(APP)

    def on_conn():
        s = server.nextPendingConnection()

        def read():
            cmd = bytes(s.readAll()).decode()
            s.disconnectFromServer()
            if cmd == "capture":
                ctl.capture()
            elif cmd == "quit":
                app.quit()
        s.readyRead.connect(read)
    server.newConnection.connect(on_conn)

    if "--capture" in args:
        QTimer.singleShot(0, ctl.capture)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
