// Device Detail Page JavaScript - Standalone version

// Global variables
let currentDevice = null;
let refreshInterval = null;
let volumeDebounceTimers = {};  // For debouncing volume changes
let lastVolumeValues = {};      // Track last sent values to avoid duplicates

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    if (!deviceInfo) {
        console.error('No device information available');
        return;
    }
    
    currentDevice = deviceInfo.id || deviceInfo.ip;
    console.log(`[DEVICE-DETAIL] Initializing for device: ${currentDevice}`);
    
    attachEventListeners();
    loadDeviceData();
    loadWiFiStatus();  // One-time WiFi status fetch
    startAutoRefresh();
});

// Attach event listeners
function attachEventListeners() {
    // Control buttons
    document.getElementById('startBtn').addEventListener('click', () => controlPlayback('start'));
    document.getElementById('stopBtn').addEventListener('click', () => controlPlayback('stop'));
    document.getElementById('saveConfigBtn').addEventListener('click', saveConfig);
    document.getElementById('rebootBtn').addEventListener('click', rebootDevice);
    document.getElementById('viewFilesBtn').addEventListener('click', toggleFiles);
    
    // Volume slider with throttling and release detection
    const volumeSlider = document.getElementById('globalVolume');
    const volumeValue = document.getElementById('globalVolumeValue');
    
    // Update display during drag
    volumeSlider.addEventListener('input', function() {
        volumeValue.textContent = this.value;
        setGlobalVolume(this.value, false);  // false = still dragging
    });
    
    // Send final value when user releases (mouse or touch)
    const sendFinalVolume = function(event) {
        const finalValue = volumeSlider.value;  // Get value from slider, not 'this'
        setGlobalVolume(finalValue, true);  // true = final value
    };
    
    // Bind all release events with debugging
    volumeSlider.addEventListener('mouseup', sendFinalVolume);
    volumeSlider.addEventListener('touchend', sendFinalVolume);
    volumeSlider.addEventListener('keyup', sendFinalVolume);
    
    // Also capture when slider loses focus
    volumeSlider.addEventListener('blur', function(event) {
        const finalValue = volumeSlider.value;
        setGlobalVolume(finalValue, true);
    });
}

// Load device data and loops
async function loadDeviceData() {
    if (!currentDevice) return;
    
    try {
        // Get device status
        const statusResponse = await fetch(`/api/device/${currentDevice}`);
        if (statusResponse.ok) {
            const deviceData = await statusResponse.json();
            updateDeviceInfo(deviceData);
        }
        
        // Get loops
        const loopsResponse = await fetch(`/api/device/${currentDevice}/loops`);
        if (loopsResponse.ok) {
            const loopData = await loopsResponse.json();
            updateLoops(loopData);
        }
    } catch (error) {
        console.error('[DEVICE-DETAIL] Error loading device data:', error);
        // Show clear error message if Flask server is down
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Please check if the server is running', 'error');
        }
    }
}

// Update device information display
function updateDeviceInfo(data) {
    document.getElementById('deviceStatus').textContent = data.status.toUpperCase();
    document.getElementById('deviceStatus').className = `device-status ${data.status}`;
    
    if (data.mac_address) {
        document.getElementById('deviceMac').textContent = data.mac_address;
    }
    if (data.firmware_version) {
        document.getElementById('deviceFirmware').textContent = data.firmware_version;
    }
    // Don't update SSID here - it's handled by loadWiFiStatus()
    if (data.uptime) {
        document.getElementById('deviceUptime').textContent = data.uptime;
    }
    
    // Handle offline status
    if (data.status === 'offline') {
        document.getElementById('loopsConfig').innerHTML = '<p style="color: #dc3545;">Device is offline</p>';
        document.getElementById('deviceUptime').textContent = 'N/A';
        disableControls(true);
    } else {
        disableControls(false);
    }
}

