#!/usr/bin/env python3
"""
ESP32 Device Controller
Controls a single ESP32 device by its ID.

Usage:
    python device_controller.py --id <device_id> --command <command> [options]
    
Commands:
    status      - Show device status
    stop        - Stop all loops on the device
    start       - Start all loops on the device
    set-volume  - Set volume on the device
    set-id      - Change the device ID
    save-config - Save current configuration
    load-config - Load saved configuration
    get-loops   - Get current loop status
    set-file    - Set file for a track
    
Examples:
    python device_controller.py --id LOUDFRAME-001 --command status
    python device_controller.py --id LOUDFRAME-001 --command stop
    python device_controller.py --id LOUDFRAME-001 --command set-volume --track 0 --volume 50
    python device_controller.py --id LOUDFRAME-001 --command set-id --new-id STAGE-01
"""

import asyncio
import aiohttp
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Any
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class DeviceController:
    """Controller for operations on a single ESP32 device."""
    
    def __init__(self, device_id: str, map_file: str = "device_map.json", timeout: int = 5):
        """
        Initialize the controller.
        
        Args:
            device_id: ID of the device to control
            map_file: Path to device map JSON file
            timeout: Request timeout in seconds
        """
        self.device_id = device_id
        self.map_file = Path(map_file)
        self.timeout = timeout
        self.device = None
        
    def load_device(self) -> Optional[Dict[str, Any]]:
        """Load specific device from the device map."""
        if not self.map_file.exists():
            logger.error(f"Device map file not found: {self.map_file}")
            logger.error("Please run device_scanner.py first to create a device map")
            return None
            
        try:
            with open(self.map_file, 'r') as f:
                data = json.load(f)
                
            devices = []
            if 'devices' in data:
                devices = data['devices']
            elif isinstance(data, list):
                devices = data
            else:
                logger.error("Invalid device map format")
                return None
            
            # Find device by ID
            for device in devices:
                if device.get('id') == self.device_id:
                    self.device = device
                    logger.info(f"Found device: {self.device_id} at {device['ip_address']}")
                    return device
            
            logger.error(f"Device with ID '{self.device_id}' not found in device map")
            logger.info("Available devices:")
            for device in devices:
                logger.info(f"  - {device.get('id', 'UNKNOWN')} ({device.get('ip_address', 'UNKNOWN')})")
            return None
            
        except Exception as e:
            logger.error(f"Error loading device map: {e}")
            return None
    
    async def send_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Send HTTP request to the device.
        
        Args:
            method: HTTP method (GET, POST, DELETE)
            endpoint: API endpoint path
            data: JSON data for POST requests
            
        Returns:
            Response data with success status
        """
        if not self.device:
            return {'success': False, 'error': 'Device not loaded'}
            
        ip = self.device['ip_address']
        url = f"http://{ip}{endpoint}"
        
        result = {
            'success': False,
            'response': None,
            'error': None
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {'timeout': aiohttp.ClientTimeout(total=self.timeout)}
                if data:
                    kwargs['json'] = data
                    
                async with session.request(method, url, **kwargs) as response:
                    result['response'] = await response.json()
                    result['success'] = response.status == 200
                    
                    if not result['success']:
                        logger.warning(f"HTTP {response.status} from {self.device_id}")
                        
        except asyncio.TimeoutError:
            result['error'] = 'Timeout'
            logger.error(f"Timeout connecting to {self.device_id} ({ip})")
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"Error connecting to {self.device_id} ({ip}): {e}")
            
        return result
    
    async def show_status(self) -> None:
        """Show device status."""
        logger.info(f"Getting status for device: {self.device_id}")
        
        result = await self.send_request('GET', '/api/status')
        
        if result['success'] and result['response']:
            data = result['response']
            print(f"\nDevice Status: {self.device_id}")
            print("-" * 40)
            print(f"IP Address:    {data.get('ip_address', self.device['ip_address'])}")
            print(f"MAC Address:   {data.get('mac_address', 'N/A')}")
            print(f"WiFi Status:   {'Connected' if data.get('wifi_connected') else 'Disconnected'}")
            print(f"Firmware:      {data.get('firmware_version', 'N/A')}")
            print(f"Uptime:        {data.get('uptime_formatted', 'N/A')}")
        else:
            logger.error(f"Failed to get status: {result.get('error', 'Unknown error')}")
    
    async def stop_loops(self) -> None:
        """Stop all loops on the device."""
        logger.info(f"Stopping all loops on {self.device_id}")
        
        for track in range(3):
            result = await self.send_request('POST', '/api/loop/stop', {'track': track})
            if result['success']:
                logger.info(f"✓ Track {track} stopped")
            else:
                logger.error(f"✗ Failed to stop track {track}")
    
    async def start_loops(self) -> None:
        """Start all configured loops on the device."""
        logger.info(f"Starting loops on {self.device_id}")
        
        # First get current configuration
        result = await self.send_request('GET', '/api/loops')
        
        if result['success'] and result['response']:
            loops = result['response'].get('loops', [])
            
            for loop in loops:
                if loop.get('file'):  # Only start if a file is configured
                    track = loop['track']
                    start_result = await self.send_request('POST', '/api/loop/start', {'track': track})
                    if start_result['success']:
                        logger.info(f"✓ Track {track} started: {loop['file']}")
                    else:
                        logger.error(f"✗ Failed to start track {track}")
                else:
                    logger.info(f"Track {loop['track']} has no file configured, skipping")
        else:
            logger.error(f"Failed to get loop configuration: {result.get('error', 'Unknown error')}")
    
    async def set_volume(self, track: Optional[int], volume: int, global_vol: bool = False) -> None:
        """
        Set volume on the device.
        
        Args:
            track: Track number (0-2) or None for global
            volume: Volume level (0-100)
            global_vol: Whether to set global volume
        """
        if global_vol:
            logger.info(f"Setting global volume to {volume}% on {self.device_id}")
            result = await self.send_request('POST', '/api/global/volume', {'volume': volume})
        else:
            if track is None:
                logger.error("Track number required for track volume (use --track)")
                return
                
            logger.info(f"Setting track {track} volume to {volume}% on {self.device_id}")
            result = await self.send_request('POST', '/api/loop/volume', 
                                            {'track': track, 'volume': volume})
        
        if result['success']:
            logger.info("✓ Volume set successfully")
        else:
            logger.error(f"✗ Failed to set volume: {result.get('error', 'Unknown error')}")
    
    async def set_device_id(self, new_id: str) -> None:
        """
        Change the device ID.
        
        Args:
            new_id: New device ID to set
        """
        logger.info(f"Changing device ID from {self.device_id} to {new_id}")
        
        result = await self.send_request('POST', '/api/id', {'id': new_id})
        
        if result['success']:
            logger.info(f"✓ Device ID changed successfully to: {new_id}")
            logger.info("Note: You'll need to rescan the network to update the device map")
        else:
            logger.error(f"✗ Failed to set device ID: {result.get('error', 'Unknown error')}")
    
    async def save_config(self) -> None:
        """Save configuration on the device."""
        logger.info(f"Saving configuration on {self.device_id}")
        
        result = await self.send_request('POST', '/api/config/save')
        
        if result['success']:
            logger.info("✓ Configuration saved successfully")
        else:
            logger.error(f"✗ Failed to save configuration: {result.get('error', 'Unknown error')}")
    
    async def load_config(self) -> None:
        """Load saved configuration on the device."""
        logger.info(f"Loading saved configuration on {self.device_id}")
        
        result = await self.send_request('POST', '/api/config/load')
        
        if result['success']:
            logger.info("✓ Configuration loaded successfully")
        else:
            logger.error(f"✗ Failed to load configuration: {result.get('error', 'Unknown error')}")
    
    async def get_loops(self) -> None:
        """Get current loop status."""
        logger.info(f"Getting loop status for {self.device_id}")
        
        result = await self.send_request('GET', '/api/loops')
        
        if result['success'] and result['response']:
            data = result['response']
            print(f"\nLoop Status for {self.device_id}")
            print("-" * 60)
            print(f"Global Volume: {data.get('global_volume', 'N/A')}%")
            print(f"Active Loops:  {data.get('active_count', 0)}/{data.get('max_tracks', 3)}")
            print("\nTracks:")
            
            for loop in data.get('loops', []):
                status = "PLAYING" if loop.get('playing') else "STOPPED"
                file_name = loop.get('file', 'No file').split('/')[-1] if loop.get('file') else 'No file'
                print(f"  Track {loop['track']}: {status:<8} Volume: {loop.get('volume', 0)}% File: {file_name}")
        else:
            logger.error(f"Failed to get loop status: {result.get('error', 'Unknown error')}")
    
    async def set_file(self, track: int, file_index: Optional[int], file_path: Optional[str]) -> None:
        """
        Set file for a track.
        
        Args:
            track: Track number (0-2)
            file_index: File index from /api/files
            file_path: Direct file path
        """
        if file_index is not None:
            logger.info(f"Setting file index {file_index} for track {track} on {self.device_id}")
            data = {'track': track, 'file_index': file_index}
        elif file_path:
            logger.info(f"Setting file {file_path} for track {track} on {self.device_id}")
            data = {'track': track, 'file_path': file_path}
        else:
            logger.error("Either --file-index or --file-path must be specified")
            return
        
        result = await self.send_request('POST', '/api/loop/file', data)
        
        if result['success']:
            logger.info(f"✓ File set and loop started on track {track}")
        else:
            logger.error(f"✗ Failed to set file: {result.get('error', 'Unknown error')}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='device_controller',
        description='ESP32 Device Controller - Control a single ESP32 device by ID',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Show device status
    %(prog)s --id LOUDFRAME-001 --command status
    
    # Stop all loops
    %(prog)s --id LOUDFRAME-001 --command stop
    
    # Start configured loops
    %(prog)s --id LOUDFRAME-001 --command start
    
    # Set volume
    %(prog)s --id LOUDFRAME-001 --command set-volume --track 0 --volume 50
    %(prog)s --id LOUDFRAME-001 --command set-volume --global --volume 75
    
    # Change device ID
    %(prog)s --id LOUDFRAME-001 --command set-id --new-id STAGE-01
    
    # Save/load configuration
    %(prog)s --id LOUDFRAME-001 --command save-config
    %(prog)s --id LOUDFRAME-001 --command load-config
    
    # Get loop status
    %(prog)s --id LOUDFRAME-001 --command get-loops
    
    # Set file for a track
    %(prog)s --id LOUDFRAME-001 --command set-file --track 0 --file-index 2
    %(prog)s --id LOUDFRAME-001 --command set-file --track 1 --file-path /sdcard/music.wav

Commands:
    status       - Show device status
    stop         - Stop all loops on the device
    start        - Start all configured loops
    set-volume   - Set volume on the device
    set-id       - Change the device ID
    save-config  - Save current configuration
    load-config  - Load saved configuration
    get-loops    - Get current loop status
    set-file     - Set file for a track
"""
    )
    
    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--id', 
                         dest='device_id',
                         required=True,
                         metavar='ID',
                         help='Device ID to control')
    
    required.add_argument('--command', '-c',
                         required=True,
                         choices=['status', 'stop', 'start', 'set-volume', 'set-id',
                                 'save-config', 'load-config', 'get-loops', 'set-file'],
                         help='Command to execute on the device')
    
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
    
    # Volume control
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
    
    # Device ID change
    id_group = parser.add_argument_group('device ID control')
    id_group.add_argument('--new-id', 
                         metavar='ID',
                         help='New device ID for set-id command')
    
    # File control
    file_group = parser.add_argument_group('file control')
    file_group.add_argument('--file-index', 
                           type=int,
                           metavar='INDEX',
                           help='File index from /api/files for set-file command')
    
    file_group.add_argument('--file-path', 
                           metavar='PATH',
                           help='Direct file path for set-file command')
    
    args = parser.parse_args()
    
    # Create controller
    controller = DeviceController(
        device_id=args.device_id,
        map_file=args.map_file,
        timeout=args.timeout
    )
    
    # Load device
    device = controller.load_device()
    if not device:
        sys.exit(1)
    
    # Check if device is online
    if not device.get('online', False):
        logger.warning(f"Device {args.device_id} is marked as offline in the device map")
        logger.warning("Commands may fail if the device is not reachable")
    
    # Execute command
    try:
        if args.command == 'status':
            asyncio.run(controller.show_status())
            
        elif args.command == 'stop':
            asyncio.run(controller.stop_loops())
            
        elif args.command == 'start':
            asyncio.run(controller.start_loops())
            
        elif args.command == 'set-volume':
            if args.volume is None:
                logger.error("Volume level required (use --volume)")
                sys.exit(1)
            asyncio.run(controller.set_volume(args.track, args.volume, args.global_volume))
            
        elif args.command == 'set-id':
            if not args.new_id:
                logger.error("New ID required (use --new-id)")
                sys.exit(1)
            asyncio.run(controller.set_device_id(args.new_id))
            
        elif args.command == 'save-config':
            asyncio.run(controller.save_config())
            
        elif args.command == 'load-config':
            asyncio.run(controller.load_config())
            
        elif args.command == 'get-loops':
            asyncio.run(controller.get_loops())
            
        elif args.command == 'set-file':
            if args.track is None:
                logger.error("Track number required (use --track)")
                sys.exit(1)
            asyncio.run(controller.set_file(args.track, args.file_index, args.file_path))
            
    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
