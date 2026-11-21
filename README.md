# 🌿 TerraCode — Monitoreo Acústico Ambiental

Plataforma autónoma de monitoreo acústico continuo y alerta temprana para la conservación de ecosistemas y protección de predios privados[cite: 1, 2, 11].

A diferencia de las soluciones convencionales en la nube, **TerraCode** opera bajo una arquitectura local de bajo costo y alta resiliencia, procesando señales acústicas en el borde sin generar costos recurrentes por API ni depender de conectividad constante a internet[cite: 2, 3, 5, 11].

---

## 📋 Resumen Ejecutivo

**TerraCode** está diseñado para detectar, clasificar y alertar en tiempo real sobre amenazas ambientales críticas como **tala ilegal (motosierras)**, **caza furtiva (disparos)** e **incendios forestales**[cite: 1, 2, 3, 11].

---

## ⚡ Instalación y Uso Rápido (Despliegue en 1 Clic)

El sistema está pensado para ejecutarse sin configuraciones manuales complejas:

1. **Clonar o descargar el repositorio:**
   ```bash
   git clone https://github.com/TerraCode-eco/TerraCode-Monitoreo.git
   ```
2. **Entrar a la carpeta del proyecto:**
   ```text
   monitoreo_app/
   ```
3. **Ejecutar el instalador automático:**
   Abre `INICIAR.bat`[cite: 9, 11].

> 🚀 **Nota:** Al abrir `INICIAR.bat`, el script verifica el entorno, instala automáticamente las dependencias necesarias desde `requirements.txt` y lanza la aplicación de monitoreo de forma automática[cite: 9, 11].

---

## 🏗️ Arquitectura del Sistema

El flujo combina captura continua de audio, clasificación acústica y apoyo de modelos locales mediante **LM Studio** para señales ambiguas[cite: 2, 3, 11]:

```text
[Nodos Sensores: Micrófonos Boya BY-M1 / Celulares]
        │
        ▼  Transmisión de audio en vivo
[Motor de Clasificación Acústica (YAMNet / Core)]
        ├── Amenaza clara ──► [Alerta Inmediata: Telegram / SMS]
        └── Señal ambigua ──► [Servidor IA Local: LM Studio (Qwen3-4B / Llama 8B)]
```

### Componentes principales
* **Captura en el borde:** Micrófonos omnidireccionales de 360° (como Boya BY-M1) distribuidos cada 70–100 m (o celulares registrados como nodos) para cubrir el terreno[cite: 2, 3, 11].
* **Motor de clasificación acústica:** Procesa segmentos cortos de audio para identificar sonidos de amenaza y generar alertas con ubicación[cite: 1, 2, 3, 11].
* **Orquestación de IA local (LM Studio):** Apoya la revisión en casos ambiguos sin depender de APIs de pago ni exponer datos fuera del entorno local[cite: 2, 3, 11].
* **Tolerancia a fallos:** Si el servicio de IA local falla o se desactiva, el análisis acústico continúa operando para no interrumpir las alertas[cite: 2, 3, 11].

---

## 📁 Estructura del Proyecto

```text
monitoreo_app/
├── INICIAR.bat                 # Ejecutable automático (instala dependencias y arranca el sistema)
├── monitoreo.py                # Orquestador principal de escucha y alertas
├── clasificador_sonidos.py     # Módulo de procesamiento y clasificación de audio
├── sensor_cliente.py           # Cliente para envío de audio desde nodos
├── celular_sensor.html         # Interfaz web para usar un celular como sensor
├── requirements.txt            # Dependencias de Python
├── LEEME.md                    # Documentación interna del módulo
├── modelos/
│   ├── yamnet.npz              # Pesos del modelo acústico
│   └── yamnet_clases.csv       # Etiquetas de clases de audio
└── herramientas/
    ├── entrenar_con_mis_audios.py
    ├── evaluar_con_esc50.py
    └── exportar_yamnet.py
```

---

## 🎯 Margen de Mejora y Hoja de Ruta

* **Triangulación acústica:** Estimar con mayor precisión la dirección o coordenadas $(x, y)$ de la fuente sonora combinando múltiples nodos cercanos[cite: 11].
* **Nodos autónomos de bajo consumo:** Adaptar clientes a hardware embebido con alimentación solar para zonas remotas sin red eléctrica[cite: 5, 11].
* **Mayor robustez ante ruido ambiental:** Ampliar el dataset de entrenamiento con sonidos de lluvia, viento y fauna local para reducir falsas alarmas en bosques extensos[cite: 2, 5, 11].
* **Panel web de monitoreo:** Incorporar visualización centralizada de alertas, historial y estado de los sensores en tiempo real[cite: 2, 3, 11].
