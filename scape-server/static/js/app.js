// Scape Device Manager Server Frontend Application

// Global variables
let socket = null;
let devices = [];
let selectedDevices = new Set();
let currentDevice = null;
let autoScanActive = false;
let autoRefreshInterval = null;
let autoRefreshActive = true;
let refreshIntervalSeconds = 10;  // Default, will be loaded from config
let modalRefreshInterval = null;  // For refreshing modal data
let modalRefreshActive = false;  // Track if modal is open

// Initialize the application
document.addEventListener('DOMContentLoaded', function() {
    initializeSocket();
    attachEventListeners();
    loadDevices();
    startAutoRefresh();  // Start auto-refresh timer
});

// Initialize Socket.IO connection
function initializeSocket() {
    socket = io();
    
    socket.on('connect', function() {
        console.log('Connected to server');
        updateConnectionStatus(true);
    });
    
    socket.on('disconnect', function() {
        console.log('Disconnected from server');
        updateConnectionStatus(false);
    });
    
    socket.on('connected', function(data) {
        console.log(data.message);
    });
    
    socket.on('scan_progress', function(data) {
        console.log('[WEBSOCKET] scan_progress event received:', data);
        updateScanProgress(data.percent);
    });
    
    socket.on('scanning_network', function(data) {
        console.log('[WEBSOCKET] scanning_network event received:', data);
        updateNetworkInfo(data.network, data.current, data.total);
    });
    
    socket.on('scan_complete', function(data) {
        console.log('[WEBSOCKET] scan_complete event received:', data);
        console.log(`Scan complete: found ${data.count} devices`);
        hideScanProgress();
        hideError();
        loadDevices();
    });
    
    socket.on('scan_error', function(data) {
        console.error('Scan error:', data.error);
        hideScanProgress();
        showError(data.message || 'Network scan failed');
    });
    
    socket.on('devices_update', function(data) {
        console.log('Devices updated');
        updateDeviceList(data.devices);
    });
    
    socket.on('auto_scan_started', function() {
        autoScanActive = true;
        document.getElementById('autoScanBtn').textContent = 'Stop Auto Scan';
    });
    
    socket.on('auto_scan_stopped', function() {
        autoScanActive = false;
        document.getElementById('autoScanBtn').textContent = 'Start Auto Scan';
    });
}

// Attach event listeners to UI elements
function attachEventListeners() {
    // Main controls
    document.getElementById('scanBtn').addEventListener('click', startNetworkScan);
    document.getElementById('autoScanBtn').addEventListener('click', toggleAutoScan);
    document.getElementById('refreshBtn').addEventListener('click', loadDevices);
    document.getElementById('configBtn').addEventListener('click', openNetworkConfig);
    document.getElementById('deleteAllBtn').addEventListener('click', deleteAllDevices);
    
    // Batch controls
    document.getElementById('selectAllBtn').addEventListener('click', selectAllDevices);
    document.getElementById('deselectAllBtn').addEventListener('click', deselectAllDevices);
    document.getElementById('batchPlayBtn').addEventListener('click', () => batchControlPlayback('play'));
    document.getElementById('batchStopBtn').addEventListener('click', () => batchControlPlayback('stop'));
    document.getElementById('batchVolumeBtn').addEventListener('click', batchSetVolume);
    document.getElementById('batchSaveConfigBtn').addEventListener('click', batchSaveConfig);
    document.getElementById('batchRebootBtn').addEventListener('click', batchReboot);
    
    // Volume slider
    const batchVolume = document.getElementById('batchVolume');
    const batchVolumeValue = document.getElementById('batchVolumeValue');
    batchVolume.addEventListener('input', function() {
        batchVolumeValue.textContent = this.value + '%';
    });
    
    // Modal controls
    document.querySelector('.close').addEventListener('click', closeModal);
    document.getElementById('modalStartBtn').addEventListener('click', () => controlDevicePlayback('start'));
    document.getElementById('modalStopBtn').addEventListener('click', () => controlDevicePlayback('stop'));
    document.getElementById('modalSaveConfigBtn').addEventListener('click', saveDeviceConfig);
    document.getElementById('modalRebootBtn').addEventListener('click', rebootDevice);
    document.getElementById('modalFilesBtn').addEventListener('click', loadDeviceFiles);
    
    const modalVolume = document.getElementById('modalVolume');
    const modalVolumeValue = document.getElementById('modalVolumeValue');
    modalVolume.addEventListener('input', function() {
        modalVolumeValue.textContent = this.value;
        setDeviceGlobalVolume(this.value);
    });
    
    // Network config modal controls
    document.querySelector('.close-network').addEventListener('click', closeNetworkModal);
    document.getElementById('saveNetworkConfig').addEventListener('click', saveNetworkConfig);
    document.getElementById('cancelNetworkConfig').addEventListener('click', closeNetworkModal);
    
    // Close modal when clicking outside
    window.addEventListener('click', function(event) {
        const deviceModal = document.getElementById('deviceModal');
        const networkModal = document.getElementById('networkModal');
        if (event.target === deviceModal) {
            closeModal();
        } else if (event.target === networkModal) {
            closeNetworkModal();
        }
    });
}

// Update connection status indicator
function updateConnectionStatus(connected) {
    const indicator = document.querySelector('.status-indicator');
    const text = document.querySelector('.status-text');
    
    if (connected) {
        indicator.classList.add('connected');
        text.textContent = 'Connected';
    } else {
        indicator.classList.remove('connected');
        text.textContent = 'Disconnected';
    }
}

