/**
 * Main application JavaScript for Tart VM Manager
 */

document.addEventListener('DOMContentLoaded', () => {
    // Initialize the application
    initApp();
});

/**
 * Initialize the application
 */
function initApp() {
    // Add any global event listeners or initialization code here
    console.log('Tart VM Manager initialized');
    
    // Example: Add a global click handler for data-action attributes
    document.body.addEventListener('click', (e) => {
        const actionElement = e.target.closest('[data-action]');
        if (!actionElement) return;
        
        const action = actionElement.getAttribute('data-action');
        const vmName = actionElement.closest('[data-vm]')?.dataset.vm;
        
        if (action === 'refresh') {
            refreshVMs();
        } else if (vmName) {
            handleVMAction(vmName, action);
        }
    });
    
    // Initial data load
    refreshVMs();
}

/**
 * Refresh the list of VMs
 */
async function refreshVMs() {
    try {
        showLoading();
        const vms = await api.get('/api/vms');
        renderVMs(vms);
    } catch (error) {
        console.error('Error refreshing VMs:', error);
        showToast('Failed to refresh VMs', 'error');
    } finally {
        hideLoading();
    }
}

/**
 * Render the list of VMs
 * @param {Array} vms - Array of VM objects
 */
function renderVMs(vms) {
    const vmsList = document.getElementById('vms-list');
    const vmsEmpty = document.getElementById('vms-empty');
    const vmsContainer = document.getElementById('vms-container');
    
    if (!vms || vms.length === 0) {
        vmsContainer.classList.add('hidden');
        vmsEmpty.classList.remove('hidden');
        return;
    }
    
    vmsList.innerHTML = vms.map(vm => `
        <tr class="hover:bg-gray-50" data-vm="${vm.name}">
            <td class="px-6 py-4 whitespace-nowrap">
                <div class="flex items-center">
                    <div class="flex-shrink-0 h-10 w-10 flex items-center justify-center rounded-full bg-indigo-100">
                        <i class="fas fa-server text-indigo-600"></i>
                    </div>
                    <div class="ml-4">
                        <div class="text-sm font-medium text-gray-900">${escapeHtml(vm.name)}</div>
                        <div class="text-sm text-gray-500">${vm.source || 'Local VM'}</div>
                    </div>
                </div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
                ${getStatusBadge(vm.status)}
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                ${vm.ip_address ? `<span class="font-mono">${vm.ip_address}</span>` : 'N/A'}
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
                <div class="text-sm text-gray-900">${vm.cpu || 'N/A'}</div>
                <div class="text-sm text-gray-500">CPU</div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap">
                <div class="text-sm text-gray-900">${vm.memory || 'N/A'}</div>
                <div class="text-sm text-gray-500">Memory</div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                <div class="flex justify-end space-x-2">
                    <button 
                        class="text-indigo-600 hover:text-indigo-900 disabled:opacity-50 disabled:cursor-not-allowed" 
                        data-action="start"
                        ${vm.status === 'running' ? 'disabled' : ''}
                        title="Start VM"
                    >
                        <i class="fas fa-play"></i>
                    </button>
                    <button 
                        class="text-yellow-600 hover:text-yellow-900 disabled:opacity-50 disabled:cursor-not-allowed" 
                        data-action="stop"
                        ${vm.status !== 'running' ? 'disabled' : ''}
                        title="Stop VM"
                    >
                        <i class="fas fa-stop"></i>
                    </button>
                    <button 
                        class="text-red-600 hover:text-red-900" 
                        data-action="delete"
                        title="Delete VM"
                    >
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </td>
        </tr>
    `).join('');
    
    vmsContainer.classList.remove('hidden');
    vmsEmpty.classList.add('hidden');
}

/**
 * Handle VM actions (start, stop, delete)
 * @param {string} vmName - Name of the VM
 * @param {string} action - Action to perform (start, stop, delete)
 */
