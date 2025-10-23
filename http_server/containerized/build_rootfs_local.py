import os
import tarfile
import urllib.request
import subprocess
from pathlib import Path
import json 
import pwd, grp 
import shutil 
import sys 

# === CONFIGURACIÓN (RUTAS CORREGIDAS PARA SUDO) ===
# Determinar la ruta base, usando SUDO_USER si se está ejecutando con sudo
if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
    try:
        user_info = pwd.getpwnam(os.environ['SUDO_USER'])
        user_home = Path(user_info.pw_dir)
    except KeyError:
        # Fallback si SUDO_USER no está disponible
        user_home = Path.home()
else:
    user_home = Path.home()

BASE_DIR = user_home / "faas-lab"
ROOTFS_DIR = BASE_DIR / "rootfs"

ARCH = "x86_64"
ALPINE_VERSION = "3.20.0"
ROOTFS_URL = f"https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/{ARCH}/alpine-minirootfs-{ALPINE_VERSION}-{ARCH}.tar.gz"
TAR_PATH = BASE_DIR / f"alpine-minirootfs-{ALPINE_VERSION}-{ARCH}.tar.gz"

PACKAGES_CONFIG_FILE = Path("packages.json")

COMMON_PYTHON_PACKAGES = ""
COMMON_NODE_PACKAGES = ""

def load_package_config():
    """Carga la lista de paquetes desde packages.json."""
    global COMMON_PYTHON_PACKAGES, COMMON_NODE_PACKAGES
    try:
        if not PACKAGES_CONFIG_FILE.exists():
            print(f"🚨 Error: Archivo de configuración de paquetes no encontrado en {PACKAGES_CONFIG_FILE}.")
            sys.exit(1) 

        with open(PACKAGES_CONFIG_FILE, 'r') as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError as e:
                print(f"❌ Error de formato en packages.json: {e}")
                sys.exit(1)
                
            COMMON_PYTHON_PACKAGES = config.get("common_python_packages", "")
            COMMON_NODE_PACKAGES = config.get("common_node_packages", "")
        print("✅ Configuración de paquetes cargada exitosamente.")

    except Exception as e:
        print(f"❌ Error al cargar la configuración de paquetes: {e}. Abortando.")
        sys.exit(1)


def download_rootfs():
    """Descarga el minirootfs de Alpine"""
    if TAR_PATH.exists():
        print(f"📦 El archivo ya existe: {TAR_PATH}")
        return
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"⬇️  Descargando Alpine rootfs ({ARCH})...")
    urllib.request.urlretrieve(ROOTFS_URL, TAR_PATH)
    print(f"✅ Descargado: {TAR_PATH}")

def extract_rootfs():
    """Extrae el rootfs en la carpeta destino"""
    # Solo extraemos si el directorio está vacío o no existe
    if ROOTFS_DIR.exists() and any(ROOTFS_DIR.iterdir()):
        print("📂 El rootfs ya existe, omitiendo extracción.")
        return
    print(f"📦 Extrayendo rootfs en {ROOTFS_DIR} ...")
    ROOTFS_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(TAR_PATH, "r:gz") as tar:
        tar.extractall(ROOTFS_DIR)
    print("✅ Extracción completada.")

def setup_network_for_chroot():
    """Copia el /etc/resolv.conf del host al rootfs para permitir la resolución de DNS."""
    resolv_conf_host = Path("/etc/resolv.conf")
    resolv_conf_rootfs = ROOTFS_DIR / "etc" / "resolv.conf"
    
    print("🌐 Configurando DNS en el rootfs...")
    try:
        shutil.copy(resolv_conf_host, resolv_conf_rootfs)
        print("✅ /etc/resolv.conf copiado. La red debería funcionar ahora.")
    except Exception as e:
        print(f"⚠️ Error al copiar /etc/resolv.conf: {e}. La instalación puede fallar.")