// Load devices from server
async function loadDevices() {
    console.log(`[AUTO-REFRESH] Loading devices at ${new Date().toLocaleTimeString()}`);
    try {
        const response = await fetch('/api/devices');
        const data = await response.json();
        
        devices = data.devices;
        updateDeviceList(devices);
        updateStatusBar(data.count, data.online);
        
        // Update refresh indicator
        updateRefreshIndicator();
        
    } catch (error) {
        console.error('Error loading devices:', error);
    }
}

// Start auto-refresh timer
function startAutoRefresh() {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
    }
    
    console.log(`[AUTO-REFRESH] Starting auto-refresh every ${refreshIntervalSeconds} seconds`);
    autoRefreshInterval = setInterval(() => {
        if (autoRefreshActive) {
            console.log(`[AUTO-REFRESH] Triggering refresh at ${new Date().toLocaleTimeString()}`);
            loadDevices();
        }
    }, refreshIntervalSeconds * 1000);
}

// Stop auto-refresh
function stopAutoRefresh() {
    if (autoRefreshInterval) {
        console.log('[AUTO-REFRESH] Stopping auto-refresh');
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
}

// Toggle auto-refresh
function toggleAutoRefresh() {
    autoRefreshActive = !autoRefreshActive;
    console.log(`[AUTO-REFRESH] Auto-refresh ${autoRefreshActive ? 'enabled' : 'disabled'}`);
    updateRefreshIndicator();
}

// Update refresh indicator
function updateRefreshIndicator() {
    const statusText = document.querySelector('.status-text');
    if (statusText) {
        const currentText = statusText.textContent;
        if (autoRefreshActive) {
            // Add refresh indicator if not present
            if (!currentText.includes('🔄')) {
                statusText.textContent = currentText + ' 🔄';
            }
        } else {
            // Remove refresh indicator
            statusText.textContent = currentText.replace(' 🔄', '');
        }
    }
}

// Update device list in UI
function updateDeviceList(deviceList) {
    const container = document.getElementById('deviceList');
    
    if (deviceList.length === 0) {
        container.innerHTML = '<div class="loading"><p>No devices found. Click "Scan Network" to discover devices.</p></div>';
        return;
    }
    
    container.innerHTML = '';
    
    deviceList.forEach(device => {
        const card = createDeviceCard(device);
        container.appendChild(card);
    });
    
    devices = deviceList;
}

// Create device card element
function createDeviceCard(device) {
    const card = document.createElement('div');
    card.className = `device-card ${device.status === 'offline' ? 'offline' : ''}`;
    
    const deviceId = device.id || device.ip;
    
    // Build loop information display
    let loopInfo = '';
    if (device.loops && device.loops.length > 0) {
        loopInfo = '<div class="loop-info">';
        loopInfo += '<div class="loop-header">Loops:</div>';
        device.loops.forEach(loop => {
            const status = loop.playing ? '▶' : '■';
            const statusClass = loop.playing ? 'playing' : 'stopped';
            const filename = loop.filename || 'No file';
            loopInfo += `
                <div class="loop-item ${statusClass}">
                    <span class="loop-status">${status}</span>
                    <span class="loop-track">T${loop.track}:</span>
                    <span class="loop-volume">${loop.volume}%</span>
                    <span class="loop-file" title="${filename}">${filename}</span>
                </div>
            `;
        });
        loopInfo += '</div>';
    }
    
    card.innerHTML = `
        <div class="device-select">
            <input type="checkbox" id="select-${deviceId}" data-device-id="${deviceId}">
        </div>
        <div class="device-header">
            <div class="device-id">${deviceId}</div>
            <div class="device-status ${device.status}">${device.status.toUpperCase()}</div>
        </div>
        <div class="device-info-grid">
            <div class="device-info-item">
                <span class="device-info-label">IP:</span>
                <span class="device-info-value">${device.ip}</span>
            </div>
            <div class="device-info-item">
                <span class="device-info-label">Global Volume:</span>
                <span class="device-info-value">${device.global_volume || device.volume || 0}%</span>
            </div>
            <div class="device-info-item">
                <span class="device-info-label">Active Loops:</span>
                <span class="device-info-value">${device.active_loops || 0}/3 ${device.playing ? '(Playing)' : '(Stopped)'}</span>
            </div>
        </div>
        ${loopInfo}
    `;
    
    // Add click handler for the card (excluding checkbox)
    card.addEventListener('click', function(e) {
        // Don't navigate if clicking checkbox
        if (e.target.type !== 'checkbox') {
            // Open device detail page in same tab
            window.location.href = `/device/${deviceId}`;
        }
    });
    
    // Add checkbox handler
    const checkbox = card.querySelector(`#select-${deviceId}`);
    checkbox.addEventListener('change', function() {
        if (this.checked) {
            selectedDevices.add(deviceId);
        } else {
            selectedDevices.delete(deviceId);
        }
        updateSelectedCount();
    });
    
    // Restore checkbox state if device was selected
    if (selectedDevices.has(deviceId)) {
        checkbox.checked = true;
    }
    
    return card;
}

// Update status bar
function updateStatusBar(total, online) {
    document.getElementById('deviceCount').textContent = `Devices: ${total}`;
    document.getElementById('onlineCount').textContent = `Online: ${online}`;
}

// Start network scan
async function startNetworkScan() {
    try {
        showScanProgress();
        hideError();
        const response = await fetch('/api/scan', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (!response.ok) {
            throw new Error('Failed to start scan');
        }
        
        const data = await response.json();
        console.log(data.message);
    } catch (error) {
        console.error('Error starting scan:', error);
        hideScanProgress();
        showError('Failed to start network scan. Check console for details.');
    }
}

// Toggle auto scan
function toggleAutoScan() {
    if (autoScanActive) {
        socket.emit('stop_auto_scan');
    } else {
        socket.emit('start_auto_scan');
    }
}

// Show scan progress
function showScanProgress() {
    const progressBar = document.getElementById('scanProgress');
    progressBar.style.display = 'block';
}

// Hide scan progress
function hideScanProgress() {
    const progressBar = document.getElementById('scanProgress');
    progressBar.style.display = 'none';
    const fill = progressBar.querySelector('.progress-fill');
    const text = progressBar.querySelector('.progress-text');
    const networkInfo = progressBar.querySelector('.scan-network-info');
    
    fill.style.width = '0%';
    text.textContent = 'Scanning...';
    if (networkInfo) {
        networkInfo.textContent = '';
    }
}

// Update scan progress
function updateScanProgress(percent) {
    const progressBar = document.getElementById('scanProgress');
    const fill = progressBar.querySelector('.progress-fill');
    const text = progressBar.querySelector('.progress-text');
    
    // Ensure progress bar is visible
    progressBar.style.display = 'block';
    
    fill.style.width = `${percent}%`;
    text.textContent = `Scanning... ${Math.round(percent)}%`;
}

// Update network info during scan
function updateNetworkInfo(network, current, total) {
    const progressBar = document.getElementById('scanProgress');
    const networkInfo = progressBar.querySelector('.scan-network-info');
    
    // Ensure progress bar is visible
    progressBar.style.display = 'block';
    
    if (networkInfo) {
        networkInfo.textContent = `Scanning network ${current}/${total}: ${network}`;
        networkInfo.style.display = 'block';
        networkInfo.style.marginTop = '10px';
        networkInfo.style.fontSize = '14px';
        networkInfo.style.color = '#666';
        networkInfo.style.fontWeight = '500';
    }
}

// Select all devices
function selectAllDevices() {
    devices.forEach(device => {
        const deviceId = device.id || device.ip;
        selectedDevices.add(deviceId);
        const checkbox = document.querySelector(`#select-${deviceId}`);
        if (checkbox) {
            checkbox.checked = true;
        }
    });
    updateSelectedCount();
}

// Deselect all devices
function deselectAllDevices() {
    selectedDevices.clear();
    document.querySelectorAll('.device-select input[type="checkbox"]').forEach(checkbox => {
        checkbox.checked = false;
    });
    updateSelectedCount();
}

// Update selected device count display
function updateSelectedCount() {
    const count = selectedDevices.size;
    const countDisplay = document.getElementById('selectedCount');
    if (countDisplay) {
        countDisplay.textContent = `${count} device${count !== 1 ? 's' : ''} selected`;
    }
}

// Batch control playback
async function batchControlPlayback(action) {
    // If no devices selected, use all devices
    let targetDevices = [];
    if (selectedDevices.size === 0) {
        // No selection means apply to all devices
        targetDevices = devices.map(d => d.id || d.ip);
        console.log(`No devices selected, applying ${action} to all ${targetDevices.length} devices`);
    } else {
        targetDevices = Array.from(selectedDevices);
        console.log(`Applying ${action} to ${targetDevices.length} selected devices`);
    }
    
    if (targetDevices.length === 0) {
        alert('No devices available');
        return;
    }
    
    try {
        const response = await fetch('/api/batch/play', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: targetDevices,
                action: action
            })
        });
        const data = await response.json();
        console.log('Batch control results:', data.results);
        showSuccess(`${action === 'play' ? 'Started' : 'Stopped'} ${targetDevices.length} device(s)`);
        setTimeout(loadDevices, 1000);
    } catch (error) {
        console.error('Error controlling devices:', error);
        showError('Failed to control devices');
    }
}

