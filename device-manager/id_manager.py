#!/usr/bin/env python3
"""
ESP32 Device ID Manager
Manages device IDs across the network, handles duplicates, and provides device identification.

Usage:
    python id_manager.py --command <command> [options]
    
Commands:
    find-duplicates  - Find all devices with duplicate IDs
    set-id          - Set device ID based on MAC address
    identify        - Start identify mode on a device (loops a sound)
    list-all        - List all devices with their IDs and MAC addresses
    auto-assign     - Automatically assign unique IDs to all devices
    provision-single - Scan network and set ID if exactly one device is found
    
Examples:
    python id_manager.py --command find-duplicates
    python id_manager.py --command set-id --mac 34:5F:45:26:76:2C --new-id STAGE-01
    python id_manager.py --command identify --id LOUDFRAME-001
    python id_manager.py --command identify --mac 34:5F:45:26:76:2C
    python id_manager.py --command list-all
    python id_manager.py --command auto-assign --prefix LOUD
    python id_manager.py --command provision-single --network 192.168.1.0/24 --new-id TEST-01
"""

import asyncio
import aiohttp
import argparse
import json
import sys
import ipaddress
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from collections import defaultdict
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class IDManager:
    """Manager for ESP32 device IDs and identification."""
    
    def __init__(self, map_file: str = "device_map.json", timeout: int = 2):
        """
        Initialize the ID manager.
        
        Args:
            map_file: Path to device map JSON file
            timeout: Request timeout in seconds
        """
        self.map_file = Path(map_file)
        self.timeout = timeout
        self.devices = []
        self.device_by_mac = {}
        self.device_by_id = defaultdict(list)
        
    def load_device_map(self) -> bool:
        """
        Load device map from JSON file.
        
        Returns:
            True if successful, False otherwise
        """
        if not self.map_file.exists():
            logger.error(f"Device map file not found: {self.map_file}")
            logger.error("Please run device_scanner.py first to create a device map")
            return False
            
        try:
            with open(self.map_file, 'r') as f:
                data = json.load(f)
                
            if 'devices' in data:
                self.devices = data['devices']
            elif isinstance(data, list):
                self.devices = data
            else:
                logger.error("Invalid device map format")
                return False
            
            # Build lookup tables
            self.device_by_mac = {}
            self.device_by_id = defaultdict(list)
            
            for device in self.devices:
                mac = device.get('mac_address')
                dev_id = device.get('id')
                
                if mac:
                    self.device_by_mac[mac] = device
                if dev_id:
                    self.device_by_id[dev_id].append(device)
            
            logger.info(f"Loaded {len(self.devices)} devices from device map")
            return True
            
        except Exception as e:
            logger.error(f"Error loading device map: {e}")
            return False
    
    def find_duplicates(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Find all devices with duplicate IDs.
        
        Returns:
            Dictionary mapping duplicate IDs to lists of devices
        """
        duplicates = {}
        
        for dev_id, devices in self.device_by_id.items():
            if len(devices) > 1:
                duplicates[dev_id] = devices
                
        return duplicates
    
    def show_duplicates(self) -> None:
        """Display all devices with duplicate IDs."""
        duplicates = self.find_duplicates()
        
        if not duplicates:
            print("\n✓ No duplicate IDs found!")
            print(f"All {len(self.devices)} devices have unique IDs.")
            return
        
        print("\n⚠ DUPLICATE IDs FOUND:")
        print("=" * 70)
        
        total_duplicates = 0
        for dev_id, devices in duplicates.items():
            total_duplicates += len(devices)
            print(f"\nID: {dev_id} ({len(devices)} devices)")
            print("-" * 50)
            
            for i, device in enumerate(devices, 1):
                status = "ONLINE" if device.get('online') else "OFFLINE"
                print(f"  {i}. MAC: {device.get('mac_address', 'UNKNOWN'):<20} "
                      f"IP: {device.get('ip_address', 'UNKNOWN'):<15} "
                      f"Status: {status}")
                
                if device.get('last_seen'):
                    print(f"     Last seen: {device['last_seen']}")
        
        print("\n" + "=" * 70)
        print(f"Summary: {total_duplicates} devices with {len(duplicates)} duplicate ID(s)")
        print("\nRecommendation: Use 'set-id' command to assign unique IDs based on MAC addresses")
        print("Example: python id_manager.py --command set-id --mac <MAC_ADDRESS> --new-id <UNIQUE_ID>")
    
    def list_all_devices(self) -> None:
        """List all devices with their IDs and MAC addresses."""
        if not self.devices:
            print("\nNo devices found in device map.")
            return
        
        # Sort devices by ID for better readability
        sorted_devices = sorted(self.devices, key=lambda d: d.get('id', 'UNKNOWN'))
        
        print("\nDEVICE LIST")
        print("=" * 80)
        print(f"{'ID':<20} {'MAC Address':<20} {'IP Address':<15} {'Status':<10} {'Firmware'}")
        print("-" * 80)
        
        online_count = 0
        for device in sorted_devices:
            dev_id = device.get('id', 'UNKNOWN')
            mac = device.get('mac_address', 'UNKNOWN')
            ip = device.get('ip_address', 'UNKNOWN')
            status = "ONLINE" if device.get('online') else "OFFLINE"
            firmware = device.get('firmware_version', 'N/A')
            
            if device.get('online'):
                online_count += 1
            
            # Highlight duplicates
            duplicate_marker = " ⚠" if len(self.device_by_id.get(dev_id, [])) > 1 else ""
            
            print(f"{dev_id:<20} {mac:<20} {ip:<15} {status:<10} {firmware}{duplicate_marker}")
        
        print("-" * 80)
        print(f"Total: {len(self.devices)} devices ({online_count} online, {len(self.devices) - online_count} offline)")
        
        # Check for duplicates
        duplicates = self.find_duplicates()
        if duplicates:
            print(f"\n⚠ Warning: {len(duplicates)} ID(s) have duplicates (marked with ⚠)")
    
    async def set_device_id(self, mac_address: str, new_id: str) -> bool:
        """
        Set device ID based on MAC address.
        
        Args:
            mac_address: MAC address of the device
            new_id: New ID to set
            
        Returns:
            True if successful, False otherwise
        """
        # Find device by MAC address
        device = self.device_by_mac.get(mac_address.upper())
        
        if not device:
            logger.error(f"Device with MAC address {mac_address} not found in device map")
            logger.info("Available MAC addresses:")
            for mac in sorted(self.device_by_mac.keys()):
                logger.info(f"  - {mac}")
            return False
        
        if not device.get('online'):
            logger.warning(f"Device {mac_address} is marked as offline")
            logger.warning("The operation may fail if the device is not reachable")
        
        ip = device['ip_address']
        old_id = device.get('id', 'UNKNOWN')
        
        logger.info(f"Setting ID for device at {ip} (MAC: {mac_address})")
        logger.info(f"Current ID: {old_id} → New ID: {new_id}")
        
        # Check if new ID would create a duplicate
        if new_id in self.device_by_id and len(self.device_by_id[new_id]) > 0:
            existing_macs = [d.get('mac_address') for d in self.device_by_id[new_id]]
            if mac_address.upper() not in existing_macs:
                logger.warning(f"⚠ Warning: ID '{new_id}' is already used by {len(existing_macs)} device(s)")
                logger.warning(f"  MACs: {', '.join(existing_macs)}")
                logger.warning("This will create duplicate IDs!")
        
        # Send request to change ID
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://{ip}/api/id"
                data = {'id': new_id}
                
                async with session.post(url, json=data, 
                                       timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    if response.status == 200:
                        logger.info(f"✓ Successfully changed ID from '{old_id}' to '{new_id}'")
                        logger.info("Note: Run device_scanner.py to update the device map")
                        return True
                    else:
                        logger.error(f"Failed to set ID: HTTP {response.status}")
                        return False
                        
        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to device at {ip}")
            return False
        except Exception as e:
            logger.error(f"Error setting device ID: {e}")
            return False
    
    async def identify_device(self, device_id: Optional[str] = None, 
                            mac_address: Optional[str] = None,
                            duration: int = 30) -> bool:
        """
        Start identify mode on a device (loops a sound).
        
        Args:
            device_id: Device ID to identify
            mac_address: MAC address to identify
            duration: How long to play the identify sound (seconds)
            
        Returns:
            True if successful, False otherwise
        """
        # Find device
        device = None
        
        if mac_address:
            device = self.device_by_mac.get(mac_address.upper())
            if not device:
                logger.error(f"Device with MAC address {mac_address} not found")
                return False
                
        elif device_id:
            devices = self.device_by_id.get(device_id, [])
            if not devices:
                logger.error(f"Device with ID {device_id} not found")
                return False
            elif len(devices) > 1:
                logger.error(f"Multiple devices found with ID {device_id}:")
                for d in devices:
                    logger.error(f"  - MAC: {d.get('mac_address')} IP: {d.get('ip_address')}")
                logger.error("Please use --mac to specify which device")
                return False
            else:
                device = devices[0]
        else:
            logger.error("Either --id or --mac must be specified")
            return False
        
        if not device.get('online'):
            logger.warning(f"Device is marked as offline")
            logger.warning("The operation may fail if the device is not reachable")
        
        ip = device['ip_address']
        dev_id = device.get('id', 'UNKNOWN')
        mac = device.get('mac_address', 'UNKNOWN')
        
        logger.info(f"Starting identify mode on device:")
        logger.info(f"  ID: {dev_id}")
        logger.info(f"  MAC: {mac}")
        logger.info(f"  IP: {ip}")
        logger.info(f"  Duration: {duration} seconds")
        
        try:
            async with aiohttp.ClientSession() as session:
                # First, stop any playing loops
                logger.info("Stopping current loops...")
                for track in range(3):
                    url = f"http://{ip}/api/loop/stop"
                    data = {'track': track}
                    await session.post(url, json=data, 
                                      timeout=aiohttp.ClientTimeout(total=self.timeout))
                
                # Start identify sound on track 0
                # Using file index 0 as a default identify sound
                # You may want to adjust this based on available files
                logger.info("Starting identify sound loop...")
                url = f"http://{ip}/api/loop/start"
                data = {
                    'track': 0,
                    'file_index': 0  # Use first available file as identify sound
                }
                
                async with session.post(url, json=data,
                                       timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    if response.status == 200:
                        logger.info(f"✓ Identify mode started on {dev_id} (MAC: {mac})")
                        logger.info(f"  The device will play a sound loop for identification")
                        logger.info(f"  Sound will play for {duration} seconds...")
                        
                        # Wait for the specified duration
                        await asyncio.sleep(duration)
                        
                        # Stop the identify sound
                        logger.info("Stopping identify sound...")
                        url = f"http://{ip}/api/loop/stop"
                        data = {'track': 0}
                        await session.post(url, json=data,
                                         timeout=aiohttp.ClientTimeout(total=self.timeout))
                        
                        logger.info("✓ Identify mode completed")
                        return True
                    else:
                        logger.error(f"Failed to start identify mode: HTTP {response.status}")
                        return False
                        
        except asyncio.TimeoutError:
            logger.error(f"Timeout connecting to device at {ip}")
            return False
        except Exception as e:
            logger.error(f"Error in identify mode: {e}")
            return False
    
    async def auto_assign_ids(self, prefix: str = "LOUD", start_num: int = 1) -> None:
        """
        Automatically assign unique IDs to all devices based on MAC addresses.
        
        Args:
            prefix: Prefix for generated IDs
            start_num: Starting number for ID generation
        """
        logger.info(f"Starting auto-assignment of IDs with prefix '{prefix}'")
        
        # Sort devices by MAC for consistent ordering
        sorted_devices = sorted(self.devices, key=lambda d: d.get('mac_address', ''))
        
        assignments = []
        current_num = start_num
        
        for device in sorted_devices:
            if not device.get('online'):
                logger.info(f"Skipping offline device: MAC {device.get('mac_address')}")
                continue
                
            mac = device.get('mac_address')
            old_id = device.get('id', 'UNKNOWN')
            
            # Generate new ID
            new_id = f"{prefix}-{current_num:03d}"
            
            # Check if ID needs to be changed
            if old_id == new_id:
                logger.info(f"Device {mac} already has ID {new_id}, skipping")
            else:
                assignments.append((mac, old_id, new_id))
                current_num += 1
        
        if not assignments:
            logger.info("No ID changes needed!")
            return
        
        # Show planned assignments
        print("\nPlanned ID Assignments:")
        print("-" * 60)
        for mac, old_id, new_id in assignments:
            print(f"  {mac}: {old_id} → {new_id}")
        
        print(f"\nTotal: {len(assignments)} devices will be updated")
        
        # Confirm with user
        response = input("\nProceed with auto-assignment? (y/n): ")
        if response.lower() != 'y':
            logger.info("Auto-assignment cancelled")
            return
        
        # Execute assignments
        success_count = 0
        for mac, old_id, new_id in assignments:
            logger.info(f"\nAssigning {new_id} to {mac}...")
            if await self.set_device_id(mac, new_id):
                success_count += 1
            else:
                logger.error(f"Failed to assign {new_id} to {mac}")
        
        logger.info(f"\n{'='*60}")
        logger.info(f"Auto-assignment complete: {success_count}/{len(assignments)} successful")
        
        if success_count > 0:
            logger.info("Run device_scanner.py to update the device map with new IDs")
    
    async def scan_single_device(self, session: aiohttp.ClientSession, ip: str) -> Optional[Dict[str, Any]]:
        """
        Scan a single IP address for an ESP32 device.
        
        Args:
            session: aiohttp session
            ip: IP address to scan
            
        Returns:
            Device information if found, None otherwise
        """
        url = f"http://{ip}/api/status"
        
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                if response.status == 200:
                    data = await response.json()
                    
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
                    
                    return device_info
                    
        except asyncio.TimeoutError:
            # Timeout is expected for most IPs, don't log
            pass
        except aiohttp.ClientError:
            # Connection errors are expected for non-device IPs
            pass
        except Exception as e:
            logger.debug(f"Unexpected error scanning {ip}: {e}")
            
        return None
    
    async def provision_single_device(self, network_range: str, new_id: str, 
                                     concurrent_limit: int = 50) -> bool:
        """
        Scan network and set ID if exactly one device is found.
        Perfect for provisioning a single device on the network.
        
        Args:
            network_range: Network range in CIDR format (e.g., 192.168.1.0/24)
            new_id: New ID to set on the device
            concurrent_limit: Maximum concurrent connections
            
        Returns:
            True if successful, False otherwise
        """
        try:
            network = ipaddress.ip_network(network_range, strict=False)
        except ValueError as e:
            logger.error(f"Invalid network range: {e}")
            return False
        
        logger.info(f"Starting network scan of {network}")
        logger.info(f"Mode: PROVISION-SINGLE, Timeout: {self.timeout}s, "
                   f"Concurrent limit: {concurrent_limit}")
        logger.info("This is designed for provisioning a single device on the network")
        
        # Generate all IP addresses in the network
        all_ips = [str(ip) for ip in network.hosts()]
        total_ips = len(all_ips)
        
        # Initialize scan statistics
        scan_stats = {
            'total_ips': total_ips,
            'scanned': 0,
            'found': 0,
            'start_time': datetime.now()
        }
        
        logger.info(f"Scanning {total_ips} IP addresses...")
        
        # Scan the network
        found_devices = []
        connector = aiohttp.TCPConnector(limit=concurrent_limit, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Process IPs in batches
            batch_size = concurrent_limit
            for i in range(0, len(all_ips), batch_size):
                batch = all_ips[i:i + batch_size]
                tasks = [self.scan_single_device(session, ip) for ip in batch]
                results = await asyncio.gather(*tasks)
                
                # Update progress and collect found devices
                scan_stats['scanned'] += len(batch)
                for device in results:
                    if device:
                        found_devices.append(device)
                        scan_stats['found'] += 1
                        logger.info(f"✓ Found device at {device['ip_address']}: "
                                  f"ID={device['id']}, MAC={device['mac_address']}")
                
                # Report progress
                progress = (scan_stats['scanned'] / scan_stats['total_ips']) * 100
                logger.info(f"Progress: {scan_stats['scanned']}/{scan_stats['total_ips']} "
                          f"({progress:.1f}%) - Found: {scan_stats['found']} devices")
        
        scan_duration = (datetime.now() - scan_stats['start_time']).total_seconds()
        
        logger.info(f"Scan completed in {scan_duration:.2f} seconds")
        logger.info(f"Total IPs scanned: {scan_stats['scanned']}")
        logger.info(f"Devices found: {scan_stats['found']}")
        logger.info("=" * 60)
        
        # Check results
        if len(found_devices) == 0:
            logger.error("❌ No devices found on the network")
            logger.error(f"Please ensure a device is connected to network {network}")
            return False
            
        elif len(found_devices) == 1:
            # Perfect - exactly one device found
            device = found_devices[0]
            ip = device['ip_address']
            mac = device['mac_address']
            old_id = device['id']
            
            logger.info(f"✓ Found exactly one device!")
            logger.info(f"  IP: {ip}")
            logger.info(f"  MAC: {mac}")
            logger.info(f"  Current ID: {old_id}")
            logger.info(f"  New ID: {new_id}")
            logger.info("-" * 60)
            
            # Set the new ID
            try:
                logger.info(f"Setting device ID to '{new_id}'...")
                async with aiohttp.ClientSession() as session:
                    url = f"http://{ip}/api/id"
                    data = {'id': new_id}
                    
                    async with session.post(url, json=data, 
                                           timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                        if response.status == 200:
                            logger.info("=" * 60)
                            logger.info(f"✅ PROVISIONING SUCCESSFUL!")
                            logger.info(f"   Device at {ip} (MAC: {mac})")
                            logger.info(f"   ID changed from '{old_id}' to '{new_id}'")
                            logger.info("=" * 60)
                            return True
                        else:
                            logger.error(f"Failed to set ID: HTTP {response.status}")
                            return False
                            
            except asyncio.TimeoutError:
                logger.error(f"Timeout connecting to device at {ip}")
                return False
            except Exception as e:
                logger.error(f"Error setting device ID: {e}")
                return False
                
        else:
            # Multiple devices found
            logger.error(f"❌ Found {len(found_devices)} devices on the network!")
            logger.error("This command is designed for provisioning a single device.")
            logger.error("-" * 60)
            logger.error("Found devices:")
            for i, device in enumerate(found_devices, 1):
                logger.error(f"  {i}. IP: {device['ip_address']:<15} "
                           f"ID: {device['id']:<20} "
                           f"MAC: {device['mac_address']}")
            logger.error("-" * 60)
            logger.error("Please ensure only ONE device is on the network, or use the")
            logger.error("'set-id' command with --mac to target a specific device.")
            return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='id_manager',
        description='ESP32 Device ID Manager - Manage device IDs and identification',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Find all devices with duplicate IDs
    %(prog)s --command find-duplicates
    
    # Set device ID based on MAC address
    %(prog)s --command set-id --mac 34:5F:45:26:76:2C --new-id STAGE-01
    
    # Identify a device by ID (will play a sound)
    %(prog)s --command identify --id LOUDFRAME-001
    
    # Identify a device by MAC address
    %(prog)s --command identify --mac 34:5F:45:26:76:2C
    
    # Identify with custom duration
    %(prog)s --command identify --id LOUDFRAME-001 --duration 60
    
    # List all devices
    %(prog)s --command list-all
    
    # Auto-assign unique IDs to all devices
    %(prog)s --command auto-assign --prefix LOUD
    %(prog)s --command auto-assign --prefix STAGE --start-num 100
    
    # Provision a single device on the network
    %(prog)s --command provision-single --network 192.168.1.0/24 --new-id TEST-01

Commands:
    find-duplicates  - Find all devices with duplicate IDs
    set-id          - Set device ID based on MAC address
    identify        - Start identify mode on a device (loops a sound)
    list-all        - List all devices with their IDs and MAC addresses
    auto-assign     - Automatically assign unique IDs to all devices
    provision-single - Scan network and set ID if exactly one device is found
"""
    )
    
    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--command', '-c',
                         required=True,
                         choices=['find-duplicates', 'set-id', 'identify', 'list-all', 'auto-assign', 'provision-single'],
                         help='Command to execute')
    
    # Optional arguments
    optional = parser.add_argument_group('optional arguments')
    optional.add_argument('--map-file', '-f',
                         default='device_map.json',
                         metavar='PATH',
                         help='Path to device map JSON file (default: device_map.json)')
    
    optional.add_argument('--timeout', '-t',
                         type=int,
                         default=2,
                         metavar='SEC',
                         help='Request timeout in seconds (default: 2)')
    
    # Device identification
    id_group = parser.add_argument_group('device identification')
    id_group.add_argument('--id', '-i',
                         dest='device_id',
                         metavar='ID',
                         help='Device ID')
    
    id_group.add_argument('--mac', '-m',
                         dest='mac_address',
                         metavar='MAC',
                         help='Device MAC address (e.g., 34:5F:45:26:76:2C)')
    
    # ID management
    mgmt_group = parser.add_argument_group('ID management')
    mgmt_group.add_argument('--new-id', '-n',
                           metavar='ID',
                           help='New device ID for set-id command')
    
    # Identify options
    identify_group = parser.add_argument_group('identify options')
    identify_group.add_argument('--duration', '-d',
                               type=int,
                               default=30,
                               metavar='SEC',
                               help='Duration of identify sound in seconds (default: 30)')
    
    # Auto-assign options
    auto_group = parser.add_argument_group('auto-assign options')
    auto_group.add_argument('--prefix', '-p',
                           default='LOUD',
                           metavar='PREFIX',
                           help='Prefix for auto-generated IDs (default: LOUD)')
    
    auto_group.add_argument('--start-num', '-s',
                           type=int,
                           default=1,
                           metavar='NUM',
                           help='Starting number for auto-generated IDs (default: 1)')
    
    # Provision single device options
    provision_group = parser.add_argument_group('provision-single options')
    provision_group.add_argument('--network',
                                metavar='CIDR',
                                help='Network range in CIDR format (e.g., 192.168.1.0/24)')
    
    args = parser.parse_args()
    
    # Create ID manager
    manager = IDManager(map_file=args.map_file, timeout=args.timeout)
    
    # Load device map only for commands that need it
    # provision-single doesn't need the device map
    if args.command != 'provision-single':
        if not manager.load_device_map():
            sys.exit(1)
    
    # Execute command
    try:
        if args.command == 'find-duplicates':
            manager.show_duplicates()
            
        elif args.command == 'list-all':
            manager.list_all_devices()
            
        elif args.command == 'set-id':
            if not args.mac_address:
                logger.error("MAC address required (use --mac)")
                sys.exit(1)
            if not args.new_id:
                logger.error("New ID required (use --new-id)")
                sys.exit(1)
                
            asyncio.run(manager.set_device_id(args.mac_address, args.new_id))
            
        elif args.command == 'identify':
            asyncio.run(manager.identify_device(
                device_id=args.device_id,
                mac_address=args.mac_address,
                duration=args.duration
            ))
            
        elif args.command == 'auto-assign':
            asyncio.run(manager.auto_assign_ids(
                prefix=args.prefix,
                start_num=args.start_num
            ))
            
        elif args.command == 'provision-single':
            if not args.network:
                logger.error("Network range required (use --network)")
                sys.exit(1)
            if not args.new_id:
                logger.error("New ID required (use --new-id)")
                sys.exit(1)
            
            asyncio.run(manager.provision_single_device(args.network, args.new_id))
            
    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
