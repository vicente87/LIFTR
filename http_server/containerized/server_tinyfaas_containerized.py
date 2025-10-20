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

app = Flask(__name__)
CORS(app) 

# ========================================================
# 🔒 CONFIGURACIÓN DE SEGURIDAD (HTTP Basic Auth)
# ========================================================
USERNAME = "admin"
PASSWORD = "1234"

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
FUNCTIONS_DIR = "functions"
DATA_DIR = "data"
os.makedirs(FUNCTIONS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

FUNCTIONS_FILE = os.path.join(DATA_DIR, "functions.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")

functions = {}
logs = {}

# 📦 Configuración de Contenedores (de faas_launcher.py)
BASE_DIR = Path.home() / "faas-lab"
ROOTFS_DIR = BASE_DIR / "rootfs"
CRUN_BIN = shutil.which("crun") or "/usr/bin/crun" 

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
    except Exception as e:
        functions = {}
        logs = {}
        
def save_state():
    try:
        functions_to_save = {k: {key: v for key, v in data.items()} 
                             for k, data in functions.items()}
                             
        with open(FUNCTIONS_FILE, 'w') as f:
            json.dump(functions_to_save, f, indent=4)
        
        with open(LOGS_FILE, 'w') as f:
            json.dump(logs, f, indent=4)
    except Exception as e:
        print(f"Error al guardar el estado: {e}")

# ========================================================
# 🛠️ FUNCIONES DE EJECUCIÓN CON CRUN (VERSIÓN LEGACY - FINAL)
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
    """
    Crea un config.json TEMPORAL, apuntando al RootFS con una ruta ABSOLUTA
    y sin la opción de solo lectura en los mounts.
    """
    
    # 1. Cargar el config.json base 
    base_config_path = ROOTFS_DIR / "config.json"
    if not base_config_path.exists():
        raise FileNotFoundError(f"El archivo config.json base no se encontró en: {base_config_path}")
        
    with open(base_config_path, 'r') as f:
        config = json.load(f)

    # Establecer la ruta ABSOLUTA del RootFS (corregida)
    alpine_rootfs_path = ROOTFS_DIR
    config['root']['path'] = str(alpine_rootfs_path.resolve())
    
    # 2. Modificar el config.json para el comando específico
    config['process']['args'] = command
    
    # 3. Añadir los mounts temporales
    oci_mounts = [
        # 🟢 FIX CLAVE 3: Eliminamos el flag 'ro' (read-only)
        {"destination": dst, "type": "bind", "source": src, "options": ["rbind", "rprivate"]} 
        for src, dst in mounts
    ]
    # Añadimos los mounts específicos a los mounts predefinidos (si existen)
    config['mounts'] = config.get('mounts', []) + oci_mounts

    # 4. Guardar el archivo modificado en una ubicación temporal
    temp_dir = Path(tempfile.gettempdir()) / container_id
    temp_dir.mkdir(exist_ok=True)
    
    temp_config_path = temp_dir / "config.json"
    with open(temp_config_path, 'w') as f:
        json.dump(config, f, indent=4)
        
    TEMP_CONFIG_FILES[container_id] = temp_config_path
    
    return temp_dir

def build_c_function(src_path: Path, dest_path: Path):
    """Compila una función C DENTRO de un contenedor rootless temporal."""
    print(f"⚙️ Compilando función C: {src_path.name}")
    
    gcc_command = [
        "sh", "-c", 
        # FIX FINAL: Cambiamos el CWD a /mnt (escribible) antes de correr GCC.
        f"cd /mnt && gcc {src_path.name} -o {dest_path.stem}" 
    ]
    
    mounts = [(src_path.parent.as_posix(), "/mnt")] 
    
    out, err, code = run_in_container(gcc_command, mounts)
    
    if code != 0:
        # Aquí la salida de error será más informativa ya que la compilación falla
        raise Exception(f"Fallo de compilación C. Código: {code}. Error: {err or out}")
        
    print("✅ Compilado correctamente.")



def run_in_container(command, mounts=None):
    """
    Ejecuta un comando dentro del rootfs usando el comando RUN simple de crun (Legacy).
    """
    mounts = mounts or []
    container_id = "faas-task-" + str(uuid.uuid4()).split('-')[0] 
    
    bundle_path = create_temp_config(container_id, command, mounts)
    
    # Ejecutamos `crun run [id]` desde el directorio bundle (bundle_path)
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
        # 3. Limpieza: crun delete Y limpiar el archivo de configuración temporal
        subprocess.run([CRUN_BIN, "delete", container_id], stderr=subprocess.DEVNULL)
        if container_id in TEMP_CONFIG_FILES:
            temp_config_path = TEMP_CONFIG_FILES.pop(container_id)
            try:
                shutil.rmtree(temp_config_path.parent) 
            except OSError:
                 pass
        
    return out, err, code

# ========================================================
# 🌐 ENDPOINTS DE ADMINISTRACIÓN Y EJECUCIÓN 
# ========================================================

@app.route('/admin/gui')
@requires_auth 
def admin_gui():
    return render_template('dashboard.html')


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
    
    func_dir = os.path.join(FUNCTIONS_DIR, func_name)
    os.makedirs(func_dir, exist_ok=True)
    
    func_path = os.path.join(func_dir, file_name)
    func_file.save(func_path)
    
    try:
        abs_func_path = Path(func_path).resolve().as_posix()
        
        functions[func_name] = {
            "name": func_name,
            "file_path": abs_func_path,
            "file_ext": Path(file_name).suffix, 
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        save_state()
        return jsonify({"status": "success", "message": f"Función cargada: {func_name} ({Path(file_name).suffix})"}), 201
    
    except Exception as e:
        shutil.rmtree(func_dir, ignore_errors=True)
        return jsonify({"status": "error", "message": f"Fallo en la carga de la función: {str(e)}"}), 500

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
            func_dir = Path(functions[func_name]['file_path']).parent.as_posix()
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


@app.route('/admin/status', methods=['GET'])
@requires_auth
def get_server_status():
    uptime_seconds = time.time() - psutil.boot_time()
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"

    status_data = {
        "status": "ok",
        "uptime": uptime_str,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "ram_percent": psutil.virtual_memory().percent,
        "loaded_functions": len(functions)
    }
    return jsonify(status_data)


@app.route('/admin/logs/<func_name>', methods=['GET'])
@requires_auth
def get_function_logs(func_name):
    if func_name in logs:
        return jsonify(logs[func_name])
    else:
        return jsonify({"status": "error", "message": f"Logs no encontrados para: {func_name}"}), 404

@app.route('/function/<func_name>', methods=['POST'])
def core_execute_function(func_name):
    if func_name not in functions:
        return jsonify({"status": "error", "message": f"Función no cargada: {func_name}"}), 404
        
    data = request.get_json(silent=True)
    args = data.get('args', []) if data and isinstance(data, dict) else []
    
    func_data = functions[func_name]
    abs_func_path = Path(func_data["file_path"])
    file_ext = func_data["file_ext"]

    s_time = time.time()
    start_time = datetime.fromtimestamp(s_time).strftime("%Y-%m-%d %H:%M:%S.%f") 

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            
            # 🟢 FIX CLAVE 1: Asegurar que el directorio temporal sea accesible (0o777)
            os.chmod(tmpdir_path, 0o777) 
            
            temp_func_path = tmpdir_path / abs_func_path.name
            shutil.copy(abs_func_path, temp_func_path)
            
            # 🟢 FIX CLAVE 2: Establecer permisos 0o755 al archivo
            os.chmod(temp_func_path, 0o755)

            mounts = [(tmpdir_path.as_posix(), "/mnt")]
            
            if file_ext == ".py":
                command = ["python3", f"/mnt/{abs_func_path.name}"] + [str(a) for a in args]
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
                "id": str(uuid.uuid4()), 
                "args": args, 
                "result": result, 
                "status": "success",
                "time_start": start_time,
                "time_end": datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")
            }

    except Exception as e:
        e_time = time.time()
        
        entry = {
            "id": str(uuid.uuid4()), 
            "args": args, 
            "error": str(e), 
            "status": "error",
            "time_start": start_time,
            "time_end": datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")
        }

    logs.setdefault(func_name, []).append(entry)
    save_state()
    
    return jsonify(entry)

# ========================================================
# 🚀 MAIN
# ========================================================
if __name__ == "__main__":
    if not ROOTFS_DIR.exists():
        print("!!! ADVERTENCIA CRÍTICA !!!")
        print(f"No se encontró el rootfs en: {ROOTFS_DIR}")
        print("Por favor, ejecute primero: python3 build_rootfs_local.py")
        sys.exit(1)
        
    load_state() 
    
    print("TinyFaaS V3.0 HTTP Server (Containerized & Rootless) iniciado en http://127.0.0.1:8080")
    
    app.run(host='0.0.0.0', port=8080, debug=True)