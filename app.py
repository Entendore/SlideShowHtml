from __future__ import annotations

import sys
import os
import json
import shutil
import subprocess
import platform
import re
import logging
import socket
import zipfile
from datetime import datetime
from typing import Optional, List, Dict, Any

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QToolBar, QLabel, QMessageBox,
    QDockWidget, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QVBoxLayout, QHBoxLayout, QWidget, QSplitter, QMenu, QDialog, QFormLayout,
    QLineEdit, QSpinBox, QCheckBox, QComboBox, QDialogButtonBox, QInputDialog,
    QTextEdit, QFrame, QSizePolicy, QTreeView, QAbstractItemView
)
from PySide6.QtGui import (
    QAction, QKeySequence, QShortcut, QFont, QIcon, QPixmap, QColor,
    QPalette, QDesktopServices, QTextDocument, QPageLayout, QPageSize,
    QSyntaxHighlighter, QTextCharFormat, QPainter, QPen, QGuiApplication
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtCore import (
    QUrl, Qt, QFileSystemWatcher, QSize, QTimer, QObject, Signal, QThread, QModelIndex, QPoint
)
from PySide6.QtWidgets import QFileSystemModel
from PySide6.QtPrintSupport import QPrinter

# ---------- Optional Dependencies ----------
try:
    import markdown
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

try:
    import qrcode
    HAS_QRCODE = True
except ImportError:
    HAS_QRCODE = False

try:
    from flask import Flask
    from werkzeug.serving import make_server
    HAS_REMOTE = True
except ImportError:
    HAS_REMOTE = False

# ---------- Constants ----------
CONFIG_FILE = "presentations_config.json"
SUPPORTED_EXTENSIONS = ('.html', '.htm')
AUTOSAVE_DELAY_MS = 2000
LIVE_PREVIEW_DELAY_MS = 500
FILE_WATCH_RELOAD_DELAY_MS = 200
MIN_ZOOM, MAX_ZOOM = 0.25, 5.0
RECENT_PRESENTATIONS_LIMIT = 10
GLOBAL_CSS_NAME = "styles.css"

logger = logging.getLogger("html_pres_manager")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DEFAULT_SLIDE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; padding: 0; height: 100vh;
            display: flex; align-items: center; justify-content: center;
            background: {bg}; color: {fg};
        }}
        .content {{ text-align: center; padding: 2rem; }}
        h1 {{ font-size: 3.5rem; margin-bottom: 1rem; }}
        p  {{ font-size: 1.5rem; opacity: 0.9; }}
        .notes {{ display: none; }}
    </style>
</head>
<body>
    <div class="content">
        <h1>{title}</h1>
        <p>{subtitle}</p>
    </div>
    <div class="notes">Speaker notes for {title}.</div>
