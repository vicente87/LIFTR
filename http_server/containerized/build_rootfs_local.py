import os
import tarfile
import urllib.request
import subprocess
from pathlib import Path

# === CONFIGURACIÓN ===
BASE_DIR = Path.home() / "faas-lab"
ROOTFS_DIR = BASE_DIR / "rootfs"
ARCH = "x86_64"  # usa "aarch64" si es Raspberry Pi
ALPINE_VERSION = "3.20.0"
ROOTFS_URL = f"https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/{ARCH}/alpine-minirootfs-{ALPINE_VERSION}-{ARCH}.tar.gz"
TAR_PATH = BASE_DIR / f"alpine-minirootfs-{ALPINE_VERSION}-{ARCH}.tar.gz"

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
    if ROOTFS_DIR.exists() and any(ROOTFS_DIR.iterdir()):
        print("📂 El rootfs ya existe, omitiendo extracción.")
        return
    print(f"📦 Extrayendo rootfs en {ROOTFS_DIR} ...")
    ROOTFS_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(TAR_PATH, "r:gz") as tar:
        tar.extractall(path=ROOTFS_DIR)
    print("✅ Extracción completada.")

def install_packages():
    """Instala Python, Node.js y GCC dentro del rootfs"""
    print("⚙️ Instalando Python, Node.js y GCC dentro del rootfs...")

    subprocess.run(["sudo", "mount", "-t", "proc", "proc", f"{ROOTFS_DIR}/proc"])
    subprocess.run(["sudo", "mount", "-t", "sysfs", "sys", f"{ROOTFS_DIR}/sys"])
    subprocess.run(["sudo", "mount", "--bind", "/dev", f"{ROOTFS_DIR}/dev"])

    # 👇 Agrega esta línea
    subprocess.run(["sudo", "cp", "/etc/resolv.conf", f"{ROOTFS_DIR}/etc/resolv.conf"])

    try:
        subprocess.run([
            "sudo", "chroot", str(ROOTFS_DIR),
            "sh", "-c",
            "apk update && apk add --no-cache python3 py3-pip nodejs npm gcc musl-dev bash curl"
        ], check=True)
    finally:
        subprocess.run(["sudo", "umount", f"{ROOTFS_DIR}/proc"], stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "umount", f"{ROOTFS_DIR}/sys"], stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "umount", f"{ROOTFS_DIR}/dev"], stderr=subprocess.DEVNULL)

    print("✅ Paquetes instalados correctamente.")


def clean_rootfs():
    """Limpia archivos innecesarios para reducir tamaño"""
    print("🧹 Limpiando archivos innecesarios...")
    cleanup_paths = [
        "var/cache/apk", "usr/share/man", "usr/share/doc", "usr/share/locale"
    ]
    for path in cleanup_paths:
        full_path = ROOTFS_DIR / path
        if full_path.exists():
            subprocess.run(["sudo", "rm", "-rf", str(full_path)])
    print("✅ Limpieza completada.")

def main():
    print("🚀 Construyendo rootfs multilenguaje (Python, Node.js, C)...\n")
    download_rootfs()
    extract_rootfs()
    install_packages()
    clean_rootfs()

    print("\n🎉 Rootfs listo para usar con crun o runC.")
    print(f"📂 Ubicación: {ROOTFS_DIR}")
    print("💡 Puedes probarlo con: python3 faas_launcher.py --func ./functions/sum.py --args '5 7'")
# (Añade esta función en algún lugar del archivo build_rootfs_local.py)

def create_oci_config():
    """Genera el config.json estándar usando crun spec."""
    print("⚙️ Generando archivo config.json (OCI spec)...")
    
    # 1. Crear el config.json base
    try:
        # crun spec crea un archivo config.json en el directorio actual.
        subprocess.run(["crun", "spec"], check=True, cwd=str(ROOTFS_DIR))
    except FileNotFoundError:
        # Esto ocurre si crun no está en el PATH
        print("❌ Error: crun no se encontró. Asegúrate de que crun está instalado y en el PATH.")
        return
    except subprocess.CalledProcessError as e:
        print(f"❌ Error al ejecutar crun spec: {e.stderr}")
        return

    # 2. Modificar el config.json para que apunte al rootfs y active las capacidades.
    config_path = ROOTFS_DIR / "config.json"
    with open(config_path, 'r') as f:
        config = json.load(f)

    # El rootfs debe apuntar a la carpeta 'rootfs' (que es el directorio actual)
    config['root']['path'] = 'rootfs'
    
    # Configuramos el proceso para que use bash por defecto (lo usaremos para sh -c)
    config['process']['args'] = ["/bin/bash"]
    
    # Añadir los mapeos de subuid/subgid al config.json (necesario para Rootless)
    # Estos valores se obtienen del sistema.
    import pwd, grp
    username = os.getlogin()
    
    # Lee el mapeo del sistema (asumiendo que ya lo configuraste)
    def get_id_map(path):
        try:
            with open(path, 'r') as f:
                for line in f:
                    parts = line.strip().split(':')
                    if parts[0] == username:
                        return [{"containerID": 0, "hostID": int(parts[1]), "size": int(parts[2])}]
            return []
        except:
            return []

    config['linux']['uidMappings'] = get_id_map("/etc/subuid")
    config['linux']['gidMappings'] = get_id_map("/etc/subgid")
    
    # Para el modo Rootless, debemos eliminar o modificar las capacidades sensibles
    # Eliminamos las capacidades sensibles si están presentes (crun lo necesita)
    config['process']['capabilities']['bounding'] = config['process']['capabilities'].get('bounding', [])
    config['process']['capabilities']['effective'] = config['process']['capabilities'].get('effective', [])
    config['process']['capabilities']['inheritable'] = config['process']['capabilities'].get('inheritable', [])
    config['process']['capabilities']['permitted'] = config['process']['capabilities'].get('permitted', [])
    config['process']['capabilities']['ambient'] = config['process']['capabilities'].get('ambient', [])

    # Añadimos las variables de entorno para que Python y Node funcionen
    config['process']['env'].append("PYTHONPATH=/usr/lib/python3.12/site-packages")
    
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=4)
        
    print("✅ config.json generado y modificado para ejecución Rootless.")


# (Modifica la función main() para llamar a create_oci_config)
def main():
    print("🚀 Construyendo rootfs multilenguaje (Python, Node.js, C)...\n")
    download_rootfs()
    extract_rootfs()
    install_packages()
    clean_rootfs()
    create_oci_config() # ⬅️ AÑADE ESTA LLAMADA AQUÍ
    print(f"\n✅ ¡Rootfs completado! Listo para ejecutar desde: {ROOTFS_DIR}")

# (Asegúrate de importar las librerías necesarias en build_rootfs_local.py)
import json
import pwd, grp


if __name__ == "__main__":
    main()
