"""
EVA BOT — Control Center (2026 Edition)
Modern sidebar-navigation app for Chitchat Bot control: live chat feed,
browser session manager, picture sender, performance presets.
"""
import sys
import os
import argparse
import random
import threading
import re

import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', message='.*greenlet.*')

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QTextEdit, QLabel,
                             QProgressBar, QGroupBox, QRadioButton, QButtonGroup,
                             QSizePolicy, QLineEdit, QFileDialog, QFrame, QScrollArea,
                             QGridLayout, QFormLayout, QMessageBox,
                             QTabWidget, QCheckBox, QComboBox, QListWidget,
                             QListWidgetItem, QSplitter, QStackedWidget,
                             QTableWidget, QTableWidgetItem, QHeaderView)
from PyQt6.QtCore import QThread, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QFont, QIcon, QColor
import psutil
import datetime
import ctypes
import platform

try:
    import winsound
except ImportError:
    winsound = None

# Ensure the project root is importable when this file is run directly
# (``python entry/main.py``) and not only via ``python -m entry.main``.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from browser.account_session_store import load_saved_account_sessions
from entry.thread_manager import ThreadManager

BUILD_ID = "EVA-CONTROL-CENTER-V5"
from core.config_loader import load_chat_timing

# ----------------------------------------------------------------------------
# MODERN THEME (2026) — single source of truth for colors + global stylesheet
# ----------------------------------------------------------------------------
C_BG        = "#0B0E14"   # app background
C_PANEL     = "#11151F"   # card / panel background
C_PANEL_2   = "#161B28"   # slightly raised panel
C_INPUT     = "#1A2130"   # input fields
C_BORDER    = "#232B3A"   # subtle borders
C_TEXT      = "#E6EAF2"   # primary text
C_TEXT_DIM  = "#8A93A6"   # secondary text
C_ACCENT    = "#8B5CF6"   # purple accent
C_GREEN     = "#00D68F"   # success / go
C_RED       = "#FF5555"   # danger / ban
C_AMBER     = "#FFC107"   # warning
C_BLUE      = "#4DA3FF"   # info
C_TEAL      = "#2DD4BF"   # sessions
C_ORANGE    = "#FB923C"   # cpu
C_PINK      = "#F472B6"   # pics
C_PURPLE    = "#A78BFA"   # pic feed

GLOBAL_QSS = f"""
* {{
    font-family: 'Segoe UI Variable Display', 'Segoe UI', 'Inter', sans-serif;
    outline: none;
}}
QMainWindow, QWidget {{ background-color: {C_BG}; color: {C_TEXT}; }}
QFrame#sidebar {{
    background-color: #0D1018;
    border-right: 1px solid {C_BORDER};
}}
QFrame#topbar {{
    background-color: #0D1018;
    border-bottom: 1px solid {C_BORDER};
}}
QFrame#navBtn, QFrame#navBtnActive {{
    border-radius: 10px;
}}
QLabel {{ color: {C_TEXT}; background: transparent; }}
QLabel#dim {{ color: {C_TEXT_DIM}; }}
QLabel#statTitle {{
    color: {C_TEXT_DIM}; font-size: 11px; font-weight: 700;
    letter-spacing: 1.5px; background: transparent;
}}
QLabel#statValue {{
    color: {C_TEXT}; font-size: 26px; font-weight: 800;
    background: transparent;
}}
QLabel#pageEmoji {{ font-size: 26px; background: transparent; }}
QLabel#brand {{ color: {C_TEXT}; font-size: 19px; font-weight: 800; background: transparent; }}
QLabel#brandSub {{ color: {C_TEXT_DIM}; font-size: 10px; letter-spacing: 2px; background: transparent; }}
QLabel#pill {{
    border-radius: 13px; padding: 5px 16px; font-weight: 700;
}}
QGroupBox {{
    font-size: 13px; font-weight: 700; color: {C_TEXT};
    border: 1px solid {C_BORDER}; border-radius: 14px;
    margin-top: 14px; padding: 14px;
    background-color: {C_PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin; subcontrol-position: top left;
    padding: 0 10px; color: {C_TEXT_DIM}; letter-spacing: 1px;
}}
QLineEdit, QComboBox, QSpinBox {{
    background-color: {C_INPUT}; border: 1px solid {C_BORDER};
    border-radius: 9px; padding: 8px 12px; color: {C_TEXT};
    font-size: 13px; min-height: 22px; min-width: 90px;
    selection-background-color: {C_ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {C_ACCENT}; }}
QLineEdit:disabled {{ color: {C_TEXT_DIM}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox QAbstractItemView {{
    background-color: {C_PANEL_2}; color: {C_TEXT};
    border: 1px solid {C_BORDER}; selection-background-color: {C_ACCENT};
    selection-color: white; outline: none;
}}
QPushButton {{
    background-color: {C_PANEL_2}; color: {C_TEXT}; border: 1px solid {C_BORDER};
    border-radius: 9px; padding: 9px 16px; font-size: 13px; font-weight: 600;
    min-height: 20px;
}}
QPushButton:hover {{ background-color: #1D2434; border-color: #2E3950; }}
QPushButton:pressed {{ background-color: #151B29; }}
QPushButton:disabled {{ color: #5A6375; background-color: #12161F; }}
QPushButton#startBtn {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #00B383, stop:1 #00D68F);
    color: white; border: none; border-radius: 12px;
    font-size: 17px; font-weight: 800; letter-spacing: 2px; padding: 15px;
}}
QPushButton#startBtn:hover {{ background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #00A073, stop:1 #00C27F); }}
QPushButton#startBtn:disabled {{ background-color: #16211C; color: #3E5A50; }}
QPushButton#stopBtn {{
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #D64550, stop:1 #FF5555);
    color: white; border: none; border-radius: 12px;
    font-size: 15px; font-weight: 800; letter-spacing: 2px; padding: 12px;
}}
QPushButton#stopBtn:hover {{ background-color: #C93B46; }}
QPushButton#stopBtn:disabled {{ background-color: #1C1216; color: #5A3A40; }}
QRadioButton, QCheckBox {{
    color: {C_TEXT}; spacing: 10px; font-size: 13px;
    background: transparent; padding: 4px;
}}
QRadioButton::indicator, QCheckBox::indicator {{
    width: 20px; height: 20px; border-radius: 6px;
    border: 2px solid #303A4E; background-color: {C_INPUT};
}}
QRadioButton::indicator:checked, QCheckBox::indicator:checked {{
    background-color: {C_GREEN}; border-color: {C_GREEN};
}}
QCheckBox::indicator {{ width: 38px; height: 21px; border-radius: 11px; }}
QCheckBox::indicator:checked {{ background-color: {C_GREEN}; border-color: {C_GREEN}; }}
QTextEdit {{
    background-color: #0D1117; border: 1px solid {C_BORDER};
    border-radius: 12px; padding: 10px; color: {C_TEXT};
    selection-background-color: {C_ACCENT};
}}
QTableWidget {{
    background-color: #0D1117; alternate-background-color: #10151F;
    border: 1px solid {C_BORDER}; border-radius: 12px;
    gridline-color: {C_BORDER}; font-size: 13px;
}}
QHeaderView::section {{
    background-color: {C_PANEL_2}; color: {C_TEXT_DIM};
    border: none; border-bottom: 1px solid {C_BORDER};
    padding: 10px 8px; font-size: 12px; font-weight: 700;
}}
QTableWidget::item {{ padding: 6px 8px; }}
QListWidget {{
    background-color: #0D1117; border: 1px solid {C_BORDER};
    border-radius: 12px; padding: 6px; color: {C_TEXT}; font-size: 13px;
}}
QListWidget::item {{ padding: 8px 10px; border-radius: 8px; }}
QListWidget::item:selected {{ background-color: {C_ACCENT}; color: white; }}
QScrollBar:vertical {{
    background-color: transparent; width: 10px; border-radius: 5px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background-color: #263042; border-radius: 5px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background-color: #32405A; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QTabWidget::pane {{
    border: 1px solid {C_BORDER}; background-color: #0D1117; border-radius: 12px;
}}
QTabBar::tab {{
    background-color: {C_PANEL_2}; color: {C_TEXT_DIM};
    padding: 9px 18px; margin-right: 3px;
    border-top-left-radius: 10px; border-top-right-radius: 10px;
    font-size: 12px; font-weight: 700;
}}
QTabBar::tab:selected {{ background-color: {C_ACCENT}; color: white; }}
QScrollArea {{ border: none; background: transparent; }}
QSplitter::handle {{ background-color: {C_BORDER}; width: 2px; }}
QFrame#card {{
    background-color: {C_PANEL}; border: 1px solid {C_BORDER};
    border-radius: 14px;
}}
"""


