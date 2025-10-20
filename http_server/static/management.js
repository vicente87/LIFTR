// static/management.js

// 🚨 CONFIGURACIÓN: AJUSTA ESTAS CONSTANTES 🚨
const BASE_URL = 'http://127.0.0.1:8080'; // URL de tu servidor Flask
const ADMIN_USER = 'admin';
const ADMIN_PASS = '1234';

// Genera la cabecera Basic Auth. El valor debe ser "Basic [base64(user:pass)]"
const AUTH_HEADER = 'Basic ' + btoa(`${ADMIN_USER}:${ADMIN_PASS}`);

document.getElementById('base-url').textContent = BASE_URL;
const logElement = document.getElementById('output-log');

// =========================================================
// 1. UTILIDADES Y LOGGING
// =========================================================

/** Muestra un mensaje en el área de log */
function log(message, isError = false) {
    const timestamp = new Date().toLocaleTimeString();
    logElement.textContent = `[${timestamp}] ${message}\n` + logElement.textContent;
    if (isError) {
        logElement.style.color = '#ff0000';
    } else {
        logElement.style.color = '#00ff00';
    }
}

/** Realiza una petición genérica a la API con autenticación */
async function apiRequest(endpoint, method = 'GET', body = null, isMultipart = false) {
    const url = BASE_URL + endpoint;
    const headers = {
        'Authorization': AUTH_HEADER,
    };

    if (!isMultipart && body && method !== 'GET') {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(body);
    }

    try {
        log(`Petición: ${method} ${endpoint}`, false);
        
        const response = await fetch(url, {
            method: method,
            headers: isMultipart ? { 'Authorization': AUTH_HEADER } : headers,
            body: body
        });

        // Manejo de errores de HTTP (4xx, 5xx)
        if (!response.ok) {
            const errorText = await response.text();
            log(`Error ${response.status} en la API: ${errorText}`, true);
            return null;
        }

        // Intenta parsear el JSON si el cuerpo no está vacío
        const contentType = response.headers.get("content-type");
        if (contentType && contentType.indexOf("application/json") !== -1) {
            const jsonResponse = await response.json();
            log(`Respuesta de ${endpoint}: ${JSON.stringify(jsonResponse, null, 2)}`);
            return jsonResponse;
        } else {
            const textResponse = await response.text();
            log(`Respuesta de ${endpoint}: ${textResponse}`);
            return textResponse;
        }
    } catch (error) {
        log(`Error fatal de red/fetch: ${error.message}`, true);
        return null;
    }
}

// =========================================================
// 2. FUNCIONES DE ADMINISTRACIÓN (LLAMADAS API)
// =========================================================

/** Muestra el estado de CPU y RAM del servidor */
async function getServerStatus() {
    const status = await apiRequest('/admin/status');
    if (status) {
        alert(`Estado del Servidor:\nUptime: ${status.uptime}\nCPU: ${status.cpu_percent}%\nRAM: ${status.ram_percent}%`);
    }
}

/** Obtiene y renderiza la lista de funciones */
async function listFunctions() {
    const functions = await apiRequest('/admin/functions');
    const tbody = document.getElementById('functions-table').querySelector('tbody');
    tbody.innerHTML = ''; // Limpiar lista

    if (!functions || Object.keys(functions).length === 0) {
        tbody.innerHTML = '<tr><td colspan="3">No hay funciones desplegadas.</td></tr>';
        return;
    }

    for (const name in functions) {
        const funcData = functions[name];
        const row = tbody.insertRow();
        row.insertCell().textContent = name;
        row.insertCell().textContent = funcData.created_at;

        const actionCell = row.insertCell();
        actionCell.innerHTML = `
            <button class="action-btn" onclick="promptInvoke('${name}')">Invocar</button>
            <button class="action-btn delete-btn" onclick="deleteFunction('${name}')">Eliminar</button>
        `;
    }
}

/** Elimina una función */
async function deleteFunction(funcName) {
    if (confirm(`¿Estás seguro de que quieres eliminar la función "${funcName}"?`)) {
        await apiRequest(`/admin/functions/${funcName}`, 'DELETE');
        listFunctions(); // Recargar lista
    }
}

/** Invoca una función (pide argumentos al usuario) */
function promptInvoke(funcName) {
    const args = prompt(`Invocar ${funcName}. Ingresa los argumentos como array JSON:\nEj: [10, 5, "test"]`, '[]');
    if (args !== null) {
        try {
            const argsArray = JSON.parse(args);
            // El servidor TinyFaaS espera el formato {"args": [..., ...]}
            invokeFunction(funcName, { args: argsArray });
        } catch (e) {
            log(`Error al parsear JSON de argumentos: ${e.message}`, true);
        }
    }
}

/** Ejecuta la función de TinyFaaS */
async function invokeFunction(funcName, payload) {
    await apiRequest(`/function/${funcName}`, 'POST', payload, false);
}

// =========================================================
// 3. GESTIÓN DE SUBIDA (UPLOAD)
// =========================================================

document.getElementById('upload-form').addEventListener('submit', async function(e) {
    e.preventDefault();

    const form = e.target;
    const formData = new FormData();
    
    // 1. Agregar nombre de la función (Campo 'name')
    const funcName = form['name'].value;
    formData.append('name', funcName);

    // 2. Agregar código (Campo 'code')
    const codeFile = form['code'].files[0];
    if (codeFile) {
        // En multipart/form-data, se usa append(campo, archivo, nombre_archivo)
        formData.append('code', codeFile, 'func.py'); 
    }

    // 3. Agregar requisitos (Campo 'requirements') - Opcional
    const reqsFile = form['requirements'].files[0];
    if (reqsFile) {
        formData.append('requirements', reqsFile, 'requirements.txt');
    }

    log(`Iniciando subida de la función: ${funcName}...`, false);

    // La cabecera Content-Type para multipart/form-data la añade Fetch automáticamente
    // al usar un objeto FormData, ¡no se debe incluir manualmente!
    const response = await apiRequest('/admin/upload', 'POST', formData, true);

    if (response) {
        log(`Función "${funcName}" desplegada con éxito.`, false);
        form.reset();
        listFunctions();
    } else {
        log(`Fallo al desplegar la función: ${funcName}`, true);
    }
});
