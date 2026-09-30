/**
 * ===============================================================================
 * Wire Heating and Air - After-Hours Service Frontend
 * ===============================================================================
 *
 * Handles WebRTC connection to the after-hours service agent and displays
 * service requests in real-time.
 *
 * ===============================================================================
 */

// -------------------------------------------------------------------------------
// Global State
// -------------------------------------------------------------------------------

let client = null;
let roomSession = null;
let currentToken = null;
let currentDestination = null;
let isConnected = false;

// Voice settings
let afterhoursSettings = {
    voiceSelection: null  // Will be set after loading voices
};

// Load saved settings from localStorage
const savedSettings = localStorage.getItem('afterhoursSettings');
if (savedSettings) {
    try {
        afterhoursSettings = JSON.parse(savedSettings);
    } catch (e) {
        console.error('Error parsing afterhoursSettings:', e);
    }
}

// Load voices from JSON files and populate dropdown
async function loadVoices() {
    const voiceSelect = document.getElementById('voiceSelect');
    if (!voiceSelect) return;

    try {
        const [inworldResp, elevenlabsResp] = await Promise.all([
            fetch('/inworld_voices.json'),
            fetch('/elevenlabs_voices.json')
        ]);

        const inworldVoices = await inworldResp.json();
        const elevenlabsVoices = await elevenlabsResp.json();

        voiceSelect.innerHTML = '';

        const inworldGroup = document.createElement('optgroup');
        inworldGroup.label = 'Inworld';
        inworldVoices.forEach(voice => {
            const option = document.createElement('option');
            option.value = voice.voiceId;
            option.textContent = voice.displayName;
            option.title = voice.description;
            inworldGroup.appendChild(option);
        });
        voiceSelect.appendChild(inworldGroup);

        const elevenlabsGroup = document.createElement('optgroup');
        elevenlabsGroup.label = 'ElevenLabs';
        elevenlabsVoices.forEach(voice => {
            const option = document.createElement('option');
            option.value = voice.voiceId;
            option.textContent = voice.displayName;
            option.title = voice.description;
            elevenlabsGroup.appendChild(option);
        });
        voiceSelect.appendChild(elevenlabsGroup);

        if (!afterhoursSettings.voiceSelection && inworldVoices.length > 0) {
            afterhoursSettings.voiceSelection = inworldVoices[0].voiceId;
        }

        if (afterhoursSettings.voiceSelection) {
            voiceSelect.value = afterhoursSettings.voiceSelection;
        }

        voiceSelect.addEventListener('change', (e) => {
            afterhoursSettings.voiceSelection = e.target.value;
            localStorage.setItem('afterhoursSettings', JSON.stringify(afterhoursSettings));
            console.log('Voice saved:', afterhoursSettings);
        });

        console.log('Loaded voices:', inworldVoices.length + elevenlabsVoices.length);
    } catch (error) {
        console.error('Failed to load voices:', error);
        const option = document.createElement('option');
        option.value = 'inworld.Elizabeth:inworld-tts-1.5-max';
        option.textContent = 'Elizabeth (Default)';
        voiceSelect.appendChild(option);
        afterhoursSettings.voiceSelection = option.value;
    }
}

// Initialize voices on load
loadVoices();


// -------------------------------------------------------------------------------
// DOM Element References
// -------------------------------------------------------------------------------

const videoContainer = document.getElementById('video-container');
const connectBtn = document.getElementById('connect-btn');
const disconnectBtn = document.getElementById('disconnect-btn');
const statusEl = document.getElementById('status');
const eventLogEl = document.getElementById('event-log');
const requestsContainer = document.getElementById('requests-container');
const emergencyCountEl = document.getElementById('emergency-count');
const totalCountEl = document.getElementById('total-count');


// -------------------------------------------------------------------------------
// Connection Functions
// -------------------------------------------------------------------------------