// Batch set volume
async function batchSetVolume() {
    // If no devices selected, use all devices
    let targetDevices = [];
    if (selectedDevices.size === 0) {
        // No selection means apply to all devices
        targetDevices = devices.map(d => d.id || d.ip);
        console.log(`No devices selected, setting volume for all ${targetDevices.length} devices`);
    } else {
        targetDevices = Array.from(selectedDevices);
        console.log(`Setting volume for ${targetDevices.length} selected devices`);
    }
    
    if (targetDevices.length === 0) {
        alert('No devices available');
        return;
    }
    
    const volume = document.getElementById('batchVolume').value;
    
    try {
        const response = await fetch('/api/batch/volume', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: targetDevices,
                volume: parseInt(volume)
            })
        });
        const data = await response.json();
        console.log('Batch volume results:', data.results);
        showSuccess(`Volume set to ${volume}% for ${targetDevices.length} device(s)`);
        setTimeout(loadDevices, 1000);
    } catch (error) {
        console.error('Error setting volume:', error);
        showError('Failed to set volume');
    }
}

// Store current device IP for direct API calls
let currentDeviceIP = null;

// Open device modal
async function openDeviceModal(device) {
    currentDevice = device.id || device.ip;
    currentDeviceIP = device.ip;  // Store IP for API calls
    
    // Update modal header
    document.getElementById('modalDeviceId').textContent = `Device: ${currentDevice}`;
    
    // Update device info with accurate data
    const ipLink = document.getElementById('modalIp');
    ipLink.textContent = device.ip;
    ipLink.href = `http://${device.ip}`;
    
    // MAC address should NEVER be unknown - it's the fundamental device identifier
    const macAddress = device.mac_address;
    if (!macAddress || macAddress === 'Unknown') {
        console.error(`WARNING: Device ${device.id || device.ip} has no MAC address! This should never happen.`);
        document.getElementById('modalMac').textContent = 'ERROR: No MAC';
        document.getElementById('modalMac').style.color = 'red';
    } else {
        document.getElementById('modalMac').textContent = macAddress;
        document.getElementById('modalMac').style.color = '';
    }
    document.getElementById('modalStatus').textContent = device.status;
    document.getElementById('modalSsid').textContent = device.ssid || 'Unknown';
    
    // Update volume with global volume (accurate)
    const volumeSlider = document.getElementById('modalVolume');
    const volumeValue = document.getElementById('modalVolumeValue');
    const actualVolume = device.global_volume || device.volume || 0;
    volumeSlider.value = actualVolume;
    volumeValue.textContent = actualVolume;
    
    // Display loop information if available
    const loopsSection = document.getElementById('loopsSection');
    const loopsConfig = document.getElementById('loopsConfig');
    
    if (device.loops && device.loops.length > 0) {
        // Build loop display similar to summary card
        let loopsHTML = '<div class="modal-loops-list">';
        loopsHTML += '<h4>Track Configuration</h4>';
        
        device.loops.forEach(loop => {
            const status = loop.playing ? '▶' : '■';
            const statusClass = loop.playing ? 'playing' : 'stopped';
            const filename = loop.filename || 'No file';
            
            loopsHTML += `
                <div class="modal-loop-item ${statusClass}" data-track="${loop.track}">
                    <span class="loop-status">${status}</span>
                    <span class="loop-track">Track ${loop.track}:</span>
                    <span class="loop-volume">Vol: ${loop.volume}%</span>
                    <span class="loop-file" title="${filename}">${filename}</span>
                </div>
            `;
        });
        
        loopsHTML += '</div>';
        loopsConfig.innerHTML = loopsHTML;
        loopsSection.style.display = 'block';
    } else {
        loopsSection.style.display = 'none';
    }
    
    // Reset files section
    document.getElementById('filesSection').style.display = 'none';
    
    // Show modal
    document.getElementById('deviceModal').style.display = 'flex';
    
    // Load fresh device data including loops
    try {
        const response = await fetch(`/api/device/${currentDevice}/loops`);
        if (response.ok) {
            const loopData = await response.json();
            
            // Update with fresh loop data
            if (loopData.loops) {
                let loopsHTML = '<div class="modal-loops-list">';
                loopsHTML += '<h4>Track Configuration</h4>';
                
                loopData.loops.forEach(loop => {
                    const statusClass = loop.playing ? 'playing' : 'stopped';
                    const filename = loop.file ? loop.file.split('/').pop() : 'No file';
                    
                    loopsHTML += `
                        <div class="modal-loop-item ${statusClass}" data-track="${loop.track}">
                            <div class="track-controls">
                                <button class="track-play-btn ${statusClass}" data-track="${loop.track}" data-playing="${loop.playing}" 
                                        title="${loop.playing ? 'Stop' : 'Start'} Track ${loop.track}">
                                    ${loop.playing ? '■' : '▶'}
                                </button>
                                <span class="loop-track">Track ${loop.track}:</span>
                                <input type="range" class="track-volume-slider" data-track="${loop.track}" 
                                       min="0" max="100" value="${loop.volume}">
                                <span class="track-volume-value">${loop.volume}%</span>
                                <span class="loop-file clickable" data-track="${loop.track}" 
                                      title="Click to change file">
                                    ${filename}
                                </span>
                            </div>
                        </div>
                    `;
                });
                
                loopsHTML += '</div>';
                loopsConfig.innerHTML = loopsHTML;
                loopsSection.style.display = 'block';
                
                // Attach event handlers to track controls
                attachTrackControlHandlers();
                
                // Update global volume from fresh data
                const freshVolume = loopData.global_volume || 0;
                volumeSlider.value = freshVolume;
                volumeValue.textContent = freshVolume;
            }
        }
    } catch (error) {
        console.error('Error loading device loop details:', error);
    }
    
    // Start auto-refresh for the modal
    startModalRefresh();
}