def install_packages():
    """Instala las herramientas y dependencias de lenguaje dentro del rootfs usando chroot."""
    print("🛠️ Instalando paquetes de sistema (apk)...")
    
    # 1. Instalar paquetes esenciales de Alpine
    # 🔴 CORRECCIÓN: Se añade g++ (compilador C++)
    packages = "python3 py3-pip python3-dev nodejs npm gcc g++ libc-dev" 
    apk_cmd = f"apk add --no-cache {packages}"
    
    subprocess.run(["chroot", ROOTFS_DIR.as_posix(), "sh", "-c", "apk update"], check=True)
    subprocess.run(["chroot", ROOTFS_DIR.as_posix(), "sh", "-c", apk_cmd], check=True)
    
    # 2. INSTALACIÓN DE PAQUETES PYTHON MONOLÍTICOS
    if COMMON_PYTHON_PACKAGES:
        print("📦 Instalando paquetes comunes de Python (pip)...")
        # Corrección para PEP 668: Usar --break-system-packages
        pip_install_cmd = f"python3 -m pip install --break-system-packages {COMMON_PYTHON_PACKAGES}"
        subprocess.run(["chroot", ROOTFS_DIR.as_posix(), "sh", "-c", pip_install_cmd], check=True)
    else:
        print("⚠️ Saltando instalación de paquetes Python (lista vacía).")
    
    # 3. INSTALACIÓN DE PAQUETES NODE.JS MONOLÍTICOS
    if COMMON_NODE_PACKAGES:
        print("📦 Instalando paquetes comunes de Node.js (npm)...")
        npm_install_cmd = f"npm install -g --prefix /usr/local {COMMON_NODE_PACKAGES}"
        subprocess.run(["chroot", ROOTFS_DIR.as_posix(), "sh", "-c", npm_install_cmd], check=True)
    else:
        print("⚠️ Saltando instalación de paquetes Node.js (lista vacía).")
    
    print("✅ Instalación de lenguajes y librerías comunes completada.")

def clean_rootfs():
    """Limpia el rootfs eliminando la caché de apk y archivos temporales."""
    print("🧹 Limpiando el rootfs...")
    # Corrección: Se reemplaza 'apk cache --wipe' por 'apk cache clean'
    subprocess.run(["chroot", ROOTFS_DIR.as_posix(), "apk", "cache", "clean"], check=True)
    # Elimina archivos de caché grandes fuera del chroot
    subprocess.run(["rm", "-rf", ROOTFS_DIR / "root/.cache"], check=False)
    subprocess.run(["rm", "-rf", ROOTFS_DIR / "usr/local/lib/node_modules/npm/node_modules"], check=False)
    print("✅ Limpieza completada.")
def create_oci_config():
    """Crea un config.json base y lo modifica para la ejecución rootless."""
    print("⚙️ Generando config.json para OCI...")
    
    config_path = ROOTFS_DIR / "config.json"
    
    if not config_path.exists():
        print("🚨 Generando config.json base con runc...")
        original_cwd = Path.cwd()
        os.chdir(ROOTFS_DIR)
        # runc spec --rootless debe ejecutarse desde el directorio del bundle (rootfs)
        subprocess.run(["runc", "spec", "--rootless"], check=True)
        os.chdir(original_cwd)
    
    with open(config_path, 'r') as f:
        config = json.load(f)

    # Obtenemos el UID/GID del usuario que invocó 'sudo' (vrodben1) para los mappings
    current_uid = os.getuid()
    current_gid = os.getgid()
    
    if os.environ.get("SUDO_USER"):
        try:
            user_info = pwd.getpwnam(os.environ['SUDO_USER'])
            uid = user_info.pw_uid
            gid = user_info.pw_gid
        except KeyError:
            uid = current_uid
            gid = current_gid
    else:
        uid = current_uid
        gid = current_gid


    config['process']['user']['uid'] = uid
    config['process']['user']['gid'] = gid
    
    config['linux']['uidMappings'] = [
        {"hostID": uid, "containerID": uid, "size": 1}
    ]
    config['linux']['gidMappings'] = [
        {"hostID": gid, "containerID": gid, "size": 1}
    ]
    
    # Asegurar la existencia de las capacidades
    config['process']['capabilities']['bounding'] = config['process']['capabilities'].get('bounding', [])
    config['process']['capabilities']['effective'] = config['process']['capabilities'].get('effective', [])
    config['process']['capabilities']['inheritable'] = config['process']['capabilities'].get('inheritable', [])
    config['process']['capabilities']['permitted'] = config['process']['capabilities'].get('permitted', [])
    config['process']['capabilities']['ambient'] = config['process']['capabilities'].get('ambient', [])

    # Añadimos la variable de entorno para que Python encuentre los paquetes monolíticos
    # 3.12.12 es la versión que instala Alpine 3.20
    config['process']['env'].append("PYTHONPATH=/usr/lib/python3.12/site-packages:/usr/local/lib/python3.12/site-packages")
    
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=4)
        
    print("✅ config.json generado y modificado para ejecución Rootless.")

def main():
    print("🚀 Construyendo rootfs monolítico (Python, Node.js, C)...\\n")
    
    skip_download = "--skip-download" in sys.argv
    
    load_package_config() 
    
    if not skip_download:
        download_rootfs()
    else:
        print("⚠️ Omitiendo descarga de rootfs (bandera --skip-download detectada).")
        
    extract_rootfs()
    setup_network_for_chroot()
    install_packages() 
    clean_rootfs()
    create_oci_config()
    print(f"\n✅ ¡Rootfs completado! Listo para ejecutar desde: {ROOTFS_DIR}")


if __name__ == "__main__":
    main()