async function connect() {
    if (isConnected) {
        logEvent('system', 'Already connected');
        return;
    }

    updateStatus('connecting', 'Getting token...');
    logEvent('system', 'Fetching authentication token...');

    try {
        const voiceParam = encodeURIComponent(afterhoursSettings.voiceSelection);
        const tokenResp = await fetch(`/get_token?voice=${voiceParam}`);
        const tokenData = await tokenResp.json();
        console.log('Selected voice:', afterhoursSettings.voiceSelection);

        if (tokenData.error) {
            throw new Error(tokenData.error);
        }

        currentToken = tokenData.token;
        currentDestination = tokenData.address;

        logEvent('system', `Token received, destination: ${currentDestination}`);
        updateStatus('connecting', 'Initializing client...');

        client = await window.SignalWire.SignalWire({
            token: currentToken,
            logLevel: 'debug'
        });

        logEvent('system', 'Client initialized');

        // Set up event listeners on the client
        client.on('user_event', (params) => {
            console.log('CLIENT EVENT: user_event', params);
            handleUserEvent(params);
        });

        client.on('calling.user_event', (params) => {
            console.log('CLIENT EVENT: calling.user_event', params);
            handleUserEvent(params);
        });

        client.on('signalwire.event', (params) => {
            console.log('CLIENT EVENT: signalwire.event', params);
            if (params.event_type === 'user_event') {
                handleUserEvent(params.params || params);
            }
        });

        updateStatus('connecting', 'Dialing agent...');

        roomSession = await client.dial({
            to: currentDestination,
            rootElement: videoContainer,
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            },
            video: false,   // camera OFF - receive-only
            negotiateVideo: true,
            userVariables: {
                userName: 'Web Client',
                interface: 'web-ui',
                timestamp: new Date().toISOString()
            }
        });

        logEvent('system', 'Call initiated, waiting for connection...');

        // Room session event listeners
        roomSession.on('user_event', (params) => {
            console.log('ROOM EVENT: user_event', params);
            handleUserEvent(params);
        });

        roomSession.on('room.joined', () => {
            logEvent('system', 'Connected to Wire Heating and Air');
            updateStatus('connected', 'Connected');
            isConnected = true;
            updateButtons();

            const placeholder = videoContainer.querySelector('.placeholder');
            if (placeholder) {
                placeholder.style.display = 'none';
            }
        });

        roomSession.on('room.left', () => {
            logEvent('system', 'Disconnected from agent');
            handleDisconnect();
        });

        roomSession.on('destroy', () => {
            logEvent('system', 'Session destroyed');
            handleDisconnect();
        });

        await roomSession.start();
        logEvent('system', 'Call started successfully');

    } catch (error) {
        console.error('Connection error:', error);
        logEvent('error', `Connection failed: ${error.message}`);
        updateStatus('error', 'Connection failed');
        handleDisconnect();
    }
}


async function disconnect() {
    if (!isConnected && !roomSession) {
        logEvent('system', 'Not connected');
        return;
    }

    logEvent('system', 'Disconnecting...');
    updateStatus('disconnecting', 'Disconnecting...');

    try {
        if (roomSession) {
            await roomSession.hangup();
        }
    } catch (error) {
        console.error('Disconnect error:', error);
    }

    handleDisconnect();
}


function handleDisconnect() {
    isConnected = false;
    roomSession = null;

    videoContainer.innerHTML = `
        <div class="placeholder">
            <div class="placeholder-content">
                <div class="placeholder-icon">HVAC</div>
                <p>Click Connect to speak with our after-hours service</p>
            </div>
        </div>
    `;

    updateStatus('disconnected', 'Disconnected');
    updateButtons();
}


// -------------------------------------------------------------------------------
// User Event Handling
// -------------------------------------------------------------------------------

function handleUserEvent(params) {
    console.log('Processing user event:', params);

    let eventData = params;
    if (params && params.params) {
        eventData = params.params;
    }
    if (params && params.event) {
        eventData = params.event;
    }

    if (!eventData || typeof eventData.type !== 'string') {
        console.log('Skipping non-application event:', params);
        return;
    }

    const internalTypes = ['room.joined', 'room.left', 'member.joined', 'member.left', 'playback.started', 'playback.ended'];
    if (internalTypes.includes(eventData.type)) {
        console.log('Skipping internal event type:', eventData.type);
        return;
    }

    const eventType = eventData.type;

    switch (eventType) {
        case 'request_submitted':
            const req = eventData.request;
            const urgency = req.is_emergency ? 'EMERGENCY' : 'Service';
            const issueType = req.issue_type === 'ac_repair' ? 'AC' : 'Heating';
            logEvent('request', `New ${urgency} ${issueType} request: ${req.customer_name} at ${req.service_address}`);
            refreshRequests();
            break;

        default:
            console.log('Unknown event type:', eventType, eventData);
            logEvent('event', `Event: ${eventType}`);
    }
}