// Close modal
function closeModal() {
    document.getElementById('deviceModal').style.display = 'none';
    currentDevice = null;
    stopModalRefresh();  // Stop refreshing when modal closes
}

// Start modal auto-refresh
function startModalRefresh() {
    if (modalRefreshInterval) {
        clearInterval(modalRefreshInterval);
    }
    
    modalRefreshActive = true;
    console.log('[MODAL-REFRESH] Starting modal auto-refresh every 2 seconds');
    
    // Refresh modal data every 2 seconds (more frequent than main page)
    modalRefreshInterval = setInterval(() => {
        if (modalRefreshActive && currentDevice) {
            console.log(`[MODAL-REFRESH] Refreshing modal data for ${currentDevice}`);
            refreshModalData();
        }
    }, 2000);  // Refresh every 2 seconds for responsive updates
}

// Stop modal auto-refresh
function stopModalRefresh() {
    modalRefreshActive = false;
    if (modalRefreshInterval) {
        console.log('[MODAL-REFRESH] Stopping modal auto-refresh');
        clearInterval(modalRefreshInterval);
        modalRefreshInterval = null;
    }
}

// Refresh modal data (device status and loops)
async function refreshModalData() {
    if (!currentDevice) return;
    
    try {
        // First check if device is still online
        const deviceResponse = await fetch(`/api/device/${currentDevice}`);
        if (deviceResponse.ok) {
            const deviceData = await deviceResponse.json();
            
            // Update device status in modal
            document.getElementById('modalStatus').textContent = deviceData.status;
            
            // If device is offline, update UI accordingly
            if (deviceData.status === 'offline') {
                console.log(`[MODAL-REFRESH] Device ${currentDevice} is offline`);
                // Clear loops display or show offline message
                const loopsConfig = document.getElementById('loopsConfig');
                if (loopsConfig) {
                    loopsConfig.innerHTML = '<p style="color: #dc3545;">Device is offline</p>';
                }
                return;  // Don't try to fetch loops if offline
            }
            
            // Update MAC and SSID if available
            if (deviceData.mac_address) {
                document.getElementById('modalMac').textContent = deviceData.mac_address;
                document.getElementById('modalMac').style.color = '';
            }
            if (deviceData.ssid) {
                document.getElementById('modalSsid').textContent = deviceData.ssid;
            }
        }
        
        // Get fresh loop data - same as refreshLoopData but without recursion
        const loopsResponse = await fetch(`/api/device/${currentDevice}/loops`);
        if (loopsResponse.ok) {
            const loopData = await loopsResponse.json();
            
            // Update loop display
            const loopsConfig = document.getElementById('loopsConfig');
            const loopsSection = document.getElementById('loopsSection');
            
            if (loopData.loops && loopsConfig) {
                let loopsHTML = '<div class="modal-loops-list">';
                loopsHTML += '<h4>Track Configuration</h4>';
                
                loopData.loops.forEach(loop => {
                    const statusClass = loop.playing ? 'playing' : 'stopped';
                    const filename = loop.file ? loop.file.split('/').pop() : 'No file';
                    
                    loopsHTML += `
                        <div class="modal-loop-item ${statusClass}" data-track="${loop.track}">
                            <div class="track-controls">
                                <button class="track-play-btn ${statusClass}" data-track="${loop.track}" data-playing="${loop.playing}" 
                                        title="${loop.playing ? 'Stop' : 'Start'} Track ${loop.track}">
                                    ${loop.playing ? '■' : '▶'}
                                </button>
                                <span class="loop-track">Track ${loop.track}:</span>
                                <input type="range" class="track-volume-slider" data-track="${loop.track}" 
                                       min="0" max="100" value="${loop.volume}">
                                <span class="track-volume-value">${loop.volume}%</span>
                                <span class="loop-file clickable" data-track="${loop.track}" 
                                      title="Click to change file">
                                    ${filename}
                                </span>
                            </div>
                        </div>
                    `;
                });
                
                loopsHTML += '</div>';
                loopsConfig.innerHTML = loopsHTML;
                loopsSection.style.display = 'block';
                
                // Re-attach event handlers
                attachTrackControlHandlers();
                
                // Update global volume
                const volumeSlider = document.getElementById('modalVolume');
                const volumeValue = document.getElementById('modalVolumeValue');
                if (volumeSlider && volumeValue) {
                    const freshVolume = loopData.global_volume || 0;
                    // Only update if value actually changed to avoid disrupting user interaction
                    if (Math.abs(volumeSlider.value - freshVolume) > 1) {
                        volumeSlider.value = freshVolume;
                        volumeValue.textContent = freshVolume;
                    }
                }
            }
        }
    } catch (error) {
        console.error('[MODAL-REFRESH] Error refreshing modal data:', error);
    }
}

