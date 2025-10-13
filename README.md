# LIFTR
**LIFTR: The Lightweight IoT Function Tiny Runtime.**

LIFTR es un sistema de Funciones como Servicio (FaaS) ultraligero y de código abierto, diseñado para llevar la lógica de negocio directamente a dispositivos IoT de bajos recursos y entornos de Edge/Fog Computing.

Desarrollado mayormente sobre Python, LIFTR actúa como un runtime Tiny, eliminando la sobrecarga y la latencia asociadas con las soluciones FaaS tradicionales de la nube.

## 💡 ¿Por Qué LIFTR?

Los runtimes FaaS existentes son demasiado pesados para el hardware limitado (Raspberry Pi, Gateways, Micro-servidores) que opera en el borde de la red. LIFTR resuelve esto con un motor optimizado que garantiza:

- **Mínima Huella de Recursos**: Diseñado para consumir la menor cantidad de RAM y CPU posible.

- **Latencia Cero de Origen**: Ejecuta el código a milisegundos del dispositivo IoT, no a segundos de la nube.

- **Aislamiento y Concurrencia**: Utiliza mecanismos avanzados (como multiprocesamiento) para garantizar la estabilidad y el aislamiento del código de las funciones.

- **Ecosistema Python**: Permite el despliegue de cualquier función Python, aprovechando su vasta librería para tareas de pre-procesamiento o Machine Learning en el Edge.
