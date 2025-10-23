import os
import subprocess
import sys
import uuid
import json  
from flask import Flask, request, jsonify, Response, render_template
from functools import wraps
import shutil
import time
from datetime import datetime
import psutil
from flask_cors import CORS 
import traceback 
from pathlib import Path
import tempfile 
import atexit 
from dotenv import load_dotenv 
import threading 

# Cargar variables de entorno si existe un archivo .env
load_dotenv()

app = Flask(__name__)
CORS(app) 

# ========================================================
# 🔒 CONFIGURACIÓN DE SEGURIDAD (HTTP Basic Auth)
# ========================================================
USERNAME = os.environ.get("FAAS_USERNAME", "admin")
PASSWORD = os.environ.get("FAAS_PASSWORD", "1234")

def check_auth(username, password):
    return username == USERNAME and password == PASSWORD

def authenticate():
    return Response(
        "No autorizado. Ingresa credenciales válidas.\n",
        401,
        {"WWW-Authenticate": 'Basic realm=\"Login Required\"'}
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# ========================================================
# ⚙️ CONFIGURACIÓN DE PERSISTENCIA Y ENTORNO (FaaS + Contenedores)
# ========================================================
FUNCTIONS_DIR = Path("functions").resolve()
DATA_DIR = Path("data").resolve()

os.makedirs(FUNCTIONS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

FUNCTIONS_FILE = DATA_DIR / "functions.json"
LOGS_FILE = DATA_DIR / "logs.json"

functions = {}
logs = {}

# 📦 Configuración de Contenedores
BASE_DIR = Path.home() / "faas-lab"
ROOTFS_DIR = BASE_DIR / "rootfs"
CRUN_BIN = shutil.which("crun") or "/usr/bin/crun" 

# 🟢 Archivo de configuración para paquetes
PACKAGES_CONFIG_FILE = Path("packages.json")

# 📦 Configuración de Tareas Asíncronas 
ASYNC_TASKS = {} 

# ========================================================
# 💾 FUNCIONES DE PERSISTENCIA
# ========================================================

def load_state():
    global functions, logs
    try:
        if os.path.exists(FUNCTIONS_FILE):
            with open(FUNCTIONS_FILE, 'r') as f:
                functions = json.load(f) 
        if os.path.exists(LOGS_FILE):
            with open(LOGS_FILE, 'r') as f:
                logs = json.load(f)
    except Exception:
        functions = {}
        logs = {}
        
def save_state():
    try:
        # Omitimos execution_log y otros campos grandes de ASYNC_TASKS si se guardara en persistencia,
        # pero por ahora solo guardamos logs y funciones.
        functions_to_save = {k: {key: v for key, v in data.items()} 
                             for k, data in functions.items()}
                             
        with open(FUNCTIONS_FILE, 'w') as f:
            json.dump(functions_to_save, f, indent=4)
        
        with open(LOGS_FILE, 'w') as f:
            json.dump(logs, f, indent=4)
    except Exception as e:
        print(f"Error al guardar el estado: {e}")


# ========================================================
# 🟢 FUNCIÓN: Actualización y Reconstrucción del RootFS
# ========================================================

def update_and_rebuild_rootfs(new_python_reqs=None, new_node_packages=None):
    """
    Lee, actualiza packages.json con nuevos requisitos, y reconstruye el rootfs.
    """
    if not new_python_reqs and not new_node_packages:
        return 
    
    print(f"⚠️  Actualizando configuración de paquetes...")
    
    config = {}
    if PACKAGES_CONFIG_FILE.exists():
        with open(PACKAGES_CONFIG_FILE, 'r') as f:
            config = json.load(f)
            
    has_changes = False
            
    # --- PROCESAR PYTHON (requirements.txt) ---
    if new_python_reqs:
        current_reqs_str = config.get("common_python_packages", "")
        current_reqs = set(current_reqs_str.split()) if current_reqs_str else set()
        
        new_req_list = [
            r.split('==')[0].split('<')[0].split('>')[0].strip()
            for r in new_python_reqs.splitlines() 
            if r.strip() and not r.startswith('#')
        ]
        
        all_reqs = current_reqs | set(new_req_list)
        
        if len(all_reqs) > len(current_reqs):
            config["common_python_packages"] = " ".join(all_reqs)
            print("✅ packages.json actualizado con nuevos requisitos de Python.")
            has_changes = True
    
    # --- PROCESAR NODE.JS (packages.json o listado simple) ---
    if new_node_packages:
        current_node_str = config.get("common_node_packages", "")
        current_node = set(current_node_str.split()) if current_node_str else set()
        
        new_node_list = [
            r.strip()
            for r in new_node_packages.splitlines() 
            if r.strip() and not r.startswith('#')
        ]
        
        all_node_packages = current_node | set(new_node_list)
        
        if len(all_node_packages) > len(current_node):
            config["common_node_packages"] = " ".join(all_node_packages)
            print("✅ packages.json actualizado con nuevos paquetes de Node.js.")
            has_changes = True

    # 2. Reconstruir el RootFS solo si hay cambios
    if has_changes:
        with open(PACKAGES_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
        
        print("⏳ Iniciando reconstrucción del RootFS (utilizando --skip-download)...")
        try:
            result = subprocess.run(
                ["sudo", sys.executable, "build_rootfs_local.py", "--skip-download"], 
                capture_output=True, text=True, check=True, timeout=600 
            )
            print("✅ Reconstrucción del RootFS completada.")
            print(result.stdout[-500:]) 
        except subprocess.TimeoutExpired:
            raise Exception("Fallo en la reconstrucción del RootFS: Tiempo de espera agotado (10 minutos).")
        except subprocess.CalledProcessError as e:
            print(f"❌ Fallo de reconstrucción de RootFS. STDOUT: {e.stdout}")
            print(f"❌ Fallo de reconstrucción de RootFS. STDERR: {e.stderr}")
            raise Exception(f"Fallo de reconstrucción de RootFS. Código de salida: {e.returncode}")
    else:
        print("✅ No hay nuevos paquetes para instalar. Omite la reconstrucción del RootFS.")


# ========================================================
# 🛠️ FUNCIONES DE EJECUCIÓN CON CRUN 
# ========================================================

TEMP_CONFIG_FILES = {}

def cleanup_temp_configs():
    for config_path in list(TEMP_CONFIG_FILES.values()):
        try:
            os.remove(config_path)
        except OSError:
            pass 

atexit.register(cleanup_temp_configs)


def create_temp_config(container_id, command, mounts):
    base_config_path = ROOTFS_DIR / "config.json"
    if not base_config_path.exists():
        raise FileNotFoundError(f"El archivo config.json base no se encontró en: {base_config_path}")
        
    with open(base_config_path, 'r') as f:
        config = json.load(f)

    alpine_rootfs_path = ROOTFS_DIR
    config['root']['path'] = str(alpine_rootfs_path.resolve())
    config['process']['args'] = command
    
    # CORRECCIÓN DE PERMISOS
    config['process']['cwd'] = "/mnt" 
    
    oci_mounts = [
        {"destination": dst, "type": "bind", "source": src, "options": ["rbind", "rprivate"]} 
        for src, dst in mounts
    ]
    config['mounts'] = config.get('mounts', []) + oci_mounts

    temp_dir = Path(tempfile.gettempdir()) / container_id
    temp_dir.mkdir(exist_ok=True)
    
    temp_config_path = temp_dir / "config.json"
    with open(temp_config_path, 'w') as f:
        json.dump(config, f, indent=4)
        
    TEMP_CONFIG_FILES[container_id] = temp_config_path
    
    return temp_dir

def build_c_function(src_path: Path, dest_path: Path):
    print(f"⚙️ Compilando función C: {src_path.name}")
    
    gcc_command = [
        "sh", "-c", 
        f"cd /mnt && gcc {src_path.name} -o {dest_path.stem}" 
    ]
    
    mounts = [(src_path.parent.as_posix(), "/mnt")] 
    
    out, err, code = run_in_container(gcc_command, mounts)
    
    if code != 0:
        raise Exception(f"Fallo de compilación C. Código: {code}. Error: {err or out}")
        
    print("✅ Compilado correctamente.")


def run_in_container(command, mounts=None):
    mounts = mounts or []
    container_id = "faas-task-" + str(uuid.uuid4()).split('-')[0] 
    
    bundle_path = create_temp_config(container_id, command, mounts)
    
    cmd = [
        "sh", "-c",
        f"cd {bundle_path} && {CRUN_BIN} run {container_id}" 
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = result.stdout.strip()
        err = result.stderr.strip()
        code = result.returncode
    
    except subprocess.TimeoutExpired:
        out = ""
        err = "Timeout: La función excedió el límite de tiempo de ejecución (30s)."
        code = 124 
    except Exception as e:
        out = ""
        err_msg = f"Error al ejecutar crun. Error: {e}"
        err = err_msg
        code = 125
    finally:
        subprocess.run([CRUN_BIN, "delete", container_id], stderr=subprocess.DEVNULL)
        if container_id in TEMP_CONFIG_FILES:
            temp_config_path = TEMP_CONFIG_FILES.pop(container_id)
            try:
                shutil.rmtree(temp_config_path.parent) 
            except OSError:
                 pass
        
    return out, err, code

# ========================================================
# ⚙️ FUNCIONES DE EJECUCIÓN (Lógica extraída para DRY) 
# ========================================================

def _execute_function_logic(func_name, args, task_id, start_time_str):
    """
    Contiene la lógica central de ejecución dentro del contenedor. 
    Devuelve el diccionario de entrada (log entry) o lanza una excepción.
    """
    func_data = functions[func_name]
    abs_func_path = Path(func_data["file_path"])
    file_ext = func_data["file_ext"]
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        os.chmod(tmpdir_path, 0o777) 
        
        temp_func_path = tmpdir_path / abs_func_path.name
        shutil.copy(abs_func_path, temp_func_path)
        os.chmod(temp_func_path, 0o755)

        mounts = [(tmpdir_path.as_posix(), "/mnt")]
        command = None

        if file_ext == ".py":
            python_command = ["python3", f"/mnt/{abs_func_path.name}"] + [str(a) for a in args]
            command = [
                "sh", "-c", 
                f"PYTHONPATH=/usr/local/lib/python3.12/site-packages { ' '.join(python_command) }"
            ]
        elif file_ext == ".js":
            command = ["node", f"/mnt/{abs_func_path.name}"] + [str(a) for a in args]
        elif file_ext == ".c":
            build_c_function(temp_func_path, temp_func_path)
            executable_name = abs_func_path.stem
            command = [f"/mnt/{executable_name}"] + [str(a) for a in args]
        else:
            raise ValueError(f"Extensión de archivo no soportada: {file_ext}")
        
        out, err, code = run_in_container(command, mounts)
        
        if code != 0:
            raise Exception(f"Fallo de ejecución. Código de salida: {code}. Error: {err or out}")
            
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            result = out 

        e_time = time.time()
        entry = {
            "id": task_id, 
            "args": args, 
            "result": result, 
            "status": "success",
            "time_start": start_time_str,
            "time_end": datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")
        }
        return entry


def async_function_worker(task_id, func_name, args, start_time_str):
    """
    Ejecuta la lógica de la función en un hilo separado y almacena el resultado.
    """
    global logs, ASYNC_TASKS
    
    # Marcamos la tarea como en ejecución
    if task_id in ASYNC_TASKS:
        ASYNC_TASKS[task_id]['status'] = 'running'

    try:
        entry = _execute_function_logic(func_name, args, task_id, start_time_str)
        
        # Actualizar ASYNC_TASKS
        if task_id in ASYNC_TASKS:
            ASYNC_TASKS[task_id].update({
                'status': 'completed',
                'result': entry['result'],
                'time_end': entry['time_end'],
                'execution_log': entry
            })
            
    except Exception as e:
        e_time = time.time()
        error_msg = str(e)
        
        entry = {
            "id": task_id, 
            "args": args, 
            "error": error_msg, 
            "status": "error",
            "time_start": start_time_str,
            "time_end": datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")
        }
        
        # Actualizar ASYNC_TASKS
        if task_id in ASYNC_TASKS:
            ASYNC_TASKS[task_id].update({
                'status': 'failed',
                'error': error_msg,
                'time_end': entry['time_end'],
                'execution_log': entry
            })

    # Guardar el registro de ejecución en el log global
    logs.setdefault(func_name, []).append(entry)
    save_state()


# ========================================================
# 🌐 ENDPOINT DE SUBIDA
# ========================================================

@app.route('/admin/upload', methods=['POST'])
@requires_auth
def upload_function():
    if 'code' not in request.files:
        return jsonify({"status": "error", "message": "Falta el archivo 'code'."}), 400
        
    func_file = request.files['code']
    file_name = func_file.filename
    func_name = request.form.get('name', Path(file_name).stem) 
    
    if not func_name:
        return jsonify({"status": "error", "message": "El nombre de la función es obligatorio."}), 400
    
    func_dir = FUNCTIONS_DIR / func_name
    os.makedirs(func_dir, exist_ok=True)
    
    # Guardamos el archivo de la función
    func_path = func_dir / file_name
    func_file.save(func_path)
    
    file_ext = Path(file_name).suffix
    dependency_content = None
    dependency_file_name = None
    
    message_suffix = "."

    # 1. Determinar el nombre del archivo de dependencias
    if file_ext == ".py":
        expected_dep_file = "requirements.txt"
    elif file_ext == ".js":
        expected_dep_file = "packages.json"
    else:
        expected_dep_file = None 

    # 2. Buscar el archivo de dependencias en el request bajo el campo 'dependencies'
    dep_file = request.files.get('dependencies')
    print(dep_file) 
    
    if expected_dep_file and dep_file and dep_file.filename == expected_dep_file:
        dependency_file_name = expected_dep_file
        
        # Leer el contenido
        dependency_content = dep_file.read().decode('utf-8')
        dep_file.seek(0) # Resetear puntero para el save
        
        # Guardar el archivo en el directorio de la función
        dep_path = func_dir / dependency_file_name
        dep_file.save(dep_path)
        
        # 3. LLAMADA CLAVE: Actualizar la configuración y reconstruir el rootfs
        try:
            if file_ext == ".py":
                update_and_rebuild_rootfs(new_python_reqs=dependency_content)
            elif file_ext == ".js":
                update_and_rebuild_rootfs(new_node_packages=dependency_content)
                
            message_suffix = ". RootFS reconstruido. ¡La(s) nueva(s) librería(s) están listas para usarse!"
        except Exception as e:
             # Si falla la reconstrucción, eliminamos la función cargada
             shutil.rmtree(func_dir, ignore_errors=True)
             return jsonify({"status": "error", "message": f"Fallo al cargar la función. Falló la reconstrucción de la imagen: {str(e)}"}), 500


    try:
        abs_func_path = Path(func_path).resolve().as_posix()
        
        functions[func_name] = {
            "name": func_name,
            "file_path": abs_func_path,
            "file_ext": file_ext, 
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dependencies": dependency_file_name, 
        }
        
        save_state()
        return jsonify({"status": "success", "message": f"Función cargada: {func_name} ({file_ext}){message_suffix}"}), 201
    
    except Exception as e:
        shutil.rmtree(func_dir, ignore_errors=True)
        return jsonify({"status": "error", "message": f"Fallo en la carga de la función: {str(e)}"}), 500

# ========================================================
# 🌐 ENDPOINT DE EJECUCIÓN SÍNCRONA 
# ========================================================

@app.route('/function/sync/<func_name>', methods=['POST'])
def execute_function_sync(func_name):
    if func_name not in functions:
        return jsonify({"status": "error", "message": f"Función no cargada: {func_name}"}), 404
        
    data = request.get_json(silent=True)
    args = data.get('args', []) if data and isinstance(data, dict) else []
    
    s_time = time.time()
    start_time_str = datetime.fromtimestamp(s_time).strftime("%Y-%m-%d %H:%M:%S.%f") 
    task_id = str(uuid.uuid4()) # Usamos un ID único para el log

    try:
        # Llama a la lógica de ejecución síncrona
        entry = _execute_function_logic(func_name, args, task_id, start_time_str)
        
    except Exception as e:
        e_time = time.time()
        entry = {
            "id": task_id, 
            "args": args, 
            "error": str(e), 
            "status": "error",
            "time_start": start_time_str,
            "time_end": datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")
        }
    
    # Log y respuesta para ejecución síncrona
    logs.setdefault(func_name, []).append(entry)
    save_state()
    
    return jsonify(entry)

# ========================================================
# 🌐 ENDPOINT DE EJECUCIÓN ASÍNCRONA 
# ========================================================

@app.route('/function/async/<func_name>', methods=['POST'])
def execute_function_async(func_name):
    if func_name not in functions:
        return jsonify({"status": "error", "message": f"Función no cargada: {func_name}"}), 404
        
    data = request.get_json(silent=True)
    args = data.get('args', []) if data and isinstance(data, dict) else []
    
    s_time = time.time()
    start_time_str = datetime.fromtimestamp(s_time).strftime("%Y-%m-%d %H:%M:%S.%f") 
    task_id = str(uuid.uuid4())

    # Inicializar el estado de la tarea
    ASYNC_TASKS[task_id] = {
        'task_id': task_id,
        'function_name': func_name,
        'status': 'queued',
        'time_start': start_time_str,
        'time_end': None, # Inicialmente nulo
        'args': args
    }
    
    # Iniciar el hilo de ejecución
    thread = threading.Thread(
        target=async_function_worker, 
        args=(task_id, func_name, args, start_time_str)
    )
    thread.start()
    
    # Devolver la ID de la tarea inmediatamente
    return jsonify({
        "status": "queued",
        "message": "Función iniciada en modo asíncrono.",
        "task_id": task_id,
        "check_status_url": f"/task/status/{task_id}"
    }), 202


# ========================================================
# 🌐 ENDPOINT DE CONSULTA DE TAREA ASÍNCRONA 
# ========================================================

@app.route('/task/status/<task_id>', methods=['GET'])
def get_task_status(task_id):
    if task_id not in ASYNC_TASKS:
        return jsonify({"status": "error", "message": f"ID de tarea no encontrado: {task_id}"}), 404
        
    task_info = ASYNC_TASKS[task_id]
    
    if task_info['status'] in ['completed', 'failed']:
        # 🚀 CORRECCIÓN: Devolver el log de ejecución fusionado con el nombre de la función
        response = task_info.get('execution_log', {}).copy()
        # Aseguramos que el nombre de la función y el estado estén en el nivel superior
        response['function_name'] = task_info['function_name']
        response['status'] = task_info['status'] 
        response['time_end'] = task_info.get('time_end') 
        return jsonify(response)
    else:
        # Si está en cola o ejecutándose
        return jsonify({
            "task_id": task_id,
            "function_name": task_info['function_name'],
            "status": task_info['status'], # 'queued' o 'running'
            "time_start": task_info['time_start'],
            'time_end': task_info.get('time_end'), # <--- ¡Este campo es clave!
            "message": f"Tarea en curso. Estado: {task_info['status']}"
        })

# ========================================================
# 🌐 RESTO DE ENDPOINTS (ADMIN)
# ========================================================

@app.route('/admin/gui')
@requires_auth 
def admin_gui():
    # Asume que tienes un dashboard.html en la carpeta 'templates'
    return render_template('dashboard.html')


@app.route('/admin/functions', methods=['GET'])
@requires_auth
def list_functions():
    func_list = {k: v for k, v in functions.items()}
    return jsonify(func_list)


@app.route('/admin/functions/<func_name>', methods=['DELETE'])
@requires_auth
def delete_function(func_name):
    if func_name in functions:
        try:
            func_dir = Path(functions[func_name]['file_path']).parent
            shutil.rmtree(func_dir)
            
            del functions[func_name]
            if func_name in logs:
                del logs[func_name]
            
            save_state()
            return jsonify({"status": "success", "message": f"Función eliminada: {func_name}"})
        except Exception as e:
            return jsonify({"status": "error", "message": f"Error al eliminar la función: {str(e)}"}), 500
    else:
        return jsonify({"status": "error", "message": f"Función no encontrada: {func_name}"}), 404

# Nuevo Endpoint: Borrar todas las tareas
@app.route('/admin/clear_tasks', methods=['POST'])
@requires_auth
def clear_all_async_tasks():
    global ASYNC_TASKS
    ASYNC_TASKS.clear() # Vacía completamente el registro en memoria
    return jsonify({"status": "success", "message": "Todas las tareas asíncronas han sido borradas del panel de visualización."})

@app.route('/admin/status', methods=['GET'])
def get_server_status():
    """
    Devuelve información de estado del servidor.
    Aplica una lógica de limpieza de tareas.
    Devuelve métricas básicas si no hay autenticación, y métricas completas
    (incluida la lista de tareas) si las credenciales son válidas.
    """
    global ASYNC_TASKS # Necesario para modificar el diccionario global

    # 1. Obtener métricas básicas (siempre disponibles)
    uptime_seconds = time.time() - psutil.boot_time()
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"

    # --- LÓGICA DE LIMPIEZA DE TAREAS Y PREPARACIÓN DE LA LISTA ---
    task_list_to_send = []
    tasks_to_keep = {}
    
    # Ordenar por tiempo de inicio (para mantener las más recientes en el histórico)
    # Usamos .get('time_start', '') para manejar tareas sin tiempo de inicio por si acaso.
    sorted_task_ids = sorted(ASYNC_TASKS.keys(), key=lambda x: ASYNC_TASKS[x].get('time_start', ''), reverse=True)
    
    finished_count = 0
    MAX_FINISHED_TASKS = 20
    
    for task_id in sorted_task_ids:
        task = ASYNC_TASKS[task_id]
        
        # Preparamos la info limpia para la lista que podría enviarse al frontend (sin hilos ni logs completos)
        task_info_clean = {
            'task_id': task_id,
            'function_name': task.get('function_name'),
            'status': task.get('status'),
            'time_start': task.get('time_start'),
            'time_end': task.get('time_end'),
            # Incluir args y result a partir del execution_log (si existe)
            'args': task.get('args', task.get('execution_log', {}).get('args', [])), 
            'result': task.get('execution_log', {}).get('result', {}) 
        }
        
        # La lista a enviar siempre incluye todas las tareas, pero la limpieza decide cuáles persisten en memoria.
        task_list_to_send.append(task_info_clean)

        # Si está activa (en curso/en cola), la mantenemos en memoria.
        if task['status'] in ['queued', 'running']:
            tasks_to_keep[task_id] = task
        # Si está finalizada, solo mantenemos las MAX_FINISHED_TASKS más recientes.
        elif task['status'] in ['completed', 'failed']:
            if finished_count < MAX_FINISHED_TASKS: 
                 tasks_to_keep[task_id] = task
                 finished_count += 1
            # Si ya tenemos 20 finalizadas, la tarea antigua no se añade a tasks_to_keep y se descarta al final.
        
    ASYNC_TASKS = tasks_to_keep # Aplicar limpieza de tareas antiguas
    # --------------------------------------------------------------------

    # 2. Construir el estado base (siempre se devuelve)
    status_data = {
        "status": "ok",
        "uptime": uptime_str,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": psutil.virtual_memory().percent,
        "loaded_functions": len(functions),
        "async_tasks_running": len([t for t in ASYNC_TASKS.values() if t['status'] in ['queued', 'running']]),
    }
    
    # 3. Verificar autenticación para incluir la lista detallada de tareas
    auth = request.authorization
    is_authenticated = auth and check_auth(auth.username, auth.password)
    
    if is_authenticated:
        # Si está autenticado, incluimos la lista de tareas ya filtrada y preparada
        status_data["async_tasks_list"] = task_list_to_send
        
    
        
    return jsonify(status_data)


@app.route('/admin/logs/<func_name>', methods=['GET'])
@requires_auth
def get_function_logs(func_name):
    if func_name in logs:
        return jsonify(logs[func_name])
    else:
        return jsonify({"status": "error", "message": f"Logs no encontrados para: {func_name}"}), 404

# ========================================================
# 🚀 MAIN
# ========================================================
if __name__ == "__main__":
    if not ROOTFS_DIR.exists():
        print("!!! ADVERTENCIA CRÍTICA !!!")
        print(f"No se encontró el rootfs en: {ROOTFS_DIR}")
        print("Por favor, ejecute primero: sudo python3 build_rootfs_local.py")
        sys.exit(1)
        
    load_state() 
    
    print("TinyFaaS V3.1 HTTP Server (Containerized & Threaded) iniciado en http://127.0.0.1:8080")
    
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)