// Control device playback - start/stop all tracks
async function controlDevicePlayback(action) {
    if (!currentDevice) return;
    
    // Use batch endpoint but with single device to avoid CORS issues
    try {
        const response = await fetch('/api/batch/play', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: [currentDevice],
                action: action === 'start' ? 'play' : 'stop'
            })
        });
        
        const data = await response.json();
        
        // Check if successful for this device
        let success = false;
        if (data.results && data.results.length > 0) {
            success = data.results[0].status === 'success';
        }
        
        if (success) {
            showSuccess(`Device ${action === 'start' ? 'started' : 'stopped'}`);
        } else {
            showError(`Failed to ${action} device`);
        }
        setTimeout(loadDevices, 1000);
    } catch (error) {
        console.error(`Error controlling device:`, error);
        showError(`Error controlling device`);
    }
}

// Set device global volume
async function setDeviceGlobalVolume(volume) {
    if (!currentDevice) return;
    
    // Use batch endpoint but with single device to avoid CORS issues
    try {
        const response = await fetch('/api/batch/volume', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: [currentDevice],
                volume: parseInt(volume)
            })
        });
        
        if (response.ok) {
            console.log(`Global volume set to ${volume}%`);
        } else {
            console.error('Failed to set volume');
        }
    } catch (error) {
        console.error('Error setting volume:', error);
    }
}

// Save device configuration
async function saveDeviceConfig() {
    if (!currentDevice) return;
    
    // Use batch endpoint but with single device to avoid CORS issues
    try {
        const response = await fetch('/api/batch/save-config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: [currentDevice]
            })
        });
        
        const data = await response.json();
        
        // Check if successful for this device
        let success = false;
        if (data.results && data.results.length > 0) {
            success = data.results[0].status === 'success';
        }
        
        if (success) {
            showSuccess('Configuration saved on device');
        } else {
            showError('Failed to save configuration');
        }
    } catch (error) {
        console.error('Error saving configuration:', error);
        showError('Error saving configuration');
    }
}

