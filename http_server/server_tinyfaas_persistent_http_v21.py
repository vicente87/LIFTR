import os
import subprocess
import sys
import uuid
import importlib.util
import json  
from flask import Flask, request, jsonify, Response, render_template
from functools import wraps
import shutil
import time
from datetime import datetime
import psutil
from flask_cors import CORS 
import traceback 

app = Flask(__name__)
CORS(app) 

# ========================================================
# 🔒 CONFIGURACIÓN DE SEGURIDAD (HTTP Basic Auth)
# ========================================================
USERNAME = "admin"
PASSWORD = "1234"
# ... (Funciones check_auth, authenticate, requires_auth) ...
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
# ⚙️ CONFIGURACIÓN DE PERSISTENCIA Y ENTORNO
# ========================================================
FUNCTIONS_DIR = "functions"
DATA_DIR = "data"
os.makedirs(FUNCTIONS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

FUNCTIONS_FILE = os.path.join(DATA_DIR, "functions.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")

functions = {}
logs = {}

# ========================================================
# 💾 FUNCIONES DE PERSISTENCIA Y ENTORNO
# ========================================================

def load_state():
    """Carga el estado de TinyFaaS desde archivos JSON al inicio. Ahora es más robusto."""
    global functions, logs
    
    try:
        if os.path.exists(FUNCTIONS_FILE):
            with open(FUNCTIONS_FILE, 'r') as f:
                functions = json.load(f)
        if os.path.exists(LOGS_FILE):
            with open(LOGS_FILE, 'r') as f:
                logs = json.load(f)
    except Exception as e:
        print(f"ADVERTENCIA: Fallo al leer archivos de estado JSON ({e}). Reiniciando el estado.")
        functions = {}
        logs = {}
        
    try:
        functions_to_keep = {}
        for func_name, data in functions.items():
            if isinstance(data, dict) and "file_path" in data:
                try:
                    load_function_module(func_name, data["file_path"])
                    functions_to_keep[func_name] = functions[func_name]
                except Exception as e:
                    print(f"ADVERTENCIA: No se pudo recargar el módulo '{func_name}' ({e}). Se omitirá.")
            else:
                print(f"ADVERTENCIA: Saltando función '{func_name}' debido a metadatos incompletos o corruptos.")
        
        functions = functions_to_keep 

        print("Estado y módulos cargados correctamente.")

    except Exception as e:
        print(f"Error al cargar el estado: {e}. Inicializando vacío.") 
        functions = {}
        logs = {}
        
def save_state():
    try:
        functions_to_save = {k: {key: v for key, v in data.items() if key != "module"} 
                             for k, data in functions.items()}
                             
        with open(FUNCTIONS_FILE, 'w') as f:
            json.dump(functions_to_save, f, indent=4)
        
        with open(LOGS_FILE, 'w') as f:
            json.dump(logs, f, indent=4)
    except Exception as e:
        print(f"Error al guardar el estado: {e}")

# 🚀 FUNCIÓN CRÍTICA DE CARGA DE MÓDULO (Robustez mejorada)
def load_function_module(func_name, file_path):
    """Carga dinámicamente el módulo Python de la función, verificando el contrato."""
    spec = importlib.util.spec_from_file_location(func_name, file_path)
    if spec is None:
        raise FileNotFoundError(f"No se encontró el archivo en: {file_path}")
        
    module = importlib.util.module_from_spec(spec)
    sys.modules[func_name] = module
    
    try:
        spec.loader.exec_module(module)
        
        if not hasattr(module, 'main') or not callable(module.main):
            raise AttributeError("El código no define la función de entrada requerida: 'def main(*args)'.")
            
    except Exception as e:
        if func_name in sys.modules:
            del sys.modules[func_name]
        raise 
    
    functions[func_name]["module"] = module 
    
    if func_name not in logs:
        logs[func_name] = []

# 🚀 FUNCIÓN CRÍTICA DE INSTALACIÓN (Anti-Timeout)
def install_requirements(requirements_path):
    """Instala dependencias si existe requirements.txt y no está vacío."""
    if os.path.exists(requirements_path):
        with open(requirements_path, 'r') as f:
            lines = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            
        if not lines:
            print("Requirements.txt vacío o solo con comentarios. Saltando instalación de pip.")
            return

        print(f"Instalando {len(lines)} dependencias desde {requirements_path}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", requirements_path, "--break-system-packages"])
            print("Dependencias instaladas con éxito.")
        except subprocess.CalledProcessError as e:
            print(f"Error al instalar dependencias: {e}")
            raise Exception(f"Fallo al instalar dependencias. {e}")
    else:
        print("No se encontraron dependencias para instalar.")

