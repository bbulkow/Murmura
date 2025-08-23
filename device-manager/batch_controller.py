#!/usr/bin/env python3
"""
ESP32 Batch Controller
Performs batch operations on multiple ESP32 devices from a device map.

Usage:
    python batch_controller.py --command <command> [options]
    
Commands:
    status      - Show status of all devices in the map
    stop-all    - Stop all loops on all devices
    start-all   - Start all loops on all devices
    set-volume  - Set volume on all devices
    save-config - Save configuration on all devices
    load-config - Load configuration on all devices
    
Examples:
    python batch_controller.py --command status
    python batch_controller.py --command stop-all
    python batch_controller.py --command set-volume --track 0 --volume 50
    python batch_controller.py --command set-volume --global --volume 75
"""

import asyncio
import aiohttp
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class BatchController:
    """Controller for batch operations on multiple ESP32 devices."""
    
    def __init__(self, map_file: str = "device_map.json", timeout: int = 5, concurrent_limit: int = 10):
        """
        Initialize the controller.
        
        Args:
            map_file: Path to device map JSON file
            timeout: Request timeout in seconds
            concurrent_limit: Maximum concurrent connections
        """
        self.map_file = Path(map_file)
        self.timeout = timeout
        self.concurrent_limit = concurrent_limit
        self.devices = []
        self.results = []
        
    def load_device_map(self) -> List[Dict[str, Any]]:
        """Load device map from JSON file."""
        if not self.map_file.exists():
            logger.error(f"Device map file not found: {self.map_file}")
            logger.error("Please run device_scanner.py first to create a device map")
            sys.exit(1)
            
        try:
            with open(self.map_file, 'r') as f:
                data = json.load(f)
                
            if 'devices' in data:
                self.devices = data['devices']
            elif isinstance(data, list):
                self.devices = data
            else:
                logger.error("Invalid device map format")
                sys.exit(1)
                
            # Filter only online devices by default
            online_devices = [d for d in self.devices if d.get('online', False)]
            logger.info(f"Loaded {len(online_devices)} online devices (out of {len(self.devices)} total)")
            
            return online_devices
            
        except Exception as e:
            logger.error(f"Error loading device map: {e}")
            sys.exit(1)
    
    async def send_request(self, session: aiohttp.ClientSession, device: Dict[str, Any], 
                          method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Send HTTP request to a device.
        
        Args:
            session: aiohttp session
            device: Device information
            method: HTTP method (GET, POST, DELETE)
            endpoint: API endpoint path
            data: JSON data for POST requests
            
        Returns:
            Response data with success status
        """
        ip = device['ip_address']
        device_id = device.get('id', 'UNKNOWN')
        url = f"http://{ip}{endpoint}"
        
        result = {
            'device_id': device_id,
            'ip_address': ip,
            'mac_address': device.get('mac_address', 'UNKNOWN'),
            'success': False,
            'response': None,
            'error': None
        }
        
        try:
            kwargs = {'timeout': aiohttp.ClientTimeout(total=self.timeout)}
            if data:
                kwargs['json'] = data
                
            async with session.request(method, url, **kwargs) as response:
                result['response'] = await response.json()
                result['success'] = response.status == 200
                
                if result['success']:
                    logger.info(f"✓ {device_id} ({ip}): Success")
                else:
                    logger.warning(f"✗ {device_id} ({ip}): HTTP {response.status}")
                    
        except asyncio.TimeoutError:
            result['error'] = 'Timeout'
            logger.error(f"✗ {device_id} ({ip}): Timeout")
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"✗ {device_id} ({ip}): {e}")
            
        return result
    
    async def batch_request(self, devices: List[Dict[str, Any]], method: str, 
                           endpoint: str, data: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Send requests to multiple devices concurrently.
        
        Args:
            devices: List of devices
            method: HTTP method
            endpoint: API endpoint
            data: JSON data for requests
            
        Returns:
            List of results
        """
        connector = aiohttp.TCPConnector(limit=self.concurrent_limit, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [self.send_request(session, device, method, endpoint, data) for device in devices]
            results = await asyncio.gather(*tasks)
            
        return results
    
    async def show_status(self, devices: List[Dict[str, Any]]) -> None:
        """Show status of all devices."""
        logger.info("=" * 70)
        logger.info("DEVICE STATUS")
        logger.info("=" * 70)
        
        # Get current status from all devices
        results = await self.batch_request(devices, 'GET', '/api/status')
        
        # Sort by IP for consistent display
        results.sort(key=lambda r: r['ip_address'])
        
        # Display results in a table format
        print(f"\n{'ID':<20} {'IP Address':<15} {'MAC Address':<17} {'Status':<10} {'Uptime':<15}")
        print("-" * 80)
        
        for result in results:
            device_id = result['device_id']
            ip = result['ip_address']
            mac = result['mac_address']
            
            if result['success'] and result['response']:
                status = "Online"
                uptime = result['response'].get('uptime_formatted', 'N/A')
            else:
                status = "Offline"
                uptime = "N/A"
                
            print(f"{device_id:<20} {ip:<15} {mac:<17} {status:<10} {uptime:<15}")
        
        # Summary
        online_count = sum(1 for r in results if r['success'])
        print(f"\nSummary: {online_count}/{len(results)} devices online")
    
    async def stop_all_loops(self, devices: List[Dict[str, Any]]) -> None:
        """Stop all loops on all devices."""
        logger.info("Stopping all loops on all devices...")
        
        # Stop all 3 tracks on each device
        for track in range(3):
            logger.info(f"Stopping track {track}...")
            results = await self.batch_request(devices, 'POST', '/api/loop/stop', {'track': track})
            
            success_count = sum(1 for r in results if r['success'])
            logger.info(f"Track {track}: {success_count}/{len(results)} devices stopped successfully")
        
        logger.info("All stop commands sent")
    
    async def start_all_loops(self, devices: List[Dict[str, Any]]) -> None:
        """Start all loops on all devices (using their configured files)."""
        logger.info("Starting all loops on all devices...")
        
        # First get the current configuration of each device
        status_results = await self.batch_request(devices, 'GET', '/api/loops')
        
        # Process each device
        for i, device in enumerate(devices):
            if status_results[i]['success'] and status_results[i]['response']:
                loops = status_results[i]['response'].get('loops', [])
                
                for loop in loops:
                    if loop.get('file'):  # Only start if a file is configured
                        track = loop['track']
                        logger.info(f"Starting track {track} on {device['id']}...")
                        await self.batch_request([device], 'POST', '/api/loop/start', {'track': track})
        
        logger.info("All start commands sent")
    
    async def set_volume(self, devices: List[Dict[str, Any]], track: Optional[int], 
                        volume: int, global_vol: bool = False) -> None:
        """
        Set volume on all devices.
        
        Args:
            devices: List of devices
            track: Track number (0-2) or None for global
            volume: Volume level (0-100)
            global_vol: Whether to set global volume
        """
        if global_vol:
            logger.info(f"Setting global volume to {volume}% on all devices...")
            results = await self.batch_request(devices, 'POST', '/api/global/volume', {'volume': volume})
        else:
            if track is None:
                logger.error("Track number required for track volume (use --track)")
                return
                
            logger.info(f"Setting track {track} volume to {volume}% on all devices...")
            results = await self.batch_request(devices, 'POST', '/api/loop/volume', 
                                              {'track': track, 'volume': volume})
        
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Volume set on {success_count}/{len(results)} devices")
    
    async def save_configs(self, devices: List[Dict[str, Any]]) -> None:
        """Save configuration on all devices."""
        logger.info("Saving configuration on all devices...")
        
        results = await self.batch_request(devices, 'POST', '/api/config/save')
        
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Configuration saved on {success_count}/{len(results)} devices")
    
    async def load_configs(self, devices: List[Dict[str, Any]]) -> None:
        """Load saved configuration on all devices."""
        logger.info("Loading saved configuration on all devices...")
        
        results = await self.batch_request(devices, 'POST', '/api/config/load')
        
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Configuration loaded on {success_count}/{len(results)} devices")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='batch_controller',
        description='ESP32 Batch Controller - Batch operations on multiple ESP32 devices',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Show status of all devices
    %(prog)s --command status
    
    # Stop all loops on all devices
    %(prog)s --command stop-all
    
    # Start all configured loops
    %(prog)s --command start-all
    
    # Set volume on all devices
    %(prog)s --command set-volume --track 0 --volume 50
    %(prog)s --command set-volume --global --volume 75
    
    # Save/load configuration
    %(prog)s --command save-config
    %(prog)s --command load-config
    
    # Filter devices
    %(prog)s --command status --filter-id "^STAGE"
    %(prog)s --command stop-all --all-devices

Commands:
    status       - Show status of all devices in the map
    stop-all     - Stop all loops on all devices
    start-all    - Start all configured loops
    set-volume   - Set volume on all devices
    save-config  - Save current configuration on all devices
    load-config  - Load saved configuration on all devices
"""
    )
    
    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--command', '-c',
                         required=True,
                         choices=['status', 'stop-all', 'start-all', 'set-volume', 
                                 'save-config', 'load-config'],
                         help='Command to execute on all devices')
    
    # Optional arguments
    optional = parser.add_argument_group('optional arguments')
    optional.add_argument('--map-file', 
                         default='device_map.json',
                         metavar='PATH',
                         help='Path to device map JSON file (default: device_map.json)')
    
    optional.add_argument('--timeout', 
                         type=int, 
                         default=5,
                         metavar='SEC',
                         help='Request timeout in seconds (default: 5)')
    
    optional.add_argument('--concurrent', 
                         type=int, 
                         default=10,
                         metavar='NUM',
                         help='Maximum concurrent connections (default: 10)')
    
    # Volume-specific arguments
    volume_group = parser.add_argument_group('volume control')
    volume_group.add_argument('--track', 
                             type=int, 
                             choices=[0, 1, 2],
                             help='Track number for track-specific commands')
    
    volume_group.add_argument('--volume', 
                             type=int, 
                             metavar='LEVEL',
                             help='Volume level (0-100)')
    
    volume_group.add_argument('--global', 
                             dest='global_volume',
                             action='store_true',
                             help='Set global volume instead of track volume')
    
    # Filter arguments
    filter_group = parser.add_argument_group('device filters')
    filter_group.add_argument('--all-devices', 
                             action='store_true',
                             help='Include offline devices (default: online only)')
    
    filter_group.add_argument('--filter-id', 
                             metavar='REGEX',
                             help='Filter devices by ID pattern (regex)')
    
    args = parser.parse_args()
    
    # Create controller
    controller = BatchController(
        map_file=args.map_file,
        timeout=args.timeout,
        concurrent_limit=args.concurrent
    )
    
    # Load devices
    devices = controller.load_device_map()
    
    # Apply filters
    if not args.all_devices:
        devices = [d for d in devices if d.get('online', False)]
        
    if args.filter_id:
        import re
        pattern = re.compile(args.filter_id)
        devices = [d for d in devices if pattern.search(d.get('id', ''))]
        logger.info(f"Filtered to {len(devices)} devices matching pattern: {args.filter_id}")
    
    if not devices:
        logger.error("No devices to control (check filters or run scanner first)")
        sys.exit(1)
    
    # Execute command
    try:
        if args.command == 'status':
            asyncio.run(controller.show_status(devices))
            
        elif args.command == 'stop-all':
            asyncio.run(controller.stop_all_loops(devices))
            
        elif args.command == 'start-all':
            asyncio.run(controller.start_all_loops(devices))
            
        elif args.command == 'set-volume':
            if args.volume is None:
                logger.error("Volume level required (use --volume)")
                sys.exit(1)
            asyncio.run(controller.set_volume(devices, args.track, args.volume, args.global_volume))
            
        elif args.command == 'save-config':
            asyncio.run(controller.save_configs(devices))
            
        elif args.command == 'load-config':
            asyncio.run(controller.load_configs(devices))
            
    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
