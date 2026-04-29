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
    reboot-all  - Reboot all devices
    
Examples:
    python batch_controller.py --command status
    python batch_controller.py --command stop-all
    python batch_controller.py --command set-volume --track 0 --volume 50
    python batch_controller.py --command set-volume --global --volume 75
    python batch_controller.py --command reboot-all
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

    async def _patch_scene(self, session: aiohttp.ClientSession, device: Dict[str, Any],
                           build_patch, scene_name: Optional[str]) -> Dict[str, Any]:
        """
        Patch a named scene on a device. If scene_name is None, resolve the device's
        active scene first (one GET /api/scenes round-trip).

        build_patch(scene_dict) -> patch body dict, or None to skip this device
        (a no-op is treated as success). scene_dict is {} when scene_name was supplied
        by the caller and we didn't GET first.
        """
        ip = device['ip_address']
        device_id = device.get('id', 'UNKNOWN')
        result = {
            'device_id': device_id,
            'ip_address': ip,
            'mac_address': device.get('mac_address', 'UNKNOWN'),
            'success': False,
            'response': None,
            'error': None,
        }

        try:
            scenes_url = f"http://{ip}/api/scenes"
            timeout = aiohttp.ClientTimeout(total=self.timeout)

            target_name = scene_name
            target_scene: Dict[str, Any] = {}

            if target_name is None:
                async with session.get(scenes_url, timeout=timeout) as r:
                    if r.status != 200:
                        result['error'] = f"GET /api/scenes HTTP {r.status}"
                        logger.warning(f"✗ {device_id} ({ip}): {result['error']}")
                        return result
                    scenes_data = await r.json()
                target_name = scenes_data.get('active_scene') or 'default'
                target_scene = (scenes_data.get('scenes') or {}).get(target_name, {})

            patch_body = build_patch(target_scene)
            if patch_body is None:
                result['success'] = True
                result['response'] = {'message': 'no-op (nothing to patch)'}
                logger.info(f"- {device_id} ({ip}): no-op")
                return result

            body = {target_name: patch_body}
            async with session.post(scenes_url, json=body, timeout=timeout) as r:
                result['response'] = await r.json()
                result['success'] = r.status == 200 and bool(result['response'].get('success', True))
                if result['success']:
                    logger.info(f"✓ {device_id} ({ip}): patched scene '{target_name}'")
                else:
                    err = result['response'].get('error', f"HTTP {r.status}")
                    logger.warning(f"✗ {device_id} ({ip}): {err}")

        except asyncio.TimeoutError:
            result['error'] = 'Timeout'
            logger.error(f"✗ {device_id} ({ip}): Timeout")
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"✗ {device_id} ({ip}): {e}")

        return result

    async def batch_scene_patch(self, devices: List[Dict[str, Any]], build_patch,
                                scene_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Concurrently patch a scene on each device.

        scene_name=None → resolve each device's active scene (GET first, then POST).
        scene_name='foo' → patch scene 'foo' on every device directly (fails per-device
                           if 'foo' doesn't exist on that device).
        """
        connector = aiohttp.TCPConnector(limit=self.concurrent_limit, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [self._patch_scene(session, device, build_patch, scene_name) for device in devices]
            return await asyncio.gather(*tasks)

    async def batch_scene_action(self, devices: List[Dict[str, Any]], action: str,
                                 name: str) -> List[Dict[str, Any]]:
        """POST /api/scene with {action, name} to every device (create/delete/activate/set_default)."""
        return await self.batch_request(devices, 'POST', '/api/scene',
                                        {'action': action, 'name': name})

    async def batch_set_device_volume(self, devices: List[Dict[str, Any]],
                                      volume: int) -> List[Dict[str, Any]]:
        """POST /api/device with {device_volume} — per-device master attenuator."""
        return await self.batch_request(devices, 'POST', '/api/device',
                                        {'device_volume': volume})
    
    async def show_status(self, devices: List[Dict[str, Any]]) -> None:
        """Show status of all devices."""
        logger.info("=" * 70)
        logger.info("DEVICE STATUS")
        logger.info("=" * 70)
        
        # Get current status from all devices
        results = await self.batch_request(devices, 'GET', '/api/device')
        
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
    
    async def stop_all_loops(self, devices: List[Dict[str, Any]],
                             scene_name: Optional[str] = None) -> None:
        """Stop all tracks on a scene (default: each device's active scene)."""
        target = scene_name or "active scene"
        logger.info(f"Stopping all tracks on {target} on all devices...")

        def patch(_scene):
            return {'tracks': [{'track': i, 'active': False} for i in range(3)]}

        results = await self.batch_scene_patch(devices, patch, scene_name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Stopped on {success_count}/{len(results)} devices")

    async def start_all_loops(self, devices: List[Dict[str, Any]],
                              scene_name: Optional[str] = None) -> None:
        """
        Start loop-mode tracks on a scene (default: each device's active scene).
        Only activates tracks that have a file_path and mode='loop'.

        Note: with a named scene, we'd need a GET per device to know which tracks
        have files. To keep this simple and scene-agnostic, we send active=true
        for all 3 tracks when --scene is provided — the device's scene_apply_patch
        will no-op on empty file_paths. With no --scene we still inspect the
        active scene's current track configs and only activate ones with a file.
        """
        target = scene_name or "active scene"
        logger.info(f"Starting loop tracks on {target} on all devices...")

        if scene_name is None:
            def patch(scene):
                updates = []
                for t in (scene.get('tracks') or []):
                    if t.get('mode') == 'loop' and t.get('file_path'):
                        updates.append({'track': t.get('track'), 'active': True})
                return {'tracks': updates} if updates else None
        else:
            def patch(_scene):
                return {'tracks': [{'track': i, 'active': True} for i in range(3)]}

        results = await self.batch_scene_patch(devices, patch, scene_name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Started on {success_count}/{len(results)} devices")

    async def set_volume(self, devices: List[Dict[str, Any]], track: Optional[int],
                        volume: int, global_vol: bool = False,
                        scene_name: Optional[str] = None) -> None:
        """
        Set a volume on a scene across all devices.

        Args:
            devices: List of devices
            track: Track number (0-2) or None for scene-global
            volume: Volume level (0-100)
            global_vol: When True, set the scene's global_volume (not a track volume)
            scene_name: Target scene name; None = each device's active scene
        """
        target = scene_name or "active scene"

        if global_vol:
            logger.info(f"Setting scene global_volume to {volume}% on {target} on all devices...")

            def patch(_scene):
                return {'global_volume': volume}
        else:
            if track is None:
                logger.error("Track number required for track volume (use --track)")
                return
            logger.info(f"Setting track {track} volume to {volume}% on {target} on all devices...")

            def patch(_scene):
                return {'tracks': [{'track': track, 'volume': volume}]}

        results = await self.batch_scene_patch(devices, patch, scene_name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Volume set on {success_count}/{len(results)} devices")

    async def create_scene(self, devices: List[Dict[str, Any]], name: str) -> None:
        """Create a scene on all devices (cloned from each device's current active scene)."""
        logger.info(f"Creating scene '{name}' on all devices...")
        results = await self.batch_scene_action(devices, 'create', name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Scene '{name}' created on {success_count}/{len(results)} devices")

    async def delete_scene(self, devices: List[Dict[str, Any]], name: str) -> None:
        """Delete a scene on all devices."""
        logger.info(f"Deleting scene '{name}' on all devices...")
        results = await self.batch_scene_action(devices, 'delete', name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Scene '{name}' deleted on {success_count}/{len(results)} devices")

    async def activate_scene(self, devices: List[Dict[str, Any]], name: str) -> None:
        """Activate a scene on all devices."""
        logger.info(f"Activating scene '{name}' on all devices...")
        results = await self.batch_scene_action(devices, 'activate', name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Scene '{name}' activated on {success_count}/{len(results)} devices")

    async def set_default_scene(self, devices: List[Dict[str, Any]], name: str) -> None:
        """Set a scene as the default (boot-time) scene on all devices."""
        logger.info(f"Setting default scene to '{name}' on all devices...")
        results = await self.batch_scene_action(devices, 'set_default', name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Default set to '{name}' on {success_count}/{len(results)} devices")

    async def set_scene(self, devices: List[Dict[str, Any]], scene_name: str,
                        track: Optional[int] = None, mode: Optional[str] = None,
                        active: Optional[bool] = None, filename: Optional[str] = None,
                        volume: Optional[int] = None, global_volume: Optional[int] = None,
                        trigger_name: Optional[str] = None,
                        trigger_type: Optional[str] = None) -> None:
        """Patch a named scene across all devices — any subset of scene/track fields."""
        scene_body: Dict[str, Any] = {}

        if track is not None:
            entry: Dict[str, Any] = {'track': track}
            if mode is not None: entry['mode'] = mode
            if active is not None: entry['active'] = active
            if filename is not None: entry['file'] = filename
            if volume is not None: entry['volume'] = volume
            if trigger_name is not None: entry['trigger_name'] = trigger_name
            if trigger_type is not None: entry['trigger_type'] = trigger_type
            scene_body['tracks'] = [entry]

        if global_volume is not None:
            scene_body['global_volume'] = global_volume

        if not scene_body:
            logger.error("No scene fields specified (use --track and/or --global-volume etc.)")
            return

        logger.info(f"Patching scene '{scene_name}' on all devices: {json.dumps(scene_body)}")

        def patch(_scene):
            return scene_body

        results = await self.batch_scene_patch(devices, patch, scene_name)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"Scene '{scene_name}' patched on {success_count}/{len(results)} devices")

    async def set_device_volume(self, devices: List[Dict[str, Any]], volume: int) -> None:
        """Set the per-device master volume on all devices."""
        logger.info(f"Setting device_volume to {volume}% on all devices...")
        results = await self.batch_set_device_volume(devices, volume)
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"device_volume set on {success_count}/{len(results)} devices")
    
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
    
    async def reboot_all(self, devices: List[Dict[str, Any]], delay_ms: int = 1000) -> None:
        """
        Reboot all devices.
        
        Args:
            devices: List of devices
            delay_ms: Delay before reboot in milliseconds (default: 1000)
        """
        logger.info(f"Rebooting all devices in {delay_ms}ms...")
        
        results = await self.batch_request(devices, 'POST', '/api/system/reboot', {'delay_ms': delay_ms})
        
        success_count = sum(1 for r in results if r['success'])
        logger.info(f"✓ Reboot command sent to {success_count}/{len(results)} devices")
        logger.info("Note: Devices will be offline for a moment during reboot")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='batch_controller',
        description='ESP32 Batch Controller - Batch operations on multiple ESP32 devices',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Status / lifecycle
    %(prog)s --command status
    %(prog)s --command reboot-all
    %(prog)s --command save-config
    %(prog)s --command load-config

    # Active-scene playback control
    %(prog)s --command stop-all
    %(prog)s --command start-all

    # Target a specific scene instead of each device's active scene
    %(prog)s --command stop-all --scene night
    %(prog)s --command set-volume --scene day --global --volume 75
    %(prog)s --command set-volume --scene day --track 0 --volume 50

    # Scene lifecycle (via /api/scene)
    %(prog)s --command create-scene --name show
    %(prog)s --command delete-scene --name show
    %(prog)s --command activate-scene --name night
    %(prog)s --command set-default-scene --name day

    # Patch a named scene's fields (analog of device_controller's set-scene)
    %(prog)s --command set-scene --scene night --global-volume 40
    %(prog)s --command set-scene --scene night --track 0 --file crickets.wav --volume 100 --active true
    %(prog)s --command set-scene --scene night --track 2 --mode trigger --trigger-name RedButton.Button_1

    # Per-device master (composes with scene + track volumes)
    %(prog)s --command set-device-volume --volume 80

    # Filter devices
    %(prog)s --command status --filter-id "^STAGE"
    %(prog)s --command stop-all --all-devices

Commands:
    status               - Show status of all devices in the map
    stop-all             - Stop all 3 tracks on a scene (default: active)
    start-all            - Start loop-mode tracks on a scene (default: active)
    set-volume           - Set global_volume or a track's volume on a scene
    create-scene         - Create a scene (cloned from active) on all devices
    delete-scene         - Delete a scene on all devices
    activate-scene       - Activate a scene on all devices
    set-default-scene    - Set the boot-time default scene on all devices
    set-scene            - Patch a named scene (global_volume or per-track fields)
    set-device-volume    - Set the per-device master attenuator (0-100)
    save-config          - Save scenes + gateway config to SD on all devices
    load-config          - Reload config from SD on all devices
    reboot-all           - Reboot all devices
"""
    )

    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--command', '-c',
                         required=True,
                         choices=['status', 'stop-all', 'start-all', 'set-volume',
                                 'create-scene', 'delete-scene', 'activate-scene',
                                 'set-default-scene', 'set-scene', 'set-device-volume',
                                 'save-config', 'load-config', 'reboot-all'],
                         help='Command to execute on all devices')
    
    # Optional arguments
    optional = parser.add_argument_group('optional arguments')
    optional.add_argument('--map-file', '-m',
                         default='device_map.json',
                         metavar='PATH',
                         help='Path to device map JSON file (default: device_map.json)')
    
    optional.add_argument('--timeout', '-t',
                         type=int, 
                         default=5,
                         metavar='SEC',
                         help='Request timeout in seconds (default: 5)')
    
    optional.add_argument('--concurrent', '-n',
                         type=int, 
                         default=10,
                         metavar='NUM',
                         help='Maximum concurrent connections (default: 10)')
    
    # Scene + track arguments
    scene_group = parser.add_argument_group('scene / track control')
    scene_group.add_argument('--scene', '-s',
                             metavar='NAME',
                             help='Target scene for stop-all/start-all/set-volume/set-scene '
                                  '(default: each device\'s active scene)')

    scene_group.add_argument('--name',
                             metavar='NAME',
                             help='Scene name for create-scene/delete-scene/activate-scene/set-default-scene')

    scene_group.add_argument('--track', '-k',
                             type=int,
                             choices=[0, 1, 2],
                             help='Track number for track-specific commands')

    scene_group.add_argument('--mode',
                             choices=['loop', 'trigger'],
                             help='Track mode (for set-scene)')

    scene_group.add_argument('--active',
                             choices=['true', 'false'],
                             help='Enable/disable the track (for set-scene)')

    scene_group.add_argument('--file',
                             metavar='FILENAME',
                             help='Audio filename (for set-scene --track)')

    scene_group.add_argument('--trigger-name',
                             metavar='NAME',
                             help='Trigger event name to bind (for set-scene --track); empty string clears')

    scene_group.add_argument('--trigger-type',
                             choices=['On/Off', 'OneShot'],
                             help='Trigger type (for set-scene --track)')

    scene_group.add_argument('--global-volume',
                             type=int,
                             metavar='LEVEL',
                             help='Scene global volume (for set-scene)')

    # Volume-specific arguments
    volume_group = parser.add_argument_group('volume control')
    volume_group.add_argument('--volume', '-v',
                             type=int,
                             metavar='LEVEL',
                             help='Volume level (0-100)')

    volume_group.add_argument('--global', '-g',
                             dest='global_volume_flag',
                             action='store_true',
                             help='Set scene global_volume (for set-volume)')

    # Filter arguments
    filter_group = parser.add_argument_group('device filters')
    filter_group.add_argument('--all-devices', '-a',
                             action='store_true',
                             help='Include offline devices (default: online only)')
    
    filter_group.add_argument('--filter-id', '-f',
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
            asyncio.run(controller.stop_all_loops(devices, scene_name=args.scene))

        elif args.command == 'start-all':
            asyncio.run(controller.start_all_loops(devices, scene_name=args.scene))

        elif args.command == 'set-volume':
            if args.volume is None:
                logger.error("Volume level required (use --volume)")
                sys.exit(1)
            asyncio.run(controller.set_volume(devices, args.track, args.volume,
                                              global_vol=args.global_volume_flag,
                                              scene_name=args.scene))

        elif args.command in ('create-scene', 'delete-scene', 'activate-scene', 'set-default-scene'):
            if not args.name:
                logger.error(f"Scene name required (use --name) for {args.command}")
                sys.exit(1)
            action_map = {
                'create-scene': controller.create_scene,
                'delete-scene': controller.delete_scene,
                'activate-scene': controller.activate_scene,
                'set-default-scene': controller.set_default_scene,
            }
            asyncio.run(action_map[args.command](devices, args.name))

        elif args.command == 'set-scene':
            if not args.scene:
                logger.error("Scene name required (use --scene) for set-scene")
                sys.exit(1)
            active = None
            if args.active is not None:
                active = (args.active == 'true')
            asyncio.run(controller.set_scene(
                devices,
                scene_name=args.scene,
                track=args.track,
                mode=args.mode,
                active=active,
                filename=args.file,
                volume=args.volume,
                global_volume=args.global_volume,
                trigger_name=args.trigger_name,
                trigger_type=args.trigger_type,
            ))

        elif args.command == 'set-device-volume':
            if args.volume is None:
                logger.error("Volume level required (use --volume)")
                sys.exit(1)
            asyncio.run(controller.set_device_volume(devices, args.volume))

        elif args.command == 'save-config':
            asyncio.run(controller.save_configs(devices))

        elif args.command == 'load-config':
            asyncio.run(controller.load_configs(devices))

        elif args.command == 'reboot-all':
            asyncio.run(controller.reboot_all(devices))

    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
