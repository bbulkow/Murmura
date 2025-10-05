"""
Wrapper for device-manager scripts to integrate with Scape Server.
Uses the existing proven device_scanner.py for efficient network scanning.
"""

import sys
import os
import json
import logging
import subprocess
import ipaddress
import netifaces
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime
import time

# Add device-manager directory (parallel to scape_server) to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
device_manager_path = os.path.join(parent_dir, 'device-manager')
sys.path.insert(0, device_manager_path)

# Configure logging with detailed output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger('scape_server.network')


class NetworkConfig:
    """Manages network configuration and persistence."""
    
    def __init__(self, config_file: str = 'scape_server/network_config.json'):
        self.config_file = Path(config_file)
        self.config = self.load_config()
        
    def load_config(self) -> Dict:
        """Load network configuration from file."""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                    logger.info(f"Loaded network config: {config}")
                    return config
            except Exception as e:
                logger.error(f"Error loading config: {e}")
        
        # Default config
        return {
            'selected_networks': [],
            'selected_interfaces': [],
            'scan_all': True,
            'timeout': 2,
            'concurrent_limit': 50,
            'probe_timeout': 0.5,  # Timeout for device status probes (should be short)
            'refresh_interval': 10  # Seconds between auto-refresh cycles
        }
    
    def save_config(self):
        """Save network configuration to file."""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
            logger.info(f"Saved network config: {self.config}")
        except Exception as e:
            logger.error(f"Error saving config: {e}")
    
    def get_available_interfaces(self) -> List[Dict]:
        """Get list of available network interfaces with their details."""
        interfaces = []
        
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for addr_info in addrs[netifaces.AF_INET]:
                    ip = addr_info.get('addr', '')
                    netmask = addr_info.get('netmask', '')
                    
                    # Skip loopback
                    if ip.startswith('127.'):
                        continue
                    
                    try:
                        # Calculate network
                        network = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
                        interfaces.append({
                            'name': iface,
                            'ip': ip,
                            'netmask': netmask,
                            'network': str(network),
                            'host_count': len(list(network.hosts()))
                        })
                        logger.debug(f"Found interface: {iface} - {network}")
                    except ValueError:
                        continue
        
        return interfaces
    
    def get_selected_networks(self) -> List[str]:
        """Get list of networks to scan based on configuration."""
        if self.config['scan_all']:
            # Get all networks from all interfaces
            networks = []
            for iface in self.get_available_interfaces():
                if iface['network'] not in networks:
                    networks.append(iface['network'])
            logger.info(f"Scanning all networks: {networks}")
            return networks
        
        elif self.config['selected_networks']:
            logger.info(f"Scanning selected networks: {self.config['selected_networks']}")
            return self.config['selected_networks']
        
        elif self.config['selected_interfaces']:
            # Get networks from selected interfaces
            networks = []
            interfaces = self.get_available_interfaces()
            for iface in interfaces:
                if iface['name'] in self.config['selected_interfaces']:
                    if iface['network'] not in networks:
                        networks.append(iface['network'])
            logger.info(f"Scanning networks from selected interfaces: {networks}")
            return networks
        
        else:
            # Default to all networks
            networks = []
            for iface in self.get_available_interfaces():
                if iface['network'] not in networks:
                    networks.append(iface['network'])
            logger.info(f"No selection configured, scanning all: {networks}")
            return networks


