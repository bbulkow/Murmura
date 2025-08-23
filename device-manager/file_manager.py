#!/usr/bin/env python3
"""
ESP32 File Manager
Manages audio files on ESP32 devices - upload, sync, list, and delete files.

Usage:
    python file_manager.py --command <command> [options]
    
Commands:
    list        - List files on devices
    upload      - Upload a file to devices
    sync        - Sync files across all devices
    delete      - Delete a file from devices (Note: ESP32 must support delete endpoint)
    
Examples:
    # List files on all devices
    python file_manager.py --command list
    
    # Upload a file to all devices
    python file_manager.py --command upload --file music.wav
    
    # Upload a file to a specific device
    python file_manager.py --command upload --file music.wav --id LOUDFRAME-001
    
    # Upload a file with a different name on the device
    python file_manager.py --command upload --file music.wav --target-name loop1.wav
    
    # Sync all files from a directory to all devices
    python file_manager.py --command sync --directory ./loops
    
    # Delete a file from all devices
    python file_manager.py --command delete --file old_music.wav
"""

import asyncio
import aiohttp
import argparse
import json
import sys
import os
import hashlib
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class FileManager:
    """Manager for file operations on ESP32 devices."""
    
    def __init__(self, map_file: str = "device_map.json", timeout: int = 30, 
                 concurrent_limit: int = 5):
        """
        Initialize the file manager.
        
        Args:
            map_file: Path to device map JSON file
            timeout: Request timeout in seconds (longer for file uploads)
            concurrent_limit: Maximum concurrent connections (lower for file uploads)
        """
        self.map_file = Path(map_file)
        self.timeout = timeout
        self.concurrent_limit = concurrent_limit
        self.devices = []
        
    def load_device_map(self, device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Load device map from JSON file.
        
        Args:
            device_id: Optional specific device ID to load
            
        Returns:
            List of devices
        """
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
            
            # Filter for specific device if requested
            if device_id:
                devices = [d for d in self.devices if d.get('id') == device_id]
                if not devices:
                    logger.error(f"Device with ID '{device_id}' not found")
                    logger.info("Available devices:")
                    for d in self.devices:
                        logger.info(f"  - {d.get('id', 'UNKNOWN')}")
                    sys.exit(1)
                return devices
            
            # Filter only online devices
            online_devices = [d for d in self.devices if d.get('online', False)]
            logger.info(f"Loaded {len(online_devices)} online devices (out of {len(self.devices)} total)")
            
            return online_devices
            
        except Exception as e:
            logger.error(f"Error loading device map: {e}")
            sys.exit(1)
    
    def calculate_file_hash(self, file_path: Path) -> str:
        """
        Calculate MD5 hash of a file for comparison.
        
        Args:
            file_path: Path to the file
            
        Returns:
            MD5 hash string
        """
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    async def list_files(self, session: aiohttp.ClientSession, device: Dict[str, Any]) -> Dict[str, Any]:
        """
        List files on a device.
        
        Args:
            session: aiohttp session
            device: Device information
            
        Returns:
            File list response
        """
        ip = device['ip_address']
        device_id = device.get('id', 'UNKNOWN')
        url = f"http://{ip}/api/files"
        
        result = {
            'device_id': device_id,
            'ip_address': ip,
            'success': False,
            'files': [],
            'error': None
        }
        
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                if response.status == 200:
                    data = await response.json()
                    result['files'] = data.get('files', [])
                    result['success'] = True
                else:
                    result['error'] = f"HTTP {response.status}"
                    
        except asyncio.TimeoutError:
            result['error'] = 'Timeout'
            logger.error(f"✗ {device_id} ({ip}): Timeout listing files")
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"✗ {device_id} ({ip}): Error listing files: {e}")
            
        return result
    
    async def check_file_exists(self, session: aiohttp.ClientSession, device: Dict[str, Any], 
                               filename: str) -> Tuple[bool, Optional[int]]:
        """
        Check if a file exists on a device.
        
        Args:
            session: aiohttp session
            device: Device information
            filename: Name of the file to check
            
        Returns:
            Tuple of (exists, file_size)
        """
        result = await self.list_files(session, device)
        
        if result['success']:
            for file_info in result['files']:
                if file_info.get('name') == filename:
                    # File exists, return True and size if available
                    return True, file_info.get('size')
        
        return False, None
    
    async def upload_file(self, session: aiohttp.ClientSession, device: Dict[str, Any], 
                         file_path: Path, skip_existing: bool = True, 
                         target_filename: Optional[str] = None) -> Dict[str, Any]:
        """
        Upload a file to a device.
        
        Args:
            session: aiohttp session
            device: Device information
            file_path: Path to the file to upload
            skip_existing: Skip if file already exists
            target_filename: Optional custom filename for the uploaded file
            
        Returns:
            Upload result
        """
        ip = device['ip_address']
        device_id = device.get('id', 'UNKNOWN')
        # Use custom filename if provided, otherwise use original filename
        filename = target_filename if target_filename else file_path.name
        
        result = {
            'device_id': device_id,
            'ip_address': ip,
            'filename': filename,
            'success': False,
            'skipped': False,
            'error': None
        }
        
        try:
            # Get file size
            file_size = file_path.stat().st_size
            file_size_mb = file_size / (1024 * 1024)
            
            # Check if file already exists
            if skip_existing:
                exists, remote_size = await self.check_file_exists(session, device, filename)
                if exists:
                    if remote_size and remote_size == file_size:
                        logger.info(f"⊡ {device_id} ({ip}): {filename} already exists (same size), skipping")
                        result['skipped'] = True
                        result['success'] = True
                        return result
                    else:
                        logger.info(f"↻ {device_id} ({ip}): {filename} exists but size differs, re-uploading")
            
            # Upload the file - ESP32 expects filename as query parameter
            encoded_filename = urllib.parse.quote(filename)
            url = f"http://{ip}/api/upload?filename={encoded_filename}"
            
            logger.info(f"⬆ {device_id} ({ip}): Uploading {filename} ({file_size_mb:.2f} MB)...")
            start_time = time.time()
            
            # Read the entire file into memory
            # ESP32's embedded HTTP server doesn't support chunked transfer encoding
            with open(file_path, 'rb') as f:
                file_data = f.read()
            
            # Use timeout that's appropriate for large files
            # Socket timeout will trigger if no data is received for the specified time
            # This allows large uploads to complete as long as data keeps flowing
            if file_size_mb > 100:  # For files over 100MB
                timeout = aiohttp.ClientTimeout(
                    total=None,           # No total timeout for large files
                    sock_read=30,         # Timeout if no data received for 30 seconds
                    sock_connect=10,      # Connection timeout
                )
            else:  # For smaller files
                timeout = aiohttp.ClientTimeout(
                    total=max(60, file_size_mb * 2),  # At least 60 seconds, or 2 seconds per MB
                    sock_connect=10,      # Connection timeout
                )
            
            # Send as raw binary data with Content-Type header
            headers = {'Content-Type': 'application/octet-stream'}
            
            async with session.post(url, data=file_data, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    result['success'] = True
                    elapsed = time.time() - start_time
                    avg_speed = (file_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                    logger.info(f"✓ {device_id} ({ip}): {filename} uploaded successfully in {elapsed:.1f}s ({avg_speed:.2f} MB/s avg)")
                else:
                    result['error'] = f"HTTP {response.status}"
                    error_text = await response.text()
                    logger.error(f"✗ {device_id} ({ip}): Upload failed with HTTP {response.status}: {error_text}")
                    
        except asyncio.TimeoutError:
            result['error'] = 'Timeout'
            logger.error(f"✗ {device_id} ({ip}): Upload timeout (file too large or connection too slow)")
        except FileNotFoundError:
            result['error'] = 'File not found'
            logger.error(f"✗ Local file not found: {file_path}")
        except MemoryError:
            result['error'] = 'File too large to load into memory'
            logger.error(f"✗ {device_id} ({ip}): File too large to load into memory ({file_size_mb:.2f} MB)")
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"✗ {device_id} ({ip}): Error uploading {filename}: {e}")
            
        return result
    
    async def delete_file(self, session: aiohttp.ClientSession, device: Dict[str, Any], 
                         filename: str) -> Dict[str, Any]:
        """
        Delete a file from a device.
        
        Args:
            session: aiohttp session
            device: Device information
            filename: Name of the file to delete
            
        Returns:
            Delete result
        """
        ip = device['ip_address']
        device_id = device.get('id', 'UNKNOWN')
        
        result = {
            'device_id': device_id,
            'ip_address': ip,
            'filename': filename,
            'success': False,
            'error': None
        }
        
        try:
            # Use the new delete endpoint: DELETE /api/file/delete
            url = f"http://{ip}/api/file/delete"
            
            async with session.delete(url, json={'filename': filename},
                                     timeout=aiohttp.ClientTimeout(total=self.timeout)) as response:
                if response.status == 200:
                    result['success'] = True
                    logger.info(f"✓ {device_id} ({ip}): {filename} deleted successfully")
                elif response.status == 404:
                    # Could be file not found or endpoint not found
                    error_text = await response.text()
                    try:
                        error_json = json.loads(error_text)
                        if 'error' in error_json and 'not found' in error_json['error'].lower():
                            result['error'] = 'File not found'
                            logger.error(f"✗ {device_id} ({ip}): File '{filename}' not found on device")
                        else:
                            result['error'] = 'Delete endpoint not found'
                            logger.error(f"✗ {device_id} ({ip}): Delete endpoint not available")
                    except:
                        result['error'] = 'Delete endpoint or file not found'
                        logger.error(f"✗ {device_id} ({ip}): HTTP 404 - endpoint or file not found")
                else:
                    result['error'] = f"HTTP {response.status}"
                    error_text = await response.text()
                    logger.error(f"✗ {device_id} ({ip}): Delete failed with HTTP {response.status}: {error_text}")
                    
        except asyncio.TimeoutError:
            result['error'] = 'Timeout'
            logger.error(f"✗ {device_id} ({ip}): Timeout deleting {filename}")
        except Exception as e:
            result['error'] = str(e)
            logger.error(f"✗ {device_id} ({ip}): Error deleting {filename}: {e}")
            
        return result
    
    async def process_devices_batch(self, devices: List[Dict[str, Any]], operation, *args, **kwargs):
        """
        Process operations on devices in batches with concurrency control.
        
        Args:
            devices: List of devices
            operation: Async function to execute
            *args, **kwargs: Arguments for the operation
            
        Returns:
            List of results
        """
        results = []
        
        # Create connector with concurrency limit
        connector = aiohttp.TCPConnector(limit=self.concurrent_limit, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Process in batches
            batch_size = self.concurrent_limit
            for i in range(0, len(devices), batch_size):
                batch = devices[i:i + batch_size]
                tasks = [operation(session, device, *args, **kwargs) for device in batch]
                batch_results = await asyncio.gather(*tasks)
                results.extend(batch_results)
                
        return results
    
    async def list_all_files(self, devices: List[Dict[str, Any]]) -> None:
        """List files on all devices."""
        logger.info("=" * 70)
        logger.info("FILE LISTING")
        logger.info("=" * 70)
        
        results = await self.process_devices_batch(devices, self.list_files)
        
        for result in results:
            device_id = result['device_id']
            ip = result['ip_address']
            
            if result['success']:
                files = result['files']
                print(f"\n{device_id} ({ip}):")
                if files:
                    for file_info in files:
                        name = file_info.get('name', 'UNKNOWN')
                        file_type = file_info.get('type', '').upper()
                        size = file_info.get('size', 0)
                        if size > 0:
                            size_mb = size / (1024 * 1024)
                            print(f"  - {name:<40} {file_type:<5} {size_mb:>8.2f} MB")
                        else:
                            print(f"  - {name:<40} {file_type:<5}")
                else:
                    print("  (No files)")
            else:
                print(f"\n{device_id} ({ip}): ERROR - {result.get('error', 'Unknown error')}")
        
        # Summary
        success_count = sum(1 for r in results if r['success'])
        total_files = sum(len(r['files']) for r in results if r['success'])
        print(f"\nSummary: {success_count}/{len(results)} devices responded, {total_files} total files")
    
    async def upload_to_devices(self, devices: List[Dict[str, Any]], file_path: Path, 
                               skip_existing: bool = True, target_filename: Optional[str] = None) -> None:
        """
        Upload a file to multiple devices.
        
        Args:
            devices: List of devices
            file_path: Path to the file
            skip_existing: Skip if file already exists
            target_filename: Optional custom filename for the uploaded file
        """
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            sys.exit(1)
        
        file_size_mb = file_path.stat().st_size / (1024 * 1024)
        upload_name = target_filename if target_filename else file_path.name
        logger.info(f"Uploading {file_path.name} as '{upload_name}' ({file_size_mb:.2f} MB) to {len(devices)} device(s)")
        
        results = await self.process_devices_batch(devices, self.upload_file, file_path, skip_existing, target_filename)
        
        # Summary
        success_count = sum(1 for r in results if r['success'])
        skipped_count = sum(1 for r in results if r.get('skipped', False))
        failed_count = len(results) - success_count
        
        logger.info("=" * 60)
        logger.info(f"Upload complete: {success_count}/{len(results)} successful")
        if skipped_count > 0:
            logger.info(f"  - {skipped_count} skipped (already exists)")
        if failed_count > 0:
            logger.info(f"  - {failed_count} failed")
    
    async def sync_directory(self, devices: List[Dict[str, Any]], directory: Path) -> None:
        """
        Sync all audio files from a directory to devices.
        
        Args:
            devices: List of devices
            directory: Directory containing files to sync
        """
        if not directory.exists() or not directory.is_dir():
            logger.error(f"Directory not found: {directory}")
            sys.exit(1)
        
        # Find all audio files
        audio_extensions = {'.wav', '.mp3', '.m4a', '.aac', '.flac'}
        audio_files = [f for f in directory.iterdir() 
                      if f.is_file() and f.suffix.lower() in audio_extensions]
        
        if not audio_files:
            logger.warning(f"No audio files found in {directory}")
            return
        
        logger.info(f"Found {len(audio_files)} audio file(s) to sync")
        
        # Upload each file
        for file_path in sorted(audio_files):
            logger.info(f"\nSyncing {file_path.name}...")
            await self.upload_to_devices(devices, file_path, skip_existing=True)
    
    async def delete_from_devices(self, devices: List[Dict[str, Any]], filename: str) -> None:
        """
        Delete a file from multiple devices.
        
        Args:
            devices: List of devices
            filename: Name of the file to delete
        """
        logger.info(f"Deleting {filename} from {len(devices)} device(s)")
        
        results = await self.process_devices_batch(devices, self.delete_file, filename)
        
        # Summary
        success_count = sum(1 for r in results if r['success'])
        failed_count = len(results) - success_count
        
        logger.info("=" * 60)
        logger.info(f"Delete complete: {success_count}/{len(results)} successful")
        
        if failed_count > 0:
            logger.info(f"  - {failed_count} failed")
            # Check if failures are due to file not found vs other errors
            not_found_count = sum(1 for r in results if not r['success'] and 'not found' in r.get('error', '').lower())
            if not_found_count > 0:
                logger.info(f"  - {not_found_count} devices reported file not found")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='file_manager',
        description='ESP32 File Manager - Manage audio files on ESP32 devices',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # List files on all devices
    %(prog)s --command list
    
    # List files on a specific device
    %(prog)s --command list --id LOUDFRAME-001
    
    # Upload a file to all devices
    %(prog)s --command upload --file music.wav
    
    # Upload a file to a specific device
    %(prog)s --command upload --file music.wav --id LOUDFRAME-001
    
    # Upload a file with a different name on the device
    %(prog)s --command upload --file music.wav --target-name loop1.wav
    
    # Upload without checking if file exists (force overwrite)
    %(prog)s --command upload --file music.wav --force
    
    # Sync all audio files from a directory
    %(prog)s --command sync --directory ./loops
    
    # Delete a file from all devices (requires ESP32 delete endpoint)
    %(prog)s --command delete --file old_music.wav

Commands:
    list    - List files on devices
    upload  - Upload a file to devices
    sync    - Sync files from a directory to all devices
    delete  - Delete a file from devices (Note: ESP32 must support delete endpoint)
"""
    )
    
    # Required arguments
    required = parser.add_argument_group('required arguments')
    required.add_argument('--command', '-c',
                         required=True,
                         choices=['list', 'upload', 'sync', 'delete'],
                         help='Command to execute')
    
    # Optional arguments
    optional = parser.add_argument_group('optional arguments')
    optional.add_argument('--map-file', '-m',
                         default='device_map.json',
                         metavar='PATH',
                         help='Path to device map JSON file (default: device_map.json)')
    
    optional.add_argument('--timeout', '-t',
                         type=int, 
                         default=30,
                         metavar='SEC',
                         help='Request timeout in seconds (default: 30)')
    
    optional.add_argument('--concurrent', '-n',
                         type=int, 
                         default=5,
                         metavar='NUM',
                         help='Maximum concurrent uploads (default: 5)')
    
    # Target device
    target_group = parser.add_argument_group('target selection')
    target_group.add_argument('--id', '-i',
                             dest='device_id',
                             metavar='ID',
                             help='Specific device ID (default: all devices)')
    
    # File operations
    file_group = parser.add_argument_group('file operations')
    file_group.add_argument('--file', '-f',
                           metavar='PATH',
                           help='File to upload or delete')
    
    file_group.add_argument('--directory', '-d',
                           metavar='PATH',
                           help='Directory for sync operation')
    
    file_group.add_argument('--force', '-F',
                           action='store_true',
                           help='Force upload even if file exists')
    
    file_group.add_argument('--target-name', '-r',
                           metavar='NAME',
                           help='Custom filename for uploaded file (default: use original filename)')
    
    args = parser.parse_args()
    
    # Create file manager
    manager = FileManager(
        map_file=args.map_file,
        timeout=args.timeout,
        concurrent_limit=args.concurrent
    )
    
    # Load devices
    devices = manager.load_device_map(args.device_id)
    if not devices:
        logger.error("No devices to manage")
        sys.exit(1)
    
    # Execute command
    try:
        if args.command == 'list':
            asyncio.run(manager.list_all_files(devices))
            
        elif args.command == 'upload':
            if not args.file:
                logger.error("File path required (use --file)")
                sys.exit(1)
            file_path = Path(args.file)
            asyncio.run(manager.upload_to_devices(devices, file_path, 
                                                 skip_existing=not args.force,
                                                 target_filename=args.target_name))
            
        elif args.command == 'sync':
            if not args.directory:
                logger.error("Directory path required (use --directory)")
                sys.exit(1)
            directory = Path(args.directory)
            asyncio.run(manager.sync_directory(devices, directory))
            
        elif args.command == 'delete':
            if not args.file:
                logger.error("Filename required (use --file)")
                sys.exit(1)
            # Extract just the filename if a path was provided
            filename = Path(args.file).name
            asyncio.run(manager.delete_from_devices(devices, filename))
            
    except KeyboardInterrupt:
        logger.info("\nOperation interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
