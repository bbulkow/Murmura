"""
Network scanner module for discovering ESP32 devices on the local network.
"""
import socket
import subprocess
import platform
import re
import threading
import time
import requests
from typing import List, Dict, Optional
import netifaces
import ipaddress
import json
from concurrent.futures import ThreadPoolExecutor, as_completed


class NetworkScanner:
    """Scans local network for ESP32 devices running the loudframe software."""
    
    def __init__(self, timeout: float = 0.5):
        self.timeout = timeout
        self.devices = []
        self.scanning = False
        
    def get_local_networks(self) -> List[str]:
        """Get all local network subnets."""
        networks = []
        for interface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(interface)
            if netifaces.AF_INET in addrs:
                for addr_info in addrs[netifaces.AF_INET]:
                    if 'addr' in addr_info and 'netmask' in addr_info:
                        ip = addr_info['addr']
                        netmask = addr_info['netmask']
                        # Skip loopback
                        if ip.startswith('127.'):
                            continue
                        try:
                            # Calculate network address
                            network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                            networks.append(str(network))
                        except ValueError:
                            continue
        return list(set(networks))  # Remove duplicates
    
    def ping_host(self, ip: str) -> bool:
        """Check if host is reachable using ping."""
        param = '-n' if platform.system().lower() == 'windows' else '-c'
        command = ['ping', param, '1', '-W', '1', ip]
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, 
                                  stderr=subprocess.DEVNULL, timeout=1)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, Exception):
            return False
    
    def check_http_device(self, ip: str) -> Optional[Dict]:
        """Check if IP responds to HTTP and appears to be an ESP32 device."""
        try:
            # Try to get device status from the HTTP API
            response = requests.get(f"http://{ip}/api/status", timeout=self.timeout)
            if response.status_code == 200:
                data = response.json()
                return {
                    'ip': ip,
                    'type': 'ESP32 Loudframe',
                    'status': 'online',
                    'playing': data.get('playing', False),
                    'volume': data.get('volume', 0),
                    'ssid': data.get('ssid', 'Unknown'),
                    'id': data.get('id', 'Unknown'),
                    'last_seen': time.time()
                }
        except requests.exceptions.RequestException:
            pass
        
        # Try basic HTTP check
        try:
            response = requests.get(f"http://{ip}/", timeout=self.timeout)
            if response.status_code == 200:
                # Check if response contains ESP32 indicators
                if 'esp32' in response.text.lower() or 'loudframe' in response.text.lower():
                    return {
                        'ip': ip,
                        'type': 'ESP32 Device',
                        'status': 'online',
                        'playing': False,
                        'volume': 0,
                        'ssid': 'Unknown',
                        'id': 'Unknown',
                        'last_seen': time.time()
                    }
        except requests.exceptions.RequestException:
            pass
        
        return None
    
    def scan_network(self, progress_callback=None) -> List[Dict]:
        """Scan all local networks for ESP32 devices."""
        self.scanning = True
        self.devices = []
        
        networks = self.get_local_networks()
        total_hosts = 0
        
        # Calculate total hosts for progress
        for network_str in networks:
            network = ipaddress.IPv4Network(network_str)
            total_hosts += len(list(network.hosts()))
        
        scanned = 0
        
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = []
            
            for network_str in networks:
                network = ipaddress.IPv4Network(network_str)
                for host in network.hosts():
                    ip = str(host)
                    futures.append(executor.submit(self.check_http_device, ip))
            
            for future in as_completed(futures):
                scanned += 1
                if progress_callback:
                    progress_callback(scanned, total_hosts)
                    
                result = future.result()
                if result:
                    self.devices.append(result)
        
        self.scanning = False
        return self.devices
    
    def quick_scan(self, known_ips: List[str]) -> List[Dict]:
        """Quickly scan a list of known IP addresses."""
        devices = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self.check_http_device, ip) for ip in known_ips]
            
            for future in as_completed(futures):
                result = future.result()
                if result:
                    devices.append(result)
        
        return devices
    
    def continuous_scan(self, interval: int = 30, callback=None):
        """Continuously scan the network at specified intervals."""
        while self.scanning:
            devices = self.scan_network()
            if callback:
                callback(devices)
            time.sleep(interval)


class DeviceRegistry:
    """Manages a registry of discovered devices with persistence."""
    
    def __init__(self, registry_file: str = 'device_registry.json'):
        self.registry_file = registry_file
        self.devices = {}
        self.load_registry()
        
    def load_registry(self):
        """Load device registry from file."""
        try:
            with open(self.registry_file, 'r') as f:
                data = json.load(f)
                self.devices = data.get('devices', {})
        except (FileNotFoundError, json.JSONDecodeError):
            self.devices = {}
    
    def save_registry(self):
        """Save device registry to file."""
        data = {
            'devices': self.devices,
            'last_updated': time.time()
        }
        with open(self.registry_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    def update_device(self, device_info: Dict):
        """Update or add a device to the registry."""
        device_id = device_info.get('id', device_info['ip'])
        
        if device_id in self.devices:
            # Update existing device
            self.devices[device_id].update(device_info)
        else:
            # Add new device
            self.devices[device_id] = device_info
            self.devices[device_id]['first_seen'] = time.time()
        
        self.devices[device_id]['last_seen'] = time.time()
        self.save_registry()
    
    def get_device(self, device_id: str) -> Optional[Dict]:
        """Get device information by ID."""
        return self.devices.get(device_id)
    
    def get_all_devices(self) -> Dict:
        """Get all registered devices."""
        return self.devices
    
    def remove_device(self, device_id: str):
        """Remove a device from the registry."""
        if device_id in self.devices:
            del self.devices[device_id]
            self.save_registry()
    
    def mark_offline(self, device_id: str):
        """Mark a device as offline."""
        if device_id in self.devices:
            self.devices[device_id]['status'] = 'offline'
            self.save_registry()


if __name__ == "__main__":
    # Test the scanner
    scanner = NetworkScanner()
    registry = DeviceRegistry()
    
    print("Starting network scan...")
    
    def progress(current, total):
        percent = (current / total) * 100
        print(f"Scanning: {current}/{total} ({percent:.1f}%)", end='\r')
    
    devices = scanner.scan_network(progress_callback=progress)
    print(f"\n\nFound {len(devices)} device(s):")
    
    for device in devices:
        print(f"  - {device['ip']}: {device['type']} (ID: {device['id']})")
        registry.update_device(device)
    
    print("\nRegistry updated.")