// Update loops display
function updateLoops(loopData) {
    const loopsConfig = document.getElementById('loopsConfig');
    
    if (!loopData.loops || loopData.loops.length === 0) {
        loopsConfig.innerHTML = '<p>No tracks configured</p>';
        return;
    }
    
    let loopsHTML = '<div class="modal-loops-list">';
    
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
    
    // Update global volume
    const volumeSlider = document.getElementById('globalVolume');
    const volumeValue = document.getElementById('globalVolumeValue');
    const globalVolume = loopData.global_volume || 0;
    
    // Only update if value changed significantly to avoid disrupting user
    if (Math.abs(volumeSlider.value - globalVolume) > 1) {
        volumeSlider.value = globalVolume;
        volumeValue.textContent = globalVolume;
    }
    
    // Attach track control handlers
    attachTrackHandlers();
}

// Attach handlers to track controls
function attachTrackHandlers() {
    // Track play/stop buttons
    document.querySelectorAll('.track-play-btn').forEach(btn => {
        btn.addEventListener('click', async function(e) {
            e.stopPropagation();
            const track = parseInt(this.dataset.track);
            const isPlaying = this.dataset.playing === 'true';
            await controlTrack(track, isPlaying ? 'stop' : 'start');
        });
    });
    
    // Track volume sliders with throttling and release detection
    document.querySelectorAll('.track-volume-slider').forEach(slider => {
        // Update during drag
        slider.addEventListener('input', async function() {
            const track = parseInt(this.dataset.track);
            const volume = parseInt(this.value);
            const valueDisplay = this.nextElementSibling;
            if (valueDisplay) {
                valueDisplay.textContent = `${volume}%`;
            }
            await setTrackVolume(track, volume, false);  // false = still dragging
        });
        
        // Send final value when released (mouse, touch, or keyboard)
        const sendFinalTrackVolume = async function(event) {
            const track = parseInt(slider.dataset.track);
            const volume = parseInt(slider.value);  // Get value from slider, not 'this'
            await setTrackVolume(track, volume, true);  // true = final value
        };
        
        // Bind all release events with debugging
        slider.addEventListener('mouseup', sendFinalTrackVolume);
        slider.addEventListener('touchend', sendFinalTrackVolume);
        slider.addEventListener('keyup', sendFinalTrackVolume);
        
        // Also capture when slider loses focus
        slider.addEventListener('blur', async function(event) {
            const track = parseInt(slider.dataset.track);
            const volume = parseInt(slider.value);
            await setTrackVolume(track, volume, true);
        });
    });
    
    // Track file selection
    document.querySelectorAll('.loop-file.clickable').forEach(fileElement => {
        fileElement.addEventListener('click', async function(e) {
            e.stopPropagation();
            const track = parseInt(this.dataset.track);
            await selectTrackFile(track);
        });
    });
}

// Control playback (start/stop all tracks)
async function controlPlayback(action) {
    try {
        const response = await fetch('/api/batch/play', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                device_ids: [currentDevice],
                action: action === 'start' ? 'play' : 'stop'
            })
        });
        
        if (response.ok) {
            showMessage(`Device ${action === 'start' ? 'started' : 'stopped'}`, 'success');
            setTimeout(loadDeviceData, 500);
        } else {
            showMessage(`Failed to ${action} device`, 'error');
        }
    } catch (error) {
        console.error('Error controlling device:', error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Cannot reach server', 'error');
        } else {
            showMessage('Error controlling device', 'error');
        }
    }
}

