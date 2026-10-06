"""
Sign In Window for Chitchat Bot
Modern authentication dialog that must be completed before accessing the main application
"""
import sys
import os
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLineEdit, QLabel, QProgressBar,
                             QFrame, QMessageBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QFont, QIcon, QPixmap

from browser.device_signin import DeviceSignin
from entry.paths import project_root as _root


class SigninWorker(QThread):
    """Worker thread for handling signin asynchronously"""
    progress_signal = pyqtSignal(int)
    status_signal = pyqtSignal(str)
    success_signal = pyqtSignal(str, str, str)  # message, device_id, app_name
    error_signal = pyqtSignal(str)
    
    def __init__(self, device_signin, password):
        super().__init__()
        self.device_signin = device_signin
        self.password = password
    
    def run(self):
        """Execute signin in background thread"""
        success, message, device_id = self.device_signin.signin(
            self.password,
            progress_callback=self.progress_signal.emit,
            status_callback=self.status_signal.emit
        )
        
        if success:
            # Convert device_id to string (API may return int)
            device_id_str = str(device_id) if device_id is not None else ""
            app_name_str = str(self.device_signin.app_name) if self.device_signin.app_name else ""
            self.success_signal.emit(message, device_id_str, app_name_str)
        else:
            self.error_signal.emit(message)


