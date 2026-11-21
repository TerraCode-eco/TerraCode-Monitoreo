🌿 TerraCode — Monitoreo Acústico AmbientalPlataforma autónoma de monitoreo acústico continuo y alerta temprana para la conservación de ecosistemas y protección de predios privados.📋 Resumen EjecutivoTerraCode es un ecosistema tecnológico diseñado para detectar, clasificar y alertar en tiempo real sobre amenazas ambientales críticas como tala ilegal (motosierras), caza furtiva (disparos) e incendios forestales.A diferencia de las soluciones convencionales en la nube, TerraCode opera bajo una arquitectura local de bajo costo y alta resiliencia, procesando señales acústicas en el borde (Edge/Local AI) sin generar costos recurrentes por API ni depender de conectividad constante a internet.⚡ Instalación y Uso Rápido (Despliegue en 1 Clic)El sistema está diseñado para ejecutarse sin complicaciones técnicas ni configuraciones manuales complejas de dependencias.Descargar / Clonar el repositorio:git clone https://github.com/TerraCode-eco/TerraCode-Monitoreo.git
Ejecutar el instalador automático:Entra a la carpeta monitoreo_app.Ejecuta el archivo INICIAR.bat.🚀 Nota: Al abrir INICIAR.bat, el script verifica el entorno, instala automáticamente todas las dependencias necesarias de Python (requirements.txt) y despliega la aplicación de monitoreo de forma transparente.🏗️ Arquitectura del SistemaEl flujo de procesamiento combina captura continua, redes neuronales de clasificación acústica y modelos de lenguaje locales para análisis de contexto:┌───────────────────────────┐
│ Nodos Sensores (Micrófonos│
│ Boya BY-M1 / Celulares)   │
└─────────────┬─────────────┘
              │ Transmisión de Audio en Vivo
              ▼
┌───────────────────────────┐      🚨 Alerta Inmediata
│ Motor de Clasificación    ├─────────────────────────────► (Telegram / SMS)
│ Acústica (YAMNet / Core)  │   (Respuesta en < 3s)
└─────────────┬─────────────┘
              │ Señales ambiguas / Confirmación
              ▼
┌───────────────────────────┐
│ Servidor IA Local         │
│ (LM Studio)               │ ───► Evaluado con Qwen3-4B / Meta Llama 8B
└───────────────────────────┘
Componentes PrincipalesCapta Sonido (Bordo de Red): Red de micrófonos omnidireccionales de 360° (Boya BY-M1) distribuidos estratégicamente a distancias de 70–100 m para asegurar una cobertura del terreno superior al 95%.Motor de Clasificación Acústica: Extrae espectrogramas y procesa patrones sonoros en ventanas de tiempo de 2 segundos.Orquestación de IA Local (LM Studio): Emplea modelos cuantizados como qwen/qwen3-4b o Meta Llama 8B para validar eventos límite sin costo operativo y preservando la privacidad del predio.Tolerancia a Fallos: Si el servicio de IA local se interrumpe, la canalización acústica principal mantiene activas las alertas inmediatas de detección directa.📁 Estructura del ProyectoTerraCode-Monitoreo/
├── monitoreo_app/
│   ├── INICIAR.bat               # ⚡ Ejecutable automático (Instala dependencias y lanza el sistema)
│   ├── monitoreo.py              # Orquestador principal del servicio de escucha continua
│   ├── clasificador_sonidos.py   # Módulo de procesamiento y extracción de características sonoras
│   ├── sensor_cliente.py         # Cliente para transmisión de nodos de captura hacia el servidor
│   ├── celular_sensor.html       # Interfaz web de captura desde dispositivos móviles
│   ├── requirements.txt          # Lista de dependencias de Python
│   ├── LEEME.md                  # Documentación interna del módulo
│   ├── modelos/                  # Redes neuronales previa/re-entrenadas
│   │   ├── yamnet.npz
│   │   └── yamnet_clases.csv
│   └── herramientas/             # Scripts de utilidad
│       ├── entrenar_con_mis_audios.py   # Re-entrenamiento con datos locales
│       ├── evaluar_con_esc50.py         # Benchmarking contra dataset ESC-50
│       └── exportar_yamnet.py           # Optimización de modelos
🎯 Hoja de Ruta y Margen de MejoraTerraCode cuenta con un plan de desarrollo enfocado en maximizar el radio de protección y la autonomía en campo:[ ] Triangulación Acústica Georreferenciada (TDOA): Integración de algoritmos basados en la diferencia de tiempo de llegada para estimar las coordenadas $(x, y)$ exactas de la fuente del disparo o la motosierra.[ ] Nodos IoT Fotovoltaicos Standalone: Adaptación del módulo cliente a microcontroladores de ultra bajo consumo (ESP32 / Raspberry Pi Pico) con alimentación por micro-paneles solares.[ ] Ajuste Fino de Dataset Bioacústico: Ampliación del catálogo de entrenamiento con sonidos nativos de fauna neotropical para elevar la precisión al 99% ante interferencias climáticas (lluvia, truenos, viento).[ ] Dashboard Web Geográfico: Desarrollo de una consola central con mapas interactivos de calor y monitoreo de salud de los nodos en tiempo real.
