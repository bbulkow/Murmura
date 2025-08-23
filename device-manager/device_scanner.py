#!/usr/bin/env python3
"""
ESP32 Network Device Scanner
Discovers ESP32 devices on the network and maintains a device map JSON file.

Usage:
    python device_scanner.py <network_range> <mode> [--timeout <seconds>] [--concurrent <num>]
    
Arguments:
    network_range: Network range in CIDR format (e.g., 192.168.1.0/24)
    mode: Operation mode - 'create', 'add', or 'update'
    
Options:
    --timeout: Connection timeout in seconds (default: 2)
    --concurrent: Maximum concurrent connections (default: 50)
    --map-file: Path to device map JSON file (default: device_map.json)
    
Examples:
    python device_scanner.py 192.168.1.0/24 create
    python device_scanner.py 192.168.1.0/24 add --timeout 3
    python device_scanner.py 192.168.1.0/24 update --concurrent 100
"""

import asyncio
import aiohttp
import argparse
import json
import ipaddress
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class DeviceScanner:
    """Asynchronous network scanner for ESP32 devices."""
    
    def __init__(self, network_range: str, mode: str, timeout: int = 2, 
                 concurrent_limit: int = 50, map_file: str = "device_map.json"):
        """
        Initialize the scanner.
        
        Args:
            network_range: Network range in CIDR format
            mode: Operation mode ('create', 'add', 'update')
            timeout: Connection timeout in seconds
            concurrent_limit: Maximum concurrent connections
            map_file: Path to device map JSON file
        """
        self.network = ipaddress.ip_network(network_range, strict=False)
        self.mode = mode.lower()
        self.timeout = timeout
        self.concurrent_limit = concurrent_limit
        self.map_file = Path(map_file)
        self.discovered_devices = {}
        self.scan_stats = {
            'total_ips': 0,
            'scanned': 0,
            'found': 0,
            'errors': 0,
            'start_time': None,
            'end_time': None
        }
        
    async def scan_device(self, session: aiohttp.ClientSession, ip: str) -> Optional[Dict[str, Any]]:
        """
        Scan a single IP address for ESP32 device.
        
        Args:
            session: aiohttp session for making requests
            ip: IP address to scan
            
        Returns:
            Device information if found, None otherwise
        """
        url = f"http://{ip}/api/status"
        
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Extract device information
                    device_info = {
                        'ip_address': ip,
                        'mac_address': data.get('mac_address', 'UNKNOWN'),
                        'id': data.get('id', 'UNKNOWN'),
                        'wifi_connected': data.get('wifi_connected', False),
                        'firmware_version': data.get('firmware_version', 'UNKNOWN'),
                        'uptime_seconds': data.get('uptime_seconds', 0),
                        'last_seen': datetime.now().isoformat(),
                        'online': True
                    }
                    
                    logger.info(f"✓ Found device at {ip}: ID={device_info['id']}, MAC={device_info['mac_address']}")
                    return device_info
                    
        except asyncio.TimeoutError:
            # Timeout is expected for most IPs, don't log
            pass
        except aiohttp.ClientError as e:
            # Connection errors are expected for non-device IPs
            pass
        except Exception as e:
            logger.debug(f"Unexpected error scanning {ip}: {e}")
            self.scan_stats['errors'] += 1
            
        return None
    
    async def scan_batch(self, session: aiohttp.ClientSession, ips: List[str]) -> List[Dict[str, Any]]:
        """
        Scan a batch of IP addresses concurrently.
        
        Args:
            session: aiohttp session
            ips: List of IP addresses to scan
            
        Returns:
            List of discovered devices
        """
        tasks = [self.scan_device(session, ip) for ip in ips]
        results = await asyncio.gather(*tasks)
        
        # Update progress
        self.scan_stats['scanned'] += len(ips)
        progress = (self.scan_stats['scanned'] / self.scan_stats['total_ips']) * 100
        logger.info(f"Progress: {self.scan_stats['scanned']}/{self.scan_stats['total_ips']} "
                   f"({progress:.1f}%) - Found: {self.scan_stats['found']} devices")
        
        return [r for r in results if r is not None]
    
    async def scan_network(self) -> Dict[str, Dict[str, Any]]:
        """
        Scan the entire network range for devices.
        
        Returns:
            Dictionary of discovered devices keyed by MAC address
        """
        logger.info(f"Starting network scan of {self.network}")
        logger.info(f"Mode: {self.mode.upper()}, Timeout: {self.timeout}s, "
                   f"Concurrent limit: {self.concurrent_limit}")
        
        # Generate all IP addresses in the network
        all_ips = [str(ip) for ip in self.network.hosts()]
        self.scan_stats['total_ips'] = len(all_ips)
        self.scan_stats['start_time'] = time.time()
        
        logger.info(f"Scanning {len(all_ips)} IP addresses...")
        
        # Create aiohttp session with custom connector
        connector = aiohttp.TCPConnector(limit=self.concurrent_limit, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Process IPs in batches
            batch_size = self.concurrent_limit
            for i in range(0, len(all_ips), batch_size):
                batch = all_ips[i:i + batch_size]
                devices = await self.scan_batch(session, batch)
                
                # Store discovered devices by MAC address
                for device in devices:
                    mac = device['mac_address']
                    self.discovered_devices[mac] = device
                    self.scan_stats['found'] += 1
        
        self.scan_stats['end_time'] = time.time()
        scan_duration = self.scan_stats['end_time'] - self.scan_stats['start_time']
        
        logger.info(f"Scan completed in {scan_duration:.2f} seconds")
        logger.info(f"Found {self.scan_stats['found']} devices")
        
        return self.discovered_devices
    
    def load_existing_map(self) -> Dict[str, Dict[str, Any]]:
        """
        Load existing device map from JSON file.
        
        Returns:
            Dictionary of existing devices keyed by MAC address
        """
        if not self.map_file.exists():
            return {}
            
        try:
            with open(self.map_file, 'r') as f:
                data = json.load(f)
                # Convert to MAC-keyed dictionary if needed
                if isinstance(data, dict) and 'devices' in data:
                    return {d['mac_address']: d for d in data['devices']}
                elif isinstance(data, list):
                    return {d['mac_address']: d for d in data}
                else:
                    return data
        except Exception as e:
            logger.error(f"Error loading existing map: {e}")
            return {}
    
    def merge_device_maps(self, existing: Dict[str, Dict[str, Any]], 
                         new: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """
        Merge new device discoveries with existing map based on mode.
        
        Args:
            existing: Existing device map
            new: Newly discovered devices
            
        Returns:
            Merged device map
        """
        if self.mode == 'create':
            # Create mode: completely replace with new discoveries
            logger.info("CREATE mode: Replacing entire device map")
            return new
            
        elif self.mode == 'add':
            # Add mode: add new devices and update existing ones
            logger.info("ADD mode: Adding new devices and updating existing ones")
            merged = existing.copy()
            
            # Mark all existing devices as offline initially
            for mac in merged:
                merged[mac]['online'] = False
            
            # Add/update with new discoveries
            for mac, device in new.items():
                if mac in merged:
                    logger.info(f"Updating existing device: {mac}")
                    # Preserve some fields from existing entry if needed
                    merged[mac].update(device)
                else:
                    logger.info(f"Adding new device: {mac}")
                    merged[mac] = device
                    
            return merged
            
        elif self.mode == 'update':
            # Update mode: only update devices that already exist in the map
            logger.info("UPDATE mode: Only updating existing devices")
            merged = existing.copy()
            
            # Mark all devices as offline initially
            for mac in merged:
                merged[mac]['online'] = False
            
            # Only update devices that exist in both maps
            for mac, device in new.items():
                if mac in merged:
                    logger.info(f"Updating device: {mac}")
                    merged[mac].update(device)
                else:
                    logger.info(f"Skipping new device (not in existing map): {mac}")
                    
            return merged
            
        else:
            raise ValueError(f"Invalid mode: {self.mode}")
    
    def save_device_map(self, devices: Dict[str, Dict[str, Any]]) -> None:
        """
        Save device map to JSON file.
        
        Args:
            devices: Device map to save
        """
        # Convert to list format for better readability
        device_list = list(devices.values())
        
        # Sort by IP address for consistency
        device_list.sort(key=lambda d: ipaddress.ip_address(d['ip_address']))
        
        output = {
            'scan_time': datetime.now().isoformat(),
            'scan_mode': self.mode,
            'network_range': str(self.network),
            'device_count': len(device_list),
            'devices': device_list
        }
        
        # Save to file with pretty formatting
        with open(self.map_file, 'w') as f:
            json.dump(output, f, indent=2, sort_keys=False)
            
        logger.info(f"Device map saved to {self.map_file}")
        logger.info(f"Total devices in map: {len(device_list)}")
        
        # Print summary
        online_count = sum(1 for d in device_list if d.get('online', False))
        offline_count = len(device_list) - online_count
        logger.info(f"Online: {online_count}, Offline: {offline_count}")
    
    async def run(self) -> None:
        """Run the complete scanning process."""
        try:
            # Load existing map if needed
            existing_map = {}
            if self.mode in ['add', 'update']:
                existing_map = self.load_existing_map()
                logger.info(f"Loaded {len(existing_map)} devices from existing map")
            
            # Scan the network
            discovered = await self.scan_network()
            
            # Merge based on mode
            final_map = self.merge_device_maps(existing_map, discovered)
            
            # Save the result
            self.save_device_map(final_map)
            
            # Print statistics
            logger.info("=" * 60)
            logger.info("SCAN COMPLETE")
            logger.info(f"Total IPs scanned: {self.scan_stats['scanned']}")
            logger.info(f"Devices found online: {self.scan_stats['found']}")
            logger.info(f"Total devices in map: {len(final_map)}")
            logger.info(f"Scan duration: {self.scan_stats['end_time'] - self.scan_stats['start_time']:.2f} seconds")
            
        except Exception as e:
            logger.error(f"Scan failed: {e}")
            raise


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='device_scanner',
        description='ESP32 Network Device Scanner - Discovers ESP32 devices on your network',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Create a new device map (overwrites existing)
    %(prog)s --net 192.168.1.0/24 --action create
    
    # Add new devices to existing map (preserves offline devices)
    %(prog)s --net 192.168.1.0/24 --action add
    
    # Update existing devices only (ignores new devices)
    %(prog)s --net 192.168.1.0/24 --action update
    
    # Scan with custom timeout and concurrency
    %(prog)s --net 192.168.1.0/24 --action create --timeout 5 --concurrent 100
    
    # Use custom device map file
    %(prog)s --net 192.168.1.0/24 --action add --map-file production_devices.json

Action Modes:
    create  - Creates a new device map, replacing any existing one
    add     - Adds new devices and updates existing ones (by MAC address)
    update  - Only updates devices already in the map, ignores new devices
"""
    )
    
    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--net', '--network',
                         dest='network_range',
                         required=True,
                         metavar='CIDR',
                         help='Network range in CIDR format (e.g., 192.168.1.0/24)')
    
    required.add_argument('--action', '--mode',
                         dest='mode',
                         required=True,
                         choices=['create', 'add', 'update'],
                         help='Scan action: create (new map), add (merge), or update (existing only)')
    
    # Optional arguments
    optional = parser.add_argument_group('optional arguments')
    optional.add_argument('--timeout', 
                         type=int, 
                         default=2,
                         metavar='SEC',
                         help='Connection timeout in seconds (default: 2)')
    
    optional.add_argument('--concurrent', 
                         type=int, 
                         default=50,
                         metavar='NUM',
                         help='Maximum concurrent connections (default: 50)')
    
    optional.add_argument('--map-file', 
                         default='device_map.json',
                         metavar='PATH',
                         help='Path to device map JSON file (default: device_map.json)')
    
    optional.add_argument('--verbose', '-v',
                         action='store_true',
                         help='Enable verbose debug logging')
    
    args = parser.parse_args()
    
    # Configure logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Validate network range
    try:
        ipaddress.ip_network(args.network_range, strict=False)
    except ValueError as e:
        logger.error(f"Invalid network range: {e}")
        sys.exit(1)
    
    # Create and run scanner
    scanner = DeviceScanner(
        network_range=args.network_range,
        mode=args.mode,
        timeout=args.timeout,
        concurrent_limit=args.concurrent,
        map_file=args.map_file
    )
    
    # Run the async scanner
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        logger.info("\nScan interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