// Set global volume with throttling and immediate final value
async function setGlobalVolume(volume, isFinal = false) {
    const volumeKey = 'global';
    const volumeInt = parseInt(volume);
    
    // Reset auto-refresh timer to give server time to process the change
    resetAutoRefresh();
    
    // Clear any existing timer for this control
    if (volumeDebounceTimers[volumeKey]) {
        clearTimeout(volumeDebounceTimers[volumeKey]);
    }
    
    // If this is the final value (user released slider), send immediately
    if (isFinal) {
        // Cancel any pending request and send final value now
        const previousValue = lastVolumeValues[volumeKey];
        lastVolumeValues[volumeKey] = volumeInt;
        
        try {
            const response = await fetch('/api/batch/volume', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    device_ids: [currentDevice],
                    volume: volumeInt
                })
            });
            
            if (!response.ok) {
                console.error('Failed to set volume');
            }
        } catch (error) {
            console.error('Error setting volume:', error);
            if (error.message.includes('Failed to fetch')) {
                showMessage('Flask server error - Volume change not saved', 'error');
            }
        }
        return;
    }
    
    // Otherwise, throttle the requests during dragging
    volumeDebounceTimers[volumeKey] = setTimeout(async () => {
        // Only send if value actually changed
        if (lastVolumeValues[volumeKey] === volumeInt) {
            return;
        }
        
        const previousValue = lastVolumeValues[volumeKey];
        lastVolumeValues[volumeKey] = volumeInt;
        
        try {
            const response = await fetch('/api/batch/volume', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    device_ids: [currentDevice],
                    volume: volumeInt
                })
            });
            
            if (!response.ok) {
                console.error('Failed to set volume');
            }
        } catch (error) {
            console.error('Error setting volume:', error);
            if (error.message.includes('Failed to fetch')) {
                showMessage('Flask server error - Volume change not saved', 'error');
            }
        }
    }, 200);  // 200ms = max 5 requests per second while dragging
}

// Save configuration
async function saveConfig() {
    try {
        const response = await fetch('/api/batch/save-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                device_ids: [currentDevice]
            })
        });
        
        if (response.ok) {
            showMessage('Configuration saved', 'success');
        } else {
            showMessage('Failed to save configuration', 'error');
        }
    } catch (error) {
        console.error('Error saving configuration:', error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Cannot save configuration', 'error');
        } else {
            showMessage('Error saving configuration', 'error');
        }
    }
}

// Reboot device
async function rebootDevice() {
    try {
        const response = await fetch('/api/batch/reboot', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                device_ids: [currentDevice],
                delay_ms: 1000
            })
        });
        
        if (response.ok) {
            showMessage('Device reboot initiated', 'success');
            // Pause refresh during reboot
            stopAutoRefresh();
            setTimeout(() => {
                startAutoRefresh();
                loadDeviceData();
            }, 10000);
        } else {
            showMessage('Failed to reboot device', 'error');
        }
    } catch (error) {
        console.error('Error rebooting device:', error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server connection error - Cannot send reboot command', 'error');
        } else {
            showMessage('Error rebooting device', 'error');
        }
    }
}