async function handleVMAction(vmName, action) {
    try {
        let endpoint = '';
        let actionText = '';
        
        switch (action) {
            case 'start':
                if (!confirm(`Start VM ${vmName}?`)) return;
                endpoint = `/api/vms/${encodeURIComponent(vmName)}/start`;
                actionText = 'Starting';
                break;
                
            case 'stop':
                if (!confirm(`Stop VM ${vmName}?`)) return;
                endpoint = `/api/vms/${encodeURIComponent(vmName)}/stop`;
                actionText = 'Stopping';
                break;
                
            case 'delete':
                if (!confirm(`Are you sure you want to delete ${vmName}? This action cannot be undone.`)) {
                    return;
                }
                endpoint = `/api/vms/${encodeURIComponent(vmName)}`;
                actionText = 'Deleting';
                break;
                
            default:
                console.error('Unknown action:', action);
                return;
        }
        
        showToast(`${actionText} VM ${vmName}...`, 'info');
        const result = await api.post(endpoint, {});
        
        if (result.task_id) {
            showTaskLogs(result.task_id, `${actionText} VM ${vmName}`);
        } else {
            showToast(`VM ${vmName} ${action}ed successfully`, 'success');
            refreshVMs();
        }
        
    } catch (error) {
        console.error(`Error ${action}ing VM:`, error);
        showToast(`Failed to ${action} VM: ${error.message}`, 'error');
    }
}

/**
 * Show task logs in a modal
 * @param {string} taskId - Task ID to show logs for
 * @param {string} title - Title for the modal
 */
function showTaskLogs(taskId, title) {
    const modal = document.getElementById('task-logs-modal');
    const modalTitle = document.getElementById('task-logs-title');
    const modalLogs = document.getElementById('task-logs-content');
    
    // Set modal title
    modalTitle.textContent = title;
    modalLogs.innerHTML = '<div class="p-4 text-gray-500">Loading logs...</div>';
    
    // Show the modal
    modal.classList.remove('hidden');
    
    // Connect to WebSocket for real-time logs
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/tasks/${taskId}`;
    const ws = new WebSocket(wsUrl);
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        // Update logs
        if (data.logs && data.logs.length > 0) {
            modalLogs.innerHTML = data.logs
                .map(log => `<div class="p-1 font-mono text-sm">${escapeHtml(log)}</div>`)
                .join('');
            modalLogs.scrollTop = modalLogs.scrollHeight;
        }
        
        // Handle task completion
        if (data.status === 'completed' || data.status === 'failed') {
            // Close the modal after a short delay if successful
            if (data.status === 'completed') {
                setTimeout(() => {
                    modal.classList.add('hidden');
                    refreshVMs();
                }, 2000);
            }
        }
    };
    
    ws.onclose = () => {
        console.log('WebSocket connection closed');
    };
    
    // Close button handler
    const closeButton = document.getElementById('close-task-logs');
    closeButton.onclick = () => {
        ws.close();
        modal.classList.add('hidden');
        refreshVMs();
    };
}

/**
 * Show a toast notification
 * @param {string} message - Message to display
 * @param {string} type - Type of notification (success, error, info)
 */
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `fixed bottom-4 right-4 p-4 rounded-md shadow-lg ${
        type === 'error' ? 'bg-red-100 text-red-800' :
        type === 'success' ? 'bg-green-100 text-green-800' :
        'bg-blue-100 text-blue-800'
    }`;
    
    toast.innerHTML = `
        <div class="flex items-center">
            <i class="fas ${
                type === 'error' ? 'fa-exclamation-circle' :
                type === 'success' ? 'fa-check-circle' :
                'fa-info-circle'
            } mr-2"></i>
            <span>${escapeHtml(message)}</span>
        </div>
    `;
    
    document.body.appendChild(toast);
    
    // Auto-remove after 5 seconds
    setTimeout(() => {
        toast.remove();
    }, 5000);
}

/**
 * Show loading state
 */
function showLoading() {
    const loading = document.getElementById('loading');
    if (loading) loading.classList.remove('hidden');
}

/**
 * Hide loading state
 */
function hideLoading() {
    const loading = document.getElementById('loading');
    if (loading) loading.classList.add('hidden');
}

/**
 * Get status badge HTML
 * @param {string} status - VM status
 * @returns {string} HTML for the status badge
 */
function getStatusBadge(status) {
    const statusMap = {
        'running': { text: 'Running', color: 'green' },
        'stopped': { text: 'Stopped', color: 'red' },
        'paused': { text: 'Paused', color: 'yellow' },
        'error': { text: 'Error', color: 'red' }
    };
    
    const statusInfo = statusMap[status] || { text: 'Unknown', color: 'gray' };
    
    return `
        <span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full 
            bg-${statusInfo.color}-100 text-${statusInfo.color}-800">
            ${statusInfo.text}
        </span>
    `;
}

/**
 * Escape HTML to prevent XSS
 * @param {string} text - Text to escape
 * @returns {string} Escaped text
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