# ========================================================
# 🌐 ENDPOINTS DE ADMINISTRACIÓN (PROTEGIDOS)
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
    func_name = request.form.get('name', func_file.filename.replace('.py', ''))
    
    if not func_name:
        return jsonify({"status": "error", "message": "El nombre de la función es obligatorio."}), 400

    func_dir = os.path.join(FUNCTIONS_DIR, func_name)
    os.makedirs(func_dir, exist_ok=True)
    
    func_path = os.path.join(func_dir, "func.py")
    func_file.save(func_path)
    
    reqs_file = request.files.get('requirements')
    reqs_path = os.path.join(func_dir, "requirements.txt")
    
    if reqs_file:
        reqs_file.save(reqs_path)
        try:
            install_requirements(reqs_path)
        except Exception as e:
            shutil.rmtree(func_dir, ignore_errors=True)
            return jsonify({"status": "error", "message": f"Fallo al instalar dependencias: {str(e)}"}), 500
    
    try:
        functions[func_name] = {
            "name": func_name,
            "file_path": func_path,
            "requirements_path": reqs_path if reqs_file else None,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        load_function_module(func_name, func_path)
        
        save_state()
        return jsonify({"status": "success", "message": f"Función cargada: {func_name}"})
    
    except Exception as e:
        if func_name in functions:
            del functions[func_name]
            
        print("\n\n#####################################################")
        print(f"!!! FALLO CRÍTICO DE CARGA DE MÓDULO PARA: {func_name} !!!")
        traceback.print_exc() 
        print("#####################################################\n")
        
        shutil.rmtree(func_dir, ignore_errors=True)
        return jsonify({"status": "error", "message": f"Fallo en la carga del módulo: {str(e)}"}), 500


@app.route('/admin/functions', methods=['GET'])
@requires_auth
def list_functions():
    func_list = {k: {key: v for key, v in data.items() if key != "module"} 
                 for k, data in functions.items()}
    return jsonify(func_list)


@app.route('/admin/functions/<func_name>', methods=['DELETE'])
@requires_auth
def delete_function(func_name):
    if func_name in functions:
        try:
            func_dir = os.path.join(FUNCTIONS_DIR, func_name)
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
        # 🟢 Este valor es el que usa el frontend.
        "loaded_functions": len(functions)
    }
    return jsonify(status_data)


@app.route('/admin/logs/<func_name>', methods=['GET'])
@requires_auth
def get_function_logs(func_name):
    """Devuelve el historial de logs de ejecución para una función."""
    if func_name in logs:
        return jsonify(logs[func_name])
    else:
        return jsonify({"status": "error", "message": f"Logs no encontrados para: {func_name}"}), 404

# ========================================================
# 🚀 ENDPOINT DE INVOCACIÓN (NO PROTEGIDO)
# ========================================================

@app.route('/function/<func_name>', methods=['POST'])
def core_execute_function(func_name):
    if func_name not in functions:
        return jsonify({"status": "error", "message": f"Función no cargada: {func_name}"}), 404
        
    data = request.get_json(silent=True)
    args = data.get('args', []) if data and isinstance(data, dict) else []
    
    module = functions[func_name].get("module")
    if module is None:
        return jsonify({"status": "error", "message": "Módulo de función no cargado en memoria."}), 500
        
    s_time = time.time()
    start_time = datetime.fromtimestamp(s_time).strftime("%Y-%m-%d %H:%M:%S.%f") 

    try:
        result = module.main(*args)
        
        e_time = time.time()
        end_time = datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")   
        
        entry = {
            "id": str(uuid.uuid4()), 
            "args": args, 
            "result": result, 
            "status": "success",
            "time_start": start_time,
            "time_end": end_time
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
    load_state() 
    
    print("TinyFaaS V2.3 HTTP Server (Final) iniciado en http://127.0.0.1:8080")
    print("Accede a la GUI de administración en: http://127.0.0.1:8080/admin/gui")
    
    app.run(host='0.0.0.0', port=8080, debug=True)