// Reboot device
async function rebootDevice() {
    if (!currentDevice) return;
    
    // Use batch endpoint but with single device to avoid CORS issues
    try {
        const response = await fetch('/api/batch/reboot', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: [currentDevice],
                delay_ms: 1000
            })
        });
        
        const data = await response.json();
        
        // Check if successful for this device
        let success = false;
        if (data.results && data.results.length > 0) {
            success = data.results[0].status === 'success';
        }
        
        if (success) {
            showSuccess('Device reboot initiated');
            // Close modal and refresh after delay
            setTimeout(() => {
                closeModal();
                loadDevices();
            }, 2000);
        } else {
            showError('Failed to reboot device');
        }
    } catch (error) {
        console.error('Error rebooting device:', error);
        showError('Error rebooting device');
    }
}

// Load device files
async function loadDeviceFiles() {
    if (!currentDevice) return;
    
    const filesSection = document.getElementById('filesSection');
    const filesList = document.getElementById('filesList');
    
    filesList.innerHTML = '<p>Loading files...</p>';
    filesSection.style.display = 'block';
    
    try {
        const response = await fetch(`/api/device/${currentDevice}/files`);
        const data = await response.json();
        
        if (data.files && data.files.length > 0) {
            filesList.innerHTML = '';
            data.files.forEach(file => {
                const fileItem = document.createElement('div');
                fileItem.className = 'file-item';
                fileItem.innerHTML = `
                    <span>${file.name}</span>
                    <span>${formatFileSize(file.size)}</span>
                `;
                filesList.appendChild(fileItem);
            });
        } else {
            filesList.innerHTML = '<p>No files found on device</p>';
        }
    } catch (error) {
        console.error('Error loading files:', error);
        filesList.innerHTML = '<p>Error loading files</p>';
    }
}

// Load device loops configuration
async function loadDeviceLoops() {
    if (!currentDevice) return;
    
    const loopsSection = document.getElementById('loopsSection');
    const loopsConfig = document.getElementById('loopsConfig');
    
    loopsConfig.innerHTML = '<p>Loading loop configuration...</p>';
    loopsSection.style.display = 'block';
    
    try {
        const response = await fetch(`/api/device/${currentDevice}/loops`);
        const data = await response.json();
        
        // Display loop configuration (customize based on your actual loop data structure)
        loopsConfig.innerHTML = `
            <p>Loop configuration loaded</p>
            <pre>${JSON.stringify(data, null, 2)}</pre>
        `;
    } catch (error) {
        console.error('Error loading loops:', error);
        loopsConfig.innerHTML = '<p>Error loading loop configuration</p>';
    }
}

// Utility function to format file size
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

// Network Configuration Functions

async function openNetworkConfig() {
    console.log('Opening network configuration');
    
    // Show modal
    document.getElementById('networkModal').style.display = 'flex';
    
    // Load current configuration
    try {
        const configResponse = await fetch('/api/network/config');
        const config = await configResponse.json();
        
        // Set timeout and concurrent limit
        document.getElementById('scanTimeout').value = config.timeout || 2;
        document.getElementById('probeTimeout').value = config.probe_timeout || 0.5;
        document.getElementById('refreshInterval').value = config.refresh_interval || 10;
        document.getElementById('concurrentLimit').value = config.concurrent_limit || 50;
        
        // Update the refresh interval if config has changed
        if (config.refresh_interval && config.refresh_interval !== refreshIntervalSeconds) {
            refreshIntervalSeconds = config.refresh_interval;
            console.log(`[CONFIG] Refresh interval updated to ${refreshIntervalSeconds} seconds`);
            // Restart auto-refresh with new interval
            if (autoRefreshActive) {
                startAutoRefresh();
            }
        }
        
        // Load interfaces
        const interfacesResponse = await fetch('/api/network/interfaces');
        const interfacesData = await interfacesResponse.json();
        
        displayInterfaces(interfacesData.interfaces, config.selected_interfaces || []);
        
    } catch (error) {
        console.error('Error loading network configuration:', error);
    }
}

function displayInterfaces(interfaces, selectedInterfaces) {
    const container = document.getElementById('interfacesList');
    
    if (interfaces.length === 0) {
        container.innerHTML = '<p>No network interfaces found</p>';
        return;
    }
    
    container.innerHTML = '';
    
    interfaces.forEach(iface => {
        const checked = selectedInterfaces.includes(iface.name) ? 'checked' : '';
        const ifaceDiv = document.createElement('div');
        ifaceDiv.className = 'interface-item';
        ifaceDiv.innerHTML = `
            <label style="display: block; padding: 10px; border: 1px solid #ddd; margin: 5px 0; border-radius: 5px;">
                <input type="checkbox" name="interface" value="${iface.name}" ${checked}>
                <strong>${iface.name}</strong><br>
                IP: ${iface.ip}<br>
                Network: ${iface.network}<br>
                Hosts: ${iface.host_count}
            </label>
        `;
        container.appendChild(ifaceDiv);
    });
}

