# Device Signin Module for App-Specific Authentication
# This module handles device authentication for the Chitchat Bot application

import json
import requests
import platform
import uuid
import socket
import threading


class DeviceSignin:
    """Device authentication class for app-specific signin before accessing the main application"""
    
    def __init__(self, api_url="https://app.emmarn.com/", app_id="default"):
        """
        Initialize the DeviceSignin class
        
        Args:
            api_url (str): The Cloudflare Worker URL for device authentication
            app_id (str): The specific app ID for this application
        """
        self.api_url = api_url
        self.app_id = app_id
        self.is_authenticated = False
        self.device_id = None
        self.app_name = None
        self.token = None  # Authentication token for API access
        
    def get_device_info(self):
        """Collect device information for signin"""
        device_info = {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "cpu": platform.processor(),
            "app_id": self.app_id  # App-specific identification
        }
        
        # Get MAC address
        try:
            mac = ':'.join(['{:02x}'.format((uuid.getnode() >> elements) & 0xff) 
                           for elements in range(5, -1, -1)])
            device_info["mac_address"] = mac
        except Exception:
            device_info["mac_address"] = None
        
        # System UUID (cross-platform) - primary identifier
        system_uuid = uuid.getnode()
        device_info["system_uuid"] = str(system_uuid)
        
        return device_info
    
    def signin(self, app_password, progress_callback=None, status_callback=None):
        """
        Sign in the device with the collected information
        
        Args:
            app_password (str): App-specific password for authentication
            progress_callback (callable): Optional callback for progress updates (0-100)
            status_callback (callable): Optional callback for status messages
            
        Returns:
            tuple: (success: bool, message: str, device_id: str or None)
        """
        try:
            if progress_callback:
                progress_callback(5)
            
            if status_callback:
                status_callback(f"🔍 Collecting device information...")
            
            device_info = self.get_device_info()
            device_info["password"] = app_password
            
            if progress_callback:
                progress_callback(25)
            
            if status_callback:
                status_callback("📡 Connecting to authentication server...")
            
            if progress_callback:
                progress_callback(50)
            
            response = requests.post(f"{self.api_url}/api/device/signin", 
                                   json=device_info,
                                   headers={"Content-Type": "application/json"},
                                   timeout=30)
            
            if progress_callback:
                progress_callback(75)
            
            if status_callback:
                status_callback("🔐 Verifying credentials...")
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    self.is_authenticated = True
                    self.device_id = result.get('device_id')
                    self.app_name = result.get('app_name')
                    self.token = result.get('token')  # Store the authentication token
                    
                    if progress_callback:
                        progress_callback(100)
                    
                    success_message = f"✅ Sign in successful!"
                    if status_callback:
                        status_callback(success_message)
                    
                    return True, success_message, self.device_id
                else:
                    error_message = f"❌ Sign in failed: {result.get('error')}"
                    if status_callback:
                        status_callback(error_message)
                    return False, error_message, None
            else:
                error_message = f"❌ HTTP Error {response.status_code}: {response.text}"
                if status_callback:
                    status_callback(error_message)
                return False, error_message, None
                
        except requests.exceptions.Timeout:
            error_message = "❌ Connection timeout. Please check your internet connection."
            if status_callback:
                status_callback(error_message)
            return False, error_message, None
            
        except requests.exceptions.RequestException as e:
            error_message = f"❌ Network error: {e}"
            if status_callback:
                status_callback(error_message)
            return False, error_message, None
        
        except Exception as e:
            error_message = f"❌ Unexpected error: {e}"
            if status_callback:
                status_callback(error_message)
            return False, error_message, None
    
    def signin_async(self, app_password, success_callback=None, error_callback=None, 
                     progress_callback=None, status_callback=None):
        """
        Asynchronous signin method for use with GUI threading
        
        Args:
            app_password (str): App-specific password for authentication
            success_callback (callable): Called on successful signin with (message, device_id, app_name)
            error_callback (callable): Called on signin failure with error message
            progress_callback (callable): Optional callback for progress updates (0-100)
            status_callback (callable): Optional callback for status messages
        
        Returns:
            threading.Thread: The thread object for the async operation
        """
        def _signin_thread():
            success, message, device_id = self.signin(app_password, progress_callback, status_callback)
            
            if success and success_callback:
                success_callback(message, device_id, self.app_name)
            elif not success and error_callback:
                error_callback(message)
        
        thread = threading.Thread(target=_signin_thread, daemon=True)
        thread.start()
        return thread
    
    def is_device_authenticated(self):
        """Check if device is currently authenticated"""
        return self.is_authenticated
    
    def get_device_id(self):
        """Get the current device ID if authenticated"""
        return self.device_id if self.is_authenticated else None
    
    def get_app_name(self):
        """Get the current app name if authenticated"""
        return self.app_name if self.is_authenticated else None
    
    def get_app_id(self):
        """Get the current app ID"""
        return self.app_id
    
    def get_token(self):
        """Get the authentication token if authenticated"""
        return self.token if self.is_authenticated else None
    
    def get_auth_headers(self):
        """Get authorization headers for API requests"""
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}
    
    def logout(self):
        """Logout device and invalidate token on server"""
        if self.token:
            try:
                response = requests.post(
                    f"{self.api_url}/api/device/logout",
                    headers=self.get_auth_headers(),
                    timeout=10
                )
                if response.status_code == 200:
                    print("✅ Token invalidated on server")
            except:
                pass  # Continue with local logout even if server call fails
        
        self.is_authenticated = False
        self.device_id = None
        self.app_name = None
        self.token = None