class DeviceScannerWrapper:
    """Wrapper for device_scanner.py with progress tracking and logging."""
    
    def __init__(self, config: NetworkConfig, progress_callback=None):
        self.config = config
        self.progress_callback = progress_callback
        self.device_map_file = Path('scape_server/device_map.json')
        self.total_hosts = 0
        self.scanned_hosts = 0
        
    def estimate_total_hosts(self, networks: List[str]) -> int:
        """Estimate total number of hosts to scan."""
        total = 0
        for network_str in networks:
            try:
                network = ipaddress.IPv4Network(network_str)
                total += len(list(network.hosts()))
            except ValueError:
                pass
        return total
    
    def scan_network(self, network: str, mode: str = 'add') -> Dict:
        """Run device_scanner.py for a specific network."""
        logger.info(f"Starting scan of network: {network}")
        
        # Get path to device_scanner.py (parallel directory)
        parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        device_scanner_path = os.path.join(parent_dir, 'device-manager', 'device_scanner.py')
        
        # Build command
        cmd = [
            sys.executable,
            device_scanner_path,
            '--net', network,
            '--action', mode,
            '--timeout', str(self.config.config['timeout']),
            '--concurrent', str(self.config.config['concurrent_limit']),
            '--map-file', str(self.device_map_file)
        ]
        
        logger.debug(f"Running command: {' '.join(cmd)}")
        
        try:
            # Run the scanner with real-time output
            start_time = time.time()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1
            )
            
            # Read output line by line for progress updates
            for line in process.stdout:
                line = line.strip()
                if line:
                    logger.info(f"Scanner: {line}")
                    
                    # Parse progress if possible
                    if "Progress:" in line and self.progress_callback:
                        try:
                            # Extract progress numbers from format "Progress: 50/254 (19.7%)"
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if part == "Progress:":
                                    progress_str = parts[i+1]
                                    current, total = progress_str.split('/')
                                    self.scanned_hosts = int(current)
                                    
                                    percent = (self.scanned_hosts / self.total_hosts) * 100 if self.total_hosts > 0 else 0
                                    logger.debug(f"Calling progress_callback: {self.scanned_hosts}/{self.total_hosts} = {percent:.1f}%")
                                    self.progress_callback(self.scanned_hosts, self.total_hosts, percent)
                                    break
                        except (IndexError, ValueError) as e:
                            logger.warning(f"Failed to parse progress from line: {line} - {e}")
            
            process.wait()
            elapsed = time.time() - start_time
            logger.info(f"Network {network} scan completed in {elapsed:.2f} seconds")
            
            if process.returncode != 0:
                logger.error(f"Scanner returned error code: {process.returncode}")
                return {}
            
            # Load the results
            if self.device_map_file.exists():
                with open(self.device_map_file, 'r') as f:
                    data = json.load(f)
                    return data
            
            return {}
            
        except Exception as e:
            logger.error(f"Error running scanner: {e}")
            return {}
    
    def scan_all_networks(self, progress_callback=None, network_callback=None) -> List[Dict]:
        """Scan all configured networks and return list of devices."""
        self.progress_callback = progress_callback
        networks = self.config.get_selected_networks()
        
        if not networks:
            logger.warning("No networks to scan!")
            if network_callback:
                network_callback("No networks configured", 0, 0)
            return []
        
        logger.info(f"=== Starting scan of {len(networks)} network(s) ===")
        self.total_hosts = self.estimate_total_hosts(networks)
        logger.info(f"Estimated total hosts to scan: {self.total_hosts}")
        
        all_devices = []
        
        # Always use 'add' mode to preserve existing devices
        # (devices can be manually deleted if needed)
        for i, network in enumerate(networks):
            mode = 'add'  # Always add, never overwrite
            logger.info(f"Scanning network {i+1}/{len(networks)}: {network} (mode: {mode})")
            
            # Notify which network is being scanned
            if network_callback:
                network_callback(network, i+1, len(networks))
            
            result = self.scan_network(network, mode)
            
            if result and 'devices' in result:
                devices = result['devices']
                logger.info(f"Found {len(devices)} device(s) in {network}")
                all_devices = devices  # Latest scan result contains all devices
        
        logger.info(f"=== Scan complete: {len(all_devices)} total devices found ===")
        return all_devices
    
    def clear_all_devices(self) -> bool:
        """Clear all devices from the registry."""
        try:
            logger.info("Clearing all devices from registry")
            # Write empty device map
            empty_map = {
                'scan_time': datetime.now().isoformat(),
                'scan_mode': 'create',
                'network_range': 'cleared',
                'device_count': 0,
                'devices': []
            }
            with open(self.device_map_file, 'w') as f:
                json.dump(empty_map, f, indent=2)
            logger.info("All devices cleared successfully")
            return True
        except Exception as e:
            logger.error(f"Error clearing devices: {e}")
            return False
    
    def quick_scan(self, device_ips: List[str]) -> List[Dict]:
        """Quick scan of specific IPs (for status updates)."""
        logger.info(f"Quick scan of {len(device_ips)} known devices")
        
        # Create a temporary network that includes these IPs
        # This is a workaround since device_scanner expects a network range
        if not device_ips:
            return []
        
        # Group IPs by their /24 subnet
        subnets = {}
        for ip in device_ips:
            try:
                addr = ipaddress.ip_address(ip)
                subnet = ipaddress.ip_network(f"{ip}/24", strict=False)
                subnet_str = str(subnet)
                if subnet_str not in subnets:
                    subnets[subnet_str] = []
                subnets[subnet_str].append(ip)
            except ValueError:
                pass
        
        all_devices = []
        for subnet in subnets.keys():
            logger.info(f"Quick scanning subnet: {subnet}")
            result = self.scan_network(subnet, mode='update')
            if result and 'devices' in result:
                # Filter to only the IPs we care about
                devices = [d for d in result['devices'] if d.get('ip_address') in device_ips]
                all_devices.extend(devices)
        
        return all_devices


class DeviceRegistry:
    """Compatible device registry using device_scanner.py format."""
    
    def __init__(self, registry_file: str = 'scape_server/device_map.json'):
        self.registry_file = Path(registry_file)
        self.devices = {}
        self.load_registry()
    
    def load_registry(self):
        """Load device registry from device_scanner output."""
        if self.registry_file.exists():
            try:
                with open(self.registry_file, 'r') as f:
                    data = json.load(f)
                    if 'devices' in data:
                        # Convert list to dict keyed by ID
                        for device in data['devices']:
                            device_id = device.get('id', device.get('ip_address'))
                            self.devices[device_id] = device
                    logger.info(f"Loaded {len(self.devices)} devices from registry")
            except Exception as e:
                logger.error(f"Error loading registry: {e}")
                self.devices = {}
    
    def update_device(self, device_info: Dict):
        """Update device in registry."""
        device_id = device_info.get('id', device_info.get('ip_address'))
        self.devices[device_id] = device_info
        logger.debug(f"Updated device: {device_id}")
    
    def get_device(self, device_id: str) -> Optional[Dict]:
        """Get device by ID."""
        return self.devices.get(device_id)
    
    def get_all_devices(self) -> Dict:
        """Get all devices as dict."""
        return self.devices
    
    def get_device_list(self) -> List[Dict]:
        """Get all devices as list."""
        return list(self.devices.values())