async function saveNetworkConfig() {
    console.log('Saving network configuration');
    
    // DWIM: Gather selected interfaces
    const selectedInterfaces = [];
    document.querySelectorAll('input[name="interface"]:checked').forEach(checkbox => {
        selectedInterfaces.push(checkbox.value);
    });
    
    // DWIM logic: If no interfaces selected, scan all. If some selected, scan only those.
    const scanAll = selectedInterfaces.length === 0;
    
    console.log(`Network config: ${scanAll ? 'Scanning all networks' : `Scanning ${selectedInterfaces.length} selected interface(s)`}`);
    
    const config = {
        scan_all: scanAll,
        selected_interfaces: selectedInterfaces,
        timeout: parseInt(document.getElementById('scanTimeout').value),
        probe_timeout: parseFloat(document.getElementById('probeTimeout').value),
        refresh_interval: parseInt(document.getElementById('refreshInterval').value),
        concurrent_limit: parseInt(document.getElementById('concurrentLimit').value)
    };
    
    // Update local refresh interval
    refreshIntervalSeconds = config.refresh_interval;
    
    try {
        const response = await fetch('/api/network/config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(config)
        });
        
        if (response.ok) {
            const result = await response.json();
            console.log('Configuration saved:', result);
            
            // Show feedback message
            const statusText = config.scan_all ? 
                'Configuration saved: Scanning all networks' : 
                `Configuration saved: Scanning ${selectedInterfaces.length} selected network(s)`;
            showSuccess(statusText);
            
            closeNetworkModal();
        } else {
            alert('Failed to save configuration');
        }
    } catch (error) {
        console.error('Error saving configuration:', error);
        alert('Error saving configuration');
    }
}

function closeNetworkModal() {
    document.getElementById('networkModal').style.display = 'none';
}

// Delete all devices
async function deleteAllDevices() {
    if (!confirm('Are you sure you want to delete all devices from the registry? This will clear all device data.')) {
        return;
    }
    
    try {
        const response = await fetch('/api/devices/clear', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            }
        });
        
        if (response.ok) {
            const result = await response.json();
            console.log('Devices cleared:', result);
            loadDevices(); // Refresh the list
            showSuccess('All devices cleared successfully');
        } else {
            const error = await response.json();
            showError(error.message || 'Failed to clear devices');
        }
    } catch (error) {
        console.error('Error clearing devices:', error);
        showError('Error clearing devices');
    }
}

// Show error message
function showError(message) {
    const errorDiv = document.getElementById('scanError');
    const errorText = errorDiv.querySelector('.error-text');
    errorText.textContent = message;
    errorDiv.style.display = 'block';
    
    // Auto-hide after 10 seconds
    setTimeout(() => {
        errorDiv.style.display = 'none';
    }, 10000);
}

// Hide error message
function hideError() {
    const errorDiv = document.getElementById('scanError');
    errorDiv.style.display = 'none';
}

// Show success message (using error div with different styling)
function showSuccess(message) {
    const errorDiv = document.getElementById('scanError');
    const errorText = errorDiv.querySelector('.error-text');
    errorText.textContent = message;
    errorDiv.style.display = 'block';
    errorDiv.style.background = '#d4edda';
    errorDiv.style.color = '#155724';
    errorDiv.style.borderColor = '#c3e6cb';
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        errorDiv.style.display = 'none';
        // Reset to error styling
        errorDiv.style.background = '';
        errorDiv.style.color = '';
        errorDiv.style.borderColor = '';
    }, 5000);
}

// Batch save configuration
async function batchSaveConfig() {
    // If no devices selected, use all devices
    let targetDevices = [];
    if (selectedDevices.size === 0) {
        // No selection means apply to all devices
        targetDevices = devices.map(d => d.id || d.ip);
        console.log(`No devices selected, saving config for all ${targetDevices.length} devices`);
    } else {
        targetDevices = Array.from(selectedDevices);
        console.log(`Saving config for ${targetDevices.length} selected devices`);
    }
    
    if (targetDevices.length === 0) {
        alert('No devices available');
        return;
    }
    
    try {
        const response = await fetch('/api/batch/save-config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: targetDevices
            })
        });
        const data = await response.json();
        console.log('Batch save config results:', data.results);
        
        // Count successful saves
        let successCount = 0;
        data.results.forEach(result => {
            if (result.status === 'success') successCount++;
        });
        
        showSuccess(`Configuration saved on ${successCount}/${targetDevices.length} device(s)`);
    } catch (error) {
        console.error('Error saving configuration:', error);
        showError('Failed to save configuration');
    }
}

// Attach event handlers to track controls
function attachTrackControlHandlers() {
    // Track play/stop buttons
    document.querySelectorAll('.track-play-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const track = parseInt(this.dataset.track);
            const isPlaying = this.dataset.playing === 'true';
            await controlTrack(track, isPlaying ? 'stop' : 'start');
        });
    });
    
    // Track volume sliders
    document.querySelectorAll('.track-volume-slider').forEach(slider => {
        slider.addEventListener('input', async function() {
            const track = parseInt(this.dataset.track);
            const volume = parseInt(this.value);
            const valueDisplay = this.nextElementSibling;
            if (valueDisplay) {
                valueDisplay.textContent = `${volume}%`;
            }
            await setTrackVolume(track, volume);
        });
    });
    
    // Track file selection (click on filename)
    document.querySelectorAll('.loop-file.clickable').forEach(fileElement => {
        fileElement.addEventListener('click', async function(e) {
            e.stopPropagation();
            const track = parseInt(this.dataset.track);
            await selectTrackFile(track);
        });
    });
}

// Control individual track
async function controlTrack(track, action) {
    if (!currentDevice) return;
    
    try {
        const response = await fetch(`/api/device/${currentDevice}/track/control`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                track: track,
                action: action
            })
        });
        
        if (response.ok) {
            console.log(`Track ${track} ${action === 'start' ? 'started' : 'stopped'}`);
            // Refresh loop data to update UI
            await refreshLoopData();
        } else {
            console.error(`Failed to ${action} track ${track}`);
        }
    } catch (error) {
        console.error(`Error controlling track ${track}:`, error);
    }
}