class ChitchatBotGUI(QMainWindow):
    # how many blocks the live chat feed may hold (drops the oldest)
    _MAX_CHAT_FEED_BLOCKS = 400

    def __init__(self, development_mode=False):
        super().__init__()
        self.setWindowTitle("EVA Bot — Control Center")
        self.development_mode = development_mode

        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'chitchat-bot.ico')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        screen = QApplication.primaryScreen().availableGeometry()
        self.setMinimumSize(min(1280, screen.width() - 80), min(800, screen.height() - 60))
        self.showMaximized()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(GLOBAL_QSS)

        # ---- live-chat feed buffer (for the session filter) ----
        self._chat_feed_buffer = []
        # picture sender counters
        self.total_pics_sent = 0

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ================= SIDEBAR =================
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(240)
        root_layout.addWidget(sidebar)
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(14, 18, 14, 14)
        sb_layout.setSpacing(6)

        brand_row = QHBoxLayout()
        logo = QLabel("●")
        logo.setStyleSheet(f"color: {C_GREEN}; font-size: 22px; background: transparent;")
        brand_col = QVBoxLayout()
        brand_col.setSpacing(0)
        brand = QLabel("EVA BOT")
        brand.setObjectName("brand")
        brand_sub = QLabel("CONTROL CENTER")
        brand_sub.setObjectName("brandSub")
        brand_col.addWidget(brand)
        brand_col.addWidget(brand_sub)
        brand_row.addWidget(logo)
        brand_row.addSpacing(8)
        brand_row.addLayout(brand_col)
        brand_row.addStretch()
        sb_layout.addLayout(brand_row)
        sb_layout.addSpacing(16)

        # ---- navigation ----
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons = {}
        self.pages = QStackedWidget()
        nav_items = [
            ("dashboard", "🏠", "Dashboard"),
            ("livechat",  "💬", "Live Chat"),
            ("sessions",  "🖥️", "Sessions"),
            ("pictures",  "🖼️", "Pictures"),
            ("accounts",  "👤", "Accounts"),
            ("settings",  "⚙️", "Settings"),
            ("logs",      "📜", "Logs"),
        ]
        for key, emoji, label in nav_items:
            btn = QPushButton(f"  {emoji}   {label}")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setMinimumHeight(46)
            btn.setStyleSheet(self._nav_btn_style(False))
            btn.clicked.connect(lambda _=False, k=key: self._switch_page(k))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            sb_layout.addWidget(btn)
        self.nav_buttons["dashboard"].setChecked(True)
        self._active_nav = "dashboard"

        sb_layout.addStretch()

        # ---- CPU / RAM mini monitor ----
        sys_card = QFrame()
        sys_card.setObjectName("card")
        sys_card.setFixedHeight(92)
        sys_card_layout = QVBoxLayout(sys_card)
        sys_card_layout.setContentsMargins(10, 8, 10, 8)
        sys_card_layout.setSpacing(4)
        sc_title = QLabel("SYSTEM")
        sc_title.setObjectName("statTitle")
        sys_card_layout.addWidget(sc_title)
        self.cpu_label = self._create_stat_card("CPU", "0%", C_ORANGE)
        self.ram_label = self._create_stat_card("RAM", "0%", C_RED)
        cpu_row = QHBoxLayout()
        cpu_row.setSpacing(6)
        cpu_row.addWidget(self.cpu_label, 1)
        cpu_row.addWidget(self.ram_label, 1)
        sys_card_layout.addLayout(cpu_row)
        # compact styling for the two mini cards
        self.cpu_label.setStyleSheet(self._mini_card_style(C_ORANGE))
        self.ram_label.setStyleSheet(self._mini_card_style(C_RED))
        self.cpu_label.value_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self.ram_label.value_label.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        self.cpu_label.value_label.setStyleSheet("color:#FB923C; background:transparent; border:none;")
        self.ram_label.value_label.setStyleSheet("color:#FF5555; background:transparent; border:none;")
        self.cpu_label.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ram_label.value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cpu_label.setMinimumHeight(42)
        self.ram_label.setMinimumHeight(42)
        sb_layout.addWidget(sys_card)

        sb_layout.addSpacing(10)

        # ---- START / STOP ----
        self.start_button = QPushButton("▶  START")
        self.start_button.setObjectName("startBtn")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.setMinimumHeight(58)
        self.start_button.clicked.connect(self.start_bot)
        sb_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setObjectName("stopBtn")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_bot)
        sb_layout.addWidget(self.stop_button)

        # ================= CONTENT COLUMN =================
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        root_layout.addWidget(content, 1)

        # ---- topbar ----
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(64)
        content_layout.addWidget(topbar)
        tb_layout = QHBoxLayout(topbar)
        tb_layout.setContentsMargins(22, 10, 22, 10)
        tb_layout.setSpacing(12)

        self.page_emoji = QLabel("🏠")
        self.page_emoji.setObjectName("pageEmoji")
        self.page_title_label = QLabel("Dashboard")
        self.page_title_label.setStyleSheet(
            f"color: {C_TEXT}; font-size: 20px; font-weight: 800; background: transparent;")
        tb_layout.addWidget(self.page_emoji)
        tb_layout.addWidget(self.page_title_label)
        tb_layout.addStretch()

        self.live_stats_label = QLabel("Chats: 0 | Sent: 0 | Recv: 0 | Snaps: 0 | Pics: 0")
        self.live_stats_label.setObjectName("pill")
        self.live_stats_label.setStyleSheet(
            f"background-color: {C_PANEL_2}; color: {C_GREEN}; border: 1px solid {C_BORDER};"
            "font-size: 12px; padding: 6px 16px;")
        tb_layout.addWidget(self.live_stats_label)

        self.header_status = QLabel("● READY")
        self.header_status.setObjectName("pill")
        self.header_status.setStyleSheet(
            f"background-color: rgba(0,214,143,12%); color: {C_GREEN};"
            f"border: 1px solid {C_GREEN}; font-size: 12px; padding: 6px 16px;")
        tb_layout.addWidget(self.header_status)

        # ---- pages ----
        content_layout.addWidget(self.pages, 1)

        self._build_dashboard_page()
        self._build_livechat_page()
        self._build_sessions_page()
        self._build_pictures_page()
        self._build_accounts_page()
        self._build_settings_page()
        self._build_logs_page()

        self._load_settings_into_fields()

        self.thread_manager = None
        self.active_threads = {}
        self.status_text = "Ready"
        self.start_time = None

        self.runtime_timer = QTimer()
        self.runtime_timer.timeout.connect(self.update_runtime)
        self.runtime_timer.setInterval(1000)

        self.total_users_chatted = 0
        self.total_snaps_shared = 0
        self.total_messages_sent = 0
        self.total_messages_received = 0

        self.log_clear_timer = QTimer()
        self.log_clear_timer.timeout.connect(self.auto_clear_logs)
        self.log_clear_timer.setInterval(3000)

        self.current_thread_count = 0

        # live sessions / chat-feed state
        self.session_states = {}

        self.captcha_blink_timer = QTimer()
        self.captcha_blink_timer.timeout.connect(self._toggle_captcha_banner)

    # ------------------------------------------------------------------
    # THEME HELPERS
    # ------------------------------------------------------------------
    def _nav_btn_style(self, active):
        if active:
            return (f"QPushButton {{ text-align: left; background-color: {C_ACCENT};"
                    " color: white; border: none; border-radius: 11px;"
                    " padding: 10px 12px; font-size: 13px; font-weight: 700; }"
                    "QPushButton:checked { background-color: #8B5CF6; }")
        return ("QPushButton { text-align: left; background-color: transparent;"
                f" color: {C_TEXT_DIM}; border: none; border-radius: 11px;"
                " padding: 10px 12px; font-size: 13px; font-weight: 600; }"
                "QPushButton:hover { background-color: #161C2A; color: #E6EAF2; }"
                "QPushButton:checked { background-color: #8B5CF6; color: white; }")

    def _switch_page(self, key):
        titles = {
            "dashboard": ("🏠", "Dashboard"), "livechat": ("💬", "Live Chat"),
            "sessions": ("🖥️", "Browser Sessions"), "pictures": ("🖼️", "Picture Sender"),
            "accounts": ("👤", "Accounts & Proxies"), "settings": ("⚙️", "Settings"),
            "logs": ("📜", "Logs"),
        }
        emoji, title = titles.get(key, ("🏠", "Dashboard"))
        self.page_emoji.setText(emoji)
        self.page_title_label.setText(title)
        for k, btn in self.nav_buttons.items():
            btn.setStyleSheet(self._nav_btn_style(k == key))
        self.pages.setCurrentIndex(list(self.nav_buttons).index(key))
        self._active_nav = key

    def _mini_card_style(self, accent):
        return (f"QFrame {{ background-color: {C_PANEL_2}; border: 1px solid {C_BORDER};"
                f" border-top: 2px solid {accent}; border-radius: 9px; }}")

    def _create_stat_card(self, title, value, accent_color, icon=""):
        panel = QFrame()
        panel.setObjectName("card")
        panel.setMinimumHeight(96)
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        icon_label = QLabel(icon or "•")
        icon_label.setStyleSheet(
            f"color: {accent_color}; font-size: 26px; background: transparent; border: none;")
        icon_label.setFixedWidth(34)
        layout.addWidget(icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("statTitle")
        value_label = QLabel(value)
        value_label.setObjectName("statValue")
        value_label.setStyleSheet(
            f"color: {accent_color}; font-size: 24px; font-weight: 800;"
            " background: transparent; border: none;")
        text_col.addWidget(title_label)
        text_col.addWidget(value_label)
        layout.addLayout(text_col, 1)

        panel.value_label = value_label
        panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        return panel

    def _preset_btn_style(self, color):
        return (f"QPushButton {{ background-color: {color}; color: white; border: none;"
                " border-radius: 10px; padding: 12px 22px; font-weight: 700; }"
                "QPushButton:hover { background-color: rgba(255,255,255,12%); }"
                f"QPushButton {{ background-color: {color}; }}")

    def _browse_btn_style(self):
        return (f"QPushButton {{ background-color: {C_PANEL_2}; color: {C_TEXT};"
                f" border: 1px solid {C_BORDER}; border-radius: 9px; padding: 9px 16px; }}"
                "QPushButton:hover { background-color: #1D2434; }")

    def _clear_btn_style(self):
        return (f"QPushButton {{ background-color: {C_PANEL_2}; color: {C_TEXT};"
                f" border: 1px solid {C_BORDER}; border-radius: 9px; padding: 9px; }}"
                f"QPushButton:hover {{ background-color: {C_RED}; color: white; }}")

    def _make_labeled(self, label_text, widget, tooltip=None):
        """Wrap a control in a vertical label + widget block (big & readable)."""
        col = QVBoxLayout()
        col.setSpacing(5)
        lab = QLabel(label_text)
        lab.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 12px; font-weight: 600;"
                          " background: transparent;")
        col.addWidget(lab)
        if isinstance(widget, QVBoxLayout):
            col.addLayout(widget)
        else:
            col.addWidget(widget)
        if tooltip:
            lab.setToolTip(tooltip)
            if not isinstance(widget, QVBoxLayout):
                widget.setToolTip(tooltip)
        wrap = QWidget()
        wrap.setLayout(col)
        return wrap

    # ------------------------------------------------------------------
    # PAGE 1 — DASHBOARD
    # ------------------------------------------------------------------
    def _build_dashboard_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # ---- stat cards (2 rows × 4) ----
        cards_row1 = QHBoxLayout()
        cards_row1.setSpacing(12)
        self.status_label = self._create_stat_card("STATUS", "Ready", C_GREEN, "⚡")
        self.threads_label = self._create_stat_card("THREADS", "0/0", C_BLUE, "🧵")
        self.sessions_label = self._create_stat_card("SESSIONS", "0", C_TEAL, "🖥️")
        self.banned_label = self._create_stat_card("BANNED", "0", C_RED, "⛔")
        for card in (self.status_label, self.threads_label, self.sessions_label, self.banned_label):
            cards_row1.addWidget(card, 1)
        layout.addLayout(cards_row1)

        cards_row2 = QHBoxLayout()
        cards_row2.setSpacing(12)
        self.users_label = self._create_stat_card("USERS CHATTED", "0", C_PURPLE, "👤")
        self.snaps_label = self._create_stat_card("SNAPS", "0", C_ACCENT, "💫")
        self.pics_label = self._create_stat_card("PICS SENT", "0", C_PINK, "🖼️")
        self.runtime_label = self._create_stat_card("RUNTIME", "00:00:00", C_AMBER, "⏱️")
        for card in (self.users_label, self.snaps_label, self.pics_label, self.runtime_label):
            cards_row2.addWidget(card, 1)
        layout.addLayout(cards_row2)

        # ---- quick controls ----
        quick_group = QGroupBox("🚀  QUICK CONTROLS")
        quick_layout = QGridLayout(quick_group)
        quick_layout.setContentsMargins(16, 20, 16, 16)
        quick_layout.setHorizontalSpacing(18)
        quick_layout.setVerticalSpacing(12)

        self.thread_count_input = QLineEdit("1")
        self.thread_count_input.setFixedWidth(110)
        self.thread_count_input.setToolTip("How many browser sessions run at once (1-10). More threads = more PC load.")
        quick_layout.addWidget(self._make_labeled("Threads", self.thread_count_input,
                                                  "1-10 browser sessions (keep low on a slow PC)"), 0, 0)

        self.chat_timeout_input = QLineEdit("8")
        self.chat_timeout_input.setFixedWidth(110)
        quick_layout.addWidget(self._make_labeled("Max Replies", self.chat_timeout_input,
                                                  "Max replies bot sends before moving to next user"), 0, 1)

        delay_widget = QWidget()
        delay_layout = QHBoxLayout(delay_widget)
        delay_layout.setContentsMargins(0, 0, 0, 0)
        delay_layout.setSpacing(6)
        self.reply_delay_min_input = QLineEdit("1.0")
        self.reply_delay_min_input.setFixedWidth(70)
        self.reply_delay_max_input = QLineEdit("3.0")
        self.reply_delay_max_input.setFixedWidth(70)
        delay_layout.addWidget(self.reply_delay_min_input)
        to_lbl = QLabel("–")
        to_lbl.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        delay_layout.addWidget(to_lbl)
        delay_layout.addWidget(self.reply_delay_max_input)
        quick_layout.addWidget(self._make_labeled("Reply Delay (s)", delay_widget,
                                                  "Random pause between replies"), 0, 2)

        self.chat_time_combo = QComboBox()
        self.chat_time_combo.setToolTip(
            "How long the bot chats with ONE stranger.\n"
            "Default: no time limit — chat continues until the snap is shared.")
        self.chat_time_combo.addItem("∞  Until Snap Share (default)", None)
        for minutes in (1, 2, 3, 5, 10, 15, 20, 30, 45, 60):
            self.chat_time_combo.addItem(f"{minutes} min", minutes)
        self.chat_time_combo.setFixedWidth(210)
        quick_layout.addWidget(self._make_labeled("Chat Time", self.chat_time_combo,
                                                  "Per-stranger chat duration"), 0, 3)

        self.newchat_delay_input = QLineEdit("")
        self.newchat_delay_input.setFixedWidth(110)
        self.newchat_delay_input.setPlaceholderText("auto")
        quick_layout.addWidget(self._make_labeled("New Chat Delay (s)", self.newchat_delay_input,
                                                  "Empty = use data/config.json"), 1, 0)

        self.rest_every_input = QLineEdit("")
        self.rest_every_input.setFixedWidth(110)
        self.rest_every_input.setPlaceholderText("auto")
        quick_layout.addWidget(self._make_labeled("Rest Every (min)", self.rest_every_input,
                                                  "Empty = config.json"), 1, 1)

        self.rest_for_input = QLineEdit("")
        self.rest_for_input.setFixedWidth(110)
        self.rest_for_input.setPlaceholderText("auto")
        quick_layout.addWidget(self._make_labeled("Rest For (min)", self.rest_for_input,
                                                  "Empty = config.json"), 1, 2)

        toggles_col = QVBoxLayout()
        toggles_col.setSpacing(8)
        self.show_browser_checkbox = QCheckBox("Show Browser")
        self.show_browser_checkbox.setToolTip("Show browser window during chat")
        self.hide_after_login_checkbox = QCheckBox("Hide After Login")
        self.hide_after_login_checkbox.setToolTip("Move browser off-screen after login completes")
        self.hide_after_login_checkbox.setChecked(True)
        self.shuffle_chat_checkbox = QCheckBox("Shuffle Chat")
        self.shuffle_chat_checkbox.setToolTip("Randomize message order per conversation")
        for cb in (self.show_browser_checkbox, self.hide_after_login_checkbox,
                   self.shuffle_chat_checkbox):
            toggles_col.addWidget(cb)
        quick_layout.addWidget(self._make_labeled("Browser", toggles_col,
                                                 "Window visibility options"), 1, 3)

        layout.addWidget(quick_group)

        # ---- picture quick-toggle ----
        pic_group = QGroupBox("🖼️  PICTURE SENDER")
        pic_layout = QHBoxLayout(pic_group)
        pic_layout.setContentsMargins(16, 20, 16, 16)
        pic_layout.setSpacing(16)

        self.pic_send_checkbox = QCheckBox("Send random picture\nat middle of chat")
        self.pic_send_checkbox.setToolTip(
            "At the middle chat point the bot uploads one random picture\n"
            "from your folder AND sends the SMS reply — chat engine untouched.")
        self.pic_send_checkbox.toggled.connect(self._pic_quick_toggle)
        pic_layout.addWidget(self.pic_send_checkbox)

        self.pic_folder_input = QLineEdit()
        self.pic_folder_input.setPlaceholderText("Pictures folder (select on Pictures page)…")
        pic_layout.addWidget(self.pic_folder_input, 1)

        self.pic_status_label = QLabel("no folder selected")
        self.pic_status_label.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;"
                                            " font-size: 12px;")
        pic_layout.addWidget(self.pic_status_label)

        goto_pics = QPushButton("Manage 🖼️")
        goto_pics.setCursor(Qt.CursorShape.PointingHandCursor)
        goto_pics.clicked.connect(lambda: self._switch_page("pictures"))
        pic_layout.addWidget(goto_pics)

        layout.addWidget(pic_group)

        # ---- first message ----
        first_msg_group = QGroupBox("✉️  FIRST MESSAGE")
        first_layout = QHBoxLayout(first_msg_group)
        first_layout.setContentsMargins(16, 20, 16, 16)
        first_layout.setSpacing(16)

        self.first_message_true_radio = QRadioButton("Send first")
        self.first_message_false_radio = QRadioButton("Wait for stranger")
        self.first_message_random_radio = QRadioButton("Random")
        self.first_message_false_radio.setChecked(True)
        self.first_message_true_radio.setToolTip("Send the first message after configured delay")
        self.first_message_false_radio.setToolTip("Wait for stranger to message first")
        self.first_message_random_radio.setToolTip("Randomly choose between sending and waiting")
        self.first_message_group = QButtonGroup(self)
        for radio in (self.first_message_true_radio, self.first_message_false_radio,
                      self.first_message_random_radio):
            self.first_message_group.addButton(radio)
            first_layout.addWidget(radio)

        self.first_message_delay_min_input = QLineEdit("1.0")
        self.first_message_delay_min_input.setFixedWidth(70)
        self.first_message_delay_max_input = QLineEdit("3.0")
        self.first_message_delay_max_input.setFixedWidth(70)
        first_layout.addWidget(QLabel("Delay (s):"))
        first_layout.addWidget(self.first_message_delay_min_input)
        dash = QLabel("–")
        dash.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        first_layout.addWidget(dash)
        first_layout.addWidget(self.first_message_delay_max_input)
        first_layout.addStretch()

        self.first_message_group.buttonToggled.connect(self.update_first_message_delay_state)
        self.update_first_message_delay_state()
        layout.addWidget(first_msg_group)

        # ---- performance presets ----
        preset_group = QGroupBox("🎛️  PERFORMANCE PRESETS  (for slow PC — low lag)")
        preset_layout = QHBoxLayout(preset_group)
        preset_layout.setContentsMargins(16, 20, 16, 16)
        preset_layout.setSpacing(12)
        btn_low = QPushButton("🟠 LOW — 1 thread (0% lag)")
        btn_low.setStyleSheet(self._preset_btn_style("#C2703C"))
        btn_low.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_low.clicked.connect(lambda: self._apply_preset("low"))
        btn_med = QPushButton("🔵 MEDIUM — 3 threads")
        btn_med.setStyleSheet(self._preset_btn_style("#3D7DC0"))
        btn_med.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_med.clicked.connect(lambda: self._apply_preset("medium"))
        btn_high = QPushButton("🔴 HIGH — 5 threads")
        btn_high.setStyleSheet(self._preset_btn_style("#C0392B"))
        btn_high.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_high.clicked.connect(lambda: self._apply_preset("high"))
        for b in (btn_low, btn_med, btn_high):
            preset_layout.addWidget(b)
        preset_layout.addStretch()
        layout.addWidget(preset_group)

        # ---- captcha banner ----
        self.captcha_banner = QLabel("")
        self.captcha_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.captcha_banner.setVisible(False)
        self.captcha_banner.setStyleSheet(
            f"QLabel {{ background-color: {C_RED}; color: white;"
            " border: 2px solid #FFFFFF; border-radius: 10px;"
            " padding: 12px 16px; font-size: 15px; font-weight: bold; }")
        layout.addWidget(self.captcha_banner)

        # ---- thread status ----
        status_group = QGroupBox("🧵  THREAD STATUS")
        status_layout = QHBoxLayout(status_group)
        status_layout.setContentsMargins(16, 20, 16, 16)
        self.thread_status_list = QListWidget()
        self.thread_status_list.setMaximumHeight(110)
        status_layout.addWidget(self.thread_status_list, 1)
        self.sound_checkbox = QCheckBox("🔔 Alert Sounds")
        self.sound_checkbox.setToolTip("Beep on captcha / ban / connect / snap events")
        self.sound_checkbox.setChecked(True)
        status_layout.addWidget(self.sound_checkbox)
        layout.addWidget(status_group)

        # ---- compact live log (full log on Logs page) ----
        log_group = QGroupBox("📋  LIVE LOG  (full logs on the Logs page)")
        log_v = QVBoxLayout(log_group)
        log_v.setContentsMargins(16, 20, 16, 16)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(170)
        log_font = QFont('Consolas', 10)
        log_font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_text.setFont(log_font)
        self.log_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        log_v.addWidget(self.log_text)
        layout.addWidget(log_group)

        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PAGE 2 — LIVE CHAT
    # ------------------------------------------------------------------
    def _build_livechat_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel("💬  Live conversations — every incoming SMS and bot reply, in real time")
        title.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 13px; background: transparent;")
        head.addWidget(title)
        head.addStretch()

        self.livechat_filter_combo = QComboBox()
        self.livechat_filter_combo.addItem("All sessions")
        self.livechat_filter_combo.setFixedWidth(170)
        self.livechat_filter_combo.currentTextChanged.connect(self._render_chat_feed)
        head.addWidget(QLabel("Show:"))
        head.addWidget(self.livechat_filter_combo)

        self.livechat_autoscroll = QCheckBox("Auto-scroll")
        self.livechat_autoscroll.setChecked(True)
        head.addWidget(self.livechat_autoscroll)

        self.live_chat_clear_btn = QPushButton("🗑 Clear Feed")
        self.live_chat_clear_btn.setStyleSheet(self._browse_btn_style())
        self.live_chat_clear_btn.clicked.connect(self._clear_live_chat)
        head.addWidget(self.live_chat_clear_btn)
        layout.addLayout(head)

        self.live_chat_text = QTextEdit()
        self.live_chat_text.setReadOnly(True)
        feed_font = QFont('Consolas', 11)
        feed_font.setStyleHint(QFont.StyleHint.Monospace)
        self.live_chat_text.setFont(feed_font)
        self.live_chat_text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self.live_chat_text, 1)

        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PAGE 3 — SESSIONS
    # ------------------------------------------------------------------
    def _build_sessions_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.sess_active_card = self._create_stat_card("ACTIVE SESSIONS", "0", C_GREEN, "🟢")
        self.sess_banned_card = self._create_stat_card("BANNED", "0", C_RED, "⛔")
        self.sess_pics_card = self._create_stat_card("PICS SENT", "0", C_PINK, "🖼️")
        for c in (self.sess_active_card, self.sess_banned_card, self.sess_pics_card):
            cards.addWidget(c, 1)
        layout.addLayout(cards)

        info = QLabel("Browser sessions save automatically on login/create and are "
                      "refreshed while chatting (blind/expired ones are repaired with "
                      "accounts.txt). Banned accounts show here live. "
                      "Chats / Msgs / Snaps / Pics update in real time.")
        info.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 12px; background: transparent;")
        layout.addWidget(info)

        self.sessions_table = QTableWidget(0, 7)
        self.sessions_table.setHorizontalHeaderLabels(
            ["Session #", "Account", "Status", "Chats", "Msgs In/Out", "Snaps", "Pics"])
        header = self.sessions_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sessions_table.verticalHeader().setVisible(False)
        self.sessions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.sessions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sessions_table.setAlternatingRowColors(True)
        self.sessions_table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.sessions_table, 1)

        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PAGE 4 — PICTURES
    # ------------------------------------------------------------------
    def _build_pictures_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        sel_group = QGroupBox("📁  PICTURES FOLDER")
        sel_layout = QVBoxLayout(sel_group)
        sel_layout.setContentsMargins(16, 20, 16, 16)
        sel_layout.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.pic_folder_input2 = QLineEdit()
        self.pic_folder_input2.setPlaceholderText(
            "C:\\Users\\you\\Pictures\\eva  —  click Select Folder…")
        row.addWidget(self.pic_folder_input2, 1)
        self.pic_browse_btn = QPushButton("📂 Select Folder…")
        self.pic_browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pic_browse_btn.setStyleSheet(
            f"QPushButton {{ background-color: {C_ACCENT}; color: white; border: none;"
            " border-radius: 10px; padding: 11px 20px; font-weight: 700; }"
            "QPushButton:hover { background-color: #7C4DF3; }")
        self.pic_browse_btn.clicked.connect(self._select_pics_folder)
        row.addWidget(self.pic_browse_btn)
        self.pic_rename_btn = QPushButton("🔁 Rename All Now")
        self.pic_rename_btn.setStyleSheet(self._browse_btn_style())
        self.pic_rename_btn.clicked.connect(self._rename_pictures_now)
        row.addWidget(self.pic_rename_btn)
        sel_layout.addLayout(row)

        # mirror of the folder path (dashboard field stays the single source)
        self.pic_folder_input2.textChanged.connect(self._pic_folder_mirror)
        self.pic_folder_input.textChanged.connect(self._pic_folder_mirror_back)

        self.pic_count_label = QLabel("No folder selected yet.")
        self.pic_count_label.setStyleSheet(
            f"color: {C_TEXT}; font-size: 15px; font-weight: 700; background: transparent;")
        sel_layout.addWidget(self.pic_count_label)

        self.pic_status_label2 = QLabel("")
        self.pic_status_label2.setWordWrap(True)
        self.pic_status_label2.setStyleSheet(
            f"color: {C_GREEN}; font-size: 13px; background: transparent;")
        sel_layout.addWidget(self.pic_status_label2)
        layout.addWidget(sel_group)

        cfg_group = QGroupBox("⚙️  SEND SETTINGS")
        cfg_layout = QHBoxLayout(cfg_group)
        cfg_layout.setContentsMargins(16, 20, 16, 16)
        cfg_layout.setSpacing(18)

        self.pic_send_checkbox2 = QCheckBox("Enable picture sending")
        self.pic_send_checkbox2.setToolTip(
            "When enabled, each stranger receives ONE random picture\n"
            "at the middle chat point, right before the bot's SMS reply.")
        self.pic_send_checkbox2.toggled.connect(self._pic_toggle_mirror)
        self.pic_send_checkbox.toggled.connect(self._pic_toggle_mirror_back)
        cfg_layout.addWidget(self.pic_send_checkbox2)

        self.pic_after_input = QLineEdit("4")
        self.pic_after_input.setFixedWidth(90)
        cfg_layout.addWidget(self._make_labeled(
            "Send pic at message #", self.pic_after_input,
            "The picture goes out together with this numbered reply (middle chat point). "
            "Default 4 = middle of an 8-reply chat."))

        self.pics_per_chat_input = QLineEdit("1")
        self.pics_per_chat_input.setFixedWidth(90)
        cfg_layout.addWidget(self._make_labeled(
            "Pics per stranger", self.pics_per_chat_input,
            "How many pictures one stranger receives (default 1)."))

        cfg_layout.addStretch()
        layout.addWidget(cfg_group)

        how_group = QGroupBox("ℹ️  HOW IT WORKS")
        how_layout = QVBoxLayout(how_group)
        how_layout.setContentsMargins(16, 20, 16, 16)
        for line in (
            "1.  Select a folder — every picture inside is auto-renamed to eva_pic_001.jpg, eva_pic_002.png, …",
            "2.  Each stranger gets a random picture at the middle chat point — no picture is repeated until the pool is used up.",
            "3.  Picture + SMS go together: the pic uploads first, then the normal chat reply is sent.",
            "4.  The chat engine is NEVER touched — the bot replies exactly like before and never 'remembers' pictures.",
            "5.  If the site refuses the upload, the bot simply continues chatting — nothing breaks.",
        ):
            l = QLabel(line)
            l.setWordWrap(True)
            l.setStyleSheet(f"color: {C_TEXT_DIM}; font-size: 12px; background: transparent;")
            how_layout.addWidget(l)
        layout.addWidget(how_group)

        preview_group = QGroupBox("🖼️  PICTURE POOL (first 60 files)")
        pv_layout = QVBoxLayout(preview_group)
        pv_layout.setContentsMargins(16, 20, 16, 16)
        self.pic_files_preview = QListWidget()
        self.pic_files_preview.setMaximumHeight(200)
        pv_layout.addWidget(self.pic_files_preview)
        layout.addWidget(preview_group)

        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PAGE 5 — ACCOUNTS
    # ------------------------------------------------------------------
    def _build_accounts_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(22, 18, 22, 18)
        outer.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        mode_group = QGroupBox("👤  ACCOUNT MODE")
        mode_layout = QHBoxLayout(mode_group)
        mode_layout.setContentsMargins(16, 20, 16, 16)
        mode_layout.setSpacing(18)
        self.create_account_radio = QRadioButton("Create")
        self.login_account_radio = QRadioButton("Login")
        self.restore_account_radio = QRadioButton("Restore")
        self.login_account_radio.setChecked(True)
        self.account_mode_group = QButtonGroup(self)
        for radio in (self.create_account_radio, self.login_account_radio,
                      self.restore_account_radio):
            self.account_mode_group.addButton(radio)
        self.account_mode_group.buttonToggled.connect(self.update_account_mode)
        for radio in (self.restore_account_radio, self.create_account_radio,
                      self.login_account_radio):
            mode_layout.addWidget(radio)
        mode_layout.addStretch()
        layout.addWidget(mode_group)

        files_group = QGroupBox("📂  ACCOUNT FILES")
        files_layout = QVBoxLayout(files_group)
        files_layout.setContentsMargins(16, 20, 16, 16)
        files_layout.setSpacing(12)

        accounts_row = QWidget()
        accounts_row_layout = QHBoxLayout(accounts_row)
        accounts_row_layout.setContentsMargins(0, 0, 0, 0)
        accounts_row_layout.setSpacing(8)
        accounts_row_layout.addWidget(QLabel("Accounts File:"))
        self.accounts_file_input = QLineEdit()
        self.accounts_file_input.setPlaceholderText("email:password format — one per line")
        accounts_row_layout.addWidget(self.accounts_file_input, 1)
        accounts_browse_btn = QPushButton("Browse")
        accounts_browse_btn.setStyleSheet(self._browse_btn_style())
        accounts_browse_btn.clicked.connect(self.browse_accounts_file)
        accounts_row_layout.addWidget(accounts_browse_btn)
        accounts_clear_btn = QPushButton("✕")
        accounts_clear_btn.setMaximumWidth(34)
        accounts_clear_btn.setStyleSheet(self._clear_btn_style())
        accounts_clear_btn.clicked.connect(self.clear_accounts_file)
        accounts_row_layout.addWidget(accounts_clear_btn)
        files_layout.addWidget(accounts_row)

        self.accounts_browse_widget = accounts_row
        accounts_browse_policy = self.accounts_browse_widget.sizePolicy()
        accounts_browse_policy.setRetainSizeWhenHidden(True)
        self.accounts_browse_widget.setSizePolicy(accounts_browse_policy)

        files_layout.addWidget(QLabel("Loaded Accounts:"))
        self.accounts_list = QListWidget()
        self.accounts_list.setMaximumHeight(150)
        files_layout.addWidget(self.accounts_list)
        layout.addWidget(files_group)

        proxy_group = QGroupBox("🌐  PROXY CONFIGURATION")
        proxy_layout = QVBoxLayout(proxy_group)
        proxy_layout.setContentsMargins(16, 20, 16, 16)
        proxy_layout.setSpacing(12)

        proxy_row = QWidget()
        proxy_row_layout = QHBoxLayout(proxy_row)
        proxy_row_layout.setContentsMargins(0, 0, 0, 0)
        proxy_row_layout.setSpacing(8)
        proxy_row_layout.addWidget(QLabel("Proxy File:"))
        self.proxy_file_input = QLineEdit()
        self.proxy_file_input.setPlaceholderText("host:port:user:pass — one per line (optional)")
        proxy_row_layout.addWidget(self.proxy_file_input, 1)
        proxy_browse_btn = QPushButton("Browse")
        proxy_browse_btn.setStyleSheet(self._browse_btn_style())
        proxy_browse_btn.clicked.connect(self.browse_proxy_file)
        proxy_row_layout.addWidget(proxy_browse_btn)
        proxy_clear_btn = QPushButton("✕")
        proxy_clear_btn.setMaximumWidth(34)
        proxy_clear_btn.setStyleSheet(self._clear_btn_style())
        proxy_clear_btn.clicked.connect(self.clear_proxy_file)
        proxy_row_layout.addWidget(proxy_clear_btn)
        proxy_layout.addWidget(proxy_row)

        self.proxy_warning_label = QLabel()
        self.proxy_warning_label.setWordWrap(True)
        self.proxy_warning_label.setMinimumHeight(36)
        self.proxy_warning_label.setStyleSheet(
            f"QLabel {{ background-color: {C_AMBER}; color: #242933;"
            " border: 2px solid #FF9800; border-radius: 8px;"
            " padding: 8px 12px; font-size: 12px; font-weight: bold; }")
        self.proxy_warning_label.setVisible(False)
        proxy_layout.addWidget(self.proxy_warning_label)
        layout.addWidget(proxy_group)
        layout.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PAGE 6 — SETTINGS
    # ------------------------------------------------------------------
    def _build_settings_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(22, 18, 22, 18)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        browser_group = QGroupBox("🌍  BROWSER")
        browser_layout = QGridLayout(browser_group)
        browser_layout.setContentsMargins(16, 20, 16, 16)
        browser_layout.setHorizontalSpacing(16)
        browser_layout.addWidget(QLabel("Engine:"), 0, 0)
        self.browser_engine_combo = QComboBox()
        self.browser_engine_combo.addItems(["Chromium (Fast)", "Camoufox (Stealth)"])
        self.browser_engine_combo.setFixedWidth(220)
        browser_layout.addWidget(self.browser_engine_combo, 0, 1)
        note = QLabel("Engine setting requires restart")
        note.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
        browser_layout.addWidget(note, 0, 2)
        layout.addWidget(browser_group)

        timing_group = QGroupBox("⏱️  CHAT TIMING  (config.json)")
        timing_layout = QGridLayout(timing_group)
        timing_layout.setContentsMargins(16, 20, 16, 16)
        timing_layout.setHorizontalSpacing(14)
        timing_layout.setVerticalSpacing(10)
        self.new_chat_delay_min = QLineEdit("5")
        self.new_chat_delay_min.setFixedWidth(80)
        self.new_chat_delay_max = QLineEdit("8")
        self.new_chat_delay_max.setFixedWidth(80)
        self.rest_interval_min = QLineEdit("8")
        self.rest_interval_min.setFixedWidth(80)
        self.rest_interval_max = QLineEdit("15")
        self.rest_interval_max.setFixedWidth(80)
        self.rest_duration_min = QLineEdit("2")
        self.rest_duration_min.setFixedWidth(80)
        self.rest_duration_max = QLineEdit("3")
        self.rest_duration_max.setFixedWidth(80)
        for r, (label, a, b) in enumerate((
                ("New Chat Delay (s):", self.new_chat_delay_min, self.new_chat_delay_max),
                ("Rest Interval (min):", self.rest_interval_min, self.rest_interval_max),
                ("Rest Duration (min):", self.rest_duration_min, self.rest_duration_max))):
            timing_layout.addWidget(QLabel(label), r, 0)
            timing_layout.addWidget(a, r, 1)
            dash = QLabel("–")
            dash.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
            timing_layout.addWidget(dash, r, 2)
            timing_layout.addWidget(b, r, 3)
        layout.addWidget(timing_group)

        behavior_group = QGroupBox("🧠  HUMAN BEHAVIOR")
        behavior_layout = QGridLayout(behavior_group)
        behavior_layout.setContentsMargins(16, 20, 16, 16)
        behavior_layout.setHorizontalSpacing(14)
        behavior_layout.setVerticalSpacing(10)
        self.reaction_pause_min = QLineEdit("0.5")
        self.reaction_pause_min.setFixedWidth(80)
        self.reaction_pause_max = QLineEdit("1.2")
        self.reaction_pause_max.setFixedWidth(80)
        self.typing_speed_min = QLineEdit("6")
        self.typing_speed_min.setFixedWidth(80)
        self.typing_speed_max = QLineEdit("12")
        self.typing_speed_max.setFixedWidth(80)
        self.read_reply_min = QLineEdit("1")
        self.read_reply_min.setFixedWidth(80)
        self.read_reply_max = QLineEdit("3")
        self.read_reply_max.setFixedWidth(80)
        for r, (label, a, b) in enumerate((
                ("Reaction Pause (s):", self.reaction_pause_min, self.reaction_pause_max),
                ("Typing Speed (cps):", self.typing_speed_min, self.typing_speed_max),
                ("Read Reply (s):", self.read_reply_min, self.read_reply_max))):
            behavior_layout.addWidget(QLabel(label), r, 0)
            behavior_layout.addWidget(a, r, 1)
            dash = QLabel("–")
            dash.setStyleSheet(f"color: {C_TEXT_DIM}; background: transparent;")
            behavior_layout.addWidget(dash, r, 2)
            behavior_layout.addWidget(b, r, 3)
        layout.addWidget(behavior_group)

        governor_group = QGroupBox("🛡️  RESOURCE GOVERNOR")
        governor_layout = QGridLayout(governor_group)
        governor_layout.setContentsMargins(16, 20, 16, 16)
        governor_layout.setHorizontalSpacing(14)
        governor_layout.setVerticalSpacing(10)
        self.cpu_warn_input = QLineEdit("70")
        self.cpu_warn_input.setFixedWidth(80)
        self.ram_warn_input = QLineEdit("72")
        self.ram_warn_input.setFixedWidth(80)
        self.ram_recycle_input = QLineEdit("82")
        self.ram_recycle_input.setFixedWidth(80)
        self.ram_redline_input = QLineEdit("92")
        self.ram_redline_input.setFixedWidth(80)
        for r, pairs in enumerate((
                (("CPU Warn (%):", self.cpu_warn_input), ("RAM Warn (%):", self.ram_warn_input)),
                (("RAM Recycle (%):", self.ram_recycle_input), ("RAM Redline (%):", self.ram_redline_input)))):
            for c, (label, edit) in enumerate(pairs):
                governor_layout.addWidget(QLabel(label), r, c * 2)
                governor_layout.addWidget(edit, r, c * 2 + 1)
        layout.addWidget(governor_group)

        config_group = QGroupBox("💾  CONFIG FILE (config.json)")
        config_layout = QHBoxLayout(config_group)
        config_layout.setContentsMargins(16, 20, 16, 16)
        config_layout.setSpacing(10)
        self.config_path_label = QLabel("config.json")
        self.config_path_label.setStyleSheet(f"color: {C_TEXT_DIM};")
        config_layout.addWidget(self.config_path_label)
        config_layout.addStretch()
        reload_btn = QPushButton("↻ Reload Config")
        reload_btn.setStyleSheet(self._browse_btn_style())
        reload_btn.clicked.connect(self._reload_config_from_file)
        config_layout.addWidget(reload_btn)
        save_btn = QPushButton("💾 Save Config")
        save_btn.setStyleSheet(
            f"QPushButton {{ background-color: {C_ACCENT}; color: white; border: none;"
            " border-radius: 9px; padding: 10px 20px; font-weight: 700; }"
            "QPushButton:hover { background-color: #7C4DF3; }")
        save_btn.clicked.connect(self._save_config_to_file)
        config_layout.addWidget(save_btn)
        layout.addWidget(config_group)
        layout.addStretch()

        scroll.setWidget(scroll_content)
        outer.addWidget(scroll)
        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PAGE 7 — LOGS
    # ------------------------------------------------------------------
    def _build_logs_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        filter_row = QWidget()
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(8)
        filter_layout.addWidget(QLabel("🔍"))
        self.log_search_input = QLineEdit()
        self.log_search_input.setPlaceholderText("Search logs…")
        self.log_search_input.setFixedWidth(320)
        self.log_search_input.textChanged.connect(self._filter_logs)
        filter_layout.addWidget(self.log_search_input)

        self.log_clear_btn = QPushButton("Clear Logs")
        self.log_clear_btn.setStyleSheet(self._browse_btn_style())
        self.log_clear_btn.clicked.connect(self._clear_all_logs)
        filter_layout.addWidget(self.log_clear_btn)

        self.log_export_btn = QPushButton("⬇ Export Logs")
        self.log_export_btn.setStyleSheet(self._browse_btn_style())
        self.log_export_btn.clicked.connect(self._export_logs)
        filter_layout.addWidget(self.log_export_btn)
        filter_layout.addStretch()
        self.auto_scroll_checkbox = QCheckBox("Auto-scroll")
        self.auto_scroll_checkbox.setChecked(True)
        filter_layout.addWidget(self.auto_scroll_checkbox)
        layout.addWidget(filter_row)

        self.log_tab_widget = QTabWidget()
        all_logs = QTextEdit()
        all_logs.setReadOnly(True)
        log_font = QFont('Consolas', 10)
        log_font.setStyleHint(QFont.StyleHint.Monospace)
        all_logs.setFont(log_font)
        all_logs.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        all_logs.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.log_tab_widget.addTab(all_logs, "All Logs")
        self.thread_logs = {}
        layout.addWidget(self.log_tab_widget, 1)
        self.pages.addWidget(page)

    # ------------------------------------------------------------------
    # PICTURE SENDER — GUI logic (folder select, auto-rename, pool info)
    # ------------------------------------------------------------------
    _PIC_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

    @staticmethod
    def rename_pictures_in_folder(folder_path, prefix="eva_pic"):
        """Rename EVERY picture in the folder to prefix_001.ext, prefix_002.ext …

        Collision-safe (two passes via temp names). Pure file operation —
        the chat engine is never involved.
        Returns (renamed_count, errors).
        """
        if not folder_path or not os.path.isdir(folder_path):
            return 0, [f"Folder not found: {folder_path}"]
        exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        try:
            files = sorted(
                f for f in os.listdir(folder_path)
                if os.path.isfile(os.path.join(folder_path, f))
                and os.path.splitext(f)[1].lower() in exts)
        except Exception as e:
            return 0, [str(e)]
        # pass 1 — temp names (avoid eva_pic_001.jpg already existing)
        temps = []
        for i, f in enumerate(files):
            ext = os.path.splitext(f)[1].lower()
            tmp = f"__eva_tmp_{i:04d}{ext}"
            try:
                os.rename(os.path.join(folder_path, f), os.path.join(folder_path, tmp))
                temps.append(tmp)
            except Exception:
                temps.append(None)
        # pass 2 — final names
        errors = []
        n = 0
        for i, tmp in enumerate(temps, start=1):
            if tmp is None:
                continue
            ext = os.path.splitext(tmp)[1].lower()
            final = f"{prefix}_{i:03d}{ext}"
            try:
                os.rename(os.path.join(folder_path, tmp), os.path.join(folder_path, final))
                n += 1
            except Exception as e:
                errors.append(str(e))
        return n, errors

    def _select_pics_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select Pictures Folder", os.path.expanduser("~"))
        if not folder:
            return
        self.pic_folder_input.setText(folder)
        self._refresh_pics_info()
        # auto-rename immediately (requirement: all pics auto-rename)
        renamed, errors = self.rename_pictures_in_folder(folder)
        if renamed:
            self.pic_status_label2.setText(
                f"✓ Auto-renamed {renamed} picture(s) → eva_pic_001 … eva_pic_{renamed:03d}")
            self.pic_status_label2.setStyleSheet(
                f"color: {C_GREEN}; font-size: 13px; background: transparent;")
        elif errors:
            self.pic_status_label2.setText("⚠ " + "; ".join(errors[:3]))
            self.pic_status_label2.setStyleSheet(
                f"color: {C_AMBER}; font-size: 13px; background: transparent;")
        else:
            self.pic_status_label2.setText("⚠ No pictures found in that folder "
                                           "(jpg / png / webp / gif / bmp)")
            self.pic_status_label2.setStyleSheet(
                f"color: {C_AMBER}; font-size: 13px; background: transparent;")
        self._refresh_pics_info()
        self.log_message(f"[Pics] Folder selected: {folder} — {renamed} picture(s) auto-renamed")

    def _rename_pictures_now(self):
        folder = self.pic_folder_input.text().strip() or self.pic_folder_input2.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Pictures", "Select a pictures folder first.")
            return
        renamed, errors = self.rename_pictures_in_folder(folder)
        self._refresh_pics_info()
        msg = f"Renamed {renamed} picture(s) to eva_pic_001 … eva_pic_{renamed:03d}"
        if errors:
            msg += f" ({len(errors)} errors)"
        self.pic_status_label2.setText(("✓ " if not errors else "⚠ ") + msg)
        self.log_message(f"[Pics] {msg}")

    def _refresh_pics_info(self):
        folder = self.pic_folder_input.text().strip()
        if folder and os.path.isdir(folder):
            try:
                files = sorted(
                    f for f in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, f))
                    and os.path.splitext(f)[1].lower() in self._PIC_EXTENSIONS)
            except Exception:
                files = []
            self.pic_count_label.setText(f"📸  {len(files)} picture(s) ready in pool")
            self.pic_status_label.setText(f"{len(files)} pics ready")
            status_color = "#00D68F" if files else "#FFC107"
            self.pic_status_label.setStyleSheet(
                f"color: {status_color}; font-size: 12px; background: transparent;")
            self.pic_files_preview.clear()
            for f in files[:60]:
                self.pic_files_preview.addItem(f)
        else:
            self.pic_count_label.setText("No folder selected yet.")
            self.pic_status_label.setText("no folder selected")
            self.pic_status_label.setStyleSheet(
                f"color: {C_TEXT_DIM}; font-size: 12px; background: transparent;")
            self.pic_files_preview.clear()

    def _pic_folder_mirror(self, text):
        """Pictures-page folder field mirrors the dashboard field (and back)."""
        if self.pic_folder_input2.text() != text:
            self.pic_folder_input.setText(text)
        self._refresh_pics_info()

    def _pic_folder_mirror_back(self, text):
        if self.pic_folder_input2.text() != text:
            self.pic_folder_input2.setText(text)

    def _pic_quick_toggle(self, checked):
        if self.pic_send_checkbox2.isChecked() != checked:
            self.pic_send_checkbox2.setChecked(checked)

    def _pic_toggle_mirror(self, checked):
        if self.pic_send_checkbox.isChecked() != checked:
            self.pic_send_checkbox.setChecked(checked)

    def _pic_toggle_mirror_back(self, checked):
        if self.pic_send_checkbox2.isChecked() != checked:
            self.pic_send_checkbox2.setChecked(checked)

    def _apply_preset(self, level):
        if level == "low":
            self.thread_count_input.setText("1")
            self.reply_delay_min_input.setText("2.0")
            self.reply_delay_max_input.setText("4.0")
            self.chat_timeout_input.setText("8")
        elif level == "medium":
            self.thread_count_input.setText("3")
            self.reply_delay_min_input.setText("1.0")
            self.reply_delay_max_input.setText("3.0")
            self.chat_timeout_input.setText("8")
        elif level == "high":
            self.thread_count_input.setText("5")
            self.reply_delay_min_input.setText("0.5")
            self.reply_delay_max_input.setText("2.0")
            self.chat_timeout_input.setText("6")
        self.log_message(f"Preset applied: {level.upper()}")

    def _reload_config_from_file(self):
        try:
            from core.config_loader import load_chat_timing as _load
            timing = _load()
            self.new_chat_delay_min.setText(str(int(timing['new_chat_delay_min_seconds'])))
            self.new_chat_delay_max.setText(str(int(timing['new_chat_delay_max_seconds'])))
            self.rest_interval_min.setText(str(int(timing['rest_interval_min_minutes'])))
            self.rest_interval_max.setText(str(int(timing['rest_interval_max_minutes'])))
            self.rest_duration_min.setText(str(int(timing['rest_duration_min_minutes'])))
            self.rest_duration_max.setText(str(int(timing['rest_duration_max_minutes'])))
            self.log_message("Config reloaded from config.json")
        except Exception as e:
            self.log_message(f"Failed to reload config: {e}")

    def _load_settings_into_fields(self):
        try:
            from core.config_loader import (
                load_chat_timing, load_human_behavior, load_resource_governor_config,
            )
            timing = load_chat_timing()
            human = load_human_behavior()
            governor = load_resource_governor_config()
            self.new_chat_delay_min.setText(str(int(timing['new_chat_delay_min_seconds'])))
            self.new_chat_delay_max.setText(str(int(timing['new_chat_delay_max_seconds'])))
            self.rest_interval_min.setText(str(int(timing['rest_interval_min_minutes'])))
            self.rest_interval_max.setText(str(int(timing['rest_interval_max_minutes'])))
            self.rest_duration_min.setText(str(int(timing['rest_duration_min_minutes'])))
            self.rest_duration_max.setText(str(int(timing['rest_duration_max_minutes'])))
            self.reaction_pause_min.setText(str(human['reaction_pause_min_seconds']))
            self.reaction_pause_max.setText(str(human['reaction_pause_max_seconds']))
            self.typing_speed_min.setText(str(int(human['typing_speed_min_cps'])))
            self.typing_speed_max.setText(str(int(human['typing_speed_max_cps'])))
            self.read_reply_min.setText(str(human['read_reply_min_seconds']))
            self.read_reply_max.setText(str(human['read_reply_max_seconds']))
            self.cpu_warn_input.setText(str(int(round(governor['cpu_warn'] * 100))))
            self.ram_warn_input.setText(str(int(round(governor['mem_warn'] * 100))))
            self.ram_recycle_input.setText(str(int(round(governor['mem_recycle'] * 100))))
            self.ram_redline_input.setText(str(int(round(governor['mem_redline'] * 100))))
            # picture sender settings (persisted in config.json)
            from core.config_loader import load_config
            _cfg = load_config() or {}
            _pic = _cfg.get("picture_sending", {}) if isinstance(_cfg, dict) else {}
            self.pic_folder_input.setText(str(_pic.get("folder", "") or ""))
            try:
                self.pic_after_input.setText(str(int(_pic.get("after_messages", 4))))
            except (TypeError, ValueError):
                self.pic_after_input.setText("4")
            try:
                self.pics_per_chat_input.setText(str(int(_pic.get("pics_per_chat", 1))))
            except (TypeError, ValueError):
                self.pics_per_chat_input.setText("1")
            self.pic_send_checkbox.setChecked(bool(_pic.get("enabled", False)))
            self._refresh_pics_info()
        except Exception as e:
            self.log_message(f"Failed to load settings from config: {e}")

    def _save_config_to_file(self):
        def _read_float(line_edit, name):
            try:
                value = float(line_edit.text().strip())
            except (TypeError, ValueError):
                raise ValueError(f"Invalid {name}: '{line_edit.text()}'")
            return value
        try:
            chat_timing = {
                "new_chat_delay_min_seconds": _read_float(self.new_chat_delay_min, "chat delay min"),
                "new_chat_delay_max_seconds": _read_float(self.new_chat_delay_max, "chat delay max"),
                "rest_interval_min_minutes": _read_float(self.rest_interval_min, "rest interval min"),
                "rest_interval_max_minutes": _read_float(self.rest_interval_max, "rest interval max"),
                "rest_duration_min_minutes": _read_float(self.rest_duration_min, "rest duration min"),
                "rest_duration_max_minutes": _read_float(self.rest_duration_max, "rest duration max"),
            }
            human_behavior = {
                "reaction_pause_min_seconds": _read_float(self.reaction_pause_min, "reaction pause min"),
                "reaction_pause_max_seconds": _read_float(self.reaction_pause_max, "reaction pause max"),
                "typing_speed_min_cps": _read_float(self.typing_speed_min, "typing speed min"),
                "typing_speed_max_cps": _read_float(self.typing_speed_max, "typing speed max"),
                "read_reply_min_seconds": _read_float(self.read_reply_min, "read reply min"),
                "read_reply_max_seconds": _read_float(self.read_reply_max, "read reply max"),
            }
            resource_governor = {
                "cpu_warn": _read_float(self.cpu_warn_input, "CPU warn") / 100.0,
                "mem_warn": _read_float(self.ram_warn_input, "RAM warn") / 100.0,
                "mem_recycle": _read_float(self.ram_recycle_input, "RAM recycle") / 100.0,
                "mem_redline": _read_float(self.ram_redline_input, "RAM redline") / 100.0,
            }
            for label, vmin, vmax in (
                ("chat delay", chat_timing["new_chat_delay_min_seconds"], chat_timing["new_chat_delay_max_seconds"]),
                ("rest interval", chat_timing["rest_interval_min_minutes"], chat_timing["rest_interval_max_minutes"]),
                ("rest duration", chat_timing["rest_duration_min_minutes"], chat_timing["rest_duration_max_minutes"]),
                ("reaction pause", human_behavior["reaction_pause_min_seconds"], human_behavior["reaction_pause_max_seconds"]),
                ("typing speed", human_behavior["typing_speed_min_cps"], human_behavior["typing_speed_max_cps"]),
                ("read reply", human_behavior["read_reply_min_seconds"], human_behavior["read_reply_max_seconds"]),
            ):
                if vmin <= 0 or vmin > vmax:
                    raise ValueError(f"Invalid {label} range: min must be > 0 and <= max")
            if not (resource_governor["cpu_warn"] < resource_governor["mem_warn"]
                    <= resource_governor["mem_recycle"] <= resource_governor["mem_redline"]
                    < 1.0):
                raise ValueError("Governor thresholds must be increasing and < 100%")

            pic_after_val = 4
            try:
                pic_after_val = int(self.pic_after_input.text().strip() or "4")
            except ValueError:
                pass
            pics_per_chat_val = 1
            try:
                pics_per_chat_val = int(self.pics_per_chat_input.text().strip() or "1")
            except ValueError:
                pass
            from core.config_loader import save_config_sections
            ok = save_config_sections({
                "chat_timing": chat_timing,
                "human_behavior": human_behavior,
                "resource_governor": resource_governor,
                "picture_sending": {
                    "folder": self.pic_folder_input.text().strip(),
                    "after_messages": pic_after_val,
                    "pics_per_chat": pics_per_chat_val,
                    "enabled": bool(self.pic_send_checkbox.isChecked()),
                },
            })
            self.log_message("Config saved to config.json" if ok else "Failed to save config.json")
            if not ok:
                QMessageBox.warning(self, "Save Config", "Could not write config.json")
        except ValueError as e:
            self.log_message(f"Config not saved: {e}")
            QMessageBox.warning(self, "Save Config", str(e))

    def _filter_logs(self, text):
        all_logs_widget = self.log_tab_widget.widget(0)
        if not hasattr(self, '_log_original_content'):
            self._log_original_content = all_logs_widget.toPlainText()
        if not text:
            all_logs_widget.setPlainText(self._log_original_content)
            self.log_text.setPlainText(self._log_original_content)
            return
        lines = self._log_original_content.split('\n')
        filtered = [l for l in lines if text.lower() in l.lower()]
        result = '\n'.join(filtered[-50:])
        all_logs_widget.setPlainText(result)
        self.log_text.setPlainText(result)

    def _clear_all_logs(self):
        self.log_text.clear()
        for lw in self.thread_logs.values():
            lw.clear()
        # Reset filter cache so it rebuilds from incoming messages
        self._log_original_content = ""
        # Clear the filter input so stale filter text doesn't re-filter emptily
        if hasattr(self, 'filter_input') and self.filter_input is not None:
            self.filter_input.clear()

    def _export_logs(self):
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Logs", f"eva_logs_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt);;All Files (*.*)"
        )
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.log_text.toPlainText())
                self.log_message(f"Logs exported to: {file_path}")
            except Exception as e:
                self.log_message(f"Export failed: {e}")

    def browse_accounts_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Accounts File", "", "Text Files (*.txt);;All Files (*.*)")
        if file_path:
            self.accounts_file_input.setText(file_path)
            self.log_message(f"Selected Accounts file: {file_path}")
            self._load_accounts_preview(file_path)

    def _load_accounts_preview(self, path):
        self.accounts_list.clear()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and ':' in line:
                        email = line.split(':', 1)[0]
                        self.accounts_list.addItem(QListWidgetItem(f"  {email}"))
        except Exception:
            pass

    def clear_accounts_file(self):
        self.accounts_file_input.clear()
        self.accounts_list.clear()

    def update_account_mode(self, *_args):
        needs_account_file = (
            self.login_account_radio.isChecked()
            and not self.create_account_radio.isChecked()
            and not self.restore_account_radio.isChecked()
        )
        self.accounts_browse_widget.setVisible(needs_account_file)

    def get_first_message_mode(self):
        if self.first_message_false_radio.isChecked():
            return "false"
        if self.first_message_random_radio.isChecked():
            return "random"
        return "true"

    def update_first_message_delay_state(self, *_args):
        enabled = not self.first_message_false_radio.isChecked()
        self.first_message_delay_min_input.setEnabled(enabled)
        self.first_message_delay_max_input.setEnabled(enabled)

    def load_login_accounts(self):
        accounts_file = self.accounts_file_input.text().strip()
        if not accounts_file:
            self.log_message("Accounts file is required.")
            QMessageBox.warning(self, "Configuration Required", "Please select an accounts file.\nFormat: email:password (one per line)")
            return None
        if not os.path.exists(accounts_file):
            self.log_message(f"Accounts file not found: {accounts_file}")
            QMessageBox.warning(self, "File Not Found", f"File not found:\n{accounts_file}")
            return None
        accounts = []
        try:
            with open(accounts_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and ':' in line:
                        email, password = (part.strip() for part in line.split(':', 1))
                        if email and password:
                            accounts.append({'email': email, 'password': password})
        except Exception as e:
            self.log_message(f"Error reading accounts: {e}")
            QMessageBox.warning(self, "Read Error", str(e))
            return None
        if not accounts:
            self.log_message("No valid accounts found")
            QMessageBox.warning(self, "Empty File", "No valid accounts found.\nFormat: email:password (one per line)")
            return None
        self.log_message(f"Loaded {len(accounts)} account(s)")
        self.accounts_list.clear()
        for acc in accounts:
            self.accounts_list.addItem(QListWidgetItem(f"  {acc['email']}"))
        return accounts

    def load_restore_accounts(self):
        """Load saved sessions, reporting which ones are blind (dead cookies).

        Phase 13: a blind session is not returned blindly — when accounts.txt
        holds its password the account is still offered (the bot repairs it by
        logging in); otherwise it is skipped here with a clear message instead
        of failing after a browser launch.
        """
        accounts = load_saved_account_sessions()
        if not accounts:
            self.log_message("No saved sessions to restore")
            QMessageBox.warning(self, "No Sessions", "No saved account sessions found")
            return None

        usable, blind, skipped = [], [], []
        for account in accounts:
            health = account.get("health") or {}
            if not health.get("blind"):
                usable.append(account)
                continue
            blind.append(account)
            if account.get("action") == "repair":
                usable.append(account)      # will be repaired by a login
            else:
                skipped.append(account)

        self.log_message(f"Found {len(accounts)} saved session(s) — "
                         f"{len(usable)} usable, {len(blind)} blind, {len(skipped)} skipped")
        for account in skipped:
            self.log_message(
                f"  ⛔ blind session skipped: {account.get('email')} "
                f"({(account.get('health') or {}).get('reason')}) — "
                f"add its email:password to accounts.txt to auto-repair")
        if blind:
            try:
                from browser import session_health
                self.log_message("  " + session_health.format_summary(
                    session_health.summarize(accounts)))
            except Exception:
                pass

        if not usable:
            QMessageBox.warning(
                self, "Blind Sessions",
                "Every saved session is blind (cookies expired/empty/corrupt).\n\n"
                "Add email:password lines to accounts.txt and the bot will log in "
                "again automatically, or use 'Login' mode.")
            return None
        return usable

    def browse_proxy_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Proxy File", "", "Text Files (*.txt);;All Files (*.*)")
        if file_path:
            self.proxy_file_input.setText(file_path)
            self.log_message(f"Selected proxy file: {file_path}")

    def clear_proxy_file(self):
        self.proxy_file_input.clear()

    def parse_proxy_file(self, proxy_file_path):
        try:
            with open(proxy_file_path, 'r', encoding='utf-8') as f:
                proxy_lines = [line.strip() for line in f if line.strip()]
            if not proxy_lines:
                self.log_message("No proxies found in file")
                return []
            proxy_configs = []
            for proxy_line in proxy_lines:
                parts = proxy_line.split(':')
                if len(parts) >= 4:
                    proxy_configs.append({'server': f"{parts[0]}:{parts[1]}", 'username': parts[2], 'password': parts[3]})
                elif len(parts) >= 2:
                    proxy_configs.append({'server': f"{parts[0]}:{parts[1]}"})
                else:
                    self.log_message(f"Skipping invalid proxy: {proxy_line}")
            if proxy_configs:
                self.log_message(f"Loaded {len(proxy_configs)} proxy/proxies")
            return proxy_configs
        except Exception as e:
            self.log_message(f"Error parsing proxy file: {e}")
            return []

    def update_runtime(self):
        if self.start_time:
            elapsed = datetime.datetime.now() - self.start_time
            hours = elapsed.seconds // 3600
            minutes = (elapsed.seconds % 3600) // 60
            seconds = elapsed.seconds % 60
            self.runtime_label.value_label.setText(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        if self.thread_manager and self.thread_manager.is_running:
            try:
                self.thread_manager.tick()
            except Exception:
                pass

    def set_status_message(self, message):
        self.status_text = message
        self.status_label.value_label.setText(message)

    def start_bot(self):
        if self.thread_manager and self.thread_manager.is_running:
            self.log_message("Bot is already running!")
            return

        chat_timing = load_chat_timing()

        if self.create_account_radio.isChecked():
            account_mode = "create"
        elif self.restore_account_radio.isChecked():
            account_mode = "restore"
        else:
            account_mode = "login"

        accounts = []
        if account_mode == "login":
            accounts = self.load_login_accounts()
            if accounts is None:
                return
        elif account_mode == "restore":
            accounts = self.load_restore_accounts()
            if accounts is None:
                return
        else:
            self.log_message("Create mode requires accounts file for login credentials")
            QMessageBox.warning(self, "Configuration Required",
                "Create mode requires an accounts file with email:password entries.\n"
                "Please select Login mode and choose an accounts file.")
            return

        sms_enabled = False

        # Load snap IDs from data/snap_ids.txt
        snapchat_ids = []
        snap_ids_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'snap_ids.txt')
        if os.path.exists(snap_ids_path):
            try:
                with open(snap_ids_path, 'r', encoding='utf-8') as f:
                    snapchat_ids = [line.strip() for line in f if line.strip()]
            except Exception:
                pass
        if snapchat_ids:
            self.log_message(f"Snap IDs: {len(snapchat_ids)} loaded from data/snap_ids.txt")
        else:
            self.log_message("Snap IDs: None found (rule-engine funnel only)")

        # Load message templates from data/output/flirty_questions.txt
        message_templates = []
        flirty_q_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'output', 'flirty_questions.txt')
        if os.path.exists(flirty_q_path):
            try:
                with open(flirty_q_path, 'r', encoding='utf-8') as f:
                    message_templates = [line.strip() for line in f if line.strip()]
            except Exception:
                pass
        if message_templates:
            self.log_message(f"Messages: {len(message_templates)} templates loaded")
        else:
            self.log_message("Messages: ChatRuleBot engine only")

        try:
            thread_count = int(self.thread_count_input.text().strip() or "1")
            if thread_count < 1 or thread_count > 10:
                self.log_message("Thread count must be 1-10")
                return
        except ValueError:
            self.log_message("Invalid thread count")
            return

        try:
            chat_timeout = int(self.chat_timeout_input.text().strip() or "8")
            if chat_timeout < 1 or chat_timeout > 20:
                self.log_message("Max replies must be 1-20")
                return
        except ValueError:
            self.log_message("Invalid max replies")
            return

        first_message_mode = self.get_first_message_mode()
        first_message_delay_min = 0.0
        first_message_delay_max = 0.0
        if first_message_mode != "false":
            try:
                first_message_delay_min = float(self.first_message_delay_min_input.text().strip() or "1.0")
                first_message_delay_max = float(self.first_message_delay_max_input.text().strip() or "3.0")
                if first_message_delay_min < 0 or first_message_delay_max < 0 or first_message_delay_min > 60 or first_message_delay_max > 60:
                    self.log_message("First message delay must be 0-60 seconds")
                    return
                if first_message_delay_min > first_message_delay_max:
                    self.log_message("First message delay min cannot be > max")
                    return
            except ValueError:
                self.log_message("Invalid first message delay")
                return

        try:
            reply_delay_min = float(self.reply_delay_min_input.text().strip() or "1.0")
            reply_delay_max = float(self.reply_delay_max_input.text().strip() or "3.0")
            if reply_delay_min < 0 or reply_delay_max < 0 or reply_delay_min > 60 or reply_delay_max > 60:
                self.log_message("Reply delay must be 0-60 seconds")
                return
            if reply_delay_min > reply_delay_max:
                self.log_message("Reply delay min cannot be > max")
                return
        except ValueError:
            self.log_message("Invalid reply delay")
            return

        if account_mode in {"login", "restore"} and thread_count > len(accounts):
            self.log_message(f"Adjusted thread count: {thread_count} -> {len(accounts)} (matching accounts)")
            thread_count = len(accounts)

        proxy_configs = []
        proxy_file = self.proxy_file_input.text().strip()
        if proxy_file:
            if os.path.exists(proxy_file):
                proxy_configs = self.parse_proxy_file(proxy_file)
                if not proxy_configs:
                    self.log_message("Could not parse proxy file, continuing without proxy")
            else:
                self.log_message(f"Proxy file not found: {proxy_file}")
                QMessageBox.warning(self, "File Not Found", f"Proxy file not found:\n{proxy_file}")
                return

        self.log_message(f"Starting {thread_count} thread(s)...")
        if account_mode == "create":
            self.log_message("  Account Mode: Create new session")
        elif account_mode == "restore":
            self.log_message(f"  Account Mode: Restore ({len(accounts)} sessions)")
        else:
            self.log_message(f"  Account Mode: Login ({len(accounts)} accounts)")
        self.log_message("  Messages: ChatRuleBot engine only")
        self.log_message("  Threads: {}".format(thread_count))
        self.log_message(f"  Max Replies: {chat_timeout}")
        self.log_message(f"  Reply Delay: {reply_delay_min}s - {reply_delay_max}s")
        if proxy_configs:
            self.log_message(f"  Proxies: {len(proxy_configs)}")
        else:
            self.log_message("  Proxy: None (direct)")

        from datetime import datetime
        self.start_time = datetime.now()
        self.runtime_timer.start()
        self.log_clear_timer.start()

        self.total_users_chatted = 0
        self.total_snaps_shared = 0
        self.total_messages_sent = 0
        self.total_messages_received = 0
        self.total_pics_sent = 0
        self.update_live_stats()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.proxy_warning_label.clear()
        self.proxy_warning_label.setVisible(False)
        self.status_label.value_label.setText("Running...")
        self.status_label.value_label.setStyleSheet("color: #FFC107; background: transparent; border: none;")
        self.header_status.setText("● RUNNING")
        self.header_status.setStyleSheet("background-color: rgba(255,193,7,12%); color: #FFC107;"
                                         "border: 1px solid #FFC107; font-size: 12px; padding: 6px 16px;"
                                         "border-radius: 13px; font-weight: 700;")

        show_browser = self.show_browser_checkbox.isChecked()
        hide_after_login = self.hide_after_login_checkbox.isChecked()
        shuffle_chat = self.shuffle_chat_checkbox.isChecked()
        headless = not show_browser

        # Chat time: None = chat until the snap is shared (default)
        chat_time_minutes = self.chat_time_combo.currentData()
        if chat_time_minutes is None:
            self.log_message("Chat Time: Until snap share (default, no limit)")
        else:
            self.log_message(f"Chat Time: {chat_time_minutes} min per stranger")

        # Optional timing overrides from the dashboard
        try:
            ncd = float(self.newchat_delay_input.text().strip() or "0")
            if ncd > 0:
                chat_timing["new_chat_delay_min_seconds"] = ncd
                chat_timing["new_chat_delay_max_seconds"] = ncd
                self.log_message(f"New Chat Delay override: {ncd}s")
        except ValueError:
            pass
        try:
            rem = float(self.rest_every_input.text().strip() or "0")
            if rem > 0:
                chat_timing["rest_interval_min_minutes"] = rem
                chat_timing["rest_interval_max_minutes"] = rem
                self.log_message(f"Rest Every override: {rem} min")
        except ValueError:
            pass
        try:
            rfm = float(self.rest_for_input.text().strip() or "0")
            if rfm > 0:
                chat_timing["rest_duration_min_minutes"] = rfm
                chat_timing["rest_duration_max_minutes"] = rfm
                self.log_message(f"Rest For override: {rfm} min")
        except ValueError:
            pass

        # ---- Picture sender settings ----
        pic_folder = self.pic_folder_input.text().strip()
        pic_after = 4
        try:
            pic_after = max(1, min(20, int(self.pic_after_input.text().strip() or "4")))
        except ValueError:
            pass
        pics_per_chat = 1
        try:
            pics_per_chat = max(1, min(5, int(self.pics_per_chat_input.text().strip() or "1")))
        except ValueError:
            pass
        pic_send_enabled = bool(self.pic_send_checkbox.isChecked()
                                and pic_folder and os.path.isdir(pic_folder))
        if self.pic_send_checkbox.isChecked() and not pic_send_enabled:
            self.log_message("Pictures: folder not set/missing — picture sending OFF (chat continues normally)")
        elif pic_send_enabled:
            self.log_message(f"Pictures: ON — random pic with message #{pic_after}, "
                             f"{pics_per_chat} per stranger, from {pic_folder}")
        else:
            self.log_message("Pictures: OFF")

        if show_browser:
            self.log_message("Browser: Visible during chat")
        else:
            self.log_message("Browser: Headless (hidden)")
        if hide_after_login:
            self.log_message("Browser: Will hide after login")

        self.thread_manager = ThreadManager(
            accounts=accounts,
            account_mode=account_mode,
            snapchat_ids=snapchat_ids,
            fixed_message_templates=message_templates,
            headless=headless,
            shuffle_chat=shuffle_chat,
            chat_timeout=chat_timeout,
            reply_delay_min=reply_delay_min,
            reply_delay_max=reply_delay_max,
            first_message_mode=first_message_mode,
            first_message_delay_min=first_message_delay_min,
            first_message_delay_max=first_message_delay_max,
            chat_timing=chat_timing,
            proxy_configs=proxy_configs,
            hide_after_login=hide_after_login,
            chat_time_minutes=chat_time_minutes,
            pic_folder=pic_folder if pic_send_enabled else None,
            pic_send_enabled=pic_send_enabled,
            pic_after_messages=pic_after,
            pics_per_chat=pics_per_chat,
        )

        # Live sessions / chat-feed state reset
        self.session_states = {}
        self._chat_feed_buffer = []
        self.livechat_filter_combo.blockSignals(True)
        self.livechat_filter_combo.clear()
        self.livechat_filter_combo.addItem("All sessions")
        for i in range(1, thread_count + 1):
            self.livechat_filter_combo.addItem(f"S{i}")
        self.livechat_filter_combo.setCurrentIndex(0)
        self.livechat_filter_combo.blockSignals(False)
        self.sessions_table.setRowCount(0)
        self.sessions_label.value_label.setText("0")
        self.banned_label.value_label.setText("0")
        for card in ("users_label", "snaps_label", "pics_label",
                     "sess_active_card", "sess_banned_card", "sess_pics_card"):
            w = getattr(self, card, None)
            if w is not None:
                w.value_label.setText("0")
        self.live_chat_text.clear()

        self.thread_manager.chat_message.connect(self.on_chat_message)
        self.thread_manager.session_status.connect(self.on_session_status)
        self.thread_manager.thread_started.connect(self.on_thread_started)
        self.thread_manager.thread_log.connect(self.on_thread_log)
        self.thread_manager.thread_status.connect(self.on_thread_status)
        self.thread_manager.proxy_warning.connect(self.on_proxy_warning)
        self.thread_manager.captcha_alert.connect(self.on_captcha_alert)
        self.thread_manager.account_exhausted.connect(self.on_account_exhausted)
        self.thread_manager.thread_finished.connect(self.on_thread_finished_event)
        self.thread_manager.all_threads_finished.connect(self.on_all_threads_finished)

        self.setup_thread_tabs(thread_count)
        self.thread_manager.start_threads(thread_count)
        self.threads_label.value_label.setText(f"{thread_count}/{thread_count}")

    def stop_bot(self):
        self.runtime_timer.stop()
        self.log_clear_timer.stop()

        if not self.thread_manager or not self.thread_manager.is_running:
            self.log_message("No threads running.")
            self.on_all_threads_finished()
            return

        self.log_message("Stopping browsers safely...")
        self.stop_button.setEnabled(False)
        self.status_label.value_label.setText("Stopping...")
        self.status_label.value_label.setStyleSheet("color: #FFC107; background: transparent; border: none;")

        manager = self.thread_manager

        def shutdown_with_timeout():
            import time
            manager.stop_all_threads()
            time.sleep(5)
            QTimer.singleShot(0, self.on_all_threads_finished)

        threading.Thread(target=shutdown_with_timeout, name="clean-shutdown", daemon=True).start()

    def on_thread_started(self, thread_id, message):
        self.log_message(f"[Thread {thread_id}] {message}")
        self.active_threads[thread_id] = "Running"
        self._update_status_list_item(thread_id, "Running")

    def on_thread_log(self, thread_id, message):
        if thread_id == 0:
            formatted_message = f"[Manager] {message}"
        else:
            formatted_message = f"[T{thread_id}] {message}"

        all_logs_widget = self.log_tab_widget.widget(0)
        all_logs_widget.append(formatted_message)
        self.log_text.append(formatted_message)
        if not hasattr(self, '_log_original_content'):
            self._log_original_content = ""
        self._log_original_content += formatted_message + "\n"
        if self.auto_scroll_checkbox.isChecked():
            scrollbar = all_logs_widget.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            dash_scrollbar = self.log_text.verticalScrollBar()
            dash_scrollbar.setValue(dash_scrollbar.maximum())

        if thread_id in self.thread_logs:
            thread_log_widget = self.thread_logs[thread_id]
            thread_log_widget.append(message)
            if self.auto_scroll_checkbox.isChecked():
                thread_scrollbar = thread_log_widget.verticalScrollBar()
                thread_scrollbar.setValue(thread_scrollbar.maximum())

        if "[Stats] Snap shared!" in message:
            match = re.search(r'Total: (\d+)', message)
            if match:
                self.total_snaps_shared = int(match.group(1))
            self._play_sound(2)
            self.update_live_stats()

        if "[Chat #" in message and "Waiting for stranger" in message:
            match = re.search(r'Chat #(\d+)', message)
            if match:
                chat_num = int(match.group(1))
                if chat_num > self.total_users_chatted:
                    self.total_users_chatted = chat_num
                    self.update_live_stats()

        # ---- LIVE SESSIONS table: per-session chat / snap counters ----
        if thread_id != 0:
            sess = self.session_states.get(thread_id)
            if sess is not None:
                changed = False
                m = re.search(r'\[Chat #(\d+)\] Starting conversation', message)
                if m:
                    sess["chats"] = max(sess.get("chats", 0), int(m.group(1)))
                    changed = True
                if "[Stats] Snap shared!" in message:
                    m = re.search(r'Total: (\d+)', message)
                    if m:
                        sess["snaps"] = max(sess.get("snaps", 0), int(m.group(1)))
                        changed = True
                m = re.search(r'Picture sent to stranger: .*?\(total (\d+)\)', message)
                if m:
                    sess["pics"] = max(sess.get("pics", 0), int(m.group(1)))
                    changed = True
                if "Saved browser session for " in message:
                    m = re.search(r'Saved browser session for (.+?) to', message)
                    if m and not sess.get("account"):
                        sess["account"] = m.group(1)
                        changed = True
                if changed:
                    self._update_session_row(thread_id)

        # Only count actual chat messages from the bot worker, not log echoes
        if thread_id != 0 and message.startswith("Stranger: "):
            self.total_messages_received += 1
            self.update_live_stats()

        if thread_id != 0 and message.startswith("You: "):
            self.total_messages_sent += 1
            self.update_live_stats()

        if "Connected to stranger!" in message:
            self._play_sound(1)

        if "[Ban]" in message:
            self._play_sound(3)

    # ------------------------------------------------------------------
    # LIVE CHAT FEED + LIVE SESSIONS (dashboard additions)
    # ------------------------------------------------------------------
    _MAX_CHAT_FEED_BLOCKS = 400

    def _session_state(self, thread_id):
        """Get/create the per-session state row for a worker thread."""
        if not hasattr(self, "session_states") or self.session_states is None:
            self.session_states = {}
        if thread_id not in self.session_states:
            self.session_states[thread_id] = {
                "account": "", "status": "STARTING", "chats": 0,
                "msgs_in": 0, "msgs_out": 0, "snaps": 0, "pics": 0,
            }
        return self.session_states[thread_id]

    def _chat_feed_html(self, thread_id, who, text):
        """Build one colored HTML line for the live chat feed."""
        import html as _html
        safe = _html.escape(str(text))
        if who == "user":
            color, label = "#FFB86C", "user"
        elif who == "pic":
            color, label = "#B388FF", "📷 picture"
        else:
            color, label = "#00D68F", "bot"
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        return (f'<span style="color:#8A93A6;">[{timestamp}]</span> '
                f'<b><span style="color:{color};">[S{thread_id}] {label}:</span></b> '
                f'<span style="color:#E6EAF2;">{safe}</span>')

    def on_chat_message(self, thread_id, who, text):
        """Live chat feed: one line per incoming user SMS / bot reply / picture."""
        # ---- session counters (always run, even when filtered out) ----
        sess = self._session_state(thread_id)
        if who == "user":
            sess["msgs_in"] = sess.get("msgs_in", 0) + 1
        elif who == "pic":
            sess["pics"] = sess.get("pics", 0) + 1
            self.total_pics_sent = getattr(self, "total_pics_sent", 0) + 1
            if hasattr(self, "pics_label"):
                self.pics_label.value_label.setText(str(self.total_pics_sent))
            if hasattr(self, "sess_pics_card"):
                self.sess_pics_card.value_label.setText(str(self.total_pics_sent))
        else:
            sess["msgs_out"] = sess.get("msgs_out", 0) + 1
        self._update_session_row(thread_id)

        # ---- buffer (for the session filter) ----
        self._chat_feed_buffer.append((thread_id, who, str(text)))
        if len(self._chat_feed_buffer) > self._MAX_CHAT_FEED_BLOCKS:
            del self._chat_feed_buffer[:len(self._chat_feed_buffer) - self._MAX_CHAT_FEED_BLOCKS]

        # ---- session filter ----
        ftext = (self.livechat_filter_combo.currentText()
                 if hasattr(self, "livechat_filter_combo") else "All sessions")
        if ftext != "All sessions" and ftext != f"S{thread_id}":
            return

        self.live_chat_text.append(self._chat_feed_html(thread_id, who, text))
        # keep the feed bounded (drop oldest content when huge)
        doc = self.live_chat_text.document()
        if doc.blockCount() > self._MAX_CHAT_FEED_BLOCKS:
            cursor = self.live_chat_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(
                cursor.MoveOperation.NextBlock,
                cursor.MoveMode.KeepAnchor,
                doc.blockCount() - self._MAX_CHAT_FEED_BLOCKS)
            cursor.removeSelectedText()
        autoscroll = (self.livechat_autoscroll.isChecked()
                      if hasattr(self, "livechat_autoscroll") else True)
        if autoscroll:
            scrollbar = self.live_chat_text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _render_chat_feed(self, *_args):
        """Re-render the whole feed from the buffer (filter changed)."""
        self.live_chat_text.clear()
        ftext = (self.livechat_filter_combo.currentText()
                 if hasattr(self, "livechat_filter_combo") else "All sessions")
        parts = []
        for thread_id, who, text in self._chat_feed_buffer:
            if ftext != "All sessions" and ftext != f"S{thread_id}":
                continue
            parts.append(self._chat_feed_html(thread_id, who, text))
        for p in parts[-self._MAX_CHAT_FEED_BLOCKS:]:
            self.live_chat_text.append(p)
        scrollbar = self.live_chat_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def on_session_status(self, thread_id, status):
        """Live sessions table: status change for a browser session."""
        sess = self._session_state(thread_id)
        sess["status"] = str(status)
        self._update_session_row(thread_id)
        active = sum(1 for s in self.session_states.values()
                     if s.get("status") == "ACTIVE")
        banned = sum(1 for s in self.session_states.values()
                     if s.get("status") == "BANNED")
        self.sessions_label.value_label.setText(str(active))
        self.banned_label.value_label.setText(str(banned))
        if hasattr(self, "sess_active_card"):
            self.sess_active_card.value_label.setText(str(active))
        if hasattr(self, "sess_banned_card"):
            self.sess_banned_card.value_label.setText(str(banned))

    def _update_session_row(self, thread_id):
        """Refresh one row of the Live Browser Sessions table."""
        sess = self._session_state(thread_id)
        row = self._session_row(thread_id)
        values = [
            f"S{thread_id}",
            sess.get("account") or "-",
            sess.get("status", "-"),
            str(sess.get("chats", 0)),
            f"{sess.get('msgs_in', 0)}/{sess.get('msgs_out', 0)}",
            str(sess.get("snaps", 0)),
            str(sess.get("pics", 0)),
        ]
        for col, value in enumerate(values):
            item = self.sessions_table.item(row, col)
            if item is None:
                item = QTableWidgetItem(value)
                self.sessions_table.setItem(row, col, item)
            else:
                item.setText(value)
        # color the status cell
        status_item = self.sessions_table.item(row, 2)
        if status_item is not None:
            status = status_item.text()
            if status == "ACTIVE":
                status_item.setForeground(QColor("#00D68F"))
            elif status == "BANNED":
                status_item.setForeground(QColor("#FF5555"))
            else:
                status_item.setForeground(QColor("#FFC107"))

    def _session_row(self, thread_id):
        """Map thread_id -> table row (find by Session # cell)."""
        tag = f"S{thread_id}"
        for row in range(self.sessions_table.rowCount()):
            item = self.sessions_table.item(row, 0)
            if item is not None and item.text() == tag:
                return row
        self.sessions_table.insertRow(self.sessions_table.rowCount())
        return self.sessions_table.rowCount() - 1

    def _clear_live_chat(self):
        self._chat_feed_buffer = []
        self.live_chat_text.clear()

    def update_live_stats(self):
        sessions_info = ""
        if getattr(self, "session_states", None):
            active = sum(1 for s in self.session_states.values()
                         if s.get("status") == "ACTIVE")
            sessions_info = f" | Sessions: {active} active"
        self.live_stats_label.setText(
            f"Chats: {self.total_users_chatted} | Sent: {self.total_messages_sent} | "
            f"Recv: {self.total_messages_received} | Snaps: {self.total_snaps_shared} | "
            f"Pics: {getattr(self, 'total_pics_sent', 0)}"
            f"{sessions_info}"
        )
        if hasattr(self, "users_label"):
            self.users_label.value_label.setText(str(self.total_users_chatted))
        if hasattr(self, "snaps_label"):
            self.snaps_label.value_label.setText(str(self.total_snaps_shared))
        if hasattr(self, "pics_label"):
            self.pics_label.value_label.setText(str(getattr(self, "total_pics_sent", 0)))

    def _play_sound(self, repeats=1, captcha=False):
        if not self.sound_checkbox.isChecked() or winsound is None:
            return
        def _run():
            try:
                if captcha:
                    from browser.browser_automation import play_captcha_alert
                    play_captcha_alert()
                else:
                    from browser.browser_automation import play_beep_sequence
                    play_beep_sequence(repeats)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _setup_thread_status_list(self, count):
        self.thread_status_list.clear()
        for i in range(1, count + 1):
            item = QListWidgetItem(f"Thread {i}: Starting")
            item.setForeground(QColor("#FFC107"))
            self.thread_status_list.addItem(item)

    def _update_status_list_item(self, thread_id, status):
        idx = thread_id - 1
        if idx < 0 or idx >= self.thread_status_list.count():
            return
        text = str(status)
        label = text if len(text) <= 40 else text[:37] + "..."
        lower = text.lower()
        if "ban" in lower or "captcha" in lower:
            color = QColor("#E74C3C")
        elif "rest" in lower:
            color = QColor("#9B59B6")
        elif "exit" in lower or "stop" in lower or "close" in lower:
            color = QColor("#808898")
        else:
            color = QColor("#00B383")
        item = self.thread_status_list.item(idx)
        item.setText(f"Thread {thread_id}: {label}")
        item.setForeground(color)

    def auto_clear_logs(self):
        all_logs_widget = self.log_tab_widget.widget(0)
        content = all_logs_widget.toPlainText()
        lines = content.split('\n')
        if len(lines) > 30:
            trimmed = '\n'.join(lines[-30:])
            all_logs_widget.setPlainText(trimmed)
            self.log_text.setPlainText(trimmed)

    def on_thread_status(self, thread_id, status):
        if thread_id in self.active_threads:
            self.active_threads[thread_id] = status
        self._update_status_list_item(thread_id, status)

    def on_proxy_warning(self, thread_id, message):
        prefix = f"[Thread {thread_id}] " if thread_id else ""
        warning_text = f"{prefix}{message}"
        current = self.proxy_warning_label.text().strip()
        if current and warning_text not in current:
            warning_text = f"{current}\n{warning_text}"
        self.proxy_warning_label.setText(warning_text)
        self.proxy_warning_label.setVisible(True)
        self.log_message(warning_text)

    def on_captcha_alert(self, thread_id):
        if thread_id:
            self.captcha_banner.setText(
                f"CAPTCHA DETECTED — Thread {thread_id} needs you to solve it in the browser!"
            )
        else:
            self.captcha_banner.setText("CAPTCHA DETECTED — Solve it in the browser!")
        self.captcha_banner.setVisible(True)
        self._set_captcha_banner_style(True)
        self.captcha_blink_timer.start(500)
        self._play_sound(captcha=True)
        self._update_status_list_item(thread_id, "Captcha — solve in browser")
        self.log_message(f"[CAPTCHA] Alert — Thread {thread_id} requires manual captcha solution")

    def _set_captcha_banner_style(self, bright):
        bg = "#E74C3C" if bright else "#922B21"
        self.captcha_banner.setStyleSheet(f"""
            QLabel {{
                background-color: {bg}; color: white;
                border: 2px solid #FFFFFF; border-radius: 6px;
                padding: 10px 15px; font-size: 14px; font-weight: bold;
            }}
        """)

    def _toggle_captcha_banner(self):
        bright = not getattr(self, '_captcha_banner_bright', True)
        self._captcha_banner_bright = bright
        self._set_captcha_banner_style(bright)

    def _clear_captcha_banner(self):
        self.captcha_blink_timer.stop()
        self._captcha_banner_bright = True
        self.captcha_banner.setVisible(False)

    def on_account_exhausted(self, message):
        self.log_message(f"Accounts exhausted: {message}")
        QMessageBox.warning(self, "Accounts Exhausted", message)

    def on_thread_finished_event(self, thread_id):
        if thread_id in self.active_threads:
            del self.active_threads[thread_id]
        self._update_status_list_item(thread_id, "Stopped")
        if self.thread_manager:
            active = self.thread_manager.get_active_thread_count()
            total = self.thread_manager.thread_count
            self.threads_label.value_label.setText(f"{active}/{total}")

    def on_all_threads_finished(self):
        self.log_message("All threads stopped")
        if self.thread_manager:
            try:
                self.thread_manager.thread_started.disconnect()
                self.thread_manager.thread_log.disconnect()
                self.thread_manager.chat_message.disconnect()
                self.thread_manager.session_status.disconnect()
                self.thread_manager.thread_status.disconnect()
                self.thread_manager.proxy_warning.disconnect()
                self.thread_manager.captcha_alert.disconnect()
                self.thread_manager.account_exhausted.disconnect()
                self.thread_manager.thread_finished.disconnect()
                self.thread_manager.all_threads_finished.disconnect()
            except Exception:
                pass
            self.thread_manager = None
        self.cleanup_thread_tabs()
        self._clear_captcha_banner()
        self.thread_status_list.clear()
        self.active_threads.clear()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.value_label.setText("Ready")
        self.status_label.value_label.setStyleSheet("color: #00D68F; background: transparent; border: none;")
        self.header_status.setText("● READY")
        self.header_status.setStyleSheet("background-color: rgba(0,214,143,12%); color: #00D68F;"
                                         "border: 1px solid #00D68F; font-size: 12px; padding: 6px 16px;"
                                         "border-radius: 13px; font-weight: 700;")
        self.threads_label.value_label.setText("0/0")

    def setup_thread_tabs(self, thread_count):
        self.current_thread_count = thread_count
        while self.log_tab_widget.count() > 1:
            self.log_tab_widget.removeTab(1)
        self.thread_logs.clear()
        self._setup_thread_status_list(thread_count)

        if thread_count > 1:
            for i in range(1, thread_count + 1):
                thread_log = QTextEdit()
                thread_log.setReadOnly(True)
                log_font = QFont('Consolas', 10)
                log_font.setStyleHint(QFont.StyleHint.Monospace)
                thread_log.setFont(log_font)
                thread_log.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                thread_log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
                self.log_tab_widget.addTab(thread_log, f"Thread {i}")
                self.thread_logs[i] = thread_log

    def cleanup_thread_tabs(self):
        while self.log_tab_widget.count() > 1:
            self.log_tab_widget.removeTab(1)
        self.thread_logs.clear()
        self.current_thread_count = 0

    def log_message(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        all_logs_widget = self.log_tab_widget.widget(0)
        all_logs_widget.append(formatted_message)
        self.log_text.append(formatted_message)
        if not hasattr(self, '_log_original_content'):
            self._log_original_content = ""
        self._log_original_content += formatted_message + "\n"
        if self.auto_scroll_checkbox.isChecked():
            scrollbar = all_logs_widget.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())
            dash_scrollbar = self.log_text.verticalScrollBar()
            dash_scrollbar.setValue(dash_scrollbar.maximum())