</body>
</html>
"""

TEMPLATES = {
    "Gradient Title Slide": {"bg": "linear-gradient(135deg, #667eea 0%, #764ba2 100%)", "fg": "white"},
    "Minimal White":        {"bg": "#ffffff", "fg": "#222222"},
    "Dark Theme":           {"bg": "#1a1a1a", "fg": "#eeeeee"},
}

SNIPPETS = {
    "2-Column Layout": "<div style='display:flex; gap:20px;'><div style='flex:1;'><h2>Left</h2></div><div style='flex:1;'><h2>Right</h2></div></div>",
    "Image Grid": "<div style='display:grid; grid-template-columns: repeat(3, 1fr); gap:10px;'><img src='img1.jpg' style='width:100%'><img src='img2.jpg' style='width:100%'><img src='img3.jpg' style='width:100%'></div>",
    "Code Block": "<pre><code class='python'>\nprint('Hello World')\n</code></pre>",
    "Speaker Notes": "<div class='notes'>Add notes here</div>"
}

DARK_QSS = """
QMainWindow, QDialog { background-color: #1e1e1e; color: #e0e0e0; }
QDockWidget { color: #e0e0e0; }
QDockWidget::title { background: #2d2d2d; padding: 4px; }
QListWidget, QPlainTextEdit, QLineEdit, QTextEdit, QComboBox, QSpinBox, QTreeView {
    background-color: #252526; color: #e0e0e0; border: 1px solid #3c3c3c;
}
QListWidget::item:selected { background-color: #094771; color: white; }
QPushButton {
    background-color: #2d2d2d; color: #e0e0e0; border: 1px solid #3c3c3c;
    padding: 5px 10px; border-radius: 3px;
}
QPushButton:hover { background-color: #3c3c3c; }
QPushButton:pressed { background-color: #094771; }
QToolBar { background: #2d2d2d; border: none; spacing: 3px; padding: 3px; }
QMenuBar { background: #2d2d2d; color: #e0e0e0; }
QMenuBar::item:selected { background: #3c3c3c; }
QMenu { background: #2d2d2d; color: #e0e0e0; border: 1px solid #3c3c3c; }
QMenu::item:selected { background: #094771; }
QStatusBar { background: #007acc; color: white; }
"""

# ---------- Helper Functions ----------

def extract_notes(html: str) -> str:
    pattern = re.compile(r'<(?:div|aside)[^>]*class=["\'][^"\']*\bnotes\b[^"\']*["\'][^>]*>(.*?)</(?:div|aside)>', re.IGNORECASE | re.DOTALL)
    blocks = [re.sub(r'<[^>]+>', '', m).strip() for m in pattern.findall(html)]
    return "\n\n".join(b for b in blocks if b)

def update_notes(html: str, notes: str) -> str:
    pattern = re.compile(r'<(?:div|aside)[^>]*class=["\'][^"\']*\bnotes\b[^"\']*["\'][^>]*>(.*?)</(?:div|aside)>', re.IGNORECASE | re.DOTALL)
    if pattern.search(html):
        return pattern.sub(f'<div class="notes">{notes}</div>', html, count=1)
    else:
        return html.replace('</body>', f'\n<div class="notes">{notes}</div>\n</body>')

def get_slide_title(filepath: str) -> str:
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(r'<h1[^>]*>(.*?)</h1>', content, re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r'<[^>]+>', '', match.group(1)).strip() or os.path.basename(filepath)
    except OSError:
        pass
    return os.path.basename(filepath)

def extract_tags(html: str) -> list:
    match = re.search(r'<!--\s*tags:\s*(.*?)\s*-->', html, re.IGNORECASE)
    if match:
        return [t.strip() for t in match.group(1).split(',') if t.strip()]
    return []

def slugify(text: str, max_len: int = 30) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '_', text)[:max_len] or "slide"

def render_template(template_name: str, title: str, subtitle: str) -> str:
    style = TEMPLATES.get(template_name, TEMPLATES["Gradient Title Slide"])
    return DEFAULT_SLIDE_TEMPLATE.format(title=title, subtitle=subtitle, bg=style["bg"], fg=style["fg"])


# ---------- Syntax Highlighter ----------
class HtmlHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self.rules = []
        
        tag_fmt = QTextCharFormat()
        tag_fmt.setForeground(QColor("#569cd6"))
        self.rules.append((re.compile(r"<[/]?\w+[\w-]*[^>]*>"), tag_fmt))
        
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor("#ce9178"))
        self.rules.append((re.compile(r'"[^"]*"'), str_fmt))
        
        cmt_fmt = QTextCharFormat()
        cmt_fmt.setForeground(QColor("#6a9955"))
        self.rules.append((re.compile(r"<!--[^>]*-->"), cmt_fmt))

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            for match in pattern.finditer(text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)


# ---------- Code Editor with Drag & Drop ----------
class CodeEditor(QPlainTextEdit):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        if not self.main_window.current_folder:
            event.ignore()
            return
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg')):
                        dest = os.path.join(self.main_window.current_folder, os.path.basename(path))
                        if not os.path.exists(dest):
                            try:
                                shutil.copy2(path, dest)
                            except OSError as e:
                                QMessageBox.warning(self, "Error", f"Cannot copy image:\n{e}")
                                continue
                        cursor = self.textCursor()
                        cursor.insertText(f'<img src="{os.path.basename(dest)}" alt="Image">')
                        event.acceptProposedAction()
                        return
        super().dropEvent(event)


# ---------- Drawing Overlay & Laser Pointer ----------
class AnnotationWidget(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.paths = []
        self.current_path = []
        self.drawing = False
        self.mode = "draw"  # "draw" or "laser"
        self.laser_pos = None
        self.hide()

    def mousePressEvent(self, event):
        if self.mode == "draw" and event.button() == Qt.LeftButton:
            self.drawing = True
            self.current_path = [event.pos()]
            self.update()
        elif self.mode == "laser":
            self.laser_pos = event.pos()
            self.update()

    def mouseMoveEvent(self, event):
        if self.mode == "draw" and self.drawing:
            self.current_path.append(event.pos())
            self.update()
        elif self.mode == "laser" and self.laser_pos:
            self.laser_pos = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.mode == "draw" and self.drawing:
            self.paths.append(self.current_path)
            self.drawing = False
        elif self.mode == "laser":
            self.laser_pos = None
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        if self.mode == "draw":
            painter.setPen(QPen(QColor(255, 0, 0, 200), 4))
            if len(self.current_path) > 1:
                for i in range(len(self.current_path) - 1):
                    painter.drawLine(self.current_path[i], self.current_path[i+1])
            for path in self.paths:
                if len(path) > 1:
                    for i in range(len(path) - 1):
                        painter.drawLine(path[i], path[i+1])
        elif self.mode == "laser" and self.laser_pos:
            painter.setBrush(QColor(255, 0, 0, 150))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(self.laser_pos, 15, 15)

    def clear(self):
        self.paths = []
        self.current_path = []
        self.laser_pos = None
        self.update()


# ---------- Slide List Widget (Drag & Drop Reorder) ----------
class SlideListWidget(QListWidget):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        super().dropEvent(event)
        new_order = []
        for i in range(self.count()):
            item = self.item(i)
            path = item.data(Qt.UserRole)
            if path:
                new_order.append(path)
        self.main_window.slides = new_order
        self.main_window.current_index = self.currentRow()


# ---------- Remote Server ----------
if HAS_REMOTE:
    class RemoteServerThread(QThread):
        trigger_next = Signal()
        trigger_prev = Signal()

        def __init__(self, port=5000):
            super().__init__()
            self.port = port
            self.app = Flask(__name__)
            self.app.add_url_rule('/', 'index', self.index)
            self.app.add_url_rule('/next', 'next', self.next_route, methods=['POST', 'GET'])
            self.app.add_url_rule('/prev', 'prev', self.prev_route, methods=['POST', 'GET'])
            self.server = make_server('0.0.0.0', self.port, self.app)
            self.server.daemon_threads = True

        def run(self):
            self.server.serve_forever()

        def stop(self):
            self.server.shutdown()

        def index(self):
            return "<html><body style='text-align:center;font-family:sans-serif;margin-top:50px;'><h1>Remote Control</h1><form action='/next' method='post'><button style='font-size:24px;padding:20px;'>▶ Next</button></form><br><form action='/prev' method='post'><button style='font-size:24px;padding:20px;'>◀ Prev</button></form></body></html>"

        def next_route(self):
            self.trigger_next.emit()
            return self.index()

        def prev_route(self):
            self.trigger_prev.emit()
            return self.index()


# ---------- Presenter Window ----------
class PresenterWindow(QWidget):
    def __init__(self, main_window: "HTMLPresentationApp"):
        super().__init__()
        self.main_window = main_window
        self.setWindowTitle("Presenter View")
        self.resize(900, 700)
        self.setWindowFlags(Qt.Window)
        
        self.target_seconds = 0
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        splitter = QSplitter(Qt.Vertical)
        self.current_view = QWebEngineView()
        self.current_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        bottom_widget = QWidget()
        bottom_layout = QHBoxLayout(bottom_widget)
        self.next_view = QWebEngineView()
        self.next_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.notes_edit = QTextEdit()
        self.notes_edit.setReadOnly(True)
        self.notes_edit.setFont(QFont("Arial", 12))
        bottom_layout.addWidget(self.next_view, stretch=1)
        bottom_layout.addWidget(self.notes_edit, stretch=1)
        
        splitter.addWidget(self.current_view)
        splitter.addWidget(bottom_widget)
        splitter.setSizes([400, 200])
        layout.addWidget(splitter)
        
        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        self.prev_btn = QPushButton("◀ Previous")
        self.prev_btn.clicked.connect(self.main_window.prev_slide)
        self.next_btn = QPushButton("Next ▶")
        self.next_btn.clicked.connect(self.main_window.next_slide)
        
        self.time_label = QLabel("Elapsed: 00:00")
        self.clock_label = QLabel("Time: --:--")
        for lbl in (self.time_label, self.clock_label):
            lbl.setStyleSheet("font-size: 16px; font-weight: bold; padding: 0 20px;")
            
        footer_layout.addWidget(self.prev_btn)
        footer_layout.addWidget(self.next_btn)
        footer_layout.addStretch()
        footer_layout.addWidget(self.time_label)
        footer_layout.addWidget(self.clock_label)
        layout.addWidget(footer)
        
        self.start_time = datetime.now()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_clock)
        self.timer.start(1000)
        self.update_clock()
        
        for keys, slot in [
            ("Right", self.main_window.next_slide),
            ("Space", self.main_window.next_slide),
            ("Left", self.main_window.prev_slide),
            ("Escape", self.close),
            ("F11", self.main_window.toggle_fullscreen),
            ("B", lambda: self.main_window.toggle_overlay("black")),
            ("W", lambda: self.main_window.toggle_overlay("white")),
            ("P", self.main_window.toggle_drawing),
            ("L", self.main_window.toggle_laser),
        ]:
            QShortcut(QKeySequence(keys), self, activated=slot)

    def set_target_time(self, minutes: int):
        self.target_seconds = minutes * 60

    def update_clock(self):
        elapsed = datetime.now() - self.start_time
        mins, secs = divmod(elapsed.seconds, 60)
        self.time_label.setText(f"Elapsed: {mins:02d}:{secs:02d}")
        self.clock_label.setText(datetime.now().strftime("Time: %I:%M %p"))
        
        if self.target_seconds > 0:
            if elapsed.seconds < self.target_seconds * 0.8:
                self.time_label.setStyleSheet("color: green; font-size: 16px; font-weight: bold; padding: 0 20px;")
            elif elapsed.seconds < self.target_seconds:
                self.time_label.setStyleSheet("color: orange; font-size: 16px; font-weight: bold; padding: 0 20px;")
            else:
                self.time_label.setStyleSheet("color: red; font-size: 16px; font-weight: bold; padding: 0 20px;")
        
    def update_content(self, current_path, next_path, notes):
        if current_path and os.path.exists(current_path):
            self.current_view.setUrl(QUrl.fromLocalFile(current_path))
        else:
            self.current_view.setHtml("")
            
        if next_path and os.path.exists(next_path):
            self.next_view.setUrl(QUrl.fromLocalFile(next_path))
        else:
            self.next_view.setHtml("<div style='text-align:center; margin-top: 40%; font-family:sans-serif; color:gray;'>End of presentation</div>")
            
        self.notes_edit.setText(notes)
        
    def closeEvent(self, event):
        self.main_window.exit_fullscreen()
        super().closeEvent(event)


# ---------- Dialogs ----------
class SearchReplaceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Global Search & Replace")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Find:"))
        self.find_edit = QLineEdit()
        layout.addWidget(self.find_edit)
        layout.addWidget(QLabel("Replace with:"))
        self.repl_edit = QLineEdit()
        layout.addWidget(self.repl_edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

class TargetTimeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set Presentation Target Time")
        layout = QFormLayout(self)
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 300)
        self.minutes.setValue(15)
        layout.addRow("Target Minutes:", self.minutes)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

class SpeakerNotesDialog(QDialog):
    def __init__(self, notes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Speaker Notes")
        self.resize(400, 300)
        layout = QVBoxLayout(self)
        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(notes)
        layout.addWidget(self.text_edit)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        
    def get_notes(self):
        return self.text_edit.toPlainText()


# ---------- Main Window ----------
class HTMLPresentationApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HTML Presentation Manager Pro")
        self.resize(1600, 900)

        self.presentations: Dict[str, str] = {}
        self.slides: List[str] = []
        self.current_index: int = -1
        self.current_folder: Optional[str] = None
        self.current_presentation_name: Optional[str] = None
        self.current_edit_file: Optional[str] = None
        self.recent_presentations: List[str] = []
        self.settings: Dict[str, Any] = {
            "dark_mode": False, "editor_font_size": 11, "auto_reload": True,
            "autosave": False, "wrap_text": True, "last_presentation": None,
            "last_slide_index": 0,
        }
        self.unsaved_changes: bool = False
        self.zoom_level: float = 1.0
        self._loading_editor: bool = False
        self._loading_slide: bool = False
        self.remote_server: Optional['RemoteServerThread'] = None
        self._temp_pdf_view = None
        self.live_preview_active = False
        
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(AUTOSAVE_DELAY_MS)
        self.autosave_timer.timeout.connect(self.save_and_apply_edits)
        
        self.live_preview_timer = QTimer(self)
        self.live_preview_timer.setSingleShot(True)
        self.live_preview_timer.setInterval(LIVE_PREVIEW_DELAY_MS)
        self.live_preview_timer.timeout.connect(self.update_live_preview)
        
        self.watcher = QFileSystemWatcher()
        self.watcher.fileChanged.connect(self.on_file_changed)
        self.watcher.directoryChanged.connect(self.on_dir_changed)
        
        self.create_web_view()
        self.create_library_panel()
        self.create_asset_manager_panel()
        self.create_slide_overview_panel()
        self.create_editor_panel()
        self.create_console_panel()
        self.create_toolbar()
        self.create_menu_bar()
        self.create_status_bar()
        self.create_shortcuts()
        self.apply_theme()
        
        self.black_overlay = QFrame(self.browser)
        self.black_overlay.setStyleSheet("background-color: black;")
        self.black_overlay.hide()
        self.white_overlay = QFrame(self.browser)
        self.white_overlay.setStyleSheet("background-color: white;")
        self.white_overlay.hide()
        self.annotation_widget = AnnotationWidget(self.browser)
        self.annotation_widget.hide()
        
        self.load_config()
        self.load_welcome_page()
        self.update_unsaved_indicator()
        
        last_name = self.settings.get("last_presentation")
        if last_name and last_name in self.presentations:
            for i in range(self.library_list.count()):
                if self.library_list.item(i).text() == last_name:
                    self.library_list.setCurrentRow(i)
                    self.load_selected_presentation(self.library_list.item(i))
                    last_idx = self.settings.get("last_slide_index", 0)
                    if 0 <= last_idx < len(self.slides):
                        self.current_index = last_idx
                        self.show_slide()
                    break

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isFullScreen():
            self.black_overlay.resize(self.browser.size())
            self.white_overlay.resize(self.browser.size())
            self.annotation_widget.resize(self.browser.size())

    # ---------- UI Construction ----------
    def create_web_view(self):
        self.browser = QWebEngineView()
        settings = self.browser.page().settings()
        settings.setAttribute(QWebEngineSettings.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.PluginsEnabled, True)
        settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)
        self.browser.page().fullScreenRequested.connect(self.handle_fullscreen_request)
        self.setCentralWidget(self.browser)
        self.browser.loadFinished.connect(self.on_page_loaded)
        self.browser.loadStarted.connect(lambda: self.statusBar().showMessage("Loading…", 2000))

    def create_library_panel(self):
        self.library_dock = QDockWidget("Library", self)
        self.library_dock.setObjectName("LibraryDock")
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        self.library_list = QListWidget()
        self.library_list.itemDoubleClicked.connect(self.load_selected_presentation)
        layout.addWidget(self.library_list)
        
        btn_layout = QHBoxLayout()
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self.new_presentation)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_existing_presentation)
        rm_btn = QPushButton("Remove")
        rm_btn.clicked.connect(self.remove_presentation)
        btn_layout.addWidget(new_btn)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(rm_btn)
        layout.addLayout(btn_layout)
        
        self.library_dock.setWidget(widget)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.library_dock)

    def create_asset_manager_panel(self):
        self.asset_dock = QDockWidget("Assets", self)
        self.asset_dock.setObjectName("AssetDock")
        self.asset_tree = QTreeView()
        self.asset_model = QFileSystemModel()
        self.asset_model.setRootPath("")
        self.asset_tree.setModel(self.asset_model)
        self.asset_tree.doubleClicked.connect(self.on_asset_double_click)
        self.asset_dock.setWidget(self.asset_tree)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.asset_dock)
        
    def on_asset_double_click(self, index: QModelIndex):
        path = self.asset_model.filePath(index)
        if os.path.isfile(path):
            if path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg')):
                # Smart Image Insertion
                if self.current_folder:
                    rel_path = os.path.relpath(path, self.current_folder)
                    cursor = self.editor.textCursor()
                    cursor.insertText(f'<img src="{rel_path}" alt="Image">')
                    self.on_editor_text_changed()
            elif path.lower().endswith(SUPPORTED_EXTENSIONS):
                if path in self.slides:
                    self.current_index = self.slides.index(path)
                    self.show_slide()
            else:
                self.current_edit_file = path
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        self._loading_editor = True
                        self.editor.setPlainText(f.read())
                        self._loading_editor = False
                    self.apply_btn.setText("💾 Save Asset")
                except OSError as e:
                    QMessageBox.warning(self, "Error", str(e))

    def create_slide_overview_panel(self):
        self.overview_dock = QDockWidget("Slides", self)
        self.overview_dock.setObjectName("OverviewDock")
        widget = QWidget()
        layout = QVBoxLayout(widget)
        
        self.slide_list = SlideListWidget(self)
        self.slide_list.itemClicked.connect(self.on_slide_clicked)
        layout.addWidget(self.slide_list)
        
        btn_layout = QHBoxLayout()
        add_slide_btn = QPushButton("Add Slide")
        add_slide_btn.clicked.connect(self.add_slide)
        del_slide_btn = QPushButton("Delete")
        del_slide_btn.clicked.connect(self.delete_slide)
        btn_layout.addWidget(add_slide_btn)
        btn_layout.addWidget(del_slide_btn)
        layout.addLayout(btn_layout)
        
        self.overview_dock.setWidget(widget)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.overview_dock)

    def create_editor_panel(self):
        self.editor_dock = QDockWidget("HTML Editor", self)
        self.editor_dock.setObjectName("EditorDock")
        widget = QWidget(); layout = QVBoxLayout(widget)
        
        editor_toolbar = QHBoxLayout()
        self.apply_btn = QPushButton("💾 Save & Apply (Ctrl+S)")
        self.apply_btn.clicked.connect(self.save_and_apply_edits)
        
        self.reload_btn = QPushButton("🔄 Reload")
        self.reload_btn.clicked.connect(self.reload_editor_content)
        
        self.md_convert_btn = QPushButton("📝 MD -> HTML")
        self.md_convert_btn.setToolTip("Convert Markdown to HTML")
        self.md_convert_btn.clicked.connect(self.convert_markdown)
        if not HAS_MARKDOWN: self.md_convert_btn.setEnabled(False)
        
        editor_toolbar.addWidget(self.apply_btn)
        editor_toolbar.addWidget(self.reload_btn)
        editor_toolbar.addWidget(self.md_convert_btn)
        editor_toolbar.addStretch()
        
        self.live_preview_chk = QCheckBox("Live Preview")
        self.live_preview_chk.toggled.connect(self.toggle_live_preview)
        editor_toolbar.addWidget(self.live_preview_chk)
        
        editor_toolbar.addWidget(QLabel("Transition:"))
        self.transition_combo = QComboBox()
        self.transition_combo.addItems(["None", "Fade", "Slide"])
        editor_toolbar.addWidget(self.transition_combo)
        
        self.unsaved_label = QLabel("● Saved")
        self.unsaved_label.setStyleSheet("color: green; font-weight: bold;")
        editor_toolbar.addWidget(self.unsaved_label)
        layout.addLayout(editor_toolbar)
        
        self.editor = CodeEditor(self)
        self.editor.setFont(QFont("Consolas", self.settings["editor_font_size"]))
        self.highlighter = HtmlHighlighter(self.editor.document())
        layout.addWidget(self.editor)
        
        self.editor_dock.setWidget(widget)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.editor_dock)
        self.resizeDocks([self.editor_dock], [300], Qt.Vertical)

    def create_console_panel(self):
        self.console_dock = QDockWidget("Python Console", self)
        self.console_dock.setObjectName("ConsoleDock")
        self.console_output = QTextEdit()
        self.console_output.setReadOnly(True)
        self.console_output.setFont(QFont("Consolas", 10))
        self.console_dock.setWidget(self.console_output)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.console_dock)
        self.console_dock.hide() # Hidden until needed

    def create_toolbar(self):
        self.main_toolbar = QToolBar("Main Toolbar")
        self.main_toolbar.setMovable(False)
        self.addToolBar(self.main_toolbar)
        
        self.main_toolbar.addAction("◀ Prev", self.prev_slide)
        self.main_toolbar.addAction("Next ▶", self.next_slide)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction("Zoom In", self.zoom_in)
        self.main_toolbar.addAction("Zoom Out", self.zoom_out)
        self.main_toolbar.addAction("Reset Zoom", self.reset_zoom)
        self.main_toolbar.addSeparator()
        self.main_toolbar.addAction("Present (F5)", self.start_presentation)
        
        self.slide_label = QLabel(" Slide 1 of 1 ")
        self.main_toolbar.addWidget(self.slide_label)

    def create_menu_bar(self):
        menubar = self.menuBar()
        
        file_menu = menubar.addMenu("&File")
        file_menu.addAction("New Presentation", self.new_presentation)
        file_menu.addAction("Add Existing...", self.add_existing_presentation)
        file_menu.addSeparator()
        file_menu.addAction("Export to PDF...", self.export_to_pdf)
        file_menu.addAction("Export to ZIP...", self.export_to_zip)
        file_menu.addSeparator()
        file_menu.addAction("Exit", self.close)
        
        insert_menu = menubar.addMenu("&Insert")
        snippets_menu = insert_menu.addMenu("Snippets")
        for name in SNIPPETS.keys():
            snippets_menu.addAction(name, lambda checked=False, n=name: self.insert_snippet(n))
        insert_menu.addAction("📝 Edit Speaker Notes...", self.edit_speaker_notes)
        insert_menu.addAction("📋 Generate TOC", self.generate_toc)
        insert_menu.addAction("🖼 Generate QR Code...", self.generate_qr_code)
        insert_menu.addAction("🏷 Insert Tags...", self.insert_tags_dialog)

        tools_menu = menubar.addMenu("&Tools")
        tools_menu.addAction("🔍 Global Search & Replace...", self.search_replace_in_presentation)
        tools_menu.addAction("⏱ Set Target Time...", self.set_target_time)
        tools_menu.addAction("🐍 Run Python Code Block", self.run_python_code)
        tools_menu.addAction("🌐 Start/Stop Remote Server", self.start_remote_server)
        tools_menu.addAction("🎨 Edit Global CSS", self.edit_global_css)
        
        view_menu = menubar.addMenu("&View")
        view_menu.addAction("Toggle Dark Mode", self.toggle_dark_mode).setCheckable(True)
        view_menu.addAction("Toggle Fullscreen (F11/F5)", self.toggle_fullscreen)
        view_menu.addAction("Toggle Python Console", self.toggle_console)
        
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction("About", lambda: QMessageBox.about(self, "About", "HTML Presentation Manager Pro\n\nA powerful tool for creating, managing, and presenting HTML-based slide decks."))

    def create_status_bar(self):
        self.statusBar().showMessage("Ready")
        self.file_status_label = QLabel("📂 No file loaded")
        self.statusBar().addPermanentWidget(self.file_status_label)
        self.zoom_label = QLabel(" 100% ")
        self.statusBar().addPermanentWidget(self.zoom_label)

    def create_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save_and_apply_edits)
        QShortcut(QKeySequence("Right"), self, activated=self.next_slide)
        QShortcut(QKeySequence("Left"), self, activated=self.prev_slide)
        QShortcut(QKeySequence("F5"), self, activated=self.start_presentation)
        QShortcut(QKeySequence("Escape"), self, activated=self.exit_fullscreen)
        QShortcut(QKeySequence("Ctrl++"), self, activated=self.zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self, activated=self.zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.reset_zoom)
        self.editor.textChanged.connect(self.on_editor_text_changed)

    # ---------- Theme & UI Toggles ----------
    def apply_theme(self):
        if self.settings.get("dark_mode", False):
            self.setStyleSheet(DARK_QSS)
        else:
            self.setStyleSheet("")

    def toggle_dark_mode(self):
        self.settings["dark_mode"] = not self.settings["dark_mode"]
        self.apply_theme()
        self.save_config()

    def toggle_console(self):
        self.console_dock.setVisible(not self.console_dock.isVisible())

    def toggle_live_preview(self, checked: bool):
        self.live_preview_active = checked
        if checked:
            self.update_live_preview()

    def update_live_preview(self):
        if self.live_preview_active and self.current_index != -1:
            html = self.editor.toPlainText()
            # Use base URL to resolve relative paths like images
            base_url = QUrl.fromLocalFile(self.slides[self.current_index])
            self.browser.setHtml(html, base_url)

    # ---------- Configuration ----------
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    self.presentations = data.get("presentations", {})
                    self.recent_presentations = data.get("recent", [])
                    self.settings.update(data.get("settings", {}))
            except Exception as e:
                logger.error(f"Failed to load config: {e}")
        self.library_list.clear()
        for name in self.presentations:
            self.library_list.addItem(name)

    def save_config(self):
        data = {
            "presentations": self.presentations,
            "recent": self.recent_presentations,
            "settings": self.settings
        }
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save config: {e}")

    # ---------- Presentation Management ----------
    def new_presentation(self):
        name, ok = QInputDialog.getText(self, "New Presentation", "Name:")
        if not ok or not name: return
        folder = QFileDialog.getExistingDirectory(self, "Select Folder for Presentation")
        if not folder: return
        
        html = render_template("Gradient Title Slide", name, "Subtitle")
        with open(os.path.join(folder, "slide_1.html"), 'w', encoding='utf-8') as f:
            f.write(html)
            
        self.presentations[name] = folder
        self.library_list.addItem(name)
        self.save_config()

    def add_existing_presentation(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Presentation Folder")
        if not folder: return
        name = os.path.basename(folder)
        self.presentations[name] = folder
        self.library_list.addItem(name)
        self.save_config()

    def remove_presentation(self):
        item = self.library_list.currentItem()
        if not item: return
        name = item.text()
        del self.presentations[name]
        self.library_list.takeItem(self.library_list.row(item))
        self.save_config()

    def load_selected_presentation(self, item):
        name = item.text()
        if name not in self.presentations: return
        folder = self.presentations[name]
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "Error", "Folder not found.")
            return
            
        self.current_folder = folder
        self.current_presentation_name = name
        self.asset_model.setRootPath(folder)
        self.asset_tree.setRootIndex(self.asset_model.index(folder))
        
        self.refresh_slide_overview()
        if self.slides:
            self.current_index = 0
            self.show_slide()
        
        self.settings["last_presentation"] = name
        self.save_config()

    # ---------- Slide Management ----------
    def refresh_slide_overview(self):
        self.slide_list.clear()
        if not self.current_folder: return
        self.slides = []
        for f in os.listdir(self.current_folder):
            if f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith('.'):
                self.slides.append(os.path.join(self.current_folder, f))
        self.slides.sort(key=lambda x: os.path.basename(x))
        
        for slide in self.slides:
            item = QListWidgetItem(get_slide_title(slide))
            item.setData(Qt.UserRole, slide)
            self.slide_list.addItem(item)

    def on_slide_clicked(self, item):
        idx = self.slide_list.row(item)
        if idx != self.current_index:
            self.current_index = idx
            self.show_slide()

    def add_slide(self):
        if not self.current_folder: return
        name, ok = QInputDialog.getText(self, "New Slide", "Slide Title:")
        if not ok or not name: return
        
        filename = f"slide_{len(self.slides)+1}_{slugify(name)}.html"
        path = os.path.join(self.current_folder, filename)
        html = render_template("Minimal White", name, "")
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        self.refresh_slide_overview()
        if path in self.slides:
            self.current_index = self.slides.index(path)
            self.show_slide()

    def delete_slide(self):
        if self.current_index == -1: return
        reply = QMessageBox.question(self, "Delete", f"Delete {os.path.basename(self.slides[self.current_index])}?")
        if reply == QMessageBox.Yes:
            os.remove(self.slides[self.current_index])
            self.refresh_slide_overview()
            if self.slides:
                self.current_index = min(self.current_index, len(self.slides)-1)
            else:
                self.current_index = -1
            self.show_slide()

    def show_slide(self):
        if not self.slides: return
        if self.current_index < 0: self.current_index = 0
        elif self.current_index >= len(self.slides): self.current_index = len(self.slides) - 1
        if self._loading_slide: return
        self._loading_slide = True
        try:
            file_path = self.slides[self.current_index]
            url = QUrl.fromLocalFile(file_path)
            
            if not self.live_preview_active:
                self.browser.setUrl(url)
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
            except OSError: content = ""
            
            self._loading_editor = True
            self.editor.setPlainText(content)
            self._loading_editor = False
            self.unsaved_changes = False
            self.update_unsaved_indicator()
            
            self.apply_btn.setText("💾 Save & Apply (Ctrl+S)")
            self.current_edit_file = None
            
            file_name = os.path.basename(file_path)
            self.setWindowTitle(f"HTML Presentation Manager - {file_name}")
            self.slide_label.setText(f" Slide {self.current_index + 1} of {len(self.slides)} ")
            self.file_status_label.setText(f"📂 {file_path}")
            
            if 0 <= self.current_index < self.slide_list.count():
                self.slide_list.setCurrentRow(self.current_index)
            self.browser.page().setZoomFactor(self.zoom_level)
            self.zoom_label.setText(f" {int(self.zoom_level * 100)}% ")
            
            self.update_presenter_view()
            self.settings["last_slide_index"] = self.current_index
            self.save_config()
        finally:
            self._loading_slide = False

    def on_page_loaded(self, ok: bool):
        if ok:
            self.statusBar().showMessage("Loaded", 1500)
            if self.current_folder:
                css_path = os.path.join(self.current_folder, GLOBAL_CSS_NAME)
                if os.path.exists(css_path):
                    js = f"""
                    if (!document.querySelector('link[href="{GLOBAL_CSS_NAME}"]')) {{
                        var l = document.createElement('link');
                        l.rel = 'stylesheet';
                        l.href = '{GLOBAL_CSS_NAME}';
                        document.head.appendChild(l);
                    }}
                    """
                    self.browser.page().runJavaScript(js)

    # ---------- Editor & Code ----------
    def on_editor_text_changed(self):
        if not self._loading_editor:
            if not self.unsaved_changes:
                self.unsaved_changes = True
                self.update_unsaved_indicator()
            if self.live_preview_active:
                self.live_preview_timer.start()

    def update_unsaved_indicator(self):
        if self.unsaved_changes:
            self.unsaved_label.setText("● Unsaved")
            self.unsaved_label.setStyleSheet("color: orange; font-weight: bold;")
        else:
            self.unsaved_label.setText("● Saved")
            self.unsaved_label.setStyleSheet("color: green; font-weight: bold;")

    def reload_editor_content(self):
        if self.current_index >= 0:
            self.show_slide()

    def convert_markdown(self):
        if not HAS_MARKDOWN: return
        md_text = self.editor.toPlainText()
        html_body = markdown.markdown(md_text)
        final_html = f"""<!DOCTYPE html>\n<html lang="en">\n<head>\n    <meta charset="UTF-8">\n    <style>\n        body {{ font-family: sans-serif; max-width: 800px; margin: 2em auto; line-height: 1.6; }}\n    </style>\n</head>\n<body>\n{html_body}\n</body>\n</html>"""
        self.editor.setPlainText(final_html)

    def save_and_apply_edits(self):
        if self.current_edit_file:
            try:
                with open(self.current_edit_file, 'w', encoding='utf-8') as f:
                    f.write(self.editor.toPlainText())
                self.statusBar().showMessage("Asset Saved", 2000)
                self.current_edit_file = None
                self.apply_btn.setText("💾 Save & Apply (Ctrl+S)")
                self.browser.reload()
            except OSError as e:
                QMessageBox.critical(self, "Save Error", str(e))
            return
            
        if not self.slides or self.current_index == -1: return
        self.autosave_timer.stop()
        file_path = self.slides[self.current_index]
        new_content = self.editor.toPlainText()
        
        trans = self.transition_combo.currentText()
        if trans == "Fade":
            style = "<style>body{animation: fadeIn 0.5s;} @keyframes fadeIn{from{opacity:0}to{opacity:1}}</style>"
            if style not in new_content: new_content = new_content.replace("</head>", style + "</head>")
        elif trans == "Slide":
            style = "<style>body{animation: slideIn 0.5s;} @keyframes slideIn{from{transform:translateX(100%)}to{transform:translateX(0)}}</style>"
            if style not in new_content: new_content = new_content.replace("</head>", style + "</head>")
            
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            self.browser.reload()
            self.unsaved_changes = False
            self.update_unsaved_indicator()
            self.statusBar().showMessage("Saved", 2000)
            self.refresh_slide_overview()
            self.update_presenter_view()
        except OSError as e:
            QMessageBox.critical(self, "Save Error", f"Could not save file:\n{e}")

    # ---------- Insert Features ----------
    def insert_snippet(self, name: str):
        cursor = self.editor.textCursor()
        cursor.insertText(SNIPPETS[name])
        self.on_editor_text_changed()

    def edit_speaker_notes(self):
        if self.current_index == -1: return
        html = self.editor.toPlainText()
        notes = extract_notes(html)
        dlg = SpeakerNotesDialog(notes, self)
        if dlg.exec() == QDialog.Accepted:
            new_notes = dlg.get_notes()
            new_html = update_notes(html, new_notes)
            self.editor.setPlainText(new_html)
            self.on_editor_text_changed()

    def generate_toc(self):
        if not self.current_folder: return
        toc_html = "<!DOCTYPE html>\n<html>\n<head>\n<meta charset='UTF-8'>\n<style>body{font-family:sans-serif; padding:50px;} a{display:block; font-size:1.5em; margin:15px; text-decoration:none; color:#094771; border-bottom:1px solid #ccc;}</style>\n</head>\n<body>\n<h1>Table of Contents</h1>\n"
        for i, slide in enumerate(self.slides):
            title = get_slide_title(slide)
            toc_html += f'<a href="{os.path.basename(slide)}">{i+1}. {title}</a>\n'
        toc_html += "</body>\n</html>"
        
        toc_path = os.path.join(self.current_folder, "00_toc.html")
        with open(toc_path, 'w', encoding='utf-8') as f:
            f.write(toc_html)
            
        self.refresh_slide_overview()
        if toc_path in self.slides:
            self.current_index = self.slides.index(toc_path)
            self.show_slide()

    def generate_qr_code(self):
        if not HAS_QRCODE:
            QMessageBox.warning(self, "Missing Dependency", "Please install 'qrcode' package.")
            return
        if not self.current_folder:
            QMessageBox.warning(self, "Error", "Open a presentation first.")
            return
        text, ok = QInputDialog.getText(self, "Generate QR Code", "Enter URL or text:")
        if not ok or not text: return
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img_path = os.path.join(self.current_folder, f"qr_{slugify(text)}.png")
        img.save(img_path)
        cursor = self.editor.textCursor()
        cursor.insertText(f'<img src="{os.path.basename(img_path)}" alt="QR Code" style="max-width: 300px;">')
        self.on_editor_text_changed()

    def insert_tags_dialog(self):
        html = self.editor.toPlainText()
        current_tags = extract_tags(html)
        text, ok = QInputDialog.getText(self, "Slide Tags", "Tags (comma separated):", text=", ".join(current_tags))
        if ok:
            new_tags = [t.strip() for t in text.split(',') if t.strip()]
            if new_tags:
                tag_str = f"<!-- tags: {', '.join(new_tags)} -->"
                if extract_tags(html):
                    html = re.sub(r'<!--\s*tags:\s*.*?\s*-->', tag_str, html)
                else:
                    html = tag_str + "\n" + html
                self.editor.setPlainText(html)
                self.on_editor_text_changed()

    def edit_global_css(self):
        if not self.current_folder: return
        css_path = os.path.join(self.current_folder, GLOBAL_CSS_NAME)
        if not os.path.exists(css_path):
            with open(css_path, 'w', encoding='utf-8') as f:
                f.write("/* Global Styles */\n")
        
        self.current_edit_file = css_path
        try:
            with open(css_path, 'r', encoding='utf-8') as f:
                self._loading_editor = True
                self.editor.setPlainText(f.read())
                self._loading_editor = False
            self.apply_btn.setText("💾 Save CSS")
            self.editor_dock.show()
            self.editor_dock.raise_()
        except OSError as e:
            QMessageBox.warning(self, "Error", str(e))

    # ---------- Tools ----------
    def search_replace_in_presentation(self):
        if not self.current_folder: return
        dlg = SearchReplaceDialog(self)
        if dlg.exec() != QDialog.Accepted: return
        find_t = dlg.find_edit.text()
        repl_t = dlg.repl_edit.text()
        if not find_t: return
        
        count = 0
        for path in self.slides:
            try:
                with open(path, 'r', encoding='utf-8') as f: content = f.read()
                if find_t in content:
                    new_content = content.replace(find_t, repl_t)
                    with open(path, 'w', encoding='utf-8') as f: f.write(new_content)
                    count += 1
            except OSError: continue
        self.show_slide()
        QMessageBox.information(self, "Replace", f"Replaced in {count} files.")

    def set_target_time(self):
        dlg = TargetTimeDialog(self)
        if dlg.exec() == QDialog.Accepted:
            mins = dlg.minutes.value()
            if hasattr(self, 'presenter_window'):
                self.presenter_window.set_target_time(mins)
            QMessageBox.information(self, "Timer", f"Target time set to {mins} minutes.")

    def run_python_code(self):
        content = self.editor.toPlainText()
        match = re.search(r'<code class="python">(.*?)</code>', content, re.DOTALL)
        if not match:
            QMessageBox.information(self, "Run Code", "No <code class='python'> block found.")
            return
        code = match.group(1)
        
        self.console_dock.show()
        self.console_dock.raise_()
        self.console_output.append(f"--- Running Python block from {os.path.basename(self.slides[self.current_index])} ---")
        
        try:
            result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
            output = result.stdout + result.stderr
            self.console_output.append(output or "(No output)")
            self.console_output.append("--- Execution Finished ---\n")
        except subprocess.TimeoutExpired:
            self.console_output.append("ERROR: Code execution timed out (10s limit).\n")
        except Exception as e:
            self.console_output.append(f"ERROR: {str(e)}\n")

    def start_remote_server(self):
        if not HAS_REMOTE:
            QMessageBox.warning(self, "Missing Dependency", "Please install 'flask' package.")
            return
        if self.remote_server:
            self.remote_server.stop()
            self.remote_server = None
            self.statusBar().showMessage("Remote server stopped.", 3000)
            return
            
        self.remote_server = RemoteServerThread()
        self.remote_server.trigger_next.connect(self.next_slide)
        self.remote_server.trigger_prev.connect(self.prev_slide)
        self.remote_server.start()
        
        ip = socket.gethostbyname(socket.gethostname())
        url = f"http://{ip}:5000"
        self.statusBar().showMessage(f"Remote server running at {url}", 5000)
        
        if HAS_QRCODE:
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            img.show()

    # ---------- Export Features ----------
    def export_to_pdf(self):
        if not self.slides:
            QMessageBox.warning(self, "Export", "No slides loaded.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder for PDFs")
        if not folder: return
        
        self.statusBar().showMessage("Exporting to PDF...")
        self._pdf_queue = list(self.slides)
        self._pdf_output_folder = folder
        self._process_pdf_queue()

    def _process_pdf_queue(self):
        if not self._pdf_queue:
            self.statusBar().showMessage("PDF Export Complete!", 5000)
            self._temp_pdf_view = None
            return
            
        slide_path = self._pdf_queue.pop(0)
        self.statusBar().showMessage(f"Exporting {os.path.basename(slide_path)}...")
        
        self._temp_pdf_view = QWebEngineView()
        self._temp_pdf_view.load(QUrl.fromLocalFile(slide_path))
        
        def on_loaded(ok):
            if ok:
                pdf_path = os.path.join(self._pdf_output_folder, os.path.splitext(os.path.basename(slide_path))[0] + ".pdf")
                self._temp_pdf_view.page().printToPdf(pdf_path)
                QTimer.singleShot(1000, self._process_pdf_queue)
            else:
                self._process_pdf_queue()
                
        self._temp_pdf_view.loadFinished.connect(on_loaded)

    def export_to_zip(self):
        if not self.current_folder: return
        file_path, _ = QFileDialog.getSaveFileName(self, "Export to ZIP", f"{self.current_presentation_name}.zip", "ZIP Files (*.zip)")
        if not file_path: return
        
        with zipfile.ZipFile(file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(self.current_folder):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, self.current_folder)
                    zipf.write(full_path, arcname)
        QMessageBox.information(self, "Export", f"Presentation exported to {file_path}")

    # ---------- Presentation Tools ----------
    def start_presentation(self):
        if not self.slides: return
        self.presenter_window = PresenterWindow(self)
        
        # Multi-monitor handling
        screens = QGuiApplication.screens()
        if len(screens) > 1:
            # Move presenter view to second screen
            self.presenter_window.setScreen(screens[1])
            self.presenter_window.showFullScreen()
            # Move main window to first screen
            self.setScreen(screens[0])
            self.showFullScreen()
        else:
            self.presenter_window.show()
            self.toggle_fullscreen()
            
        self.update_presenter_view()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self.showFullScreen()
            self.menuBar().hide()
            self.main_toolbar.hide()
            self.statusBar().hide()
            self.library_dock.hide()
            self.editor_dock.hide()
            self.overview_dock.hide()
            self.asset_dock.hide()
            self.console_dock.hide()

    def exit_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.menuBar().show()
            self.main_toolbar.show()
            self.statusBar().show()
            self.library_dock.show()
            self.editor_dock.show()
            self.overview_dock.show()
            self.asset_dock.show()
            self.black_overlay.hide()
            self.white_overlay.hide()
            self.annotation_widget.hide()
            self.annotation_widget.clear()

    def handle_fullscreen_request(self, request):
        request.accept()

    def toggle_overlay(self, color: str):
        if not self.isFullScreen(): return
        if color == "black":
            self.white_overlay.hide()
            self.black_overlay.setVisible(not self.black_overlay.isVisible())
            if self.black_overlay.isVisible():
                self.black_overlay.resize(self.browser.size())
                self.black_overlay.raise_()
        elif color == "white":
            self.black_overlay.hide()
            self.white_overlay.setVisible(not self.white_overlay.isVisible())
            if self.white_overlay.isVisible():
                self.white_overlay.resize(self.browser.size())
                self.white_overlay.raise_()

    def toggle_drawing(self):
        if not self.isFullScreen(): return
        self.annotation_widget.mode = "draw"
        self.annotation_widget.setVisible(not self.annotation_widget.isVisible())
        if self.annotation_widget.isVisible():
            self.annotation_widget.resize(self.browser.size())
            self.annotation_widget.raise_()
        else:
            self.annotation_widget.clear()

    def toggle_laser(self):
        if not self.isFullScreen(): return
        self.annotation_widget.mode = "laser"
        self.annotation_widget.setVisible(not self.annotation_widget.isVisible())
        if self.annotation_widget.isVisible():
            self.annotation_widget.resize(self.browser.size())
            self.annotation_widget.raise_()
        else:
            self.annotation_widget.clear()

    def update_presenter_view(self):
        if hasattr(self, 'presenter_window') and self.presenter_window.isVisible():
            curr_path = self.slides[self.current_index] if self.current_index != -1 else None
            next_path = self.slides[self.current_index+1] if self.current_index != -1 and self.current_index+1 < len(self.slides) else None
            notes = extract_notes(self.editor.toPlainText())
            self.presenter_window.update_content(curr_path, next_path, notes)

    # ---------- Navigation ----------
    def next_slide(self):
        if self.current_index < len(self.slides) - 1:
            self.current_index += 1
            self.show_slide()

    def prev_slide(self):
        if self.current_index > 0:
            self.current_index -= 1
            self.show_slide()

    def zoom_in(self):
        self.zoom_level = min(MAX_ZOOM, self.zoom_level + 0.1)
        self.browser.page().setZoomFactor(self.zoom_level)
        self.zoom_label.setText(f" {int(self.zoom_level * 100)}% ")

    def zoom_out(self):
        self.zoom_level = max(MIN_ZOOM, self.zoom_level - 0.1)
        self.browser.page().setZoomFactor(self.zoom_level)
        self.zoom_label.setText(f" {int(self.zoom_level * 100)}% ")

    def reset_zoom(self):
        self.zoom_level = 1.0
        self.browser.page().setZoomFactor(self.zoom_level)
        self.zoom_label.setText(" 100% ")

    def load_welcome_page(self):
        self.browser.setHtml("<h1 style='font-family:sans-serif; text-align:center; margin-top:20%;'>Welcome to HTML Presentation Manager Pro</h1><p style='text-align:center; font-family:sans-serif;'>Select or Create a presentation from the Library.</p>")

    # ---------- File Watcher ----------
    def on_file_changed(self, path):
        if self._loading_slide or self._loading_editor: return
        QTimer.singleShot(FILE_WATCH_RELOAD_DELAY_MS, lambda: self.reload_file(path))

    def reload_file(self, path):
        if not os.path.exists(path): return
        if self.current_index >= 0 and self.slides[self.current_index] == path:
            self.show_slide()

    def on_dir_changed(self, path):
        if self.current_folder and os.path.abspath(path) == os.path.abspath(self.current_folder):
            self.refresh_slide_overview()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = HTMLPresentationApp()
    window.show()
    sys.exit(app.exec())