// Control individual track
async function controlTrack(track, action) {
    try {
        const response = await fetch(`/api/device/${currentDevice}/track/control`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({track: track, action: action})
        });
        
        if (response.ok) {
            setTimeout(loadDeviceData, 500);
        }
    } catch (error) {
        console.error(`Error controlling track ${track}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot control track', 'error');
        }
    }
}

// Set track volume with throttling and immediate final value
async function setTrackVolume(track, volume, isFinal = false) {
    const volumeKey = `track-${track}`;
    const volumeInt = parseInt(volume);
    
    // Reset auto-refresh timer to give server time to process the change
    resetAutoRefresh();
    
    // Clear any existing timer for this track
    if (volumeDebounceTimers[volumeKey]) {
        clearTimeout(volumeDebounceTimers[volumeKey]);
    }
    
    // If this is the final value (user released slider), send immediately
    if (isFinal) {
        // Cancel any pending request and send final value now
        lastVolumeValues[volumeKey] = volumeInt;
        
        try {
            const response = await fetch(`/api/device/${currentDevice}/track/volume`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({track: track, volume: volumeInt})
            });
            
            if (!response.ok) {
                console.error(`Failed to set volume for track ${track}`);
            }
        } catch (error) {
            console.error(`Error setting volume for track ${track}:`, error);
            if (error.message.includes('Failed to fetch')) {
                showMessage('Flask server error - Track volume change not saved', 'error');
            }
        }
        return;
    }
    
    // Otherwise, throttle the requests during dragging
    volumeDebounceTimers[volumeKey] = setTimeout(async () => {
        // Only send if value actually changed
        if (lastVolumeValues[volumeKey] === volumeInt) {
            return;
        }
        
        lastVolumeValues[volumeKey] = volumeInt;
        
        try {
            const response = await fetch(`/api/device/${currentDevice}/track/volume`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({track: track, volume: volumeInt})
            });
            
            if (!response.ok) {
                console.error(`Failed to set volume for track ${track}`);
            }
        } catch (error) {
            console.error(`Error setting volume for track ${track}:`, error);
            if (error.message.includes('Failed to fetch')) {
                showMessage('Flask server error - Track volume change not saved', 'error');
            }
        }
    }, 200);  // 200ms = max 5 requests per second while dragging
}

// Global variable for track selection
let currentTrackForFileSelection = null;

// Select file for track - opens the file selection panel
async function selectTrackFile(track) {
    currentTrackForFileSelection = track;
    
    try {
        // Get available files
        const filesResponse = await fetch(`/api/device/${currentDevice}/files`);
        if (!filesResponse.ok) {
            showMessage('Failed to load files', 'error');
            return;
        }
        
        const filesData = await filesResponse.json();
        if (!filesData.files || filesData.files.length === 0) {
            showMessage('No audio files found on device', 'error');
            return;
        }
        
        // Show file selection panel
        showFileSelectionPanel(track, filesData.files);
        
    } catch (error) {
        console.error(`Error loading files for track ${track}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot load files', 'error');
        } else {
            showMessage('Error loading files', 'error');
        }
    }
}

// Show the file selection panel with clickable files
function showFileSelectionPanel(track, files) {
    // Update track label
    document.getElementById('fileSelectionTrack').textContent = `Track ${track}`;
    
    // Build file list with clickable items
    const fileListContainer = document.getElementById('fileSelectionList');
    fileListContainer.innerHTML = '';
    
    files.forEach((file, index) => {
        const fileItem = document.createElement('div');
        fileItem.style.cssText = `
            padding: 10px;
            margin: 5px 0;
            background: #f8f9fa;
            border-radius: 5px;
            cursor: pointer;
            transition: background 0.2s;
        `;
        fileItem.innerHTML = `
            <strong>${file.name}</strong>
            <span style="float: right; color: #666;">${formatFileSize(file.size)}</span>
        `;
        
        // Add hover effect
        fileItem.onmouseover = function() {
            this.style.background = '#e8f4f8';
        };
        fileItem.onmouseout = function() {
            this.style.background = '#f8f9fa';
        };
        
        // Add click handler
        fileItem.onclick = function() {
            selectFileForTrack(index, file.name);
        };
        
        fileListContainer.appendChild(fileItem);
    });
    
    // Show panel and overlay
    document.getElementById('fileSelectionPanel').style.display = 'block';
    document.getElementById('fileSelectionOverlay').style.display = 'block';
}

// Handle file selection
async function selectFileForTrack(fileIndex, fileName) {
    if (currentTrackForFileSelection === null) return;
    
    try {
        const response = await fetch(`/api/device/${currentDevice}/track/file`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                track: currentTrackForFileSelection, 
                file_index: fileIndex
            })
        });
        
        if (response.ok) {
            showMessage(`Set "${fileName}" for Track ${currentTrackForFileSelection}`, 'success');
            closeFileSelection();
            setTimeout(loadDeviceData, 500);
        } else {
            showMessage(`Failed to set file for track ${currentTrackForFileSelection}`, 'error');
        }
    } catch (error) {
        console.error(`Error setting file for track ${currentTrackForFileSelection}:`, error);
        if (error.message.includes('Failed to fetch')) {
            showMessage('Flask server error - Cannot set track file', 'error');
        } else {
            showMessage('Error setting file', 'error');
        }
    }
}

// Close file selection panel
window.closeFileSelection = function() {
    document.getElementById('fileSelectionPanel').style.display = 'none';
    document.getElementById('fileSelectionOverlay').style.display = 'none';
    currentTrackForFileSelection = null;
}