class ApplicationController:
    def __init__(self, app, development_mode=False):
        self.app = app
        self.development_mode = development_mode
        self.main_window = None
        self.signin_window = None
        self.stats_timer = None

    def start(self):
        self.on_signin_successful()

    def on_signin_successful(self):
        self.main_window = ChitchatBotGUI(development_mode=self.development_mode)

        if self.signin_window and self.signin_window.device_signin:
            device_id = self.signin_window.device_signin.get_device_id()
            if device_id:
                self.main_window.log_message(f"Authenticated - Device ID: {str(device_id)[:8]}...")

        def update_system_stats():
            try:
                self.main_window.cpu_label.value_label.setText(f"{psutil.cpu_percent()}%")
                self.main_window.ram_label.value_label.setText(f"{psutil.virtual_memory().percent}%")
            except:
                pass

        self.stats_timer = QTimer()
        self.stats_timer.timeout.connect(update_system_stats)
        self.stats_timer.start(1000)

        self.main_window.show()


def main():
    # Windowless mode (pythonw via run.vbs / app shortcut): sys.stdout/stderr are None.
    # Redirect them to data/logs/gui_console.log so diagnostics survive
    # and no print() can ever break the GUI.
    if sys.stdout is None or sys.stderr is None:
        try:
            logs_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'data', 'logs')
            os.makedirs(logs_dir, exist_ok=True)
            _console_log = open(
                os.path.join(logs_dir, 'gui_console.log'),
                'a', buffering=1, encoding='utf-8', errors='replace')
            sys.stdout = _console_log
            sys.stderr = _console_log
        except Exception:
            sys.stdout = open(os.devnull, 'w')
            sys.stderr = sys.stdout

    parser = argparse.ArgumentParser(description='EVA Bot - Browser Automation Dashboard')
    parser.add_argument('-d', '--development', action='store_true',
                        help='Run in development mode (shows browser windows)')
    args = parser.parse_args()

    app = QApplication(sys.argv)

    icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'chitchat-bot.ico')
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    if platform.system() == 'Windows':
        try:
            myappid = 'evabot.dashboard.app.4.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    controller = ApplicationController(app, development_mode=args.development)
    controller.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
