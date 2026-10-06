#!/usr/bin/env python3
"""
CLI Runner for Chitchat Bot
Terminal-based automation runner without GUI
"""
import os
import sys

# Ensure the project root is importable when this file is run directly
# (``python entry/cli_runner.py``), not only via ``python main.py``.
_ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT_DIR = os.path.dirname(_ENTRY_DIR)
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

import signal
import threading
from datetime import datetime

from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal
from entry.thread_manager import ThreadManager
from core.config_loader import load_chat_timing


class CLIRunner(QObject):
    """CLI-based runner for Chitchat Bot automation"""
    
    # Signal to safely quit application
    quit_signal = pyqtSignal()
    
    def __init__(self, thread_count: int, show_browser: bool):
        super().__init__()
        self.thread_count = thread_count
        self.show_browser = show_browser
        self.thread_manager = None
        self.start_time = None
        self.is_stopping = False
        
        # Get current working directory (not internal dir for exe)
        self.cwd = os.getcwd()
        
        # File paths (hardcoded, relative to current working directory)
        self.accounts_file = os.path.join(self.cwd, 'accounts.txt')
        self.fixed_messages_file = os.path.join(self.cwd, 'fixed-messages.txt')
        self.proxy_file = os.path.join(self.cwd, 'proxy.txt')
        self.snapchat_file = os.path.join(self.cwd, 'snapchat_username.txt')
        
    def log(self, message: str):
        """Print timestamped log message"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {message}")
    
    def load_accounts(self) -> list:
        """Load accounts from accounts.txt (email:password format)"""
        accounts = []
        
        if not os.path.exists(self.accounts_file):
            self.log(f"✗ Accounts file not found: {self.accounts_file}")
            return accounts
        
        try:
            with open(self.accounts_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and ':' in line:
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            email, password = parts[0].strip(), parts[1].strip()
                            if email and password:
                                accounts.append({'email': email, 'password': password})
            
            if accounts:
                self.log(f"✓ Loaded {len(accounts)} account(s) from {self.accounts_file}")
            else:
                self.log(f"✗ No valid accounts found in {self.accounts_file}")
        except Exception as e:
            self.log(f"✗ Error reading accounts file: {str(e)}")
        
        return accounts
    
    def load_fixed_messages(self) -> list:
        """Load fixed messages from fixed-messages.txt"""
        messages = []
        
        if not os.path.exists(self.fixed_messages_file):
            self.log(f"✗ Fixed messages file not found: {self.fixed_messages_file}")
            return messages
        
        try:
            with open(self.fixed_messages_file, 'r', encoding='utf-8') as f:
                messages = [line.strip() for line in f if line.strip()]
            
            if messages:
                self.log(f"✓ Loaded {len(messages)} message(s) from {self.fixed_messages_file}")
            else:
                self.log(f"✗ No messages found in {self.fixed_messages_file}")
        except Exception as e:
            self.log(f"✗ Error reading fixed messages file: {str(e)}")
        
        return messages
    
    def load_snapchat_ids(self) -> list:
        """Load Snapchat IDs from snapchat_username.txt"""
        ids = []
        
        if not os.path.exists(self.snapchat_file):
            self.log(f"✗ Snapchat IDs file not found: {self.snapchat_file}")
            return ids
        
        try:
            with open(self.snapchat_file, 'r', encoding='utf-8') as f:
                ids = [line.strip() for line in f if line.strip()]
            
            if ids:
                self.log(f"✓ Loaded {len(ids)} Snapchat ID(s) from {self.snapchat_file}")
            else:
                self.log(f"✗ No Snapchat IDs found in {self.snapchat_file}")
        except Exception as e:
            self.log(f"✗ Error reading Snapchat IDs file: {str(e)}")
        
        return ids
    
    def load_proxies(self) -> list:
        """Load proxies from proxy.txt (optional)"""
        proxies = []
        
        if not os.path.exists(self.proxy_file):
            self.log(f"ℹ Proxy file not found, continuing without proxy")
            return proxies
        
        try:
            with open(self.proxy_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            if not content:
                self.log(f"ℹ Proxy file is empty, continuing without proxy")
                return proxies
            
            # Parse proxy file
            for line in content.split('\n'):
                line = line.strip()
                if not line:
                    continue
                
                # Expected format: host:port:username:password or host:port
                parts = line.split(':')
                if len(parts) >= 2:
                    host = parts[0].strip()
                    port = parts[1].strip()
                    
                    # Always use HTTP proxy
                    proxy_config = {
                        'server': f'http://{host}:{port}'
                    }
                    
                    # Check if credentials are provided
                    if len(parts) >= 4:
                        proxy_config['username'] = parts[2].strip()
                        proxy_config['password'] = parts[3].strip()
                    
                    proxies.append(proxy_config)
            
            if proxies:
                self.log(f"✓ Loaded {len(proxies)} proxy(ies) from {self.proxy_file}")
            else:
                self.log(f"⚠ Could not parse proxy file, continuing without proxy")
        except Exception as e:
            self.log(f"⚠ Error reading proxy file: {str(e)}, continuing without proxy")
        
        return proxies
    
    def on_thread_started(self, thread_id: int, message: str):
        """Handle thread start event"""
        self.log(f"[Thread {thread_id}] {message}")
    
    def on_thread_log(self, thread_id: int, message: str):
        """Handle thread log event"""
        print(f"[T{thread_id}] {message}")
    
    def on_thread_status(self, thread_id: int, status: str):
        """Handle thread status event"""
        self.log(f"[Thread {thread_id}] Status: {status}")
    
    def on_thread_finished(self, thread_id: int):
        """Handle thread finished event"""
        self.log(f"[Thread {thread_id}] Finished")
    
    def on_all_threads_finished(self):
        """Handle all threads finished event"""
        self.log("All threads finished")
        if self.start_time:
            runtime = datetime.now() - self.start_time
            self.log(f"Total runtime: {runtime}")
        self.quit_signal.emit()
    
    def stop(self):
        """Stop all threads gracefully"""
        if self.is_stopping:
            return
        
        self.is_stopping = True
        self.log("Stopping all browser threads...")
        
        if self.thread_manager and self.thread_manager.is_running:
            self.thread_manager.stop_all_threads()
        else:
            self.on_all_threads_finished()
    
    def run(self):
        """Start the automation"""
        self.log("=" * 50)
        self.log("Chitchat Bot - CLI Runner")
        self.log("=" * 50)
        
        # Log configuration
        mode = "visible" if self.show_browser else "headless"
        self.log(f"Threads: {self.thread_count}")
        self.log(f"Browser mode: {mode}")
        self.log("=" * 50)
        
        # Load all required files
        accounts = self.load_accounts()
        if not accounts:
            self.log("✗ Cannot start without accounts. Exiting.")
            self.quit_signal.emit()
            return
        
        fixed_messages = self.load_fixed_messages()
        if not fixed_messages:
            self.log("✗ Cannot start without fixed messages. Exiting.")
            self.quit_signal.emit()
            return
        
        snapchat_ids = self.load_snapchat_ids()
        if not snapchat_ids:
            self.log("✗ Cannot start without Snapchat IDs. Exiting.")
            self.quit_signal.emit()
            return
        
        proxy_configs = self.load_proxies()
        chat_timing = load_chat_timing()
        
        # Log configuration summary
        self.log("=" * 50)
        self.log(f"Starting {self.thread_count} browser thread(s) for Chitchat.gg...")
        self.log(f"  Accounts: {len(accounts)} available")
        self.log(f"  Fixed Messages: {len(fixed_messages)} messages")
        self.log(f"  Snapchat IDs: {len(snapchat_ids)} available")
        self.log(f"  Threads: {self.thread_count}")
        self.log(f"  Chat Timeout: 30s")
        self.log(
            f"  New Chat Delay: {chat_timing['new_chat_delay_min_seconds']:.1f} - "
            f"{chat_timing['new_chat_delay_max_seconds']:.1f}s"
        )
        self.log(
            f"  Rest: every {chat_timing['rest_interval_min_minutes']:.1f} - "
            f"{chat_timing['rest_interval_max_minutes']:.1f}m for "
            f"{chat_timing['rest_duration_min_minutes']:.1f} - "
            f"{chat_timing['rest_duration_max_minutes']:.1f}m"
        )
        
        if proxy_configs:
            if len(proxy_configs) >= self.thread_count:
                self.log(f"  Proxies: {len(proxy_configs)} available (different proxy per thread)")
            else:
                self.log(f"  Proxies: {len(proxy_configs)} available (will rotate among threads)")
        else:
            self.log("  Proxy: None (direct connection)")
        
        # Warnings
        if self.thread_count > len(accounts):
            self.log(f"⚠ Warning: {self.thread_count} threads but only {len(accounts)} account(s). Some threads will share accounts.")
        
        if self.thread_count > len(snapchat_ids):
            self.log(f"⚠ Warning: {self.thread_count} threads but only {len(snapchat_ids)} Snap ID(s). Some threads will share IDs.")
        
        self.log("=" * 50)
        self.log("Press Ctrl+C to stop")
        self.log("=" * 50)
        
        # Start runtime tracking
        self.start_time = datetime.now()
        
        # Create thread manager
        self.thread_manager = ThreadManager(
            accounts=accounts,
            snapchat_ids=snapchat_ids,
            fixed_messages=fixed_messages,
            headless=False,  # Always show browser during login
            chat_timeout=8,
            chat_timing=chat_timing,
            proxy_configs=proxy_configs,
            hide_after_login=not self.show_browser  # Hide after login if user chose 'n'
        )
        
        # Connect signals
        self.thread_manager.thread_started.connect(self.on_thread_started)
        self.thread_manager.thread_log.connect(self.on_thread_log)
        self.thread_manager.thread_status.connect(self.on_thread_status)
        self.thread_manager.thread_finished.connect(self.on_thread_finished)
        self.thread_manager.all_threads_finished.connect(self.on_all_threads_finished)
        
        # Start threads
        self.thread_manager.start_threads(self.thread_count)


def get_user_input():
    """Get user input for thread count and browser visibility"""
    print("\n" + "=" * 50)
    print("Chitchat Bot - CLI Runner Setup")
    print("=" * 50)
    
    # Get thread count
    while True:
        try:
            thread_input = input("\nHow many threads? (1-10): ").strip()
            thread_count = int(thread_input)
            if 1 <= thread_count <= 10:
                break
            else:
                print("Please enter a number between 1 and 10.")
        except ValueError:
            print("Please enter a valid number.")
    
    # Get browser visibility
    while True:
        browser_input = input("Show browser during chat? (y/n): ").strip().lower()
        if browser_input in ['y', 'yes']:
            show_browser = True
            break
        elif browser_input in ['n', 'no']:
            show_browser = False
            break
        else:
            print("Please enter 'y' or 'n'.")
    
    return thread_count, show_browser


def main():
    # Get user input
    thread_count, show_browser = get_user_input()
    
    # Create Qt application (needed for signals/slots)
    app = QCoreApplication(sys.argv)
    
    # Create runner
    runner = CLIRunner(thread_count, show_browser)
    
    # Connect quit signal
    runner.quit_signal.connect(app.quit)
    
    # Setup signal handler for Ctrl+C
    def signal_handler(signum, frame):
        print("\n")
        runner.stop()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # On Windows, we need to periodically process events to handle signals
    if sys.platform == 'win32':
        import ctypes
        
        # Create a timer to process events and check for signals
        from PyQt6.QtCore import QTimer
        timer = QTimer()
        timer.timeout.connect(lambda: None)  # Just process events
        timer.start(100)  # Check every 100ms
    
    # Start the runner (delayed to allow event loop to start)
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(0, runner.run)
    
    # Run event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
