import os
import subprocess
import sys
import uuid
import importlib.util
import json  
from functools import wraps
import shutil
import time
from datetime import datetime
import psutil
import threading
import paho.mqtt.client as mqtt
import base64 

# ========================================================
# ⚙️ CONFIGURACIÓN DEL SERVIDOR
# ========================================================

# 🆕 Configuración del Servidor MQTT
MQTT_BROKER = "broker.emqx.io"  # Escuchando en la misma máquina
MQTT_PORT = 1883           # Puerto estándar MQTT
MQTT_USERNAME = ""
MQTT_PASSWORD = ""
MQTT_BASE_TOPIC = "faas"      # Tópico base para todas las operaciones (admin, invoke)
MQTT_RESPONSE_TOPIC = "faas/response" # Tópico para devolver resultados

# Directorios y Archivos de Persistencia
FUNCTIONS_DIR = "functions"
DATA_DIR = "data"
os.makedirs(FUNCTIONS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

FUNCTIONS_FILE = os.path.join(DATA_DIR, "functions.json")
LOGS_FILE = os.path.join(DATA_DIR, "logs.json")

# Almacenamiento en Memoria (Global)
functions = {}
logs = {}

# ========================================================
# 💾 FUNCIONES DE PERSISTENCIA Y ENTORNO
# ========================================================
# (Se mantienen iguales a la versión anterior)

def save_state():
    """Guarda el estado de TinyFaaS en archivos JSON."""
    try:
        with open(FUNCTIONS_FILE, "w") as f:
            json.dump(functions, f, indent=4)
        with open(LOGS_FILE, "w") as f:
            json.dump(logs, f, indent=4)
    except Exception as e:
        print(f"ERROR: No se pudo guardar el estado de TinyFaaS: {e}")

def load_state():
    """Carga el estado de TinyFaaS desde 'data/'."""
    global functions, logs
    try:
        if os.path.exists(FUNCTIONS_FILE):
            with open(FUNCTIONS_FILE, "r") as f:
                functions = json.load(f)
        if os.path.exists(LOGS_FILE):
            with open(LOGS_FILE, "r") as f:
                logs = json.load(f)
        print("Estado de TinyFaaS cargado exitosamente.")
    except Exception as e:
        print(f"ADVERTENCIA: No se pudo cargar el estado. Inicializando vacío: {e}")
        functions = {}
        logs = {}

def create_venv(func_name, requirements):
    """Crea un entorno virtual dedicado para una función."""
    func_path = os.path.join(FUNCTIONS_DIR, func_name)
    venv_path = os.path.join(func_path, "venv")

    if not os.path.exists(venv_path):
        subprocess.check_call([sys.executable, "-m", "venv", venv_path])

    python_path = os.path.join(venv_path, "bin", "python")

    if requirements:
        subprocess.check_call([python_path, "-m", "ensurepip", "--upgrade"])
        subprocess.check_call([python_path, "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call([python_path, "-m", "pip", "install", "-r", requirements])

    return venv_path

# ========================================================
# ⚡ FUNCIONES INTERNAS CENTRALIZADAS (Core)
# ========================================================
# (internal_upload_function, internal_list_functions, internal_get_status, 
#  internal_get_logs, internal_delete_function y core_execute_function se 
#  mantienen iguales a la versión anterior, ya que son independientes de Flask.)

def internal_upload_function(func_name, code_data, req_data=None):
    # Simplemente usa bytes, ya que la subida es por Base64 en MQTT
    func_path = os.path.join(FUNCTIONS_DIR, func_name)
    os.makedirs(func_path, exist_ok=True)

    code_path = os.path.join(func_path, "func.py")
    req_path = None

    # Escribir el archivo de código (viene como bytes decodificados de MQTT)
    with open(code_path, "wb") as f:
        f.write(code_data)
    
    # Escribir archivo de requerimientos
    if req_data:
        req_path = os.path.join(func_path, "requirements.txt")
        with open(req_path, "wb") as f:
            f.write(req_data)

    create_venv(func_name, req_path)

    functions[func_name] = {"path": code_path, "venv": os.path.join(func_path, "venv")}
    logs[func_name] = []
    save_state()
    return {"status": "ok", "function": func_name}

def internal_list_functions():
    return list(functions.keys())

def internal_get_status():
    if psutil is None: raise RuntimeError("psutil module not found.")
    process = psutil.Process(os.getpid())
    cpu_percent = process.cpu_percent(interval=0.1) 
    milicpu_usage = (cpu_percent / 100.0) * 1000
    mem_info = process.memory_info()
    rss_mb = mem_info.rss / (1024 * 1024) 
    system_mem = psutil.virtual_memory()
    system_total_gb = system_mem.total / (1024 * 1024 * 1024) 
    system_available_gb = system_mem.available / (1024 * 1024 * 1024) 
    
    return {
        "status": "running",
        "cpu_usage_absolute": {"process_milicpu": f"{milicpu_usage:.6f}"},
        "memory_usage_absolute": {"process_rss_mb": f"{rss_mb:.2f} MB"},
        "system_memory_info": {"total_ram_gb": f"{system_total_gb:.2f} GB", "available_ram_gb": f"{system_available_gb:.2f} GB"},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def internal_get_logs(func_name):
    if func_name not in logs: raise ValueError("Function not found")
    return logs[func_name]

def internal_delete_function(func_name):
    if func_name not in functions: raise ValueError("Function not found")

    func_path = os.path.join(FUNCTIONS_DIR, func_name)
    del functions[func_name]
    del logs[func_name]
    save_state()

    shutil.rmtree(func_path, ignore_errors=True)
    return {"status": "deleted", "function": func_name}

def core_execute_function(func_name, data):
    if func_name not in functions:
        return {"error": "Function not found", "status_code": 404} 

    func_info = functions[func_name]
    func_path = func_info["path"]

    spec = importlib.util.spec_from_file_location("func", func_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    args = data.get("args", [])

    s_time = time.time()
    start_time = datetime.fromtimestamp(s_time).strftime("%Y-%m-%d %H:%M:%S.%f") 

    try:
        result = module.main(*args)
        e_time = time.time()
        end_time = datetime.fromtimestamp(e_time).strftime("%Y-%m-%d %H:%M:%S.%f")   
        
        entry = {
            "id": str(uuid.uuid4()), "args": args, "result": result, "status": "success",
            "time_start": start_time, "time_end": end_time
        }
    except Exception as e:
        end_time = time.time()
        entry = {
            "id": str(uuid.uuid4()), "args": args, "error": str(e), "status": "error",
            "time_start": start_time, "time_end": end_time
        }

    logs[func_name].append(entry)
    save_state()
    return entry


# ========================================================
# 🆕 CLASE DEL SERVIDOR MQTT (YA NO NECESITA CONTEXTO DE FLASK)
# ========================================================

class TinyFaaS_MqttServer(threading.Thread):
    
    def __init__(self, execute_function_callback):
        super().__init__()
        self.client = mqtt.Client(client_id=f"TinyFaaS_Server_{os.getpid()}") 
        self.execute_function = execute_function_callback
        self.running = False

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print(f"MQTT: Conexión exitosa. Suscribiendo a {MQTT_BASE_TOPIC}/#")
            # Suscripción a todos los sub-tópicos bajo 'faas' (invoke y admin)
            client.subscribe(f"{MQTT_BASE_TOPIC}/#") 
        else:
            print(f"MQTT: Fallo de conexión con código {rc}")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode()
        
        # 1. Parsear el tópico: faas/category/command/name
        path = topic.split('/')
        if len(path) < 3: return # Tópico no válido
        category = path[1] # 'admin' o 'invoke'
        
        response_topic = None
        result_payload = {}
        command = path[2] if len(path) > 2 else category

        try:
            data = json.loads(payload) if payload else {}
            request_id = data.get("request_id", str(uuid.uuid4())) # Obtener o generar ID

            # --- A. FUNCTION INVOCATION (faas/invoke/func_name) ---
            if category == 'invoke' and len(path) == 3:
                func_name = path[2]
                result_entry = self.execute_function(func_name, data) 
                
                # Respuesta: Retornar el log completo de la ejecución
                response_topic = f"{MQTT_RESPONSE_TOPIC}/invoke/{func_name}"
                result_payload = result_entry
            
            # --- B. ADMINISTRATIVE COMMANDS (faas/admin/command[/name]) ---
            elif category == 'admin' and len(path) >= 3:
                response_topic = f"{MQTT_RESPONSE_TOPIC}/admin/{command}"

                if command == 'list':
                    result_payload = {"functions": internal_list_functions()}
                
                elif command == 'status':
                    result_payload = internal_get_status()
                
                elif command == 'logs' and len(path) == 4:
                    func_name = path[3]
                    result_payload = {"logs": internal_get_logs(func_name)}
                    response_topic = f"{MQTT_RESPONSE_TOPIC}/admin/logs/{func_name}"

                elif command == 'delete' and len(path) == 4:
                    func_name = path[3]
                    result_payload = internal_delete_function(func_name)
                    response_topic = f"{MQTT_RESPONSE_TOPIC}/admin/delete/{func_name}"
                    
                elif command == 'upload' and len(path) == 4:
                    func_name = path[3]
                    if "code_b64" not in data: raise ValueError("Payload debe contener 'code_b64'")
                    
                    # Decodificación de archivos (se asume que la data viene limpia)
                    code_data = base64.b64decode(data.get("code_b64"))
                    req_data = base64.b64decode(data.get("req_b64")) if data.get("req_b64") else None
                    
                    result_payload = internal_upload_function(func_name, code_data, req_data)
                    response_topic = f"{MQTT_RESPONSE_TOPIC}/admin/upload/{func_name}"

                else:
                    raise ValueError("Comando administrativo no válido.")

            # --- C. Publicar Respuesta ---
            if response_topic:
                # Incluir el request_id original en la respuesta
                result_payload["request_id"] = request_id
                client.publish(response_topic, json.dumps(result_payload), qos=1)
                print(f"MQTT: Comando {category}/{command} completado. Respuesta enviada a {response_topic}")
            
        except Exception as e:
            error_topic = f"{MQTT_RESPONSE_TOPIC}/error"
            error_payload = {"error": str(e), "topic": topic, "command": command}
            client.publish(error_topic, json.dumps(error_payload), qos=1)
            print(f"MQTT Error: {e}. Tópico: {topic}")


    def run(self):
        """Método que inicia el servidor MQTT."""
        self.running = True
        self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        
        try:
            print(f"TinyFaaS V3.0 MQTT Server iniciado en {MQTT_BROKER}:{MQTT_PORT}")
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.client.loop_forever() # Bucle bloqueante de escucha
        except Exception as e:
            print(f"MQTT FATAL ERROR: No se pudo conectar al broker. {e}")
            self.running = False


# ========================================================
# 🚀 MAIN
# ========================================================
if __name__ == "__main__":
    load_state() 
    
    mqtt_server = TinyFaaS_MqttServer(
        execute_function_callback=core_execute_function
    )
    
    # Dado que no hay Flask, ejecutamos el método run() directamente.
    # Nota: loop_forever() es bloqueante, por lo que este es el único punto de entrada.
    try:
        mqtt_server.run()
    except KeyboardInterrupt:
        print("\nServidor TinyFaaS MQTT detenido por el usuario.")
    except Exception as e:
        print(f"Error crítico en el servidor principal: {e}")