// Toggle files display
async function toggleFiles() {
    const filesSection = document.getElementById('filesSection');
    
    if (filesSection.style.display === 'none') {
        // Load and show files
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
            if (error.message.includes('Failed to fetch')) {
                filesList.innerHTML = '<p>Flask server error - Cannot load files</p>';
            } else {
                filesList.innerHTML = '<p>Error loading files</p>';
            }
        }
    } else {
        // Hide files
        filesSection.style.display = 'none';
    }
}

// Format file size
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

// Show message to user
function showMessage(message, type = 'info') {
    const messageArea = document.getElementById('messageArea');
    const messageText = messageArea.querySelector('.message-text');
    
    messageText.textContent = message;
    messageArea.style.display = 'block';
    
    // Set color based on type
    if (type === 'success') {
        messageArea.style.background = '#d4edda';
        messageArea.style.color = '#155724';
        messageArea.style.borderColor = '#c3e6cb';
    } else if (type === 'error') {
        messageArea.style.background = '#f8d7da';
        messageArea.style.color = '#721c24';
        messageArea.style.borderColor = '#f5c6cb';
    }
    
    // Auto-hide after 5 seconds
    setTimeout(() => {
        messageArea.style.display = 'none';
    }, 5000);
}

// Disable/enable controls
function disableControls(disabled) {
    document.getElementById('startBtn').disabled = disabled;
    document.getElementById('stopBtn').disabled = disabled;
    document.getElementById('saveConfigBtn').disabled = disabled;
    document.getElementById('rebootBtn').disabled = disabled;
    document.getElementById('globalVolume').disabled = disabled;
}

// Start auto-refresh
function startAutoRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }
    
    console.log('[DEVICE-DETAIL] Starting auto-refresh every 2 seconds');
    refreshInterval = setInterval(() => {
        loadDeviceData();
    }, 2000);
}

// Reset auto-refresh timer (delays next refresh by 2 seconds)
function resetAutoRefresh() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
        refreshInterval = setInterval(() => {
            loadDeviceData();
        }, 2000);
    }
}

// Stop auto-refresh
function stopAutoRefresh() {
    if (refreshInterval) {
        console.log('[DEVICE-DETAIL] Stopping auto-refresh');
        clearInterval(refreshInterval);
        refreshInterval = null;
    }
}

// Load WiFi status (one-time, not refreshed)
async function loadWiFiStatus() {
    if (!currentDevice || !deviceInfo.ip) return;
    
    try {
        const response = await fetch(`http://${deviceInfo.ip}/api/wifi/status`);
        
        if (response.ok) {
            const wifiData = await response.json();
            
            // Update SSID
            if (wifiData.ssid) {
                document.getElementById('deviceSsid').textContent = wifiData.ssid;
                
                // Add signal strength display if element exists
                const signalElement = document.getElementById('deviceSignal');
                if (signalElement) {
                    // Display signal strength percentage and RSSI
                    const signalPercent = wifiData.signal_strength || 0;
                    const rssi = wifiData.rssi || 0;
                    signalElement.textContent = `${signalPercent}% (${rssi} dBm)`;
                } else {
                    // If signal element doesn't exist, add it to SSID display
                    const ssidElement = document.getElementById('deviceSsid');
                    if (wifiData.signal_strength && wifiData.rssi) {
                        ssidElement.textContent = `${wifiData.ssid} (Signal: ${wifiData.signal_strength}%, ${wifiData.rssi} dBm)`;
                    }
                }
            }
        } else {
            console.error(`[WIFI-STATUS] Failed to fetch WiFi status: ${response.status}`);
        }
    } catch (error) {
        console.error('[WIFI-STATUS] Error fetching WiFi status:', error);
        // Don't show error to user - WiFi status is not critical
    }
}

// Clean up on page unload
window.addEventListener('beforeunload', () => {
    stopAutoRefresh();
});