// Set individual track volume
async function setTrackVolume(track, volume) {
    if (!currentDevice) return;
    
    try {
        const response = await fetch(`/api/device/${currentDevice}/track/volume`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                track: track,
                volume: volume
            })
        });
        
        if (response.ok) {
            console.log(`Track ${track} volume set to ${volume}%`);
        } else {
            console.error(`Failed to set volume for track ${track}`);
        }
    } catch (error) {
        console.error(`Error setting volume for track ${track}:`, error);
    }
}

// Select file for track
async function selectTrackFile(track) {
    if (!currentDevice) return;
    
    try {
        // First, get the list of available files
        const filesResponse = await fetch(`/api/device/${currentDevice}/files`);
        if (!filesResponse.ok) {
            showError('Failed to load files');
            return;
        }
        
        const filesData = await filesResponse.json();
        if (!filesData.files || filesData.files.length === 0) {
            alert('No audio files found on device');
            return;
        }
        
        // Create a simple selection dialog
        let fileList = 'Select a file for Track ' + track + ':\n\n';
        filesData.files.forEach((file, index) => {
            fileList += `${index + 1}. ${file.name}\n`;
        });
        fileList += '\nEnter file number (or 0 to cancel):';
        
        const selection = prompt(fileList);
        if (!selection || selection === '0') {
            return;
        }
        
        const fileIndex = parseInt(selection) - 1;
        if (fileIndex < 0 || fileIndex >= filesData.files.length) {
            alert('Invalid selection');
            return;
        }
        
        // Set the selected file for the track
        const response = await fetch(`/api/device/${currentDevice}/track/file`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                track: track,
                file_index: fileIndex
            })
        });
        
        if (response.ok) {
            showSuccess(`File set for track ${track}`);
            // Refresh loop data to update UI
            await refreshLoopData();
        } else {
            showError(`Failed to set file for track ${track}`);
        }
    } catch (error) {
        console.error(`Error selecting file for track ${track}:`, error);
        showError('Error selecting file');
    }
}

// Refresh loop data and update UI
async function refreshLoopData() {
    if (!currentDevice) return;
    
    try {
        const response = await fetch(`/api/device/${currentDevice}/loops`);
        if (response.ok) {
            const loopData = await response.json();
            
            // Update loop display
            const loopsConfig = document.getElementById('loopsConfig');
            if (loopData.loops && loopsConfig) {
                let loopsHTML = '<div class="modal-loops-list">';
                loopsHTML += '<h4>Track Configuration</h4>';
                
                loopData.loops.forEach(loop => {
                    const statusClass = loop.playing ? 'playing' : 'stopped';
                    const filename = loop.file ? loop.file.split('/').pop() : 'No file';
                    
                    loopsHTML += `
                        <div class="modal-loop-item ${statusClass}" data-track="${loop.track}">
                            <div class="track-controls">
                                <button class="track-play-btn ${statusClass}" data-track="${loop.track}" data-playing="${loop.playing}" 
                                        title="${loop.playing ? 'Stop' : 'Start'} Track ${loop.track}">
                                    ${loop.playing ? '■' : '▶'}
                                </button>
                                <span class="loop-track">Track ${loop.track}:</span>
                                <input type="range" class="track-volume-slider" data-track="${loop.track}" 
                                       min="0" max="100" value="${loop.volume}">
                                <span class="track-volume-value">${loop.volume}%</span>
                                <span class="loop-file clickable" data-track="${loop.track}" 
                                      title="Click to change file">
                                    ${filename}
                                </span>
                            </div>
                        </div>
                    `;
                });
                
                loopsHTML += '</div>';
                loopsConfig.innerHTML = loopsHTML;
                
                // Re-attach event handlers
                attachTrackControlHandlers();
                
                // Update global volume
                const volumeSlider = document.getElementById('modalVolume');
                const volumeValue = document.getElementById('modalVolumeValue');
                if (volumeSlider && volumeValue) {
                    const freshVolume = loopData.global_volume || 0;
                    volumeSlider.value = freshVolume;
                    volumeValue.textContent = freshVolume;
                }
            }
        }
    } catch (error) {
        console.error('Error refreshing loop data:', error);
    }
}

// Batch reboot devices
async function batchReboot() {
    // If no devices selected, use all devices
    let targetDevices = [];
    if (selectedDevices.size === 0) {
        // No selection means apply to all devices
        targetDevices = devices.map(d => d.id || d.ip);
        console.log(`No devices selected, rebooting all ${targetDevices.length} devices`);
    } else {
        targetDevices = Array.from(selectedDevices);
        console.log(`Rebooting ${targetDevices.length} selected devices`);
    }
    
    if (targetDevices.length === 0) {
        alert('No devices available');
        return;
    }
    
    try {
        const response = await fetch('/api/batch/reboot', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_ids: targetDevices,
                delay_ms: 1000  // 1 second delay before reboot
            })
        });
        const data = await response.json();
        console.log('Batch reboot results:', data.results);
        
        // Count successful reboots
        let successCount = 0;
        data.results.forEach(result => {
            if (result.status === 'success') successCount++;
        });
        
        showSuccess(`Reboot initiated on ${successCount}/${targetDevices.length} device(s). Devices will be back online shortly.`);
        
        // Stop auto-refresh temporarily to avoid errors while devices reboot
        autoRefreshActive = false;
        setTimeout(() => {
            autoRefreshActive = true;
            loadDevices();  // Refresh to check which devices are back online
        }, 10000);  // Wait 10 seconds before resuming auto-refresh
        
    } catch (error) {
        console.error('Error rebooting devices:', error);
        showError('Failed to reboot devices');
    }
}
