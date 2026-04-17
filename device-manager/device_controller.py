#!/usr/bin/env python3
"""
ESP32 Device Controller
Controls a single ESP32 device by its ID.

Usage:
    python device_controller.py --id <device_id> --command <command> [options]

Commands:
    status            - Show device status
    get-scenes        - Get all scenes with DEFAULT/ACTIVE markers
    set-scene         - Patch a scene (track settings, global_volume)
    create-scene      - Create a new scene
    delete-scene      - Delete a scene
    activate-scene    - Activate a scene
    set-default-scene - Set a scene as the default
    set-id            - Change the device ID
    save-config       - Save current configuration to SD card
    load-config       - Load saved configuration from SD card
    reboot            - Reboot the device
    list-files        - List all audio files on the device's SD card

Examples:
    python device_controller.py --id MURMURA-001 --command status
    python device_controller.py --id MURMURA-001 --command get-scenes
    python device_controller.py --id MURMURA-001 --command set-scene --scene night --track 0 --volume 80
    python device_controller.py --id MURMURA-001 --command set-scene --scene day --global-volume 60
    python device_controller.py --id MURMURA-001 --command activate-scene --scene night
    python device_controller.py --id MURMURA-001 --command create-scene --scene show
    python device_controller.py --id MURMURA-001 --command delete-scene --scene show
    python device_controller.py --id MURMURA-001 --command set-default-scene --scene day
    python device_controller.py --id MURMURA-001 --command set-id --new-id STAGE-01
    python device_controller.py --id MURMURA-001 --command reboot
    python device_controller.py --id MURMURA-001 --command list-files
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

    def __init__(self, device_id: str, map_file: str = "device_map.json", timeout: int = 5, force: bool = False):
        self.device_id = device_id
        self.map_file = Path(map_file)
        self.timeout = timeout
        self.force = force
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

    async def verify_device_id(self) -> bool:
        if self.force:
            logger.warning("Skipping device ID verification (--force enabled)")
            return True

        if not self.device:
            return False

        ip = self.device['ip_address']
        logger.info(f"Verifying device ID at {ip}...")

        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://{ip}/api/device"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                    if response.status == 200:
                        data = await response.json()
                        actual_id = data.get('id', 'UNKNOWN')
                        if actual_id == self.device_id:
                            logger.info(f"✓ Device ID verified: {self.device_id} at {ip}")
                            return True
                        else:
                            logger.error(f"✗ ID MISMATCH! Expected '{self.device_id}' but found '{actual_id}' at {ip}")
                            logger.error("The device map may be outdated. Please rescan.")
                            logger.error("Or use --force to bypass this check (not recommended)")
                            return False
                    else:
                        logger.error(f"Failed to verify device ID: HTTP {response.status}")
                        return False

        except asyncio.TimeoutError:
            logger.error(f"Timeout verifying device ID at {ip}")
            logger.error("Use --force to bypass verification (not recommended)")
            return False
        except Exception as e:
            logger.error(f"Error verifying device ID: {e}")
            return False

    async def send_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        if not self.device:
            return {'success': False, 'error': 'Device not loaded'}

        ip = self.device['ip_address']
        url = f"http://{ip}{endpoint}"

        result = {'success': False, 'response': None, 'error': None}

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

        result = await self.send_request('GET', '/api/device')

        if result['success'] and result['response']:
            data = result['response']
            wifi = data.get('wifi', {})
            print(f"\nDevice Status: {self.device_id}")
            print("-" * 40)
            print(f"IP Address:    {data.get('ip_address', self.device['ip_address'])}")
            print(f"MAC Address:   {data.get('mac_address', 'N/A')}")
            print(f"WiFi Status:   {'Connected' if wifi.get('connected') else 'Disconnected'}")
            print(f"Firmware:      {data.get('firmware_version', 'N/A')}")
            print(f"Uptime:        {data.get('uptime_seconds', 'N/A')}s")
        else:
            logger.error(f"Failed to get status: {result.get('error', 'Unknown error')}")

    async def get_scenes(self) -> None:
        """Get all scenes with DEFAULT/ACTIVE markers."""
        logger.info(f"Getting scenes for {self.device_id}")

        result = await self.send_request('GET', '/api/scenes')

        if result['success'] and result['response']:
            data = result['response']
            active_scene = data.get('active_scene', '')
            default_scene = data.get('default_scene', '')
            scenes = data.get('scenes', {})

            print(f"\nScenes for {self.device_id}")
            print("=" * 90)

            if not scenes:
                print("  (no scenes)")
                return

            for scene_name, scene_data in scenes.items():
                markers = []
                if scene_name == active_scene:
                    markers.append("ACTIVE")
                if scene_name == default_scene:
                    markers.append("DEFAULT")
                marker_str = f" [{', '.join(markers)}]" if markers else ""
                print(f"\n  Scene: {scene_name}{marker_str}")
                print(f"  Global Volume: {scene_data.get('global_volume', 'N/A')}%")
                print(f"  {'Trk':<5} {'Mode':<8} {'Active':<8} {'Vol':<5} {'TrigMode':<10} {'TrigName':<24} File")
                print("  " + "-" * 86)

                for track in scene_data.get('tracks', []):
                    active = "YES" if track.get('active') else "no"
                    file_name = track.get('file', '')
                    file_name = file_name.split('/')[-1] if file_name else '(none)'
                    trig_name = track.get('trigger_name', '') or '-'
                    trig_mode = track.get('trigger_mode', '-') if track.get('trigger_name') else '-'
                    print(f"    {track.get('track', '?'):<3} {track.get('mode','loop'):<8} {active:<8} "
                          f"{track.get('volume',0):<5} {trig_mode:<10} {trig_name:<24} {file_name}")

            print()
        else:
            logger.error(f"Failed to get scenes: {result.get('error', 'Unknown error')}")

    async def set_scene(self, scene: str, track: Optional[int], mode: Optional[str],
                        active: Optional[bool], filename: Optional[str],
                        volume: Optional[int], global_volume: Optional[int],
                        trigger_name: Optional[str] = None,
                        trigger_mode: Optional[str] = None) -> None:
        """Patch a scene via POST /api/scenes."""
        scene_body: Dict[str, Any] = {}

        # Build track entry if any track-level args provided
        if track is not None:
            track_entry: Dict[str, Any] = {'track': track}
            if mode is not None:
                track_entry['mode'] = mode
            if active is not None:
                track_entry['active'] = active
            if filename is not None:
                track_entry['file'] = filename
            if volume is not None:
                track_entry['volume'] = volume
            if trigger_name is not None:
                track_entry['trigger_name'] = trigger_name
            if trigger_mode is not None:
                track_entry['trigger_mode'] = trigger_mode
            scene_body['tracks'] = [track_entry]

        if global_volume is not None:
            scene_body['global_volume'] = global_volume

        if not scene_body:
            logger.error("No changes specified for set-scene")
            return

        payload = {scene: scene_body}

        logger.info(f"Patching scene '{scene}' on {self.device_id}: {json.dumps(payload)}")
        result = await self.send_request('POST', '/api/scenes', payload)

        if result['success']:
            logger.info(f"Scene '{scene}' updated successfully")
        else:
            resp = result.get('response', {}) or {}
            logger.error(f"Failed: {resp.get('error', result.get('error', 'Unknown error'))}")

    async def scene_action(self, action: str, name: str) -> None:
        """Perform a scene management action via POST /api/scene."""
        payload = {'action': action, 'name': name}

        logger.info(f"Scene {action} '{name}' on {self.device_id}")
        result = await self.send_request('POST', '/api/scene', payload)

        if result['success']:
            logger.info(f"Scene '{name}' {action} succeeded")
        else:
            resp = result.get('response', {}) or {}
            logger.error(f"Failed: {resp.get('error', result.get('error', 'Unknown error'))}")

    async def get_mur_gateway(self) -> None:
        """Get Mur Gateway configuration (via consolidated /api/device)."""
        result = await self.send_request('GET', '/api/device')
        if result['success'] and result['response']:
            data = result['response']
            print(f"\nMur Gateway Config for {self.device_id}")
            print(f"  Gateway IP:   {data.get('mur_gateway_ip') or '(not set)'}")
            print(f"  Gateway port: {data.get('mur_gateway_port', 4000)}")
        else:
            logger.error(f"Failed to get Mur Gateway config: {result.get('error', 'Unknown error')}")

    async def set_device_volume(self, volume: int) -> None:
        """Set the per-device master volume (0-100) via POST /api/device."""
        if volume < 0 or volume > 100:
            logger.error("Volume must be between 0 and 100")
            return
        result = await self.send_request('POST', '/api/device', {'device_volume': volume})
        if result['success']:
            resp = result.get('response') or {}
            logger.info(f"✓ device_volume set to {resp.get('device_volume', volume)}%")
        else:
            resp = result.get('response', {}) or {}
            logger.error(f"✗ Failed: {resp.get('error', result.get('error', 'Unknown error'))}")

    async def set_mur_gateway(self, ip: Optional[str], port: Optional[int]) -> None:
        """Set Mur Gateway IP and/or port (via consolidated /api/device)."""
        payload: Dict[str, Any] = {}
        if ip is not None:
            payload['mur_gateway_ip'] = ip
        if port is not None:
            payload['mur_gateway_port'] = port
        result = await self.send_request('POST', '/api/device', payload)
        if result['success']:
            resp = result['response']
            logger.info(f"✓ Mur Gateway updated: ip={resp.get('mur_gateway_ip')}, port={resp.get('mur_gateway_port')}")
        else:
            resp = result.get('response', {}) or {}
            logger.error(f"✗ Failed: {resp.get('error', result.get('error', 'Unknown error'))}")

    async def set_device_id(self, new_id: str) -> None:
        """Change the device ID."""
        logger.info(f"Changing device ID from {self.device_id} to {new_id}")

        result = await self.send_request('POST', '/api/device', {'id': new_id})

        if result['success']:
            logger.info(f"✓ Device ID changed to: {new_id}")
            logger.info("Note: Rescan the network to update the device map")
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

    async def reboot(self, delay_ms: int = 1000) -> None:
        """Reboot the device."""
        logger.info(f"Rebooting device {self.device_id} in {delay_ms}ms...")
        result = await self.send_request('POST', '/api/system/reboot', {'delay_ms': delay_ms})

        if result['success']:
            logger.info(f"✓ Device {self.device_id} will reboot in {delay_ms}ms")
        else:
            logger.error(f"✗ Failed to reboot: {result.get('error', 'Unknown error')}")

    async def list_files(self) -> None:
        """List all audio files on the device's SD card."""
        logger.info(f"Getting file list for device: {self.device_id}")

        result = await self.send_request('GET', '/api/files')

        if result['success'] and result['response']:
            data = result['response']
            files = data.get('files', [])

            print(f"\nFiles on {self.device_id}")
            print("-" * 60)

            if files:
                print(f"Total: {len(files)} file(s)\n")
                print(f"{'Index':<6} {'Name':<30} {'Type':<5} {'Size (MB)':<10}")
                print("-" * 60)

                for file_info in files:
                    index = file_info.get('index', 0)
                    name = file_info.get('name', 'UNKNOWN')
                    file_type = file_info.get('type', '').upper()
                    size = file_info.get('size', 0)

                    if size > 0:
                        size_mb = size / (1024 * 1024)
                        print(f"{index:<6} {name:<30} {file_type:<5} {size_mb:>9.2f}")
                    else:
                        print(f"{index:<6} {name:<30} {file_type:<5} {'N/A':>10}")
            else:
                print("No files found on SD card")
        else:
            logger.error(f"Failed to get file list: {result.get('error', 'Unknown error')}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='device_controller',
        description='ESP32 Device Controller - Control a single ESP32 device by ID',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Show device status
    %(prog)s --id MURMURA-001 --command status

    # Get all scenes
    %(prog)s --id MURMURA-001 --command get-scenes

    # Set track 0 volume in the 'night' scene
    %(prog)s --id MURMURA-001 --command set-scene --scene night --track 0 --volume 80

    # Set global volume in the 'day' scene
    %(prog)s --id MURMURA-001 --command set-scene --scene day --global-volume 60

    # Activate a scene
    %(prog)s --id MURMURA-001 --command activate-scene --scene night

    # Create / delete a scene
    %(prog)s --id MURMURA-001 --command create-scene --scene show
    %(prog)s --id MURMURA-001 --command delete-scene --scene show

    # Set a scene as default
    %(prog)s --id MURMURA-001 --command set-default-scene --scene day

    # Change device ID
    %(prog)s --id MURMURA-001 --command set-id --new-id STAGE-01

    # Save/load configuration
    %(prog)s --id MURMURA-001 --command save-config
    %(prog)s --id MURMURA-001 --command load-config

    # Reboot device
    %(prog)s --id MURMURA-001 --command reboot

    # List files on device
    %(prog)s --id MURMURA-001 --command list-files
"""
    )

    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--id', '-i',
                          dest='device_id',
                          required=True,
                          metavar='ID',
                          help='Device ID to control')

    required.add_argument('--command', '-c',
                          required=True,
                          choices=['status', 'get-scenes', 'set-scene',
                                   'create-scene', 'delete-scene', 'activate-scene',
                                   'set-default-scene',
                                   'set-device-volume',
                                   'set-id', 'save-config', 'load-config', 'reboot', 'list-files',
                                   'get-mur-gateway', 'set-mur-gateway'],
                          help='Command to execute on the device')

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

    optional.add_argument('--force', '-f',
                          action='store_true',
                          help='Skip device ID verification (not recommended)')

    # Scene control
    scene_group = parser.add_argument_group('scene control')
    scene_group.add_argument('--scene', '-s',
                              metavar='NAME',
                              help='Scene name (required for scene commands)')

    scene_group.add_argument('--global-volume',
                              type=int,
                              metavar='LEVEL',
                              help='Global volume level (0-100) for set-scene')

    # Track control (within a scene, for set-scene)
    track_group = parser.add_argument_group('track control (for set-scene)')
    track_group.add_argument('--track', '-k',
                              type=int,
                              choices=[0, 1, 2],
                              help='Track number (0, 1, or 2)')

    track_group.add_argument('--mode',
                              choices=['loop', 'trigger'],
                              help='Track mode: loop (continuous) or trigger (play once)')

    track_group.add_argument('--active',
                              choices=['true', 'false'],
                              help='Enable (true) or disable/stop (false) the track')

    track_group.add_argument('--file',
                              metavar='FILENAME',
                              help='Audio filename (e.g. ambient.wav)')

    track_group.add_argument('--trigger-name',
                              metavar='NAME',
                              help='Trigger event name to bind (e.g. RedButton.Button_1); empty string clears')

    track_group.add_argument('--trigger-mode',
                              choices=['momentary', 'oneshot'],
                              help='momentary: play while held; oneshot: play once on press')

    # Mur Gateway control
    trig_group = parser.add_argument_group('mur gateway control (for set-mur-gateway)')
    trig_group.add_argument('--trigger-ip',
                             metavar='IP',
                             help='IP address of the Mur Gateway')
    trig_group.add_argument('--trigger-port',
                             type=int,
                             metavar='PORT',
                             help='Port of the Mur Gateway (default 4000)')

    # Volume control (per-track, used with set-scene --track)
    volume_group = parser.add_argument_group('volume control')
    volume_group.add_argument('--volume', '-v',
                               type=int,
                               metavar='LEVEL',
                               help='Track volume level (0-100), used with --track in set-scene')

    # Device ID change
    id_group = parser.add_argument_group('device ID control')
    id_group.add_argument('--new-id', '-n',
                           metavar='ID',
                           help='New device ID for set-id command')

    args = parser.parse_args()

    # Create controller
    controller = DeviceController(
        device_id=args.device_id,
        map_file=args.map_file,
        timeout=args.timeout,
        force=args.force
    )

    # Load device
    device = controller.load_device()
    if not device:
        sys.exit(1)

    # Check if device is online
    if not device.get('online', False):
        logger.warning(f"Device {args.device_id} is marked as offline in the device map")
        logger.warning("Commands may fail if the device is not reachable")

    # Verify device ID before proceeding
    if device.get('online', False):
        if not asyncio.run(controller.verify_device_id()):
            logger.error("Device ID verification failed. Aborting operation.")
            sys.exit(1)

    # Execute command
    try:
        if args.command == 'status':
            asyncio.run(controller.show_status())

        elif args.command == 'get-scenes':
            asyncio.run(controller.get_scenes())

        elif args.command == 'set-scene':
            if not args.scene:
                logger.error("Scene name required (use --scene)")
                sys.exit(1)
            if args.track is None and args.global_volume is None:
                logger.error("Provide --track and/or --global-volume for set-scene")
                sys.exit(1)
            # Convert active string to bool
            active = None
            if args.active is not None:
                active = (args.active == 'true')
            asyncio.run(controller.set_scene(
                scene=args.scene,
                track=args.track,
                mode=args.mode,
                active=active,
                filename=args.file,
                volume=args.volume,
                global_volume=args.global_volume,
                trigger_name=args.trigger_name,
                trigger_mode=args.trigger_mode
            ))

        elif args.command in ('create-scene', 'delete-scene', 'activate-scene', 'set-default-scene'):
            if not args.scene:
                logger.error("Scene name required (use --scene)")
                sys.exit(1)
            action_map = {
                'create-scene': 'create',
                'delete-scene': 'delete',
                'activate-scene': 'activate',
                'set-default-scene': 'set_default',
            }
            asyncio.run(controller.scene_action(action_map[args.command], args.scene))

        elif args.command == 'set-device-volume':
            if args.volume is None:
                logger.error("Volume level required (use --volume)")
                sys.exit(1)
            asyncio.run(controller.set_device_volume(args.volume))

        elif args.command == 'set-id':
            if not args.new_id:
                logger.error("New ID required (use --new-id)")
                sys.exit(1)
            asyncio.run(controller.set_device_id(args.new_id))

        elif args.command == 'save-config':
            asyncio.run(controller.save_config())

        elif args.command == 'load-config':
            asyncio.run(controller.load_config())

        elif args.command == 'reboot':
            asyncio.run(controller.reboot())

        elif args.command == 'list-files':
            asyncio.run(controller.list_files())

        elif args.command == 'get-mur-gateway':
            asyncio.run(controller.get_mur_gateway())

        elif args.command == 'set-mur-gateway':
            if args.trigger_ip is None and args.trigger_port is None:
                logger.error("Provide --trigger-ip and/or --trigger-port")
                sys.exit(1)
            asyncio.run(controller.set_mur_gateway(
                ip=args.trigger_ip,
                port=args.trigger_port
            ))

    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