class SigninWindow(QWidget):
    """Sign in window that authenticates before showing main application"""
    
    # Signal emitted when signin is successful
    signin_successful = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self.device_signin = DeviceSignin(app_id="chitchat-automation")
        self.signin_worker = None
        
        self.setup_ui()
        
    def setup_ui(self):
        """Setup the signin UI"""
        self.setWindowTitle("Chitchat Bot - Sign In")
        self.setFixedSize(420, 480)
        
        # Set window icon
        icon_path = os.path.join(_root(), 'data', 'chitchat-bot.ico')
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        # Apply dark theme stylesheet
        self.setStyleSheet("""
            QWidget {
                background-color: #1a1f29;
                color: #E0E0E0;
            }
            QLabel {
                background-color: transparent;
            }
            QLineEdit {
                background-color: #2C313C;
                border: 2px solid #3A404C;
                border-radius: 8px;
                padding: 14px 15px;
                color: #E0E0E0;
                font-size: 14px;
                min-height: 20px;
            }
            QLineEdit:focus {
                border: 2px solid #00B383;
            }
            QLineEdit:hover {
                border: 2px solid #4A505C;
            }
            QPushButton {
                background-color: #00B383;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 14px 20px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #00A073;
            }
            QPushButton:pressed {
                background-color: #009063;
            }
            QPushButton:disabled {
                background-color: #3A404C;
                color: #707070;
            }
            QProgressBar {
                background-color: #2C313C;
                border: none;
                border-radius: 4px;
                text-align: center;
                color: #E0E0E0;
                height: 8px;
            }
            QProgressBar::chunk {
                background-color: #00B383;
                border-radius: 4px;
            }
        """)
        
        # Main layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)
        
        # Logo/Icon area
        logo_container = QFrame()
        logo_container.setStyleSheet("background-color: transparent;")
        logo_layout = QVBoxLayout(logo_container)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        
        # App icon
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_path = os.path.join(_root(), 'data', 'chitchat-bot.ico')
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            scaled_pixmap = pixmap.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio, 
                                         Qt.TransformationMode.SmoothTransformation)
            icon_label.setPixmap(scaled_pixmap)
        else:
            icon_label.setText("🤖")
            icon_label.setFont(QFont("Segoe UI", 48))
        
        logo_layout.addWidget(icon_label)
        layout.addWidget(logo_container)
        
        # Title
        title_label = QLabel("Chitchat Bot")
        title_label.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("color: #FFFFFF;")
        layout.addWidget(title_label)
        
        # Subtitle
        subtitle_label = QLabel("Sign in to continue")
        subtitle_label.setFont(QFont("Segoe UI", 12))
        subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_label.setStyleSheet("color: #A0A0A0;")
        layout.addWidget(subtitle_label)
        
        layout.addSpacing(20)
        
        # Password input
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Enter your password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self.attempt_signin)
        layout.addWidget(self.password_input)
        
        # Show/Hide password toggle
        password_toggle_layout = QHBoxLayout()
        password_toggle_layout.setContentsMargins(0, 0, 0, 0)
        
        self.show_password_btn = QPushButton("👁 Show Password")
        self.show_password_btn.setCheckable(True)
        self.show_password_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #A0A0A0;
                font-size: 12px;
                padding: 5px;
                font-weight: normal;
            }
            QPushButton:hover {
                color: #E0E0E0;
            }
            QPushButton:checked {
                color: #00B383;
            }
        """)
        self.show_password_btn.toggled.connect(self.toggle_password_visibility)
        password_toggle_layout.addStretch()
        password_toggle_layout.addWidget(self.show_password_btn)
        layout.addLayout(password_toggle_layout)
        
        layout.addSpacing(10)
        
        # Sign in button
        self.signin_button = QPushButton("SIGN IN")
        self.signin_button.setMinimumHeight(50)
        self.signin_button.clicked.connect(self.attempt_signin)
        self.signin_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.signin_button)
        
        # Progress bar (hidden by default)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(8)
        layout.addWidget(self.progress_bar)
        
        # Status label
        self.status_label = QLabel("")
        self.status_label.setFont(QFont("Segoe UI", 10))
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color: #A0A0A0;")
        layout.addWidget(self.status_label)
        
        layout.addStretch()
        
        # Footer
        footer_label = QLabel("© 2025 Chitchat Bot")
        footer_label.setFont(QFont("Segoe UI", 9))
        footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_label.setStyleSheet("color: #606060;")
        layout.addWidget(footer_label)
        
        # Center window on screen
        self.center_on_screen()
    
    def center_on_screen(self):
        """Center the window on the screen"""
        screen = QApplication.primaryScreen().availableGeometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)
    
    def toggle_password_visibility(self, checked):
        """Toggle password visibility"""
        if checked:
            self.password_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self.show_password_btn.setText("🙈 Hide Password")
        else:
            self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.show_password_btn.setText("👁 Show Password")
    
    def attempt_signin(self):
        """Attempt to sign in with the provided password"""
        password = self.password_input.text().strip()
        
        if not password:
            self.show_error("Please enter your password")
            return
        
        # Disable UI during signin
        self.set_ui_enabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setStyleSheet("color: #A0A0A0;")
        
        # Create and start worker thread
        self.signin_worker = SigninWorker(self.device_signin, password)
        self.signin_worker.progress_signal.connect(self.on_progress_update)
        self.signin_worker.status_signal.connect(self.on_status_update)
        self.signin_worker.success_signal.connect(self.on_signin_success)
        self.signin_worker.error_signal.connect(self.on_signin_error)
        self.signin_worker.start()
    
    def set_ui_enabled(self, enabled):
        """Enable or disable UI elements"""
        self.password_input.setEnabled(enabled)
        self.signin_button.setEnabled(enabled)
        self.show_password_btn.setEnabled(enabled)
    
    def on_progress_update(self, value):
        """Handle progress updates"""
        self.progress_bar.setValue(value)
    
    def on_status_update(self, message):
        """Handle status message updates"""
        self.status_label.setText(message)
    
    def on_signin_success(self, message, device_id, app_name):
        """Handle successful signin"""
        self.status_label.setStyleSheet("color: #00B383;")
        self.status_label.setText(message)
        
        # Emit success signal after a short delay to show success message
        QTimer.singleShot(1000, self.emit_signin_successful)
    
    def emit_signin_successful(self):
        """Emit the signin successful signal and close window"""
        self.signin_successful.emit()
        self.close()
    
    def on_signin_error(self, message):
        """Handle signin error"""
        self.set_ui_enabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setStyleSheet("color: #E74C3C;")
        self.status_label.setText(message)
        
        # Shake animation for password input
        self.shake_widget(self.password_input)
    
    def show_error(self, message):
        """Show error message"""
        self.status_label.setStyleSheet("color: #E74C3C;")
        self.status_label.setText(message)
        self.shake_widget(self.password_input)
    
    def shake_widget(self, widget):
        """Apply shake animation to a widget"""
        original_pos = widget.pos()
        
        animation = QPropertyAnimation(widget, b"pos", self)
        animation.setDuration(300)
        animation.setLoopCount(1)
        
        # Define shake keyframes
        animation.setKeyValueAt(0, original_pos)
        animation.setKeyValueAt(0.1, original_pos + Qt.point(10, 0) if hasattr(Qt, 'point') else original_pos)
        animation.setKeyValueAt(0.2, original_pos + Qt.point(-10, 0) if hasattr(Qt, 'point') else original_pos)
        animation.setKeyValueAt(0.3, original_pos + Qt.point(10, 0) if hasattr(Qt, 'point') else original_pos)
        animation.setKeyValueAt(0.4, original_pos + Qt.point(-10, 0) if hasattr(Qt, 'point') else original_pos)
        animation.setKeyValueAt(0.5, original_pos)
        
        animation.start()
    
    def get_device_signin(self):
        """Get the device signin instance for use in main app"""
        return self.device_signin
    
    def closeEvent(self, event):
        """Handle window close event"""
        # If not authenticated, quit the application
        if not self.device_signin.is_device_authenticated():
            QApplication.quit()
        event.accept()