// -------------------------------------------------------------------------------
// Service Requests Display
// -------------------------------------------------------------------------------

async function refreshRequests() {
    try {
        const resp = await fetch('/api/requests');
        const data = await resp.json();

        displayRequests(data.requests);
        updateStats(data);
    } catch (error) {
        console.error('Failed to fetch requests:', error);
        logEvent('error', 'Failed to fetch service requests');
    }
}


function displayRequests(requests) {
    const container = requestsContainer;

    if (!requests || requests.length === 0) {
        container.innerHTML = `
            <div class="no-requests">
                <p>No service requests yet.</p>
                <p class="hint">Connect to submit an after-hours service request.</p>
            </div>
        `;
        return;
    }

    let html = '';
    for (const req of requests) {
        const issueType = req.issue_type === 'ac_repair' ? 'AC Repair' : 'Heating Repair';
        const urgencyClass = req.is_emergency ? 'emergency' : '';
        const urgencyBadge = req.is_emergency ? '<span class="badge emergency">EMERGENCY</span>' : '<span class="badge">Service Request</span>';
        const ownershipText = req.ownership === 'rent' ? 'Renter' : 'Owner';
        const timeAgo = formatTimeAgo(req.created_at);

        html += `
            <div class="request-card ${urgencyClass}">
                <div class="request-header">
                    <div class="request-type">${issueType}</div>
                    ${urgencyBadge}
                </div>
                <div class="request-details">
                    <div class="request-customer">${escapeHtml(req.customer_name)}</div>
                    <div class="request-address">${escapeHtml(req.service_address)}</div>
                    <div class="request-meta">
                        <span class="request-phone">${escapeHtml(req.callback_primary)}</span>
                        <span class="request-ownership">${ownershipText}</span>
                    </div>
                    ${req.unit_info ? `<div class="request-unit">Unit: ${escapeHtml(req.unit_info)}</div>` : ''}
                    <div class="request-issue">${escapeHtml(req.issue_description)}</div>
                </div>
                <div class="request-footer">
                    <span class="request-id">${req.id}</span>
                    <span class="request-time">${timeAgo}</span>
                </div>
            </div>
        `;
    }

    container.innerHTML = html;
}


function updateStats(data) {
    emergencyCountEl.textContent = data.emergency_count || 0;
    totalCountEl.textContent = data.total_count || 0;
}


// -------------------------------------------------------------------------------
// UI Helper Functions
// -------------------------------------------------------------------------------

function updateStatus(state, text) {
    statusEl.className = `status ${state}`;
    statusEl.querySelector('.status-text').textContent = text;
}


function updateButtons() {
    connectBtn.disabled = isConnected;
    disconnectBtn.disabled = !isConnected;
}


function logEvent(type, message) {
    const entry = document.createElement('div');
    entry.className = `log-entry ${type}`;

    const timestamp = new Date().toLocaleTimeString();
    entry.innerHTML = `<span class="log-time">${timestamp}</span> ${escapeHtml(message)}`;

    eventLogEl.appendChild(entry);
    eventLogEl.scrollTop = eventLogEl.scrollHeight;

    // Keep only last 50 entries
    while (eventLogEl.children.length > 50) {
        eventLogEl.removeChild(eventLogEl.firstChild);
    }
}


function formatTimeAgo(dateStr) {
    const date = new Date(dateStr);
    const now = new Date();
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 60) return 'Just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return date.toLocaleDateString();
}


function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}


// -------------------------------------------------------------------------------
// Config Loading
// -------------------------------------------------------------------------------

async function loadConfig() {
    try {
        const resp = await fetch('/api/config');
        const config = await resp.json();

        if (config.phone_number) {
            const phoneDisplay = document.getElementById('phone-display');
            phoneDisplay.innerHTML = `Call us: <a href="tel:${config.phone_number}">${config.phone_number}</a>`;
            phoneDisplay.style.display = 'block';
        }
    } catch (error) {
        console.error('Failed to load config:', error);
    }
}


// -------------------------------------------------------------------------------
// Initialization
// -------------------------------------------------------------------------------

logEvent('system', 'Wire Heating and Air loaded');
logEvent('system', 'Ready to connect');

// Load config and requests on page load
loadConfig();
refreshRequests();
